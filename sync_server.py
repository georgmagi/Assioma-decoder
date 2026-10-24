#!/usr/bin/env python3
"""
The Assioma server: sync for your devices (cloud_sync.py) and the app's pages, read-only, in any
browser (your phone too), behind a password.

    /sync/manifest, /sync/file      the devices, with the sync key (Authorization: Bearer ...)
    /login, /logout                 the web view's password, then a signed cookie for 30 days
    everything else                 the app's own pages and data (recorder_app's handler), view only

Every version of a file it replaces or deletes is kept under <var>/history/<time>/. It listens on
127.0.0.1 only; Caddy in front does HTTPS.

    ASSIOMA_DATA=/var/lib/assioma/data ASSIOMA_VAR=/var/lib/assioma ASSIOMA_PORT=8100
    SYNC_KEY=...  WEB_PASSWORD_HASH=pbkdf2_sha256$<iterations>$<salt hex>$<hash hex>  COOKIE_SECRET=...

    python sync_server.py hash-password        prints a WEB_PASSWORD_HASH for a password you type
Standard library only (plus the app's own modules).
"""
from __future__ import annotations

import getpass
import gzip
import hashlib
import hmac
import html
import json
import os
import shutil
import sys
import threading
import time
import zlib
from datetime import datetime, timezone
from typing import Optional
from http.server import ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import cloud_sync as cs

MAX_BODY = 400 * 1024 * 1024          # one file, compressed
MAX_FILE = 800 * 1024 * 1024          # one file, as stored
COOKIE = "asess"
COOKIE_DAYS = 30
VIEW_ACTIONS = ("nav", "open", "home")   # what the web view may press: moving between pages


def hash_password(pw: str, iterations: int = 600_000) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${dk.hex()}"


def check_password(pw: str, stored: str) -> bool:
    try:
        algo, it, salt, want = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), int(it))
        return hmac.compare_digest(dk.hex(), want)
    except (ValueError, TypeError):
        return False


class Store:
    """The synced files on disk and their manifest (sha-256, size, mtime), with history."""

    def __init__(self, data: str, var: str) -> None:
        self.data, self.var = data, var
        self.lock = threading.Lock()
        os.makedirs(data, exist_ok=True)
        os.makedirs(os.path.join(var, "history"), exist_ok=True)
        self.files: dict = {}
        self._scan()

    def path(self, rel: str) -> str:
        return os.path.join(self.data, *rel.split("/"))

    def _scan(self) -> None:
        cache = cs.load_json(os.path.join(self.var, "manifest.json"), {})
        files = {}
        for top in ("", "mornings", "workouts", "rides"):
            folder = os.path.join(self.data, top) if top else self.data
            try:
                names = os.listdir(folder)
            except OSError:
                continue
            for n in names:
                rel = f"{top}/{n}" if top else n
                p = os.path.join(folder, n)
                if not cs.valid_path(rel) or not os.path.isfile(p):
                    continue
                st = os.stat(p)
                c = cache.get(rel)
                if c and c.get("size") == st.st_size and c.get("mtime_ns") == st.st_mtime_ns:
                    files[rel] = c
                else:
                    files[rel] = {"sha256": cs.sha256_file(p), "size": st.st_size, "mtime": st.st_mtime,
                                  "mtime_ns": st.st_mtime_ns}
        self.files = files
        self._save()

    def _save(self) -> None:
        cs.save_json(os.path.join(self.var, "manifest.json"), self.files)

    def manifest(self) -> dict:
        with self.lock:
            return {"files": {k: {"sha256": v["sha256"], "size": v["size"], "mtime": v["mtime"]}
                              for k, v in self.files.items()}}

    def _to_history(self, rel: str) -> None:
        p = self.path(rel)
        if os.path.exists(p):
            dest = os.path.join(self.var, "history", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S.%fZ"),
                                *rel.split("/"))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.move(p, dest)

    def put(self, rel: str, data: bytes, base: str, mtime: float) -> int:
        """200 stored, 409 the server's copy isn't the one you started from."""
        sha = hashlib.sha256(data).hexdigest()
        with self.lock:
            cur = self.files.get(rel, {}).get("sha256", "")
            if cur == sha:
                return 200
            if cur != (base or ""):
                return 409
            p = self.path(rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            tmp = p + ".part"
            with open(tmp, "wb") as fh:
                fh.write(data)
            if mtime:
                os.utime(tmp, (mtime, mtime))
            self._to_history(rel)
            os.replace(tmp, p)
            st = os.stat(p)
            self.files[rel] = {"sha256": sha, "size": st.st_size, "mtime": st.st_mtime, "mtime_ns": st.st_mtime_ns}
            self._save()
        return 200

    def delete(self, rel: str, base: str) -> int:
        with self.lock:
            cur = self.files.get(rel)
            if cur is None:
                return 404
            if cur["sha256"] != base:
                return 409
            self._to_history(rel)
            self.files.pop(rel)
            self._save()
        return 200


def gunzip_limited(body: bytes, limit: int = MAX_FILE) -> bytes:
    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    out = d.decompress(body, limit + 1)
    if len(out) > limit or d.unconsumed_tail:
        raise ValueError("too big")
    return out


LOGIN_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="apple-mobile-web-app-capable" content="yes"><meta name="theme-color" content="#0b0d10">
<title>Assioma</title><style>
body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0b0d10;color:#e6e9ee;
font:16px system-ui,-apple-system,sans-serif}form{display:grid;gap:12px;width:min(320px,86vw)}
h1{font-size:22px;margin:0 0 6px}input,button{font:inherit;padding:12px 14px;border-radius:10px;
border:1px solid #2a3039;background:#12151a;color:inherit}button{background:#5b9dff;border:0;color:#08111f;
font-weight:600}p{color:#ff8a7a;margin:0}</style></head><body>
<form method="post" action="/login"><h1>Assioma</h1>%s
<input type="password" name="password" placeholder="Password" autocomplete="current-password" autofocus required>
<button type="submit">Sign in</button></form></body></html>"""


def make_viewer(data: str):
    """The app's engine over the server's copy of your data, with a worker that has no sensors."""
    import recorder_app as rapp

    class NoSensors:
        def __init__(self, q, _hr):
            self.q, self.status, self.pedal_name, self.log_file = q, {"pedal": "", "hr": ""}, None, None

        def start(self):
            pass

        def stop(self):
            pass

        def join(self, timeout=None):
            pass

        def log(self, text):
            print(text, flush=True)

    rapp.WORKOUT_DIR = os.path.join(data, "workouts")
    os.makedirs(os.path.join(data, "rides"), exist_ok=True)
    settings_path = os.path.join(data, "settings.json")
    engine = rapp.Engine(os.path.join(data, "rides"), rapp.load_settings(settings_path), NoSensors, settings_path)
    engine.start()
    return rapp, engine


def make_handler(store: Store, env: dict, rapp, engine):
    app_mode = {"app": False, "window": None, "viewer": True}
    Base = rapp.make_handler(engine, app_mode)
    key = env.get("SYNC_KEY", "")
    pw_hash = env.get("WEB_PASSWORD_HASH", "")
    secret = (env.get("COOKIE_SECRET") or "").encode()
    fails: dict = {}                     # ip -> [times of failed logins]

    def sign(exp: int) -> str:
        return f"{exp}.{hmac.new(secret, str(exp).encode(), hashlib.sha256).hexdigest()}"

    class Handler(Base):
        server_version = "assioma"
        sys_version = ""

        def _ip(self) -> str:
            fwd = self.headers.get("X-Forwarded-For", "")
            return fwd.split(",")[-1].strip() or self.client_address[0]

        def _logged_in(self) -> bool:
            if not secret or not pw_hash:
                return False
            for part in (self.headers.get("Cookie") or "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == COOKIE:
                    exp, _, _sig = v.partition(".")
                    if exp.isdigit() and int(exp) > time.time() and hmac.compare_digest(v, sign(int(exp))):
                        return True
            return False

        def _key_ok(self) -> bool:
            got = self.headers.get("Authorization", "")
            return bool(key) and hmac.compare_digest(got.encode(), f"Bearer {key}".encode())

        def _plain(self, code: int, text: str) -> None:
            self._send(code, text.encode(), "text/plain; charset=utf-8")

        def _redirect(self, to: str, cookie: str = "") -> None:
            self.send_response(303)
            self.send_header("Location", to)
            if cookie:
                self.send_header("Set-Cookie", cookie)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _rel(self, q) -> str:
            rel = q.get("path", [""])[0]
            return rel if cs.valid_path(rel) else ""

        # ---- the devices
        def _sync(self, method: str) -> None:
            if not self._key_ok():
                time.sleep(1)
                return self._plain(401, "bad key")
            try:
                fmt = int(self.headers.get("X-Assioma-Format") or 0)
            except ValueError:
                fmt = 0
            if fmt < cs.FORMAT:                         # an older app would damage newer files: update first
                return self._plain(426, cs.OUTDATED)
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if method == "GET" and u.path == "/sync/manifest":
                return self._json(store.manifest())
            if u.path != "/sync/file":
                return self._plain(404, "not found")
            rel = self._rel(q)
            if not rel:
                return self._plain(400, "bad path")
            if method == "GET":
                with store.lock:
                    meta = store.files.get(rel)
                    if meta is None:
                        return self._plain(404, "no such file")
                    with open(store.path(rel), "rb") as fh:
                        data = fh.read()
                body = gzip.compress(data, 6)
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Encoding", "gzip")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("X-Sha256", meta["sha256"])
                self.send_header("X-Mtime", str(meta["mtime"]))
                self.end_headers()
                self.wfile.write(body)
                return
            if method == "PUT":
                n = int(self.headers.get("Content-Length") or 0)
                if n > MAX_BODY:
                    return self._plain(413, "too big")
                body = self.rfile.read(n)
                try:
                    data = gunzip_limited(body) if self.headers.get("Content-Encoding") == "gzip" else body
                except (ValueError, zlib.error, EOFError):
                    return self._plain(400, "bad body")
                if hashlib.sha256(data).hexdigest() != self.headers.get("X-Sha256", ""):
                    return self._plain(400, "checksum mismatch")
                try:
                    mtime = float(self.headers.get("X-Mtime") or 0)
                except ValueError:
                    mtime = 0
                code = store.put(rel, data, self.headers.get("X-Base-Sha256", ""), mtime)
            elif method == "DELETE":
                code = store.delete(rel, self.headers.get("X-Base-Sha256", ""))
            else:
                return self._plain(405, "method")
            if code == 200:
                engine.command("sync_reload", [rel], timeout=2)    # the web view follows
            self._plain(code, "ok" if code == 200 else "conflict" if code == 409 else "not found")

        # ---- the browser
        def _login(self) -> None:
            ip, now = self._ip(), time.time()
            recent = [t for t in fails.get(ip, []) if now - t < 600]
            fails[ip] = recent
            if len(recent) >= 10:
                return self._send(429, (LOGIN_PAGE % "<p>Too many tries. Wait 10 minutes.</p>").encode(),
                                  "text/html; charset=utf-8")
            n = min(int(self.headers.get("Content-Length") or 0), 4096)
            form = parse_qs(self.rfile.read(n).decode("utf-8", "replace"))
            if pw_hash and check_password(form.get("password", [""])[0], pw_hash):
                exp = int(now) + COOKIE_DAYS * 86400
                return self._redirect("/", f"{COOKIE}={sign(exp)}; Max-Age={COOKIE_DAYS * 86400}; Path=/; "
                                           "Secure; HttpOnly; SameSite=Lax")
            recent.append(now)
            time.sleep(1)
            self._send(401, (LOGIN_PAGE % "<p>Wrong password.</p>").encode(), "text/html; charset=utf-8")

        def do_GET(self) -> None:
            u = urlparse(self.path)
            if u.path.startswith("/sync/"):
                return self._sync("GET")
            if u.path == "/healthz":
                return self._plain(200, "ok")
            if u.path == "/login":
                return self._send(200, (LOGIN_PAGE % "").encode(), "text/html; charset=utf-8")
            if u.path == "/logout":
                return self._redirect("/login", f"{COOKIE}=; Max-Age=0; Path=/; Secure; HttpOnly; SameSite=Lax")
            if not self._logged_in():
                if u.path.startswith("/api/"):
                    return self._plain(401, "sign in")
                return self._redirect("/login")
            super().do_GET()

        def do_POST(self) -> None:
            u = urlparse(self.path)
            if u.path == "/login":
                return self._login()
            if not self._logged_in():
                return self._plain(401, "sign in")
            if u.path != "/api/action":
                return self._plain(404, "not found")
            body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length", 0)), 65536)) or b"{}")
            if body.get("name") in VIEW_ACTIONS:
                engine.command(body["name"], body.get("arg"))
            self._json(engine.snap)

        def do_PUT(self) -> None:
            self._sync("PUT") if urlparse(self.path).path.startswith("/sync/") else self._plain(404, "not found")

        def do_DELETE(self) -> None:
            self._sync("DELETE") if urlparse(self.path).path.startswith("/sync/") else self._plain(404, "not found")

        def end_headers(self) -> None:
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            super().end_headers()

    return Handler


def start_garmin(store: "Store", every: Optional[float] = None, tokens: Optional[str] = None,
                 after=None) -> threading.Event:
    """Your watch's resting and sleeping heart rate (garmin_pull.py) into garmin.json, written the
    way a device's upload is (with history), so every device syncs it down."""
    import garmin_pull as gp

    def read() -> Optional[bytes]:
        try:
            with open(store.path(gp.REL), "rb") as fh:
                return fh.read()
        except OSError:
            return None

    def write(body: bytes) -> None:
        base = store.files.get(gp.REL, {}).get("sha256", "")
        store.put(gp.REL, body, base, time.time())
        if after:
            after()                                          # the web view reads it again

    stop = threading.Event()
    threading.Thread(target=gp.pull_loop, args=(read, write, stop, lambda t: print(t, flush=True),
                                                every or gp.PULL_EVERY_S, tokens or gp.TOKENS), daemon=True).start()
    return stop


def main() -> None:
    if sys.argv[1:] == ["hash-password"]:
        pw = getpass.getpass("Password: ")
        if pw != getpass.getpass("Again: "):
            sys.exit("not the same")
        print(hash_password(pw))
        return
    env = os.environ
    data = env.get("ASSIOMA_DATA", "/var/lib/assioma/data")
    var = env.get("ASSIOMA_VAR", "/var/lib/assioma")
    for need in ("SYNC_KEY", "WEB_PASSWORD_HASH", "COOKIE_SECRET"):
        if len(env.get(need, "")) < 20:
            sys.exit(f"{need} is not set")
    store = Store(data, var)
    rapp, engine = make_viewer(data)
    srv = ThreadingHTTPServer(("127.0.0.1", int(env.get("ASSIOMA_PORT", "8100"))),
                              make_handler(store, dict(env), rapp, engine))
    srv.daemon_threads = True
    start_garmin(store, after=lambda: engine.command("sync_reload", ["garmin.json"], timeout=2))
    print(f"assioma: {len(store.files)} files, listening on {srv.server_address}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
