"""
Cloud sync: your rides, morning checks, plan, workouts and settings, the same on every device and
backed up on your server (sync_server.py).

Each device keeps a whole copy. A background thread compares three things for every file: what this
device has, what the server has, and what both had at the last sync (sync_state.json). Whichever side
changed since then wins; a file changed on both sides goes to the newer one, except morning.json,
whose checks are merged. Nothing is thrown away: the server keeps every version it replaces or
deletes (its history), and a local file that loses lands in sync_conflicts/ first.

    sync.json      {"url": "https://...", "key": "..."}   this device's link to the server (never synced)
    sync_state.json                                      what was synced last (never synced)

Paths on the server are the app folder's layout: settings.json, plan.json, morning.json,
mornings/<day>_ecg.csv, workouts/<name>.zwo, rides/<file>. Rides live in the app's out folder.
Standard library only (the Windows .exe has no extras for it).
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import shutil
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Callable, Optional

STATE_FILES = ("settings.json", "plan.json", "morning.json")
# what is synced: the app folder's own files, no deeper folders, no temp files, no logs of this device
PATH_RE = re.compile(r"^(?:settings\.json|plan\.json|morning\.json|mornings/[\w.-]+\.csv"
                     r"|workouts/[\w. ()-]+\.zwo|rides/(?!app_log\.txt$)[\w. ()-]+)$")
SKIP_SUFFIX = (".tmp", ".part")
RECENT_S = 30            # a file written in the last 30 s may still be being written: next round
EVERY_S = 300            # look for other devices' changes this often (and right after your own)
MAX_DELETES = 20         # more local deletions than this at once (and over half the files): refuse


def valid_path(rel: str) -> bool:
    return bool(PATH_RE.match(rel)) and ".." not in rel and not rel.endswith(SKIP_SUFFIX)


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_json(path: str, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def save_json(path: str, obj) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(obj, fh, indent=1)
    os.replace(tmp, path)


def parse_code(code: str) -> Optional[dict]:
    """The sync code you paste into the app: <server address>#<key>."""
    code = (code or "").strip()
    url, _, key = code.partition("#")
    if not key or not re.match(r"^https?://[\w.-]+(:\d+)?/?$", url) or not re.match(r"^[\w-]{20,200}$", key):
        return None
    return {"url": url.rstrip("/"), "key": key}


def merge_mornings(a: list, b: list) -> list:
    """Every check from both lists once (by day and time); a newer copy of the same check wins."""
    seen: dict = {}
    for x in list(a) + list(b):
        if isinstance(x, dict) and x.get("date"):
            seen[(x.get("date"), x.get("time"))] = x
    return [seen[k] for k in sorted(seen, key=lambda k: (str(k[0]), str(k[1])))]


class Client:
    """Talks to the server: the manifest, and one file up, down or deleted at a time."""

    def __init__(self, url: str, key: str, timeout: float = 60) -> None:
        self.url, self.key, self.timeout = url.rstrip("/"), key, timeout

    def _req(self, method: str, path: str, body: Optional[bytes] = None, headers: Optional[dict] = None):
        req = urllib.request.Request(self.url + path, data=body, method=method,
                                     headers={"Authorization": f"Bearer {self.key}",
                                              "User-Agent": "AssiomaRecorder-sync", **(headers or {})})
        return urllib.request.urlopen(req, timeout=self.timeout)

    def manifest(self) -> dict:
        with self._req("GET", "/sync/manifest") as r:
            return json.loads(r.read())

    def get(self, rel: str) -> tuple:
        with self._req("GET", "/sync/file?path=" + urllib.parse.quote(rel), headers={"Accept-Encoding": "gzip"}) as r:
            data = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                data = gzip.decompress(data)
            return data, r.headers.get("X-Sha256", ""), float(r.headers.get("X-Mtime") or 0)

    def put(self, rel: str, data: bytes, sha: str, base: str, mtime: float) -> bool:
        """False: the server's copy changed since we looked (someone else was faster)."""
        try:
            with self._req("PUT", "/sync/file?path=" + urllib.parse.quote(rel), gzip.compress(data, 6),
                           {"Content-Encoding": "gzip", "Content-Type": "application/octet-stream",
                            "X-Sha256": sha, "X-Base-Sha256": base or "", "X-Mtime": str(mtime)}):
                return True
        except urllib.error.HTTPError as e:
            if e.code == 409:
                return False
            raise

    def delete(self, rel: str, base: str) -> bool:
        try:
            with self._req("DELETE", "/sync/file?path=" + urllib.parse.quote(rel), headers={"X-Base-Sha256": base}):
                return True
        except urllib.error.HTTPError as e:
            if e.code in (404, 409):
                return False
            raise


class Syncer:
    """One device's side. app_dir holds the settings, plan, mornings and workouts; rides_dir the rides.
    apply(changes) puts downloaded files in place: [(rel, tmp path or None to delete, dest)]; the app
    does it on its own thread so its in-memory settings, plan and mornings follow (default: here)."""

    def __init__(self, app_dir: str, rides_dir: str, client: Client,
                 apply: Optional[Callable[[list], None]] = None, busy: Optional[Callable[[], Optional[str]]] = None,
                 log: Callable[[str], None] = lambda _t: None) -> None:
        self.app_dir, self.rides_dir, self.client = app_dir, rides_dir, client
        self.apply = apply or apply_here
        self.busy = busy or (lambda: None)      # the ride being recorded (its file name prefix), or None
        self.log = log
        self.state_path = os.path.join(app_dir, "sync_state.json")
        self.state = load_json(self.state_path, {})
        self.state.setdefault("synced", {})     # rel -> sha both sides had at the last sync
        self.state.setdefault("hashes", {})     # rel -> [size, mtime_ns, sha]: no re-hashing unchanged files
        self.status = {"state": "idle", "last": None, "msg": "", "up": 0, "down": 0}

    # ---- where things are
    def local_path(self, rel: str) -> str:
        top, _, name = rel.partition("/")
        if top == "rides" and name:
            return os.path.join(self.rides_dir, name)
        return os.path.join(self.app_dir, *rel.split("/"))

    def scan(self) -> dict:
        """rel -> (sha, mtime) of this device's files, skipping the ride being recorded and files
        written in the last 30 s."""
        out, now, busy = {}, time.time(), self.busy()
        found = [(r, os.path.join(self.app_dir, r)) for r in STATE_FILES]
        for sub, folder in (("mornings", os.path.join(self.app_dir, "mornings")),
                            ("workouts", os.path.join(self.app_dir, "workouts")), ("rides", self.rides_dir)):
            try:
                names = os.listdir(folder)
            except OSError:
                continue
            found += [(f"{sub}/{n}", os.path.join(folder, n)) for n in names]
        hashes = self.state["hashes"]
        for rel, path in found:
            if not valid_path(rel) or (busy and rel.startswith("rides/" + busy)):
                continue
            try:
                st = os.stat(path)
            except OSError:
                continue
            if not os.path.isfile(path):
                continue
            if now - st.st_mtime < RECENT_S:
                out[rel] = None                  # there, but not ready: neither upload nor count as deleted
                continue
            h = hashes.get(rel)
            if not h or h[0] != st.st_size or h[1] != st.st_mtime_ns:
                h = hashes[rel] = [st.st_size, st.st_mtime_ns, sha256_file(path)]
            out[rel] = (h[2], st.st_mtime)
        for rel in list(hashes):
            if rel not in out:
                hashes.pop(rel)
        return out

    def _save_state(self) -> None:
        try:
            save_json(self.state_path, self.state)
        except OSError as e:
            self.log(f"sync: state: {e}")

    # ---- one round
    def sync_once(self) -> dict:
        self.status.update(state="syncing", msg="")
        up = down = 0
        try:
            remote = self.client.manifest().get("files", {})
            local = self.scan()
            synced = self.state["synced"]
            deletes_local = [r for r in synced if r not in local and r in remote]
            refuse_deletes = len(deletes_local) > MAX_DELETES and len(deletes_local) > len(synced) / 2
            if refuse_deletes:
                self.log(f"sync: {len(deletes_local)} files missing here: not deleting them on the server")

            def order(rel):                       # settings and plan first, then mornings, then rides newest first
                return (0 if rel in STATE_FILES else 1 if not rel.startswith("rides/") else 2,
                        "" if not rel.startswith("rides/") else "".join(chr(255 - ord(c)) for c in rel))
            todo = []
            for rel in sorted(set(local) | set(remote) | set(synced), key=order):
                if not valid_path(rel):
                    continue
                L = local.get(rel, False)          # False: not here; None: here but busy
                if L is None:
                    continue
                lsha, lmt = L if L else (None, 0)
                r = remote.get(rel)
                rsha, rmt = (r["sha256"], r.get("mtime", 0)) if r else (None, 0)
                base = synced.get(rel)
                if lsha == rsha:
                    if lsha:
                        synced[rel] = lsha
                    else:
                        synced.pop(rel, None)
                    continue
                if lsha != base and rsha == base:
                    act = "up" if lsha else ("skip" if refuse_deletes else "delete_remote")
                elif rsha != base and lsha == base:
                    act = "down" if rsha else "delete_local"
                elif rel == "morning.json" and lsha and rsha:
                    act = "merge"
                elif base is None and lsha and rsha:
                    act = "down_keep"              # a new device: the server's copy, yours kept aside
                elif lsha and rsha:
                    act = "up" if lmt > rmt else "down_keep"
                else:                              # changed on one side, deleted on the other: keep it
                    act = "up" if lsha else "down"
                todo.append((rel, act, lsha, rsha))

            downloads = []
            for rel, act, lsha, rsha in todo:
                path = self.local_path(rel)
                if act == "up":
                    with open(path, "rb") as fh:
                        data = fh.read()
                    if rel.endswith(".json"):
                        try:
                            json.loads(data)
                        except ValueError:
                            continue               # caught mid-write: next round
                    sha = hashlib.sha256(data).hexdigest()
                    if self.client.put(rel, data, sha, rsha or "", os.path.getmtime(path)):
                        self.state["synced"][rel] = sha
                        up += 1
                elif act == "delete_remote":
                    if self.client.delete(rel, rsha):
                        self.state["synced"].pop(rel, None)
                elif act in ("down", "down_keep"):
                    data, sha, mt = self.client.get(rel)
                    if hashlib.sha256(data).hexdigest() != rsha:
                        continue                   # changed while we read it: next round
                    if act == "down_keep" and os.path.exists(path):
                        self._keep_aside(rel, path)
                    downloads.append((rel, self._stage(path, data, mt), path, rsha))
                elif act == "delete_local":
                    downloads.append((rel, None, path, None))
                elif act == "merge":
                    data, sha, _ = self.client.get(rel)
                    merged = merge_mornings(load_json(path, []), json.loads(data))
                    body = json.dumps(merged, indent=1).encode()
                    msha = hashlib.sha256(body).hexdigest()
                    if msha != sha and not self.client.put(rel, body, msha, sha, time.time()):
                        continue
                    downloads.append((rel, self._stage(path, body, time.time()), path, msha))
                    up += 1
                if len(downloads) >= 20:           # put files in place in batches: progress survives a cut
                    down += self._apply(downloads)
                    downloads = []
            down += self._apply(downloads)
            self.status.update(state="ok", last=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                               msg="", up=up, down=down)
        except (urllib.error.URLError, OSError, ValueError) as e:
            why = getattr(e, "reason", None) or e
            self.status.update(state="error", msg=f"{type(e).__name__}: {why}"[:160])
            self.log(f"sync: {self.status['msg']}")
        finally:
            self._save_state()
        if up or down:
            self.log(f"sync: {up} up, {down} down")
        return self.status

    def _stage(self, path: str, data: bytes, mtime: float) -> str:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".part"
        with open(tmp, "wb") as fh:
            fh.write(data)
        if mtime:
            os.utime(tmp, (mtime, mtime))
        return tmp

    def _keep_aside(self, rel: str, path: str) -> None:
        folder = os.path.join(self.app_dir, "sync_conflicts", datetime.now().strftime("%Y-%m-%d_%H-%M-%S"))
        dest = os.path.join(folder, *rel.split("/"))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy2(path, dest)
        self.log(f"sync: kept this device's {rel} in {folder}")

    def _apply(self, downloads: list) -> int:
        if not downloads:
            return 0
        self.apply([(rel, tmp, dest) for rel, tmp, dest, _ in downloads])
        for rel, tmp, dest, sha in downloads:
            if sha is None:
                if not os.path.exists(dest):
                    self.state["synced"].pop(rel, None)
            elif os.path.exists(dest) and not os.path.exists(tmp):
                self.state["synced"][rel] = sha
        self._save_state()
        return len(downloads)


def apply_here(changes: list) -> None:
    """Put downloaded files in place (tmp None: delete the file)."""
    for _rel, tmp, dest in changes:
        try:
            if tmp is None:
                os.remove(dest)
            else:
                os.replace(tmp, dest)
        except OSError:
            pass


class Background(threading.Thread):
    """The syncer on its own thread: a round at start, right after you save something (poke), and
    every few minutes for the other devices' changes."""

    def __init__(self, syncer: Syncer) -> None:
        super().__init__(daemon=True)
        self.syncer, self.wake, self.stop_flag = syncer, threading.Event(), threading.Event()

    def poke(self) -> None:
        self.wake.set()

    def stop(self) -> None:
        self.stop_flag.set()
        self.wake.set()

    def run(self) -> None:
        while not self.stop_flag.is_set():
            self.wake.clear()
            self.syncer.sync_once()
            if self.syncer.status["state"] == "error":
                self.wake.wait(60)                 # offline: try again in a minute
            else:
                self.wake.wait(EVERY_S)
            if self.wake.is_set() and not self.stop_flag.is_set():
                time.sleep(RECENT_S + 2)           # a save just happened: let its files settle
