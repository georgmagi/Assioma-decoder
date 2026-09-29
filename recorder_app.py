#!/usr/bin/env python3
"""
Assioma Recorder as a desktop app. The window is a web page (web/) in a native window
(pywebview, the Edge WebView2 engine that ships with Windows); Python does Bluetooth, decoding,
HRV and FIT exactly as the tkinter recorder does (Session and BleWorker from assioma_recorder).

    python recorder_app.py                             start (FTP, plan and strap are set in the app)
    python recorder_app.py --replay rides/ride_..._raw.csv --skip 20
                                                        test the window with a recorded ride
    python recorder_app.py --browser                   use the default browser instead of a window

Screens: home (sensors, settings, past rides) -> ride -> summary -> home; the sensors stay
connected between rides. Threads: the engine owns the rides (takes in notifications, ticks, runs
the buttons), a small HTTP server on 127.0.0.1 hands the page a snapshot, the new graph points and
ride summaries (ride_summary.py), and the main thread runs the window. The page only ever reads
state and sends button presses.

Read-only: like the recorder, it only subscribes to notifications and never writes to the pedals.
Needs: pip install bleak pywebview
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import queue
import sys
import threading
import time
from datetime import timezone
from typing import Optional
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import assioma_decoder as dec
import ride_summary as rs
import training
from assioma_recorder import (EVENT, HERE, BleWorker, ReplayWorker, Session, local_utc_offset_h,
                              read_raw_log, utcnow)

WEB = os.path.join(HERE, "web")
SETTINGS_FILE = os.path.join(HERE, "settings.json")
DYN_KEYS = ["rider_position"] + [f"{m}_{side}{unit}" for side in ("left", "right") for m, unit in
            [("power", "_w"), ("te", "_pct"), ("ps", "_pct"), ("pco", "_mm"), ("pp", "_start_deg"),
             ("pp", "_peak_start_deg"), ("pp", "_peak_end_deg"), ("pp", "_end_deg")]]
AUTOSTART_S = 3             # pedalling this long on the home screen starts a ride
REARM_S = 5                 # after a ride, auto-start waits until the pedals were still this long


def hms(n: int) -> str:
    return f"{n // 3600}:{n // 60 % 60:02d}:{n % 60:02d}"


# ftp, plan and strap are set on the home screen; theme, graph span and the power tile's 3 s mode are
# remembered from the page (its own browser storage is lost: the port changes every run)
SETTINGS_DEFAULTS = {"ftp": None, "lthr": None, "plan_min": 60, "hr": True, "theme": "dark", "span": 300,
                     "power3s": False}
PREFS = {"theme": ("dark", "lcd"), "span": (300, 1800, 0), "power3s": (True, False)}


def load_settings(path: str) -> dict:
    try:
        with open(path) as fh:
            saved = json.load(fh)
    except (OSError, ValueError):
        saved = {}
    return {k: saved.get(k, v) for k, v in SETTINGS_DEFAULTS.items()}


def save_settings(path: str, settings: dict) -> None:
    try:
        with open(path, "w") as fh:
            json.dump(settings, fh, indent=1)
    except OSError:
        pass


class Engine(threading.Thread):
    """The app's lifecycle: home -> ride -> summary -> home. The Bluetooth worker lives for the
    whole run (the sensors stay connected between rides); each ride is a new Session, created when
    it starts. Every change to the Session happens on this thread, so the HTTP threads only read the
    published snapshot and slices of the append-only graph lists."""

    def __init__(self, out_dir: str, settings: dict, make_worker, settings_path: Optional[str] = None,
                 log_file=None) -> None:
        super().__init__(daemon=True)
        self.out_dir, self.settings, self.settings_path = out_dir, settings, settings_path
        self.make_worker, self.log_file = make_worker, log_file
        self.q: queue.Queue = queue.Queue()
        self.worker = self._new_worker()
        self.session: Optional[Session] = None
        self.screen = "home"                 # home | ride | summary | progress
        self.back_to = "home"                # where the summary's back button goes
        self.summary_id: Optional[str] = None
        self.dialog = None                   # ride: None | "finish" | "discard"; home/summary: None | "delete"
        self.delete_id: Optional[str] = None # the ride the delete dialog is about
        self.rides_version = 0               # bumped when rides are saved or deleted: the page reloads its list
        self.error = ""
        self.pedal_since = None
        # auto-start (3 s of pedalling on the home screen) is armed at launch and again once the
        # pedals have been still for 5 s, so spinning on after a ride doesn't start the next one
        self.armed, self.quiet_since = True, None
        self.home_dec = dec.Decoder()        # home: just enough decoding for the sensor check
        self.home_live: dict = {}            # name -> (value, time)
        self.quit = threading.Event()        # the window should close
        self.snap: dict = {}
        self.cmds: queue.Queue = queue.Queue()
        self._last_tick = 0.0

    def _new_worker(self):
        w = self.make_worker(self.q, bool(self.settings.get("hr")))
        w.log_file = self.log_file
        w.start()
        return w

    # ---- commands from the page (run on this thread)
    def command(self, name: str, arg=None, timeout: float = 8.0) -> None:
        """Queue a button press and wait until it has been carried out."""
        done = threading.Event()
        self.cmds.put((name, arg, done))
        done.wait(timeout)

    def _do(self, name: str, arg=None) -> None:
        s, now = self.session, utcnow()
        if name == "prefs" and isinstance(arg, dict):
            self._prefs(arg)
        elif self.screen in ("home", "summary") and name in ("delete", "back", "delete_confirm"):
            if name == "delete":                   # from the list (arg: the ride) or the summary
                ride = arg if self.screen == "home" else self.summary_id
                if rs.find_ride(self.out_dir, ride):
                    self.dialog, self.delete_id = "delete", ride
            elif name == "back":
                self.dialog, self.delete_id = None, None
            elif name == "delete_confirm" and self.dialog == "delete":
                self._delete_ride(self.delete_id)
                self.dialog, self.delete_id = None, None
                self.screen = self.back_to if self.screen == "summary" else self.screen
                self.summary_id = None
        elif self.screen in ("home", "progress") and name == "open" and rs.find_ride(self.out_dir, arg):
            self.back_to, self.screen, self.summary_id = self.screen, "summary", arg
        elif self.screen == "home":
            if name == "start":
                self._start_ride(now)
            elif name == "progress":
                self.screen = "progress"
            elif name == "settings" and isinstance(arg, dict):
                self._settings(arg)
        elif self.screen == "progress":
            if name == "home":
                self.screen = "home"
        elif self.screen == "summary":
            if name == "home":                     # back where it was opened from
                self.screen, self.summary_id, self.dialog = self.back_to, None, None
            elif name == "show_file":
                self._show_file()
        elif self.screen == "ride" and s is not None:
            if name == "pause":
                s.pause(now)
            elif name == "resume":
                self.dialog = None
                s.resume(now)
            elif name == "toggle" and self.dialog is None:
                {"recording": s.pause, "paused": s.resume}.get(s.state, lambda _n: None)(now)
            elif name == "stop":
                s.pause(now)                       # the clock stops while you decide
                self.dialog = "finish"
            elif name == "back":
                self.dialog = "finish"
            elif name == "discard":
                self.dialog = "discard"
            elif name == "discard_confirm":
                self._drain()
                s.discard()
                self._leave_ride("home")
            elif name == "save":
                self._save()
        if name == "close":
            self.quit.set()

    def _start_ride(self, now) -> None:
        self.session = Session(self.out_dir)
        self.session.pedal_name = self.worker.pedal_name or "Assioma"
        self.session.start(now)
        self.worker.log(f"ride started: {os.path.basename(self.session.base)}")
        self.screen, self.dialog, self.pedal_since = "ride", None, None

    def _leave_ride(self, screen: str, summary_id: Optional[str] = None) -> None:
        self.session, self.dialog = None, None
        self.screen, self.summary_id, self.back_to = screen, summary_id, "home"
        self.home_dec, self.home_live = dec.Decoder(), {}
        self.armed, self.quiet_since, self.pedal_since = False, None, None

    def _save(self) -> None:
        s = self.session
        self._drain()
        result = s.finish()
        if not result:                             # nothing recorded
            self._leave_ride("home")
            return
        ride_id = os.path.basename(s.base)
        self.worker.log(f"ride saved: {ride_id}")
        self.rides_version += 1
        try:                                       # the ride list's entry, ready before it's asked for
            info = rs.short(rs.summarize(rs.load(s.base + rs.SUFFIX), self.settings.get("ftp")))
            with open(rs.cache_path(self.out_dir, ride_id), "w") as fh:
                json.dump(info, fh)
        except Exception as e:
            self.worker.log(f"summary: {type(e).__name__}: {e}")
        self._leave_ride("summary", ride_id)

    def _delete_ride(self, ride_id: Optional[str]) -> None:
        """Every file of a ride: the recorder's raw log, per-second CSV and FIT, or another app's FIT
        file; and its cached summary."""
        path = rs.find_ride(self.out_dir, ride_id)
        if path is None:
            return
        base = os.path.join(self.out_dir, ride_id)
        exts = ("_raw.csv", rs.SUFFIX, ".fit", "_summary.json", "_log.txt") if path.endswith(rs.SUFFIX) else (".fit",)
        for f in [base + e for e in exts] + [rs.cache_path(self.out_dir, ride_id)]:
            try:
                os.remove(f)
            except OSError:
                pass
        self.worker.log(f"ride deleted: {ride_id}")
        self.rides_version += 1

    def _show_file(self) -> None:
        path = os.path.join(self.out_dir, f"{self.summary_id}.fit")          # ours, or another app's file
        if self.summary_id and rs.find_ride(self.out_dir, self.summary_id) and os.path.exists(path)                 and sys.platform == "win32":
            import subprocess
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])

    def _settings(self, new: dict) -> None:
        ftp, plan, hr = new.get("ftp", self.settings.get("ftp")), new.get("plan_min"), new.get("hr")
        lthr = new.get("lthr", self.settings.get("lthr"))
        if ftp is None or (isinstance(ftp, (int, float)) and 50 <= ftp <= 600):
            self.settings["ftp"] = ftp
        if lthr is None or (isinstance(lthr, (int, float)) and 100 <= lthr <= 220):
            self.settings["lthr"] = lthr
        if isinstance(plan, (int, float)) and 10 <= plan <= 600:
            self.settings["plan_min"] = plan
        if isinstance(hr, bool) and hr != self.settings.get("hr"):
            self.settings["hr"] = hr
            self.worker.stop()                     # reconnect with or without the strap
            self.worker.join(timeout=6)
            self.worker = self._new_worker()
        if self.settings_path:
            save_settings(self.settings_path, self.settings)

    def _prefs(self, new: dict) -> None:
        changed = False
        for key, allowed in PREFS.items():
            # "in" alone would take False for the span 0 (False == 0): the types must match too
            if key in new and new[key] in allowed and isinstance(new[key], bool) == isinstance(allowed[0], bool):
                if self.settings.get(key) != new[key]:
                    self.settings[key], changed = new[key], True
        if changed and self.settings_path:
            save_settings(self.settings_path, self.settings)

    def window_closing(self) -> bool:
        """The window's close button: True lets it close. Mid-ride it asks first, like Stop."""
        s = self.session
        if s is not None and s.state in ("recording", "paused") and not self.quit.is_set():
            self.command("stop")
            return False
        return True

    # ---- the loop
    def _drain(self) -> None:
        try:
            while True:
                n = self.q.get_nowait()
                if self.session is not None:
                    self.session.add(n)
                else:
                    self._home_feed(n)
        except queue.Empty:
            pass

    def _home_feed(self, n) -> None:
        """Home: the latest power and heart rate, for the sensor check and auto-start."""
        for r in self.home_dec.feed(n):
            if r.get("type") == dec.CPM_CHAR:
                self.home_live["power"] = (r["cpm_power_w"], r["time"])
            elif r.get("type") == dec.HR_CHAR:
                self.home_live["hr"] = (r["heart_rate_bpm"], r["time"])

    def _home_value(self, name: str, now, max_age: float = 5):
        v = self.home_live.get(name)
        return v[0] if v and (now - v[1]).total_seconds() < max_age else None

    def run(self) -> None:
        while not self.quit.is_set():
            try:
                self._drain()
                while True:
                    try:
                        name, arg, done = self.cmds.get_nowait()
                    except queue.Empty:
                        break
                    try:
                        self._do(name, arg)
                    finally:
                        done.set()
                t = time.monotonic()
                if t - self._last_tick >= 0.25:
                    self._last_tick = t
                    self._tick()
                self.snap = self._snapshot()
            except Exception as e:                 # never let a glitch stop the recording
                self.error = f"{type(e).__name__}: {e}"[:160]
                self.worker.log(f"engine: {self.error}")
            time.sleep(0.1)

    def _tick(self) -> None:
        now = utcnow()
        if self.session is not None:
            self.session.tick(now)
            if self.worker.pedal_name:
                self.session.pedal_name = self.worker.pedal_name
        elif self.screen == "home":                 # pedalling for 3 s on the home screen starts a ride
            if self._home_value("power", now, 3):
                self.quiet_since = None
                if self.armed:
                    self.pedal_since = self.pedal_since or now
                    if (now - self.pedal_since).total_seconds() >= AUTOSTART_S:
                        self._start_ride(now)
            else:
                self.pedal_since = None
                self.quiet_since = self.quiet_since or now
                if (now - self.quiet_since).total_seconds() >= REARM_S:
                    self.armed = True

    def _power_3s(self, now):
        """Time-weighted mean power over exactly the last 3 s (each notification's power holds until
        the next one), so the value slides instead of jumping as notifications enter and leave the
        window. None when the pedals have been quiet for 3 s."""
        lp, t0 = self.session.live_power, self.session.t0
        if not lp or t0 is None:
            return None
        t_now = (now - t0).total_seconds()
        if t_now - lp[-1][0] > 3:
            return None
        edge, end, total, covered = t_now - 3, t_now, 0.0, 0.0
        for ts, p, _ in reversed(lp[-40:]):
            start = max(ts, edge)
            if end > start:
                total += p * (end - start)
                covered += end - start
            end = ts
            if ts <= edge:
                break
        return total / covered if covered > 0 else lp[-1][1]

    def _snapshot(self) -> dict:
        w, now = self.worker, utcnow()
        base = {
            "screen": self.screen, "summary_id": self.summary_id, "back_to": self.back_to,
            "settings": dict(self.settings),
            "sensors": [{"label": "Pedals", "status": w.status.get("pedal", "")}]
                       + ([{"label": "Strap", "status": w.status["hr"]}] if w.status.get("hr") else []),
            "error": self.error, "dialog": self.dialog, "rides_version": self.rides_version,
            "config": {"ftp": self.settings.get("ftp"), "plan_min": self.settings.get("plan_min")},
        }
        s = self.session
        if s is None:
            base["home"] = {"power_w": self._home_value("power", now), "hr": self._home_value("hr", now),
                            "pedalling_s": (now - self.pedal_since).total_seconds() if self.pedal_since else None,
                            "armed": self.armed}
            return base
        live = s.live(now)
        st = s.stats()
        h: dict = {}
        beats = s.hrv.beats
        if beats and (now - beats[-1].time).total_seconds() < 5:
            h["rr"] = beats[-1].rr
        row = s.hrv.latest(now)
        if row and row[1] is not None:
            h.update(alpha1=row[1], rmssd=row[2], art=row[3], cov=s.hrv.coverage[-1])
        elif beats:
            h["note"] = "needs 2 min of beats"
        strap = None
        if s.hr_last is not None:
            quiet = (now - s.hr_last).total_seconds()
            no_beat = (now - beats[-1].time).total_seconds() if beats else quiet
            strap = {"warn": f"no data {quiet:.0f} s" if quiet > 3 else f"no beats {no_beat:.0f} s" if no_beat > 4 else ""}
        t0 = s.t0
        ride = {"state": s.state, "clock": hms(len(s.seconds)), "elapsed": None}
        if s.started is not None:
            ride["elapsed"] = hms(int((now - s.started).total_seconds()))
        return {
            **base,
            "now": (now - t0).total_seconds() if t0 else None,
            "t0_ms": t0.replace(tzinfo=timezone.utc).timestamp() * 1000 if t0 else None,   # graph clock times
            "live": {**{k: live.get(k) for k in ("power_w", "cadence_rpm", "balance_right_pct", "heart_rate_bpm")},
                     "power_3s": self._power_3s(now)},
            "stats": {k: st.get(k) for k in ("avg_power", "np", "avg_cadence", "balance_right", "avg_hr",
                                             "pco_left", "pco_right", "pco_left_range", "pco_right_range")},
            "dyn": {k: live.get(k) for k in DYN_KEYS},
            "hrv": h, "strap": strap, "ride": ride,
            "beats": [[round((b.time - t0).total_seconds(), 3), b.artifact] for b in beats[-60:]
                      if (now - b.time).total_seconds() < 5.5] if t0 else [],      # the HR tile's strip
            "battery": s.battery,
            "dialog": self.dialog,
        }


def pedal_silhouette() -> dict:
    """The left pedal's top view as one flat shape (every layer's outline, with the body's see-through
    holes), in units of the pedal length: x across (+ = towards the crank), y front to back."""
    import pedal_outline
    solid, holes = [], []
    for name, outer, hs in pedal_outline.LAYERS:
        solid += outer
        if name == "body":
            holes += hs
    return {"solid": solid, "holes": holes}


def _r(x):
    return round(x, 2) if isinstance(x, float) else x


# --------------------------------------------------------------------------- #
# HTTP: the page, its files, the snapshot, the rides and the buttons (127.0.0.1 only)
# --------------------------------------------------------------------------- #
def make_server(engine: Engine, app_mode: dict) -> ThreadingHTTPServer:

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_a) -> None:        # quiet
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj) -> None:
            self._send(200, json.dumps(obj, separators=(",", ":")).encode(), "application/json")

        def do_GET(self) -> None:
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if u.path == "/api/pedal":
                self._json(pedal_silhouette())
            elif u.path == "/api/rides":
                self._json(rs.ride_list(engine.out_dir, engine.settings.get("ftp")))
            elif u.path == "/api/progress":
                self._json(training.progress(rs.ride_list(engine.out_dir, engine.settings.get("ftp"))))
            elif u.path == "/api/ride":
                s = rs.summarize_file(engine.out_dir, q.get("id", [""])[0], engine.settings.get("ftp"),
                                      engine.settings.get("lthr"))
                if s is None:
                    self._send(404, b"no such ride", "text/plain")
                    return
                s.pop("fit_path", None)
                self._json(s)
            elif u.path == "/api/state":
                self._json(state(engine, app_mode, *(int(q.get(k, ["0"])[0]) for k in ("p", "h", "a", "b"))))
            else:
                rel = "index.html" if u.path in ("/", "") else u.path.lstrip("/")
                path = os.path.normpath(os.path.join(WEB, rel))
                if not path.startswith(WEB + os.sep) or not os.path.isfile(path):
                    self._send(404, b"not found", "text/plain")
                    return
                with open(path, "rb") as fh:
                    self._send(200, fh.read(), mimetypes.guess_type(path)[0] or "application/octet-stream")

        def do_POST(self) -> None:
            if urlparse(self.path).path != "/api/action":
                self._send(404, b"not found", "text/plain")
                return
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            name = body.get("name", "")
            if name == "fullscreen" and app_mode.get("window") is not None:
                app_mode["window"].toggle_fullscreen()
            else:
                engine.command(name, body.get("arg"))
            self._json(engine.snap)

    return ThreadingHTTPServer(("127.0.0.1", 0), Handler)


def state(engine: Engine, app_mode: dict, p: int, h: int, a: int, b: int) -> dict:
    """The snapshot plus the graph points of the current ride that arrived since the page's counts
    (no ride: zero counts, which tells the page to clear its graphs)."""
    out = {**engine.snap, "app": app_mode["app"], "closed": engine.quit.is_set()}
    session = engine.session                       # may end while we read: keep this one
    if session is None or session.t0 is None:
        return {**out, "pn": 0, "hn": 0, "an": 0, "bn": 0, "power": [], "hr": [], "a1": [], "beats_rr": []}
    lp, lh, la, lb = session.live_power, session.live_hr, session.hrv.series, session.hrv.beats
    pn, hn, an, bn = len(lp), len(lh), len(la), session.hrv._judged
    t0 = session.t0
    return {**out, "pn": pn, "hn": hn, "an": an, "bn": bn,
            # beats once judged: [s since t0, RR ms, artifact]
            "beats_rr": [[_r((x.time - t0).total_seconds()), _r(x.rr), bool(x.artifact)] for x in lb[b:bn]]
                        if b <= bn else [],
            "power": [[_r(t), _r(w), _r(av)] for t, w, av in lp[p:pn]] if p <= pn else [],
            "hr": [[_r(t), v] for t, v in lh[h:hn]] if h <= hn else [],
            # DFA alpha 1 every 5 s: [s since t0, alpha 1 or None, artifact %]
            "a1": [[_r((ts - t0).total_seconds()), _r(v), _r(art)] for ts, v, _, art in la[a:an]] if a <= an else []}


# --------------------------------------------------------------------------- #
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hr", action="store_true", help="connect a heart-rate strap (also a setting in the app)")
    ap.add_argument("--pedal", default="", help="only use a pedal whose name contains this (e.g. 12345)")
    ap.add_argument("--hr-name", default="", help="only use a HR strap whose name contains this")
    ap.add_argument("--out", default=os.path.join(HERE, "rides"), help="folder for recordings")
    ap.add_argument("--ftp", type=float, default=None, help="your FTP in W (also a setting in the app)")
    ap.add_argument("--plan", type=float, default=None, help="planned ride length in minutes (also a setting)")
    ap.add_argument("--replay", metavar="LOG", help="test: replay a recorder raw log (or an nRF log with --date)")
    ap.add_argument("--date", help="date of an nRF log (YYYY-MM-DD)")
    ap.add_argument("--speed", type=float, default=1.0, help="replay speed")
    ap.add_argument("--skip", type=float, default=0, help="replay: start this many minutes into the log")
    ap.add_argument("--browser", action="store_true", help="open in the default browser instead of a window")
    ap.add_argument("--no-open", action="store_true", help=argparse.SUPPRESS)      # tests: serve only
    args = ap.parse_args()

    # settings live in settings.json next to the app; a replay (a test) never changes them
    settings_path = None if args.replay else SETTINGS_FILE
    settings = load_settings(SETTINGS_FILE)
    for key, value in (("ftp", args.ftp), ("plan_min", args.plan)):
        if value is not None:
            settings[key] = value
    if args.hr:
        settings["hr"] = True

    if args.replay:
        if args.replay.endswith("_raw.csv"):
            notes = [n for n in read_raw_log(args.replay) if n.char != EVENT]
        elif args.date:
            notes = list(dec.read_nrf_log(args.replay, args.date, local_utc_offset_h()))
        else:
            ap.error("--replay of an nRF log needs --date")
        make_worker = lambda q, _hr: ReplayWorker(q, notes, args.speed, args.skip * 60)
    else:
        make_worker = lambda q, hr: BleWorker(q, hr, args.pedal, args.hr_name)
    os.makedirs(args.out, exist_ok=True)
    # one log per run: connection events, rides started and saved, and on a hard crash the stack
    # of every thread (faulthandler)
    log = open(os.path.join(args.out, "app_log.txt"), "a", buffering=1)
    if sys.stderr is None:                          # started without a console (pythonw): errors go to the log
        sys.stderr = log
    import faulthandler
    faulthandler.enable(log, all_threads=True)

    engine = Engine(args.out, settings, make_worker, settings_path, log)
    engine.worker.log(f"app started, {sys.version.split()[0]}")
    engine.start()
    app_mode: dict = {"app": not args.browser, "window": None}
    server = make_server(engine, app_mode)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print("Assioma Recorder:", url, flush=True)

    try:
        if args.browser or args.no_open:
            if not args.no_open:
                import webbrowser
                webbrowser.open(url)
            engine.quit.wait()                      # Ctrl+C also ends it (finally below saves)
            time.sleep(0.5)                         # let the page fetch the final state
        else:
            import webview
            win = webview.create_window("Assioma Recorder", url, width=1360, height=840,
                                        min_size=(900, 560), background_color="#0b0d10")
            app_mode["window"] = win
            win.events.closing += engine.window_closing

            def closer():
                engine.quit.wait()
                win.destroy()
            threading.Thread(target=closer, daemon=True).start()
            webview.start()
    except KeyboardInterrupt:
        pass
    finally:
        engine.quit.set()
        engine.join(timeout=2)
        server.shutdown()
        worker = engine.worker
        worker.stop()
        worker.join(timeout=6)                      # let it disconnect before the interpreter shuts down
        worker.log("app closed")
        session = engine.session
        if session is not None and session.state != "finished":   # closed mid-ride: still save it
            result = session.finish()
            if result:
                print("saved", result[0])


if __name__ == "__main__":
    main()
