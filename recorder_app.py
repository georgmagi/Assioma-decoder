#!/usr/bin/env python3
"""
Assioma Recorder as a desktop app. The window is a web page (web/) in a native window
(pywebview, the Edge WebView2 engine that ships with Windows); Python does Bluetooth, decoding,
HRV and FIT exactly as the tkinter recorder does (Session and BleWorker from assioma_recorder).

    python recorder_app.py --hr --ftp 199              record, with a heart-rate strap
    python recorder_app.py --replay rides/ride_..._raw.csv --speed 5
                                                        test the window with a recorded ride
    python recorder_app.py --browser                   use the default browser instead of a window

Threads: the engine owns the Session (takes in notifications, ticks, runs the buttons), a small
HTTP server on 127.0.0.1 hands the page a snapshot and the new graph points, and the main thread
runs the window. The page only ever reads state and sends button presses.

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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import assioma_decoder as dec
from assioma_recorder import (EVENT, HERE, BleWorker, ReplayWorker, Session, local_utc_offset_h,
                              read_raw_log, utcnow)

WEB = os.path.join(HERE, "web")
DYN_KEYS = ["rider_position"] + [f"{m}_{side}{unit}" for side in ("left", "right") for m, unit in
            [("power", "_w"), ("te", "_pct"), ("ps", "_pct"), ("pco", "_mm"), ("pp", "_start_deg"),
             ("pp", "_peak_start_deg"), ("pp", "_peak_end_deg"), ("pp", "_end_deg")]]
AUTOSTART_S = 3             # pedalling this long while ready starts the ride


def hms(n: int) -> str:
    return f"{n // 3600}:{n // 60 % 60:02d}:{n % 60:02d}"


class Engine(threading.Thread):
    """Owns the Session: every change to it happens on this thread, so the HTTP threads only read
    the published snapshot and slices of the append-only graph lists."""

    def __init__(self, session: Session, worker, q: queue.Queue, ftp=None, plan_min: float = 60) -> None:
        super().__init__(daemon=True)
        self.session, self.worker, self.q = session, worker, q
        self.ftp, self.plan_min = ftp, plan_min
        self.cmds: queue.Queue = queue.Queue()
        self.dialog = None                 # None | "finish" | "discard" | "saved"
        self.summary: dict = {}
        self.error = ""
        self.pedal_since = None
        self.quit = threading.Event()      # the window should close
        self.snap: dict = {}
        self._last_tick = 0.0

    # ---- commands from the page (run on this thread)
    def command(self, name: str, timeout: float = 5.0) -> None:
        """Queue a button press and wait until it has been carried out."""
        done = threading.Event()
        self.cmds.put((name, done))
        done.wait(timeout)

    def _do(self, name: str) -> None:
        s, now = self.session, utcnow()
        if s.state == "finished" and name not in ("close", "show_file"):
            return
        if name == "start" and self.dialog is None:
            s.start(now)
        elif name == "pause":
            s.pause(now)
        elif name == "resume":
            self.dialog = None
            s.resume(now)
        elif name == "toggle" and self.dialog is None:
            {"ready": s.start, "recording": s.pause, "paused": s.resume}.get(s.state, lambda _n: None)(now)
        elif name == "stop":
            if s.state == "ready":                 # nothing recorded: just close
                self._end(save=False)
            else:
                s.pause(now)                       # the clock stops while you decide
                self.dialog = "finish"
        elif name == "back":
            self.dialog = "finish"
        elif name == "discard":
            self.dialog = "discard"
        elif name == "discard_confirm":
            self._end(save=False, discard=True)
        elif name == "save":
            self._save()
        elif name == "show_file":
            path = self.summary.get("path")
            if path and sys.platform == "win32":
                import subprocess
                subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        elif name == "close":
            self.quit.set()

    def _drain(self) -> None:
        if self.session.state == "finished":
            return
        try:
            while True:
                self.session.add(self.q.get_nowait())
        except queue.Empty:
            pass

    def _end(self, save: bool, discard: bool = False) -> None:
        self.worker.stop()
        self._drain()
        if discard:
            self.session.discard()
        else:
            self.session.finish()                  # never started: removes the empty files
        self.dialog = None
        self.quit.set()

    def _save(self) -> None:
        s = self.session
        self.worker.stop()
        self._drain()
        st = s.stats()
        result = s.finish()
        if not result:
            self.quit.set()
            return
        path, summ = result
        secs = s.seconds
        el = int((secs[-1]["time"] - secs[0]["time"]).total_seconds()) + 1
        a1 = [r["dfa_a1"] for r in secs if r.get("dfa_a1") is not None]
        bal = summ["balance_right"]
        self.summary = {
            "path": path, "file": os.path.basename(path),
            "stats": [["Ride time", hms(len(secs))], ["Elapsed", hms(el)],
                      ["Avg power", _num(summ["avg_power"], " W")], ["NP", _num(summ["np"], " W")],
                      ["Avg HR", _num(summ["avg_hr"], " bpm")],
                      ["Balance", "--" if bal is None else f"{100 - bal:.0f} · {bal:.0f}"],
                      ["Pw:HR", "--" if st["decoupling"] is None else f"{st['decoupling']:+.1f} %"],
                      ["DFA α1 avg", "--" if not a1 else f"{sum(a1) / len(a1):.2f}"]]}
        self.dialog = "saved"

    def window_closing(self) -> bool:
        """The window's close button: True lets it close. Mid-ride it asks first, like Stop."""
        s = self.session
        if s.state in ("recording", "paused") and not self.quit.is_set():
            self.command("stop")
            return False
        return True

    # ---- the loop
    def run(self) -> None:
        while not self.quit.is_set():
            try:
                self._drain()
                while True:
                    try:
                        name, done = self.cmds.get_nowait()
                    except queue.Empty:
                        break
                    try:
                        self._do(name)
                    finally:
                        done.set()
                t = time.monotonic()
                if t - self._last_tick >= 0.25 and self.session.state != "finished":
                    self._last_tick = t
                    self._tick()
                self.snap = self._snapshot()
            except Exception as e:                 # never let a glitch stop the recording
                self.error = f"{type(e).__name__}: {e}"[:160]
                self.worker.log(f"engine: {self.error}")
            time.sleep(0.1)

    def _tick(self) -> None:
        s, now = self.session, utcnow()
        s.tick(now)
        if self.worker.pedal_name:
            s.pedal_name = self.worker.pedal_name
        if s.state == "ready" and self.dialog is None:        # auto-start after 3 s of pedalling
            if (s.live(now).get("power_w") or 0) > 0:
                self.pedal_since = self.pedal_since or now
                if (now - self.pedal_since).total_seconds() >= AUTOSTART_S:
                    s.start(now)
            else:
                self.pedal_since = None

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
        s, now = self.session, utcnow()
        live = s.live(now) if s.state != "finished" else {}
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
        w = self.worker
        return {
            "now": (now - t0).total_seconds() if t0 else None,
            "t0_ms": t0.replace(tzinfo=timezone.utc).timestamp() * 1000 if t0 else None,   # graph clock times
            "live": {**{k: live.get(k) for k in ("power_w", "cadence_rpm", "balance_right_pct", "heart_rate_bpm")},
                     "power_3s": self._power_3s(now) if s.state != "finished" else None},
            "stats": {k: st.get(k) for k in ("avg_power", "np", "avg_cadence", "balance_right", "avg_hr",
                                             "pco_left", "pco_right", "pco_left_range", "pco_right_range")},
            "dyn": {k: live.get(k) for k in DYN_KEYS},
            "hrv": h, "strap": strap, "ride": ride,
            "beats": [[round((b.time - t0).total_seconds(), 3), b.artifact] for b in beats[-60:]
                      if (now - b.time).total_seconds() < 5.5] if t0 else [],      # the HR tile's 4 s strip
            "sensors": [{"label": "Pedals", "status": w.status.get("pedal", "")}]
                       + ([{"label": "Strap", "status": w.status["hr"]}] if w.status.get("hr") else []),
            "battery": s.battery,
            "error": self.error,
            "dialog": self.dialog, "summary": self.summary,
            "config": {"ftp": self.ftp, "plan_min": self.plan_min},
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


def _num(v, unit: str) -> str:
    return "--" if v is None else f"{round(v)}{unit}"


def _r(x):
    return round(x, 2) if isinstance(x, float) else x


# --------------------------------------------------------------------------- #
# HTTP: the page, its files, the snapshot and the buttons (127.0.0.1 only)
# --------------------------------------------------------------------------- #
def make_server(engine: Engine, app_mode: dict) -> ThreadingHTTPServer:
    session = engine.session

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
            if u.path == "/api/pedal":
                self._json(pedal_silhouette())
                return
            if u.path == "/api/state":
                q = parse_qs(u.query)
                p, h, a, b = (int(q.get(k, ["0"])[0]) for k in ("p", "h", "a", "b"))
                lp, lh, la, lb = session.live_power, session.live_hr, session.hrv.series, session.hrv.beats
                pn, hn, an, bn = len(lp), len(lh), len(la), session.hrv._judged
                t0 = session.t0
                self._json({**engine.snap, "app": app_mode["app"], "closed": engine.quit.is_set(),
                            "pn": pn, "hn": hn, "an": an, "bn": bn,
                            # beats once judged: [s since t0, RR ms, artifact]
                            "beats_rr": [[_r((x.time - t0).total_seconds()), _r(x.rr), bool(x.artifact)]
                                         for x in lb[b:bn]] if t0 and b <= bn else [],
                            "power": [[_r(t), _r(w), _r(av)] for t, w, av in lp[p:pn]] if p <= pn else [],
                            "hr": [[_r(t), b] for t, b in lh[h:hn]] if h <= hn else [],
                            # DFA alpha 1 every 5 s: [s since t0, alpha 1 or None, artifact %]
                            "a1": [[_r((ts - t0).total_seconds()), _r(a1), _r(art)] for ts, a1, _, art in la[a:an]]
                                  if t0 and a <= an else []})
                return
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
                engine.command(name)
            self._json(engine.snap)

    return ThreadingHTTPServer(("127.0.0.1", 0), Handler)


# --------------------------------------------------------------------------- #
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hr", action="store_true", help="also connect a Bluetooth heart-rate strap")
    ap.add_argument("--pedal", default="", help="only use a pedal whose name contains this (e.g. 12345)")
    ap.add_argument("--hr-name", default="", help="only use a HR strap whose name contains this")
    ap.add_argument("--out", default=os.path.join(HERE, "rides"), help="folder for recordings")
    ap.add_argument("--ftp", type=float, default=None, help="your FTP in W: power zones and the Z2 band")
    ap.add_argument("--plan", type=float, default=60, help="planned ride length in minutes")
    ap.add_argument("--replay", metavar="LOG", help="test: replay a recorder raw log (or an nRF log with --date)")
    ap.add_argument("--date", help="date of an nRF log (YYYY-MM-DD)")
    ap.add_argument("--speed", type=float, default=1.0, help="replay speed")
    ap.add_argument("--skip", type=float, default=0, help="replay: start this many minutes into the log")
    ap.add_argument("--browser", action="store_true", help="open in the default browser instead of a window")
    ap.add_argument("--no-open", action="store_true", help=argparse.SUPPRESS)      # tests: serve only
    args = ap.parse_args()

    q: queue.Queue = queue.Queue()
    session = Session(args.out)
    if args.replay:
        if args.replay.endswith("_raw.csv"):
            notes = [n for n in read_raw_log(args.replay) if n.char != EVENT]
        elif args.date:
            notes = list(dec.read_nrf_log(args.replay, args.date, local_utc_offset_h()))
        else:
            ap.error("--replay of an nRF log needs --date")
        worker = ReplayWorker(q, notes, args.speed, args.skip * 60)
    else:
        worker = BleWorker(q, args.hr, args.pedal, args.hr_name)
    log = open(session.base + "_log.txt", "a", buffering=1)
    if sys.stderr is None:                          # started without a console (pythonw): errors go to the log
        sys.stderr = log
    import faulthandler
    faulthandler.enable(log, all_threads=True)
    worker.log_file = log
    worker.log(f"app started, {sys.version.split()[0]}")
    worker.start()

    engine = Engine(session, worker, q, args.ftp, args.plan)
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
        worker.stop()
        worker.join(timeout=6)                      # let it disconnect before the interpreter shuts down
        worker.log("app closed")
        if session.state != "finished":             # closed some other way: still save what we have
            result = session.finish()
            if result:
                print("saved", result[0])


if __name__ == "__main__":
    main()
