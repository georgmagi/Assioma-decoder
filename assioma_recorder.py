#!/usr/bin/env python3
"""
Assioma recording core: a ride (Session: raw log, per-second CSV, live stats, HRV, FIT on finish),
the sensor connections (BleWorker: the left pedal over Bluetooth, heart rate over ANT+ or
Bluetooth) and a replay of a log for testing. The app (recorder_app.py) is built on these.

From the command line it converts logs to FIT:

    python assioma_recorder.py --from-nrf LOG.csv --date 2026-09-28
                                               convert an nRF Connect log to FIT
    python assioma_recorder.py --rebuild rides/ride_..._raw.csv
                                               rebuild a FIT from a raw log (e.g. after a crash)

Read-only: it only subscribes to notifications and never writes to the pedals.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import os
import queue
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from bisect import bisect_left
from collections import deque
from typing import Optional

import assioma_decoder as dec
import fit_writer
import ftms
import hrv

_BASE = "-0000-1000-8000-00805f9b34fb"
CPM_UUID = "00002a63" + _BASE
HR_UUID = "00002a37" + _BASE
HR_SERVICE = "0000180d" + _BASE
PRIVATE_UUID = dec.PRIVATE_CHAR.lower()
# your files (rides, settings, the pedal address) live next to the code, or next to the .exe
HERE = os.path.dirname(sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__))
PEDAL_FILE = os.path.join(HERE, "left_pedal_address.txt")
CSV_COLUMNS = ["time"] + dec.SECOND_COLUMNS + ["rr_ms"] + hrv.COLUMNS


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def short_uuid(u: str) -> str:
    u = u.lower()
    return u[4:8].upper() if u.startswith("0000") and u.endswith(_BASE) else u.upper()


def local_utc_offset_h() -> float:
    off = datetime.now().astimezone().utcoffset()
    return off.total_seconds() / 3600 if off else 0.0


# --------------------------------------------------------------------------- #
# Recording session: crash-safe raw log + per-second CSV, FIT at the end
# --------------------------------------------------------------------------- #
EVENT = "EVENT"          # raw-log lines for start / pause / resume / stop (hex of the ASCII word)


def recording_segments(events: list) -> Optional[list]:
    """[(time, word)] start/pause/resume/stop -> [(from, to or None)] of recorded time.
    None when there are no events at all (a log from before start/pause existed: keep everything)."""
    if not events:
        return None
    segs: list = []
    for t, word in events:
        if word in ("start", "resume") and (not segs or segs[-1][1] is not None):
            segs.append((t, None))
        elif word in ("pause", "stop") and segs and segs[-1][1] is None:
            segs[-1] = (segs[-1][0], t)
    return segs


def in_segments(t: datetime, segs) -> bool:
    return segs is None or any(a <= t and (b is None or t < b) for a, b in segs)


class Session:
    """One ride. Everything the pedals and strap send is decoded and shown from the start, but
    seconds only count (per-second CSV, FIT, averages) while recording: between Start and Stop,
    minus pauses. The raw log keeps everything plus the start/pause/resume/stop markers, so a
    --rebuild after a crash gives the same ride."""

    def __init__(self, out_dir: str) -> None:
        os.makedirs(out_dir, exist_ok=True)
        self.base = os.path.join(out_dir, "ride_" + datetime.now().strftime("%Y-%m-%d_%H-%M-%S"))
        self._raw = open(self.base + "_raw.csv", "w", newline="")
        self._raw.write("time_utc,char,hex\n")
        self._sec = open(self.base + "_per_second.csv", "w", newline="")
        self._sec_w = csv.DictWriter(self._sec, CSV_COLUMNS, extrasaction="ignore")
        self._sec_w.writeheader()
        self.decoder = dec.Decoder()
        self.ps = dec.PerSecond()
        self.seconds: list[dict] = []
        self.battery: Optional[tuple] = None
        self.pedal_name = "Assioma"
        self.hrv = hrv.HrvTracker()
        self._sums: dict = {}                     # name -> [sum, count] for ride averages
        self._np30: deque = deque(maxlen=30)
        self._pco_hist: dict = {"left": {}, "right": {}}   # mm -> seconds, for the PCO range
        self._dec_t: list = []                    # ride second of each (30 s power, HR) sample
        self._dec_p4: list = [0.0]                # prefix sums of (30 s power)^4 and of HR
        self._dec_hr: list = [0.0]
        self.hr_last: Optional[datetime] = None   # last heart-rate notification (strap silence check)
        # live series for the graphs, straight from the notifications (the per-second rows lag 2 s)
        self.t0: Optional[datetime] = None        # first notification: time zero of the graphs
        self.live_power: list[tuple] = []         # (s since t0, power, 10 s average)
        self.live_hr: list[tuple] = []            # (s since t0, bpm)
        self._p10: deque = deque()
        self.state = "ready"                      # ready -> recording <-> paused -> finished
        self.segments: list = []                  # [(from, to or None)] of recorded time
        self.started: Optional[datetime] = None

    # ---- start / pause / resume
    def _event(self, now: datetime, word: str) -> None:
        self._raw.write(f"{now.isoformat()},{EVENT},{word.encode().hex()}\n")
        self._raw.flush()

    def start(self, now: datetime) -> None:
        if self.state == "ready":
            self.state, self.started = "recording", now
            self.segments.append((now, None))
            self._event(now, "start")

    def pause(self, now: datetime) -> None:
        if self.state == "recording":
            self.state = "paused"
            self.segments[-1] = (self.segments[-1][0], now)
            self._event(now, "pause")

    def resume(self, now: datetime) -> None:
        if self.state == "paused":
            self.state = "recording"
            self.segments.append((now, None))
            self._event(now, "resume")

    def log_raw(self, n: dec.Notification) -> None:
        """Into the raw log only (e.g. the strap's stream setup from before the ride started)."""
        self._raw.write(f"{n.time.isoformat()},{n.char},{n.data.hex()}\n")

    def add(self, n: dec.Notification) -> None:
        self.log_raw(n)
        if self.t0 is None:
            self.t0 = n.time
        for r in self.decoder.feed(n):
            if r.get("battery_left_pct") is not None:
                self.battery = (r["battery_left_pct"], r["battery_right_pct"])
            ts = (r["time"] - self.t0).total_seconds()
            if r.get("type") == dec.CPM_CHAR:
                p = r["cpm_power_w"]
                self._p10.append((ts, p))
                while self._p10[0][0] < ts - 10:
                    self._p10.popleft()
                self.live_power.append((ts, p, sum(x for _, x in self._p10) / len(self._p10)))
            if r.get("type") == dec.HR_CHAR:
                self.hr_last = r["time"]
                self.live_hr.append((ts, r["heart_rate_bpm"]))
            if r.get("rr_ms"):
                self.hrv.add(r["time"], r["rr_ms"])
            for s in self.ps.ingest(r):
                self._second(s)

    def tick(self, now: datetime) -> None:
        # a 2 s lag lets late notifications land in the right second
        if self.ps.t is not None:
            for s in self.ps.advance(now - timedelta(seconds=2)):
                self._second(s)
        self.hrv.update(now)
        self._raw.flush()

    def _add_avg(self, name: str, v) -> None:
        if v is not None:
            a = self._sums.setdefault(name, [0.0, 0])
            a[0] += v
            a[1] += 1

    def _avg(self, name: str):
        a = self._sums.get(name)
        return a[0] / a[1] if a else None

    def _second(self, s: dict) -> None:
        if not self.segments or not in_segments(s["time"], self.segments):
            return                              # not recording (before Start, or paused)
        s.update(self.hrv.columns_at(s["time"] + timedelta(seconds=1)))
        self.seconds.append(s)
        self._sec_w.writerow({**s, "time": s["time"].isoformat() + "Z", "rr_ms": " ".join(map(str, s.get("rr_ms", [])))})
        self._sec.flush()
        p = s.get("power_w")
        self._add_avg("power", p)
        self._add_avg("hr", s.get("heart_rate_bpm"))
        if p is not None:
            self._np30.append(p)
            if len(self._np30) == 30:
                p4 = (sum(self._np30) / 30) ** 4
                self._add_avg("np4", p4)
                hr = s.get("heart_rate_bpm")
                if hr:                          # for decoupling: prefix sums over seconds with both
                    self._dec_t.append(len(self.seconds) - 1)
                    self._dec_p4.append(self._dec_p4[-1] + p4)
                    self._dec_hr.append(self._dec_hr[-1] + hr)
        if p:                                   # pedalling: cadence, balance and PCO averages
            self._add_avg("cadence", s.get("cadence_rpm"))
            self._add_avg("balance", s.get("balance_right_pct"))
            for side in ("left", "right"):
                v = s.get(f"pco_{side}_mm")
                self._add_avg(f"pco_{side}", v)
                if v is not None:
                    self._pco_hist[side][v] = self._pco_hist[side].get(v, 0) + 1

    def stats(self) -> dict:
        np4 = self._avg("np4")
        return {"avg_power": self._avg("power"), "np": np4 ** 0.25 if np4 else None,
                "avg_hr": self._avg("hr"), "avg_cadence": self._avg("cadence"),
                "balance_right": self._avg("balance"),
                "pco_left": self._avg("pco_left"), "pco_right": self._avg("pco_right"),
                "pco_left_range": self._pco_range("left"), "pco_right_range": self._pco_range("right"),
                **self.decoupling()}

    DEC_WARMUP, DEC_MIN = 600, 600               # skip a 10 min warm-up; need 10 min after it

    def decoupling(self) -> dict:
        """Aerobic decoupling (Pw:HR): the efficiency factor EF = NP / average HR of the first half
        of the ride after the warm-up, against the second half. Under ~5 % = well coupled."""
        t = self._dec_t
        end = t[-1] if t else 0
        if end - self.DEC_WARMUP < self.DEC_MIN:
            return {"decoupling": None, "ef": None, "decoupling_at": self.DEC_WARMUP + self.DEC_MIN}
        a = bisect_left(t, self.DEC_WARMUP)
        m = bisect_left(t, (self.DEC_WARMUP + end) / 2)
        b = len(t)

        def ef(i, j):
            n = j - i
            if n < 60:
                return None
            return ((self._dec_p4[j] - self._dec_p4[i]) / n) ** 0.25 / ((self._dec_hr[j] - self._dec_hr[i]) / n)
        ef1, ef2 = ef(a, m), ef(m, b)
        if not ef1 or ef2 is None:
            return {"decoupling": None, "ef": None, "decoupling_at": self.DEC_WARMUP + self.DEC_MIN}
        return {"decoupling": 100 * (ef1 - ef2) / ef1, "ef": ef(a, b), "decoupling_at": None}

    def _pco_range(self, side: str) -> Optional[tuple]:
        """10th and 90th percentile of the pedalling seconds' PCO (the middle 80 %)."""
        hist = self._pco_hist[side]
        n = sum(hist.values())
        if n < 10:
            return None
        out, acc = [], 0
        targets = [0.1 * n, 0.9 * n]
        for mm in sorted(hist):
            acc += hist[mm]
            while targets and acc >= targets[0]:
                out.append(mm)
                targets.pop(0)
        return tuple(out)

    def live(self, now: datetime) -> dict:
        return self.ps.values_at(now)

    def discard(self) -> None:
        """Throw the ride away: close and delete its files."""
        self.state = "finished"
        for fh in (self._raw, self._sec):
            if not fh.closed:
                fh.close()
        for ext in ("_raw.csv", "_per_second.csv", ".fit") + STRAP_FILES:
            try:
                os.remove(self.base + ext)
            except OSError:
                pass

    def finish(self) -> Optional[tuple]:
        now = utcnow()
        if self.state in ("recording", "paused"):
            if self.state == "recording":
                self.segments[-1] = (self.segments[-1][0], now)
            self._event(now, "stop")
        self.state = "finished"
        for s in self.ps.finish():
            self._second(s)
        self._raw.close()
        self._sec.close()
        if not self.seconds:            # nothing recorded: don't leave empty files behind
            for ext in ("_raw.csv", "_per_second.csv"):
                try:
                    os.remove(self.base + ext)
                except OSError:
                    pass
            return None
        path = self.base + ".fit"
        summary = fit_writer.write_activity(self.seconds, path, self.pedal_name, self.battery)
        threading.Thread(target=export_strap_streams, args=(self.base,), daemon=True).start()
        return path, summary


STRAP_FILES = ("_ecg.csv", "_acc.csv")    # the strap's raw streams, written from the raw log


def export_strap_streams(base: str) -> None:
    """base_ecg.csv / base_acc.csv from the raw log, when the strap streamed (polar_pmd.export)."""
    try:
        import polar_pmd
        polar_pmd.export(base + "_raw.csv", base)
    except Exception as e:                        # never in the way of a saved ride
        print(f"strap streams: {type(e).__name__}: {e}", file=sys.stderr)


def convert(notes, base: str) -> tuple:
    """Offline: notifications -> per-second CSV + FIT. Start/pause/resume/stop markers in a
    recorder raw log are honoured: only recorded seconds are kept."""
    notes = list(notes)
    segs = recording_segments([(n.time, n.data.decode()) for n in notes if n.char == EVENT])
    rows = dec.decode(n for n in notes if n.char != EVENT)
    sec = [r for r in dec.per_second(rows) if in_segments(r["time"], segs)]
    if not sec:
        raise SystemExit("no Assioma data found in the log")
    h = hrv.analyse([(r["time"], r["rr_ms"]) for r in rows if r.get("rr_ms")])
    for s in sec:
        s.update(h.columns_at(s["time"] + timedelta(seconds=1)))
    dec.write_csv(base + "_per_second.csv", sec, CSV_COLUMNS)
    battery = next(((r["battery_left_pct"], r["battery_right_pct"]) for r in reversed(rows)
                    if r.get("battery_left_pct") is not None), None)
    summary = fit_writer.write_activity(sec, base + ".fit", battery=battery)
    return base + ".fit", summary, len(sec)


def read_raw_log(path: str):
    with open(path, newline="") as fh:
        for row in csv.DictReader(fh):
            yield dec.Notification(datetime.fromisoformat(row["time_utc"]), row["char"], bytes.fromhex(row["hex"]))


# --------------------------------------------------------------------------- #
# Bluetooth (runs in its own thread with its own asyncio loop)
# --------------------------------------------------------------------------- #
class BleWorker(threading.Thread):
    SILENT_S = 20             # a connected sensor quiet this long is reconnected
    def __init__(self, q: queue.Queue, want_hr: bool, pedal_filter: str, hr_filter: str) -> None:
        super().__init__(daemon=True)
        self.q = q
        self.want_hr = want_hr
        self.pedal_filter = pedal_filter.lower()
        self.hr_filter = hr_filter.lower()
        self.status = {"pedal": "starting…", "hr": "starting…" if want_hr else ""}
        self.pedal_name: Optional[str] = None
        self._halt = threading.Event()
        self._not_main: dict = {}            # address -> time it turned out not to be the left pedal
        self._last_private = 0.0             # monotonic time of the last private-stream notification
        self._held: Optional[list] = None    # pedal data held back while checking which pedal it is
        self._saved = self._load_saved()     # address of the left pedal from earlier rides
        self.last_data = {"pedal": 0.0, "hr": 0.0}   # monotonic time of each sensor's last notification
        self.link = {"pedal": "Bluetooth", "hr": "", "trainer": "Bluetooth"}  # how each is connected, for the screen
        # the smart trainer (FTMS): connected only when asked for; the app sets trainer.want
        self.want_trainer = False
        self.trainer = ftms.TrainerControl(log=self.log)
        self.status["trainer"] = ""
        self.ant = None                      # ANT+ heart rate (ant_hr.AntHrWorker), preferred over Bluetooth
        self.log_file = None
        # each sensor's lifecycle, for the Connections pane: wanted or not; an attempt you started by
        # clicking (a deadline: then "couldn't find it"); and the state shown
        self.want = {"pedal": True, "hr": want_hr, "trainer": False}
        # record the strap over Bluetooth too while ANT+ is the source: a second copy of every beat,
        # kept in the raw log (BLE_HR_SECOND) for linkcheck.py; it never feeds the ride's HRV
        self.both_hr = True
        # a Polar strap's raw streams (polar_pmd.py): ECG and accelerometer at full detail, into
        # the raw log; the only writes the recorder makes to the strap are the PMD start commands
        self.strap_streams = True
        self.pmd: list = []                       # what the strap agreed to stream, for the log
        # your strap: {address, name, ant}; once set only it is connected (Bluetooth and ANT+), so a
        # neighbour's strap or watch can't slip into a ride. Learnt from the first strap that
        # connects; the app keeps it in settings.json and can forget it. {} = any strap
        self.hr_lock: dict = {}
        self._pmd_frames = 0
        self.attempt = {"pedal": None, "hr": None, "trainer": None}   # monotonic deadline of a clicked attempt
        self.state = {"pedal": "searching", "hr": "searching" if want_hr else "off", "trainer": "off"}
        self.note = {"pedal": "", "hr": "", "trainer": ""}

    ATTEMPT_S = 20.0          # how long a clicked connect tries before saying it couldn't find the sensor
    HINT = {"pedal": "Couldn't find the pedals: they sleep when still. Turn the cranks to wake them, then try again.",
            "hr": "Couldn't find the strap: it sleeps without skin contact. Wet the pads, put it on, then try again.",
            "trainer": "Couldn't find the trainer: is it plugged in, and closed in other apps? It takes one connection."}

    def set_want(self, kind: str, on: bool) -> None:
        """The Connections pane (from the app's thread): connect now, trying for ATTEMPT_S, or let go."""
        self.want[kind] = on
        if kind == "hr" and self.ant is not None:
            self.ant.enabled = on
        if on:
            self.attempt[kind] = time.monotonic() + self.ATTEMPT_S
            self.note[kind] = ""
            if self.state[kind] != "connected":
                self.state[kind] = "connecting"
        else:
            self.attempt[kind], self.state[kind], self.note[kind] = None, "off", ""
            self.status[kind] = ""

    def _waiting(self, kind: str, what: str) -> None:
        """Between scans: 'connecting' while a clicked attempt runs, then 'couldn't find it' once it
        runs out (and the search goes on quietly, so the sensor still connects once it wakes)."""
        dl = self.attempt[kind]
        if dl is not None and time.monotonic() > dl:
            self.attempt[kind] = None
            self.state[kind], self.note[kind] = "failed", self.HINT[kind]
            self.status[kind] = "not found"
            self.log(f"{kind}: not found within {self.ATTEMPT_S:.0f} s")
        elif dl is not None:
            self.state[kind], self.status[kind] = "connecting", f"looking for the {what}…"
        elif self.state[kind] != "failed":
            self.state[kind], self.status[kind] = "searching", f"waiting for the {what}…"

    def _connected(self, kind: str, name: str) -> None:
        self.state[kind], self.note[kind], self.attempt[kind] = "connected", "", None
        self.status[kind] = f"connected: {name}"

    def sensor(self, kind: str) -> dict:
        """What the Connections pane shows: {state, status, link, note}."""
        status, link = self.view(kind)
        st = self.state[kind]
        if kind == "hr" and link == "ANT+" and self.both_hr and time.monotonic() - self.last_data["hr"] < 5:
            link = "ANT+ & BT"                # both links recording
            if self.status["hr"].startswith("connected: "):
                status = self.status["hr"]    # the Bluetooth name says which strap ("Polar H10 …")
        if kind == "hr" and self.want["hr"] and self._ant_has_hr() and self.ant.receiving():
            st = "connected"
        return {"state": st, "status": status, "link": link, "note": self.note[kind]}

    @staticmethod
    def _load_saved() -> str:
        try:
            with open(PEDAL_FILE) as fh:
                return fh.read().strip()
        except OSError:
            return ""

    def _save(self, address: str) -> None:
        if address != self._saved:
            try:
                with open(PEDAL_FILE, "w") as fh:
                    fh.write(address)
                self._saved = address
            except OSError:
                pass

    def stop(self) -> None:
        self._halt.set()
        if self.ant is not None:
            self.ant.stop()

    ANT_GRACE_S = 15          # after the ANT+ stick opens, Bluetooth HR waits this long for the strap

    def _ant_has_hr(self) -> bool:
        """Heart rate comes over ANT+ (lossless RR) while the stick hears the strap, and for a moment
        after the stick opens; otherwise over Bluetooth."""
        a = self.ant
        return a is not None and a.stick and (a.receiving() or time.monotonic() - a.opened < self.ANT_GRACE_S)

    def view(self, kind: str) -> tuple:
        """(status, link) of a sensor for the screen; heart rate straight from ANT+ while it has it."""
        if kind == "hr" and self._ant_has_hr():
            return self.ant.status, "ANT+"
        return self.status.get(kind, ""), self.link.get(kind, "")

    def run(self) -> None:
        self.want["trainer"] = self.want["trainer"] or self.want_trainer
        if self.want["trainer"]:
            self.state["trainer"] = "searching"
        if True:                                      # the stick is opened even with the strap off: connecting it later is instant
            try:
                import ant_hr
                self.ant = ant_hr.AntHrWorker(self.q)
                self.ant.log = self.log
                self.ant.device_lock = self.hr_lock.get("ant")
                self.ant.enabled = self.want["hr"]
                self.ant.start()
            except Exception as e:
                self.log(f"ANT+ not available: {type(e).__name__}: {e}")
        try:
            from bleak.backends.winrt.util import uninitialize_sta   # Windows only; harmless elsewhere
            uninitialize_sta()
        except Exception:
            pass
        try:
            asyncio.run(self._main())
        except ImportError:
            self.status["pedal"] = "bleak is not installed: run  pip install bleak"
        except Exception as e:  # e.g. Bluetooth switched off
            self.status["pedal"] = f"Bluetooth error: {e}"
            self.log(f"Bluetooth thread ended: {type(e).__name__}: {e}")

    async def _main(self) -> None:
        import bleak  # noqa: F401  (fail early with a clear message)
        self._scan_lock = asyncio.Lock()
        jobs = [self._keep_connected("pedal", self._pick_pedal, [CPM_UUID, PRIVATE_UUID]),
                self._keep_connected("hr", self._pick_hr, [HR_UUID]), self._keep_trainer()]
        await asyncio.gather(*jobs)

    def _pick_trainer(self, found: dict):
        c = [(d, a) for d, a in found.values()
             if ftms.FTMS_SERVICE in [u.lower() for u in (a.service_uuids or [])]
             and "assioma" not in self._devname(d, a).lower()]
        c = sorted(c, key=lambda x: x[1].rssi or -999, reverse=True)
        return c[0] if c else None

    async def _keep_trainer(self) -> None:
        """The smart trainer: find it (FTMS service), connect, and let TrainerControl keep it on target.
        Anything that goes wrong only costs the trainer link (logged, shown, retried): never the ride."""
        from bleak import BleakClient, BleakScanner
        while not self._halt.is_set():
            if not self.want["trainer"]:
                self.state["trainer"] = "off"
                await asyncio.sleep(0.3)
                continue
            self._waiting("trainer", "trainer")
            async with self._scan_lock:
                found = await BleakScanner.discover(timeout=5.0, return_adv=True)
            if not self.want["trainer"]:
                continue
            hit = self._pick_trainer(found)
            if not hit:
                self._waiting("trainer", "trainer")
                continue
            self.state["trainer"] = "connecting"
            dev, adv = hit
            name = self._devname(dev, adv) or dev.address
            self.status["trainer"] = f"connecting to {name}…"
            loop = asyncio.get_running_loop()
            lost = asyncio.Event()
            try:
                async with BleakClient(dev, disconnected_callback=lambda _c: loop.call_soon_threadsafe(lost.set),
                                       timeout=20) as client:
                    self.log(f"trainer: connected to {name} ({dev.address})")
                    self._connected("trainer", name)
                    let_go = lambda: self._halt.is_set() or not self.want["trainer"]
                    try:
                        await self.trainer.run(client, lambda: client.is_connected and not lost.is_set(), let_go)
                    finally:
                        if let_go():
                            await self.trainer.release(client)      # hand it back (Reset): closing, or let go
                    if lost.is_set():
                        self.log("trainer: disconnected")
            except Exception as e:
                self.log(f"trainer: {type(e).__name__}: {e}")
                self.status["trainer"] = f"trainer link lost ({type(e).__name__}), retrying…"
                await asyncio.sleep(2)
            self.trainer.controlled = False

    @staticmethod
    def _devname(dev, adv) -> str:
        return adv.local_name or dev.name or ""

    def _pick_pedal(self, found: dict):
        """Left pedal only: it is the main unit and the only one with the advanced data."""
        c = [(d, a) for d, a in found.values()
             if "assioma" in self._devname(d, a).lower()
             and time.monotonic() - self._not_main.get(d.address, -1e9) > 120]   # retry after 2 min
        if self.pedal_filter:
            c = [x for x in c if self.pedal_filter in self._devname(*x).lower()]
        for x in c:                                    # the pedal that worked last time
            if x[0].address == self._saved:
                return x
        suffix = lambda x: self._devname(*x).strip().upper()[-1:]
        c = [x for x in c if suffix(x) == "L"] or [x for x in c if suffix(x) not in ("L", "R")]
        c = sorted(c, key=lambda x: x[1].rssi or -999, reverse=True)
        return c[0] if c else None

    def _pick_hr(self, found: dict):
        c = [(d, a) for d, a in found.values()
             if HR_SERVICE in [u.lower() for u in (a.service_uuids or [])]
             and "assioma" not in self._devname(d, a).lower()]
        if self.hr_filter:
            c = [x for x in c if self.hr_filter in self._devname(*x).lower()]
        if self.hr_lock.get("address"):
            c = [x for x in c if x[0].address.upper() == self.hr_lock["address"].upper()]
        c = sorted(c, key=lambda x: x[1].rssi or -999, reverse=True)
        return c[0] if c else None

    def set_hr_lock(self, lock: Optional[dict]) -> None:
        """Your strap, or None to take any (the next one to connect becomes yours)."""
        self.hr_lock = dict(lock or {})
        if self.ant is not None:
            self.ant.device_lock = self.hr_lock.get("ant")

    @staticmethod
    def ant_number(address: str) -> Optional[int]:
        """A Polar strap's ANT+ device number is the end of its Bluetooth address (H10
        24:AC:AC:12:34:56 = ANT+ #13398 = 0x3456). None when the address isn't a MAC (macOS)."""
        try:
            return int(address.replace(":", "")[-4:], 16) or None
        except ValueError:
            return None

    def log(self, text: str) -> None:
        """Connection events for the ride's log file (set by the recorder); never raises."""
        try:
            if self.log_file is not None:
                self.log_file.write(f"{datetime.now().isoformat(timespec='seconds')}  {text}\n")
                self.log_file.flush()
        except Exception:
            pass

    def _on_notify(self, sender, data: bytearray) -> None:
        n = dec.Notification(utcnow(), short_uuid(str(sender.uuid)), bytes(data))
        now = time.monotonic()
        self.last_data["hr" if n.char == dec.HR_CHAR else "pedal"] = now
        if n.char == dec.HR_CHAR and not self.want["hr"]:
            return                        # the strap is off
        if n.char == dec.HR_CHAR and self._ant_has_hr():
            if self.both_hr:              # ANT+ is the source: this copy is for the comparison only
                self.q.put(dec.Notification(n.time, dec.BLE_HR_SECOND, n.data))
            return                        # (never counted twice)
        if n.char == dec.PRIVATE_CHAR:
            self._last_private = now
        if n.char in (dec.PRIVATE_CHAR, dec.CPM_CHAR) and self._held is not None:
            self._held.append(n)          # pedal not confirmed as the left one yet
        else:
            self.q.put(n)

    async def _start_pmd(self, client) -> None:
        """A Polar strap (H10): ask for every raw stream it has at the highest settings it offers
        (polar_pmd.choose_max). Commands, answers and frames all go to the queue as they are, so
        the raw log keeps them. Any other strap has no PMD control point and is left alone."""
        import polar_pmd as pmd
        loop = asyncio.get_running_loop()
        answers: asyncio.Queue = asyncio.Queue()

        def on_cp(_s, data):
            data = bytes(data)
            self.q.put(dec.Notification(utcnow(), pmd.CP_CHAR, data))
            loop.call_soon_threadsafe(answers.put_nowait, data)

        def on_data(_s, data):
            self._pmd_frames += 1
            self.q.put(dec.Notification(utcnow(), pmd.DATA_CHAR, bytes(data)))

        self._pmd_frames = 0
        try:
            await asyncio.wait_for(client.start_notify(pmd.CP, on_cp), 10)
            await asyncio.wait_for(client.start_notify(pmd.DATA, on_data), 10)
        except Exception:
            return                                # not a Polar strap (or it won't let us subscribe)

        async def ask(cmd: bytes) -> dict:
            while not answers.empty():
                answers.get_nowait()
            self.q.put(dec.Notification(utcnow(), pmd.CMD_CHAR, cmd))
            await asyncio.wait_for(client.write_gatt_char(pmd.CP, cmd, response=True), 10)
            params = b""
            while True:                           # the answer to this op code (long ones come in parts)
                r = pmd.response(await asyncio.wait_for(answers.get(), 5))
                if r is None or r["op"] != cmd[0]:
                    continue
                params += r["params"]
                if not r["more"]:
                    return {**r, "params": params}

        got = []
        try:
            feat = bytes(await asyncio.wait_for(client.read_gatt_char(pmd.CP), 10))
            self.q.put(dec.Notification(utcnow(), pmd.FEAT_CHAR, feat))
            offered = pmd.features(feat)
            for kind in (pmd.ECG, pmd.ACC, pmd.PPG, pmd.PPI, pmd.GYRO, pmd.MAG):
                if kind not in offered or kind in (pmd.PPG, pmd.PPI):   # optical only: not on a chest strap
                    continue
                r = await ask(pmd.settings_cmd(kind))
                if r["status"]:
                    self.log(f"hr: {pmd.NAMES[kind]} settings: {r['error']}")
                    continue
                chosen = pmd.choose_max(pmd.parse_settings(r["params"]))
                r = await ask(pmd.start_cmd(kind, chosen))
                if r["status"] in (0, 6):
                    got.append(f"{pmd.NAMES[kind]} {chosen}")
                else:
                    self.log(f"hr: {pmd.NAMES[kind]} start: {r['error']}")
        except Exception as e:
            self.log(f"hr: strap streams: {type(e).__name__}: {e}")
        self.pmd = got
        self.log(f"hr: strap streams {got or 'none'} (MTU {getattr(client, 'mtu_size', '?')})")
        if got:
            # the H10 only sends its streams over a paired link: it accepts the start but stays silent.
            # Paired, the first frames can still take over 5 s (the 5-s check warned falsely, 6-8 Oct),
            # so wait up to 20 s and note how long it took
            t0 = time.monotonic()
            while not self._pmd_frames and time.monotonic() - t0 < 20 and client.is_connected:
                await asyncio.sleep(0.5)
            if self._pmd_frames:
                self.log(f"hr: first strap frames after {time.monotonic() - t0:.1f} s")
            elif client.is_connected:
                self.log("hr: the strap accepted the streams but sent nothing in 20 s: pair it with Windows "
                         "(Settings > Bluetooth & devices > Add device)")

    async def _stop_pmd(self, client) -> None:
        """Leave the strap clean: stop its streams and unsubscribe before disconnecting, so the
        next connection (ours or another app's) starts from nothing."""
        import polar_pmd as pmd
        if not self.pmd:
            return
        self.pmd = []
        for kind in (pmd.ACC, pmd.ECG):
            try:
                await asyncio.wait_for(client.write_gatt_char(pmd.CP, pmd.stop_cmd(kind), response=True), 3)
            except Exception:
                pass
        for u in (pmd.DATA, pmd.CP):
            try:
                await asyncio.wait_for(client.stop_notify(u), 3)
            except Exception:
                pass

    async def _keep_connected(self, kind: str, pick, uuids: list) -> None:
        from bleak import BleakClient, BleakScanner
        while not self._halt.is_set():
            if not self.want[kind]:
                self.state[kind] = "off"
                await asyncio.sleep(0.3)
                continue
            if kind == "hr" and not self.both_hr and (self._ant_has_hr() or (self.ant is not None and self.ant.is_alive()
                                                                                and self.ant.status.startswith("looking"))):
                if self._ant_has_hr() and self.ant.receiving():
                    self._connected("hr", self.ant.status.split(": ", 1)[-1])
                else:
                    self._waiting(kind, "strap")
                await asyncio.sleep(0.5)          # ANT+ has the strap, or is still looking for the stick
                continue
            if kind == "hr":
                self.link[kind] = "Bluetooth"
            self._waiting(kind, "pedals" if kind == "pedal" else "strap")
            async with self._scan_lock:
                found = await BleakScanner.discover(timeout=5.0, return_adv=True)
            if not self.want[kind]:
                continue
            hit = pick(found)
            if not hit:
                self._waiting(kind, "pedals" if kind == "pedal" else "strap")
                continue
            self.state[kind] = "connecting"
            dev, adv = hit
            name = self._devname(dev, adv) or dev.address
            self.status[kind] = f"connecting to {name}…"
            loop = asyncio.get_running_loop()
            lost = asyncio.Event()
            try:
                async with BleakClient(dev, disconnected_callback=lambda _c: loop.call_soon_threadsafe(lost.set),
                                       timeout=20) as client:
                    missing = []
                    for u in uuids:
                        try:
                            await client.start_notify(u, self._on_notify)
                        except Exception:
                            missing.append(short_uuid(u))
                    if kind == "pedal":
                        # The main (left) pedal streams private data even when idle.
                        # Nothing within 8 s means we got the right pedal: drop it and look again.
                        self.status[kind] = f"checking {name}…"
                        t0 = time.monotonic()
                        self._held = []
                        ok = PRIVATE_UUID.upper() not in missing
                        while ok and self._last_private < t0 and time.monotonic() - t0 < 8:
                            await asyncio.sleep(0.25)
                        held, self._held = self._held, None
                        if not ok or self._last_private < t0:
                            self._not_main[dev.address] = time.monotonic()
                            if dev.address == self._saved:
                                self._saved = ""
                            self.status[kind] = f"{name} is not the left pedal, looking again…"
                            continue
                        for n in held:
                            self.q.put(n)
                        self._save(dev.address)
                        self.pedal_name = name
                    self._connected(kind, name)
                    self.log(f"{kind}: connected to {name} ({dev.address}), missing {missing or 'none'}")
                    if kind == "hr" and not self.hr_lock.get("address"):
                        ant = self.ant_number(dev.address) if "polar" in name.lower() else None   # others: ANT+ stays open
                        self.set_hr_lock({"address": dev.address, "name": name, "ant": ant})
                        self.log(f"hr: {name} is now your strap: others are left alone")
                    if kind == "hr" and self.strap_streams:
                        await self._start_pmd(client)
                    since = time.monotonic()
                    try:
                        while not self._halt.is_set() and not lost.is_set() and client.is_connected:
                            await asyncio.sleep(0.5)
                            if not self.want[kind]:
                                self.log(f"{kind}: disconnected (asked to)")
                                break
                            if kind == "hr" and self._ant_has_hr() and not self.both_hr:
                                self.log("hr: the ANT+ stick has the strap: Bluetooth disconnected")
                                break
                            # watchdog: connected but silent (both sensors notify at least ~1/s) -> reconnect
                            quiet = time.monotonic() - max(self.last_data[kind], since)
                            if quiet > self.SILENT_S:
                                self.log(f"{kind}: no data for {quiet:.0f} s while connected: reconnecting")
                                break
                    finally:
                        if kind == "hr" and not lost.is_set() and client.is_connected:
                            await self._stop_pmd(client)
                    if lost.is_set():
                        self.log(f"{kind}: disconnected")
            except Exception as e:
                if kind == "pedal":
                    self._held = None
                self.log(f"{kind}: {type(e).__name__}: {e}")
                self.status[kind] = f"connection lost ({type(e).__name__}), retrying…"
                await asyncio.sleep(2)
            if not self._halt.is_set() and self.want[kind]:
                self.state[kind] = "connecting"
                self.status[kind] = "connection lost, reconnecting…"


class ReplayWorker(threading.Thread):
    """Plays back an nRF log as if it were live (for testing the window)."""

    def __init__(self, q: queue.Queue, notes: list, speed: float, skip_s: float = 0) -> None:
        super().__init__(daemon=True)
        self.q, self.notes, self.speed, self.skip_s = q, notes, speed, skip_s
        self.status = {"pedal": "replaying log", "hr": ""}
        self.pedal_name = "Assioma (replay)"
        self._halt = threading.Event()
        self.log_file = None

    log = BleWorker.log

    def stop(self) -> None:
        self._halt.set()

    def run(self) -> None:
        if not self.notes:
            return
        # the first skip_s seconds of the log are due at once (in the past): a replay with history
        t0, w0 = self.notes[0].time, utcnow() - timedelta(seconds=self.skip_s / self.speed)
        for n in self.notes:
            due = w0 + (n.time - t0) / self.speed
            while not self._halt.is_set() and utcnow() < due:
                time.sleep(0.02)
            if self._halt.is_set():
                return
            self.q.put(dec.Notification(due, n.char, n.data))
        self.status["pedal"] = "replay finished"


# --------------------------------------------------------------------------- #
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from-nrf", metavar="LOG", help="convert an nRF Connect log to FIT")
    ap.add_argument("--date", help="date of the nRF log (YYYY-MM-DD)")
    ap.add_argument("--utc-offset", type=float, default=None, help="hours ahead of UTC (default: this computer's)")
    ap.add_argument("--rebuild", metavar="RAW", help="rebuild FIT from a recorder raw log")
    args = ap.parse_args()
    if args.from_nrf:
        if not args.date:
            ap.error("--from-nrf needs --date")
        offset = local_utc_offset_h() if args.utc_offset is None else args.utc_offset
        notes = dec.read_nrf_log(args.from_nrf, args.date, offset)
        base = os.path.splitext(args.from_nrf)[0]
    elif args.rebuild:
        notes = read_raw_log(args.rebuild)
        base = args.rebuild[:-8] if args.rebuild.endswith("_raw.csv") else os.path.splitext(args.rebuild)[0]
    else:
        ap.error("nothing to do: record with start_app.bat, or use --from-nrf / --rebuild")
    path, s, n = convert(notes, base)
    fmt = lambda v: "-" if v is None else f"{v:.0f}"
    print(f"{n} s ({n / 60:.0f} min), avg {fmt(s['avg_power'])} W, NP {fmt(s['np'])} W -> {path}")


if __name__ == "__main__":
    main()
