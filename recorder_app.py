#!/usr/bin/env python3
"""
Assioma Recorder as a desktop app. The window is a web page (web/) in a native window
(pywebview, the Edge WebView2 engine that ships with Windows); Python does the sensors, decoding,
HRV and FIT (Session and BleWorker from assioma_recorder, ANT+ heart rate from ant_hr).

    python recorder_app.py                             start (FTP, plan and strap are set in the app)
    python recorder_app.py --replay rides/ride_..._raw.csv --skip 20
                                                        test the window with a recorded ride
    python recorder_app.py --browser                   use the default browser instead of a window

Screens: home (sensors, settings, past rides) -> ride -> summary -> home; the sensors stay
connected between rides. Threads: the engine owns the rides (takes in notifications, ticks, runs
the buttons), a small HTTP server on 127.0.0.1 hands the page a snapshot, the new graph points and
ride summaries (ride_summary.py), and the main thread runs the window. The page only ever reads
state and sends button presses.

Read-only: it only subscribes to notifications and never writes to the pedals.
Needs: pip install bleak pywebview garmin-fit-sdk (and pyusb libusb for ANT+)
"""
from __future__ import annotations

import argparse
import json
import mimetypes
mimetypes.add_type("font/woff2", ".woff2")
import os
import re
import queue
import sys
import threading
import time
from collections import deque
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import assioma_decoder as dec
import cloud_sync as cs
import keep
import morning as mo
import polar_pmd as pmd
import plan as pl
import ride_summary as rs
import workout as wk
import checks
import readiness as rd
import training
import wbal
from assioma_recorder import recover as ar_recover, unfinished as ar_unfinished
from assioma_recorder import (EVENT, HERE, STRAP_FILES, BleWorker, ReplayWorker, Session, local_utc_offset_h,
                              read_raw_log, utcnow)

WEB = os.path.join(getattr(sys, "_MEIPASS", HERE), "web")     # the .exe unpacks the page to a temp folder
SETTINGS_FILE = os.path.join(HERE, "settings.json")
WORKOUT_DIR = os.path.join(HERE, "workouts")      # your own .zwo files (Zwift workout format)
PLAN_FILE = os.path.join(HERE, "plan.json")        # the training block (plan.py)
DYN_KEYS = ["rider_position"] + [f"{m}_{side}{unit}" for side in ("left", "right") for m, unit in
            [("power", "_w"), ("te", "_pct"), ("ps", "_pct"), ("pco", "_mm"), ("pp", "_start_deg"),
             ("pp", "_peak_start_deg"), ("pp", "_peak_end_deg"), ("pp", "_end_deg")]]
AUTOSTART_S = 3             # pedalling this long on the home screen starts a ride
QUICK_S = 60                # a ride shorter than this is a test: Stop puts it in rides/discarded/ without asking
REARM_S = 5                 # after a ride, auto-start waits until the pedals were still this long


def hms(n: int) -> str:
    return f"{n // 3600}:{n // 60 % 60:02d}:{n % 60:02d}"


PAGES = ("home", "plan", "workouts", "stats")   # the app's pages (top bar); plus "ride" and "summary"
# ftp, plan and strap are set on the home screen; theme, graph span and the power tile's 3 s mode are
# remembered from the page (its own browser storage is lost: the port changes every run)
SETTINGS_DEFAULTS = {"ftp": None, "max_hr": None, "vt1_hr": None, "room_temp": None, "room_rh": None,
                     "plan_min": 60, "hr": True, "trainer": False, "theme": "dark", "span": 300,
                     "power3s": False, "hr_lock": None, "autopause": True, "fuel_g_h": 60, "layout": "auto",
                     "ftp_history": None,     # [[from_day, ftp], ...]: each FTP change, so past rides keep theirs
                     "weight_kg": None, "weight_history": None,    # [[from_day, kg], ...]: W/kg on each day
                     "goal_wkg": 4.0, "goal_date": None,           # the goal (training.goal)
                     "power_match": True, "power_match_k": None}   # ERG by the pedals (Engine._power_match)
PREFS = {"theme": ("dark", "lcd"), "layout": ("auto", "intervals", "z2", "free"), "span": (300, 1800, 0), "power3s": (True, False)}


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
        # sensors wake with the pedals (a worker with a lifecycle, BleWorker): at launch only the
        # pedals are listened for, quietly; turning the cranks, a ride or the morning check
        # connects the strap and trainer (as set in settings), and IDLE_S with none of that lets
        # them go again. Looking at your fitness never touches a sensor.
        self.awake = False
        self._active = time.monotonic()      # last time something needed the sensors
        self._pedal_was = False              # the pedals were connected at the last tick
        self.worker = self._new_worker()
        self.session: Optional[Session] = None
        self.screen = "home"                 # a page (PAGES) | ride | summary
        self.back_to = "home"                # the page the summary's back button goes to
        self.summary_id: Optional[str] = None
        self.dialog = None                   # ride: None | "finish" | "discard"; home/summary: None | "delete"
        self.delete_id: Optional[str] = None # the ride the delete dialog is about
        self.rides_version = 0               # bumped when rides are saved or deleted: the page reloads its list
        self._vt1_auto: Optional[dict] = None   # your latest VT1 crossing (vt1.py), found in the background
        self._vt1_for = -1                   # the rides_version it was found for
        self._wp_auto: Optional[dict] = None    # W' from your newest ramp test (wbal.py), found alongside
        self._wb: Optional[tuple] = None     # live W'bal: (session, seconds counted, wbal.Balance)
        self._warm_auto: Optional[dict] = None  # your normal warm-up heart rate (checks.warmup_normal)
        self._warm: Optional[tuple] = None   # live warm-up readiness: (session, seconds when computed, result)
        self._a1_low_since = None            # when alpha 1 went under 0.75 (the Z2 guard)
        # workouts (workout.py) on the smart trainer (ftms.py via the worker)
        self.workout_id: Optional[str] = None   # picked on the home screen; None = free ride
        self.player: Optional[wk.Player] = None
        self.erg = True                      # off: the trainer rides flat (simulation) whatever the workout says
        self.workout_done = False
        # the training plan (plan.py): kept in plan.json (in memory only for a replay, a test)
        self.plan_path = os.path.join(os.path.dirname(settings_path), "plan.json") if settings_path else None
        self.plan = pl.load(self.plan_path) if self.plan_path else pl.empty()
        self.plan_version, self.plan_msg = 0, ""
        self._plan_day = None                # the day whose planned workout was last put up as the next ride
        # the morning check (morning.py): resting HR and HRV lying down, kept in morning.json
        self.morning_path = os.path.join(os.path.dirname(settings_path), "morning.json") if settings_path else None
        self.mornings = mo.load(self.morning_path)
        # the watch's nights (garmin.json: the server pulls it from Garmin, sync brings it here)
        self.garmin_path = os.path.join(os.path.dirname(settings_path), "garmin.json") if settings_path else None
        self.garmin = cs.load_json(self.garmin_path, {}) if self.garmin_path else {}
        self.mcheck: Optional[mo.Check] = None
        self.mresult: Optional[dict] = None
        self.morning_version = 0
        self.notice = ""                     # a one-off word for the page (a recovered ride)
        self.fuel_log: list = []             # [[ride s, grams]] eaten on this ride (_fuel)
        self.fuel_later: Optional[int] = None
        self.keep_awake = None               # keepawake.KeepAwake, set by main(): no sleep while recording
        self._autopaused, self._still_since = False, None   # auto-pause (_autopause)
        self.step_log: list = []             # [ride second, step, intensity] as the workout went
        # the VT1 ceiling (a toggle on the ride screen): ERG targets trimmed while you're above VT1
        self.ceiling = False
        self.cap, self._cap_t = 1.0, 0.0     # the share of the target allowed, when it last changed
        self.pm_k = float(self.settings.get("power_match_k") or 1.0)   # power match (_power_match)
        self._pm, self._pm_tgt, self._pm_last = [], None, 0.0
        self.trainer_log: list = []          # [ride s, trainer's W, W asked of it] every 5 s in ERG
        self._wo_meta: Optional[dict] = None
        self.error = ""
        self.pedal_since = None
        # auto-start (3 s of pedalling on the home screen) is armed at launch and again once the
        # pedals have been still for 5 s, so spinning on after a ride doesn't start the next one
        self.armed, self.quiet_since = True, None
        self.home_dec = dec.Decoder()        # home: just enough decoding for the sensor check
        # the strap's raw streams (polar_pmd), decoded live for the ride screen: the last 15 s of
        # ECG and accelerometer on the ride's clock, and the stream setup (features, commands,
        # answers) to put at the top of each new ride's raw log so it decodes on its own
        self.pmd_dec = pmd.Decoder()
        self.pmd_setup: list = []
        self.pmd_off: Optional[timedelta] = None      # strap clock -> ours (smallest arrival lag)
        self.strap = {"ecg": deque(maxlen=130 * 15), "acc": deque(maxlen=200 * 15)}
        self.strap_n = {"ecg": 0, "acc": 0}           # samples ever added, for the page's "since"
        self.strap_lock = threading.Lock()
        self.home_live: dict = {}            # name -> (value, time)
        self.quit = threading.Event()        # the window should close
        self.snap: dict = {}
        self.cmds: queue.Queue = queue.Queue()
        self._last_tick = 0.0
        self.sync: Optional[cs.Background] = None   # cloud sync (cloud_sync.py), when sync.json says where
        self._sync_key = None                # what a change of starts a sync round: versions and settings
        self._disk: dict = {}                # settings / plan / morning file -> mtime last looked at (_watch_files)
        self._watch_t = 0.0

    def _new_worker(self):
        w = self.make_worker(self.q, bool(self.settings.get("hr")))
        asleep = hasattr(w, "set_want") and not self.awake
        if asleep:                                   # before it starts: the strap (and ANT+) stay off
            w.want["hr"], w.state["hr"], w.status["hr"] = False, "off", ""
        if hasattr(w, "want_trainer"):
            w.want_trainer = bool(self.settings.get("trainer")) and not asleep
        if hasattr(w, "set_hr_lock"):
            w.set_hr_lock(self.settings.get("hr_lock"))
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
        if name == "sync_apply" and isinstance(arg, list):      # downloads from the sync thread
            cs.apply_here(arg)
            self._sync_reload([rel for rel, _tmp, _dest in arg])
        elif name == "sync_reload" and isinstance(arg, list):   # files changed under us (the server's web view)
            self._sync_reload(arg)
        elif name == "prefs" and isinstance(arg, dict):
            self._prefs(arg)
        elif name == "nav" and self.screen in PAGES + ("summary",) and arg in PAGES and self.dialog is None:
            self.screen, self.summary_id = arg, None
        elif self.screen in ("stats", "summary") and name in ("delete", "back", "delete_confirm"):
            if name == "delete":                   # from the history (arg: the ride) or the summary
                ride = arg if self.screen == "stats" else self.summary_id
                if rs.find_ride(self.out_dir, ride):
                    self.dialog, self.delete_id = "delete", ride
            elif name == "back":
                self.dialog, self.delete_id = None, None
            elif name == "delete_confirm" and self.dialog == "delete":
                self._delete_ride(self.delete_id)
                self.dialog, self.delete_id = None, None
                self.screen = self.back_to if self.screen == "summary" else self.screen
                self.summary_id = None
        elif self.screen in PAGES and name == "open" and rs.find_ride(self.out_dir, arg):
            self.back_to, self.screen, self.summary_id = self.screen, "summary", arg
        elif self.screen in PAGES:
            if name == "start" and self.dialog is None:
                self._start_ride(now)
            elif name == "settings" and isinstance(arg, dict):
                self._settings(arg)
            elif name == "connect" and isinstance(arg, dict) and arg.get("device") in ("pedals", "hr", "trainer"):
                self._connect(arg["device"], bool(arg.get("on")))
            elif name == "workout":
                self.workout_id = arg if arg in self.workouts() else None
            elif name == "sync_code" and isinstance(arg, str):
                self._sync_code(arg)
            elif name == "hr_forget":                    # a new strap: the next one to connect becomes yours
                self.settings["hr_lock"] = None
                if hasattr(self.worker, "set_hr_lock"):
                    self.worker.set_hr_lock(None)
                if self.settings_path:
                    save_settings(self.settings_path, self.settings)
            elif name == "morning_start" and self.mcheck is None:
                self._wake("morning check")
                if hasattr(self.worker, "set_want") and not self.worker.want.get("hr"):
                    self._connect("hr", True)              # the check needs the strap
                self.mcheck, self.mresult = mo.Check(now), None
                self.worker.log("morning check started")
            elif name == "morning_cancel":
                self.mcheck = None
            elif name.startswith("plan_"):
                self._plan_action(name, arg)
            elif name == "wo_save" and isinstance(arg, dict):
                self._save_workout(arg)
            elif name == "wo_delete" and isinstance(arg, str) and arg.startswith("zwo:") and "/" not in arg and "\\" not in arg:
                path = os.path.join(WORKOUT_DIR, arg[4:] + ".zwo")
                if os.path.isfile(path):
                    keep.set_aside(path, os.path.join(WORKOUT_DIR, "deleted"))   # off the list, kept
                    self.plan_version += 1
            elif name == "home":
                self.screen = "home"
        elif self.screen == "summary":
            if name == "home":                     # back where it was opened from
                self.screen, self.summary_id, self.dialog = self.back_to, None, None
            elif name == "show_file":
                self._show_file()
            elif name == "ride_meta" and isinstance(arg, dict) and rs.find_ride(self.out_dir, self.summary_id):
                upd = {}
                for key, lo, hi in (("temp_c", 5, 40), ("rh_pct", 5, 100), ("knee", 0, 3)):   # knee: 0-3, your score
                    v = arg.get(key, "keep")
                    if v is None or (isinstance(v, (int, float)) and not isinstance(v, bool) and lo <= v <= hi):
                        upd[key] = v
                if upd:
                    rs.save_meta(self.out_dir, self.summary_id, upd)
                    self.rides_version += 1
        elif self.screen == "ride" and s is not None:
            if name in ("pause", "resume", "toggle", "stop"):
                self._autopaused = False             # yours from here: auto-pause doesn't undo it
            if name == "pause":
                s.pause(now)
            elif name == "resume":
                self.dialog = None
                s.resume(now)
            elif name == "toggle" and self.dialog is None:
                {"recording": s.pause, "paused": s.resume}.get(s.state, lambda _n: None)(now)
            elif name == "stop" and len(s.seconds) < QUICK_S:   # a test ride: put aside, no questions
                self._drain()
                kept = s.discard()
                self.worker.log(f"ride under {QUICK_S} s stopped: kept in {kept}" if kept else "ride stopped: nothing recorded")
                self._leave_ride("home")
            elif name == "stop":
                s.pause(now)                       # the clock stops while you decide
                self.dialog = "finish"
            elif name == "back":
                self.dialog = "finish"
            elif name == "discard":
                self.dialog = "discard"
            elif name == "discard_confirm":
                self._drain()
                kept = s.discard()                     # moved to rides/discarded/, its meta with it
                self.worker.log(f"ride discarded: kept in {kept}" if kept else "ride discarded: nothing recorded")
                self._leave_ride("home")
            elif name == "fuel_done":
                f = self._fuel()
                if f:
                    self.fuel_log.append([len(s.seconds), f["grams"]])
                    self.fuel_later = None
                    self.worker.log(f"fuel: {f['grams']} g at {len(s.seconds) // 60} min")
                    self._meta(s)                          # kept at once: a crash mustn't lose it
            elif name == "fuel_later":
                self.fuel_later = len(s.seconds) + 5 * 60
            elif name == "save":
                self._save()
            elif name == "wo_match":                     # power match on/off, kept for the next ride too
                self.settings["power_match"] = not self.settings.get("power_match", True)
                self._pm, self._pm_tgt = [], None
                self.worker.log(f"power match: {'on' if self.settings['power_match'] else 'off'}")
                if self.settings_path:
                    save_settings(self.settings_path, self.settings)
            elif name == "wo_ceiling" and self.player is not None:
                self.ceiling, self.cap = not self.ceiling, 1.0
            elif name in ("wo_skip", "wo_nudge", "wo_erg", "wo_end") and self.player is not None:
                if name == "wo_skip":
                    self.player.skip(len(s.seconds))
                elif name == "wo_nudge" and arg in (-5, -1, 1, 5):
                    self.player.nudge(arg)
                elif name == "wo_erg":
                    self.erg = not self.erg
                elif name == "wo_end":
                    self.player, self.workout_done = None, True
        if name == "close":
            self.quit.set()

    # ---- cloud sync (cloud_sync.py)
    def sync_dir(self) -> Optional[str]:
        return os.path.dirname(self.settings_path) if self.settings_path else None

    def start_sync(self) -> None:
        """Sync with your server when this folder's sync.json says where (set from the home screen)."""
        folder = self.sync_dir()
        cfg = folder and cs.load_json(os.path.join(folder, "sync.json"), None)
        if not cfg or not cfg.get("url") or not cfg.get("key"):
            return
        busy = lambda: os.path.basename(self.session.base) if self.session is not None else None
        apply = lambda changes: self.command("sync_apply", changes, timeout=30)
        syncer = cs.Syncer(folder, self.out_dir, cs.Client(cfg["url"], cfg["key"]), apply, busy, self.worker.log)
        self.sync = cs.Background(syncer)
        self.sync.start()

    def stop_sync(self) -> None:
        if self.sync is not None:
            self.sync.stop()
            self.sync = None

    def _sync_code(self, code: str) -> None:
        """The home screen's sync code (<server>#<key>): saved to sync.json and sync starts; empty stops it."""
        folder = self.sync_dir()
        if not folder:
            return
        path = os.path.join(folder, "sync.json")
        if not code.strip():
            self.stop_sync()
            try:
                os.remove(path)
            except OSError:
                pass
            self.sync_msg = "Sync off."
            return
        cfg = cs.parse_code(code)
        if cfg is None:
            self.sync_msg = "That isn't a sync code: it looks like https://...#..."
            return
        cs.save_json(path, cfg)
        self.stop_sync()
        self.start_sync()
        self.sync_msg = ""

    def _sync_reload(self, rels: list) -> None:
        """Files another device changed are in place: read them again and let the page know."""
        if any(r.startswith("rides/") for r in rels):
            self.rides_version += 1
        if "settings.json" in rels and self.settings_path:
            new = load_settings(self.settings_path)
            for device in ("hr", "trainer"):          # your sensors: connect or let go like the switch does
                if new.get(device) != self.settings.get(device) and hasattr(self.worker, "set_want"):
                    self.worker.set_want({"hr": "hr", "trainer": "trainer"}[device], bool(new.get(device)))
            self.settings.clear()
            self.settings.update(new)
        if "plan.json" in rels and self.plan_path:
            self.plan = pl.load(self.plan_path)
            self.plan_version += 1
            self._plan_day = None
        if any(r.startswith("workouts/") for r in rels):
            self.plan_version += 1
        if "garmin.json" in rels and self.garmin_path:
            self.garmin = cs.load_json(self.garmin_path, {})
            self.morning_version += 1
        if ("morning.json" in rels or any(r.startswith("mornings/") for r in rels)) and self.morning_path:
            self.mornings = mo.load(self.morning_path)
            self.morning_version += 1
        self._sync_key = self._sync_state_key()       # these came from the server: nothing to send back

    def _watch_files(self, every: float = 1.0) -> None:
        """settings.json, plan.json and morning.json changed on disk by something else (an editor,
        a script, another copy of the app): read them again, before a click here saves the old copy
        over them. A file that reads the same as what's in memory was our own save."""
        t = time.monotonic()
        if t - self._watch_t < every:
            return
        self._watch_t = t
        changed = []
        for rel, path, load, now in (("settings.json", self.settings_path, load_settings, lambda: self.settings),
                                     ("plan.json", self.plan_path, pl.load, lambda: self.plan),
                                     ("morning.json", self.morning_path, mo.load, lambda: self.mornings)):
            if not path:
                continue
            try:
                m = os.stat(path).st_mtime_ns
            except OSError:
                continue
            if self._disk.get(path, m) == m:          # unchanged (or first look: what we loaded at start)
                self._disk[path] = m
                continue
            try:
                with open(path, encoding="utf-8") as fh:  # the loaders forgive a broken file (defaults,
                    raw = json.load(fh)                    # an empty plan): never take that for an edit
                if not isinstance(raw, list if rel == "morning.json" else dict) or (rel == "plan.json" and "days" not in raw):
                    raise ValueError(rel)
                if load(path) != now():
                    changed.append(rel)
                self._disk[path] = m
            except Exception:                        # half written: look again next time
                self._disk[path] = None
        if changed:
            key = self._sync_key
            self._sync_reload(changed)
            self._sync_key = key                      # changed here, not by the server: sync sends it on
            self.worker.log(f"reloaded {', '.join(changed)}: changed on disk")

    def _sync_state_key(self):
        return (self.rides_version, self.plan_version, self.morning_version, json.dumps(self.settings, sort_keys=True))

    def _sync_poke(self) -> None:
        """Something of yours changed (a ride saved or deleted, a morning check, the plan, a setting):
        a sync round soon."""
        key = self._sync_state_key()
        if key != self._sync_key:
            first = self._sync_key is None
            self._sync_key = key
            if self.sync is not None and not first:
                self.sync.poke()

    def _sync_view(self) -> dict:
        if self.sync is None:
            return {"on": False, "msg": getattr(self, "sync_msg", "")}
        return {"on": True, **self.sync.syncer.status, "msg": getattr(self, "sync_msg", "") or self.sync.syncer.status.get("msg", "")}

    # ---- the Z2 guard: where you are against your own VT1
    def vt1_ref(self) -> Optional[dict]:
        """VT1 heart rate to ride against: the setting if you gave one, else the latest ride whose alpha 1
        crossed 0.75 clearly (vt1.usable; {hr, lo, hi, day, source}). The ride list is read in the background."""
        self._find_refs()
        if self.settings.get("vt1_hr"):
            return {"hr": self.settings["vt1_hr"], "source": "setting"}
        return self._vt1_auto

    def w_prime_ref(self) -> Optional[dict]:
        """W' (J) from your newest ramp test, with CP = FTP: {w_prime, day, ride} (wbal.py)."""
        self._find_refs()
        return self._wp_auto

    def _find_refs(self) -> None:
        """Read the ride list in the background for the VT1 and W' references, once per rides_version."""
        if self._vt1_for != self.rides_version:
            self._vt1_for = self.rides_version

            def find(version=self.rides_version):
                found = wp = warm = None
                try:
                    rides = self.rides()                                                # newest first
                    warm = checks.warmup_normal([r.get("warmup") for r in rides if r.get("source") == "Assioma"])
                    for r in rides:
                        e = r.get("vt1_hr")
                        if found is None and e and e.get("ok"):           # a usable crossing (vt1.usable)
                            found = {"hr": e["value"], "lo": e.get("lo"), "hi": e.get("hi"), "source": "ride",
                                     "day": r["start_utc"][:10], "beats_pct": r.get("vt1_beats_pct")}
                        if wp is None and r.get("ramp_w_prime"):
                            wp = {"w_prime": r["ramp_w_prime"], "day": r["start_utc"][:10], "ride": r["id"]}
                except Exception as ex:                    # never stops the ride screen
                    self.worker.log(f"VT1 / W' reference: {type(ex).__name__}: {ex}")
                if version == self.rides_version:
                    self._vt1_auto, self._wp_auto, self._warm_auto = found, wp, warm
            threading.Thread(target=find, daemon=True).start()

    def _warmup_live(self, s: Session) -> Optional[dict]:
        """Readiness from this ride's warm-up so far (checks.warmup), against your normal; worked out
        every 10 s while the warm-up window fills, then kept."""
        self._find_refs()
        n = len(s.seconds)
        if n < checks.READY_FROM_S + checks.READY_MIN_S:
            return None
        if self._warm is None or self._warm[0] is not s or (self._warm[1] < checks.READY_TO_S and n - self._warm[1] >= 10):
            self._warm = (s, n, checks.warmup(s.seconds))
        w, normal = self._warm[2], self._warm_auto
        return w and {**w, "normal": normal and normal["hr"], "normal_a1": normal and normal["a1"]}

    def _wbal_live(self, s: Session) -> Optional[dict]:
        """The reserve now: W'bal over the recorded seconds so far, counted on as they come."""
        ftp, ref = self.settings.get("ftp"), self.w_prime_ref()
        if not ftp or not ref:
            return None
        if self._wb is None or self._wb[0] is not s or self._wb[2].cp != ftp or self._wb[2].w_prime != ref["w_prime"]:
            self._wb = (s, 0, wbal.Balance(ftp, ref["w_prime"]))
        _, n, b = self._wb
        for row in s.seconds[n:]:
            b.add(row.get("power_w"))
        self._wb = (s, len(s.seconds), b)
        return {"j": b.w, "pct": 100 * b.w / b.w_prime, "w_prime": b.w_prime, "cp": b.cp}

    def _guard(self, s: Session, now, alpha1, coverage) -> Optional[dict]:
        """Below / near / above VT1 now: 30-s heart rate against the reference, and alpha 1 under 0.75
        for 30 s or more (when enough beats arrive to trust it). Plus the ride's time above VT1."""
        ref = self.vt1_ref()
        if alpha1 is not None and alpha1 < 0.75 and (coverage or 100) >= 90:
            self._a1_low_since = self._a1_low_since or now
        else:
            self._a1_low_since = None
        a1_low_s = (now - self._a1_low_since).total_seconds() if self._a1_low_since else 0
        if not ref:
            return {"ref": None, "a1_low_s": a1_low_s} if a1_low_s else None
        t_now = (now - s.t0).total_seconds() if s.t0 else 0
        recent = [b for t, b in s.live_hr[-60:] if t >= t_now - 30 and b]
        hr30 = sum(recent) / len(recent) if recent else None
        scan = s.__dict__.setdefault("_vt1_scan", [0, 0])          # rows looked at, seconds above
        while scan[0] < len(s.seconds):
            h = s.seconds[scan[0]].get("heart_rate_bpm")
            scan[1] += bool(h and h > ref["hr"])
            scan[0] += 1
        state = None
        if hr30 is not None:
            state = "above" if hr30 > ref["hr"] + 1 or a1_low_s >= 30 else "near" if hr30 >= ref["hr"] - 4 else "below"
        elif a1_low_s >= 30:
            state = "above"
        return {"ref": ref, "hr30": hr30, "headroom": None if hr30 is None else ref["hr"] - hr30,
                "state": state, "a1_low_s": a1_low_s, "above_s": scan[1]}

    def morning_trend(self) -> dict:
        """The morning check and the watch's nights, read together (morning.trend)."""
        return mo.trend(self.mornings, garmin=self.garmin)

    def recover_rides(self) -> list:
        """Rides the app closed or crashed in the middle of (assioma_recorder.unfinished): finished
        from their raw logs now, with a word on the page. Run once at launch, in the background."""
        busy = self.session.base if self.session is not None else None
        done = []
        for base in ar_unfinished(self.out_dir, busy):
            try:
                res = ar_recover(base)
            except Exception as e:
                self.worker.log(f"recover {os.path.basename(base)}: {type(e).__name__}: {e}")
                continue
            if res:
                done.append(os.path.basename(base))
                self.worker.log(f"recovered unfinished ride: {os.path.basename(base)} ({res[2] // 60} min)")
        if done:
            self.rides_version += 1
            self.notice = (f"Recovered {len(done)} ride{'s' if len(done) > 1 else ''} the app closed in the middle of "
                           f"({', '.join(d[5:15] + ' ' + d[16:21].replace('-', ':') for d in done)}).")
        return done

    def rides(self) -> list[dict]:
        """The ride list, each ride scored at the FTP it was ridden at (ride_summary.ride_ftp)."""
        return rs.ride_list(self.out_dir, self.settings.get("ftp"), self.settings.get("ftp_history") or [])

    def w_prime_at(self, cp: Optional[float]) -> Optional[float]:
        """W' from your newest ramp test against a given CP (a past ride's FTP): wbal.ramp_w_prime."""
        ref = self.w_prime_ref()
        if not cp or not ref:
            return None
        if cp == self.settings.get("ftp"):
            return ref["w_prime"]
        path = rs.find_ride(self.out_dir, ref["ride"])
        return path and wbal.ramp_w_prime([r.get("power_w") for r in rs.load_ride(path)], cp)

    # ---- workouts on the trainer
    def workouts(self) -> dict:
        return {**wk.library(WORKOUT_DIR), **pl.plan_workouts(self.plan)}

    # ---- the plan
    def _plan_changed(self, msg: str = "") -> None:
        self.plan_version += 1
        self.plan_msg = msg
        self._plan_day = None                       # today's entry may have changed: put it up again
        if self.plan_path:
            try:
                pl.save(self.plan_path, self.plan)
            except OSError as e:
                self.plan_msg = f"Couldn't save the plan: {e}"

    def _plan_today(self) -> None:
        """Once a day (and after the plan changes) today's planned workout becomes the next ride;
        you can still pick another one after that."""
        today = date.today()
        if self._plan_day == today:
            return
        self._plan_day = today
        e = self.plan.get("days", {}).get(today.isoformat())
        if e is not None:
            self.workout_id = pl.day_id(self.plan, today, self.workouts())

    def _plan_action(self, name: str, arg) -> None:
        lib = self.workouts()
        if name == "plan_new" and isinstance(arg, dict):
            try:
                start = date.fromisoformat(arg.get("start", ""))
            except (TypeError, ValueError):
                return
            weeks = arg.get("weeks", 4)
            self.plan = pl.empty(start - timedelta(days=start.weekday()), weeks if weeks in (1, 2, 3, 4, 5, 6) else 4)
            self._plan_changed()
        elif name == "plan_set" and isinstance(arg, dict) and isinstance(arg.get("date"), str):
            try:
                date.fromisoformat(arg["date"])
                text = str(arg.get("text") or "").strip()
                if text:
                    self.plan["days"][arg["date"]] = pl.parse_entry(text, lib)
                else:
                    self.plan["days"].pop(arg["date"], None)
                self._plan_changed()
            except ValueError as e:
                self.plan_msg = str(e)
                self.plan_version += 1
        elif name == "plan_import" and isinstance(arg, dict):
            days, problems = pl.parse_import(str(arg.get("text") or ""), date.fromisoformat(self.plan["start"]), lib)
            self.plan["days"].update(days)
            msg = f"Imported {len(days)} day{'s' if len(days) != 1 else ''}."
            if problems:
                msg += " Not understood: " + "; ".join(f"line {n}: {p}" for n, p in problems[:6])
            self._plan_changed(msg)

    def _save_workout(self, arg: dict) -> None:
        """The builder: a workout from the shorthand, saved as a Zwift file in the workouts folder."""
        name = str(arg.get("name") or "").strip()[:60]
        try:
            steps = wk.parse_text(str(arg.get("text") or ""))
        except ValueError as e:
            self.plan_msg = f"Workout not saved: {e}"
            self.plan_version += 1
            return
        if not name:
            self.plan_msg = "Workout not saved: it needs a name."
            self.plan_version += 1
            return
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:50] or "workout"
        os.makedirs(WORKOUT_DIR, exist_ok=True)
        with open(os.path.join(WORKOUT_DIR, slug + ".zwo"), "w", encoding="utf-8", newline="\n") as fh:
            fh.write(wk.to_zwo(name, str(arg.get("description") or ""), steps))
        self.plan_msg = f"Saved {name}."
        self.plan_version += 1

    def _drive_trainer(self) -> None:
        """Tell the worker's trainer link what to hold: the workout's target in ERG; flat riding
        (simulation, grade 0) for free-ride steps, with ERG off, and after the workout. Without a
        workout the trainer is left as it is. The link does the talking (and survives failures)."""
        tr = getattr(self.worker, "trainer", None)
        if tr is None:
            return
        s = self.session
        if s is not None and self.player is not None:
            a = self.player.at(len(s.seconds))
            if a["done"]:
                self.player, self.workout_done = None, True
                self.worker.log("workout done")
                tr.want = ("sim", 0.0)
            elif not self.erg or a["target_w"] is None or s.state != "recording" or not self._pedalling(s):
                tr.want = ("sim", 0.0)           # (paused or not pedalling: no ERG wall to restart against)
            else:
                self._update_cap(s)
                target = a["target_w"] * self.cap
                pm = self.settings.get("power_match", True)
                if pm:
                    self._power_match(target, self._power_3s(utcnow()), self._trainer_w(), time.monotonic())
                tr.want = ("erg", target * (self.pm_k if pm else 1.0))
                n, tw = len(s.seconds), self._trainer_w()
                if tw is not None and (not self.trainer_log or n - self.trainer_log[-1][0] >= 5):
                    self.trainer_log.append([n, round(tw), round(tr.want[1])])   # both meters, kept with the ride
        elif tr.want is not None and tr.want[0] == "erg":
            tr.want = ("sim", 0.0)                       # never leave it holding a workout's watts

    @staticmethod
    def _pedalling(s) -> bool:
        """Whether ERG may hold the target: the pedals show power (over 20 W) within the last 3 s and
        have for at least 2 s (from the data, so a pause in between doesn't matter)."""
        if s.t0 is None or not s.live_power:
            return False
        now = (utcnow() - s.t0).total_seconds()
        start = None
        for t, p, _ in reversed(s.live_power[-200:]):        # back to where this stretch of pedalling began
            if p <= 20:
                break
            start = t
        last = s.live_power[-1][0]
        return start is not None and now - last <= 3 and last - start >= 2

    CAP_DOWN, CAP_UP, CAP_MIN = 0.03, 0.02, 0.70
    CAP_WAIT_DOWN, CAP_WAIT_UP = 25.0, 30.0   # s: heart rate answers a change in power in about 30 s

    def _update_cap(self, s: Session) -> None:
        """The VT1 ceiling: above VT1 (30-s heart rate over it, or alpha 1 under 0.75 for 30 s: the Z2
        guard's own test) the target is trimmed 3 %, then it waits 25 s for heart rate to answer
        before trimming again, never under 70 %; clearly under VT1 (3 bpm below, alpha 1 fine) it
        gives 2 % back every 30 s. Slow on purpose: a fast loop on heart rate overshoots and rebounds."""
        if not self.ceiling:
            self.cap = 1.0
            return
        now = utcnow()
        row = s.hrv.latest(now)
        g = self._guard(s, now, row[1] if row else None, s.hrv.coverage[-1] if s.hrv.coverage else None)
        if not g or not g.get("ref"):
            return                                  # no VT1 known: nothing to hold you under
        t, ref, hr30 = time.monotonic(), g["ref"]["hr"], g.get("hr30")
        over = g.get("state") == "above"
        clear = hr30 is not None and hr30 < ref - 3 and g.get("a1_low_s", 0) == 0
        if over and t - self._cap_t >= self.CAP_WAIT_DOWN and self.cap > self.CAP_MIN:
            self.cap, self._cap_t = max(self.CAP_MIN, round(self.cap - self.CAP_DOWN, 2)), t
            self.worker.log(f"VT1 ceiling: target at {self.cap:.0%}")
        elif clear and self.cap < 1.0 and t - self._cap_t >= self.CAP_WAIT_UP:
            self.cap, self._cap_t = min(1.0, round(self.cap + self.CAP_UP, 2)), t

    def _trainer_w(self) -> Optional[float]:
        tr = getattr(self.worker, "trainer", None)
        return tr.power_w if tr is not None and time.monotonic() - getattr(tr, "data_at", 0.0) < 3 else None

    # Power match (as Zwift, TrainerRoad and Wahoo do): ERG holds the target by the pedals, not by the
    # trainer's own reading. pm_k = trainer W / pedal W, learnt in steady ERG; the trainer is asked
    # for target * pm_k, so the pedals read the target. The ratio between the two meters doesn't
    # depend on what the trainer is asked, so the loop can't run away; it moves slowly anyway.
    PM_SETTLE_S = 15.0       # after a target change: both meters steady again
    PM_WIN_S = 60.0          # the window the ratio comes from
    PM_EVERY_S = 10.0        # how often the ratio is updated
    PM_GAIN = 0.5            # the share of the new ratio taken each update
    PM_DEAD = 0.01           # the ratio is only touched when a window disagrees by more than 1 %: settled = still
    PM_SANE = (0.80, 1.20)   # a reading's (or window's) ratio outside this: a dropout or a surge, not the meters
    PM_LIMITS = (0.85, 1.15)
    PM_MIN_W = 60.0
    PM_OFF = 0.20            # pedals further than this from the target: not holding it (stopped, surging)

    def _power_match(self, target: float, pedal_w: Optional[float], trainer_w: Optional[float], t: float) -> None:
        if self._pm_tgt is None or abs(target - self._pm_tgt[0]) > 2:
            self._pm_tgt, self._pm = (target, t), []         # a new target: wait for both to settle
            return
        steady = (pedal_w and trainer_w and pedal_w >= self.PM_MIN_W and abs(pedal_w - target) <= self.PM_OFF * target
                  and self.PM_SANE[0] <= trainer_w / pedal_w <= self.PM_SANE[1])
        if not steady:                                       # a stop, a drink, a surge: the trainer's reading lags
            self._pm_tgt, self._pm = (target, t), []         # the pedals there, so start over once steady again
            return
        if t - self._pm_tgt[1] < self.PM_SETTLE_S:
            return
        self._pm.append((t, pedal_w, trainer_w))
        self._pm = [x for x in self._pm if t - x[0] <= self.PM_WIN_S]
        if t - self._pm_last < self.PM_EVERY_S or len(self._pm) < 20 or t - self._pm[0][0] < 0.6 * self.PM_WIN_S:
            return
        self._pm_last = t
        k_new = sum(x[2] for x in self._pm) / sum(x[1] for x in self._pm)
        if not self.PM_SANE[0] <= k_new <= self.PM_SANE[1]:
            return
        if abs(k_new - self.pm_k) <= self.PM_DEAD:
            return                                           # the pedals swing; within 1 % is the same ratio
        lo, hi = self.PM_LIMITS
        k = round(min(hi, max(lo, self.pm_k + self.PM_GAIN * (k_new - self.pm_k))), 4)
        if abs(k - self.pm_k) >= 0.0005:
            self.pm_k = k
            if abs(k - (self.settings.get("power_match_k") or 1.0)) >= 0.005:     # remembered for the next ride
                self.settings["power_match_k"] = k
                if self.settings_path:
                    save_settings(self.settings_path, self.settings)
                self.worker.log(f"power match: trainer asked for {k:.3f} × the target")

    def _workout_view(self, s) -> Optional[dict]:
        p = self.player
        if p is None:
            return {"done": True, "name": None} if self.workout_done else None
        a = p.at(len(s.seconds))
        nxt = a.get("next")
        tr = getattr(self.worker, "trainer", None)
        return {"name": p.w["name"], "steps": [[st["dur"], st["lo"], st["hi"]] for st in p.w["steps"]],
                "total": p.total, "t": a["t"], "index": a["index"], "left": a.get("left"),
                "label": (a.get("step") or {}).get("label", ""), "target": a["target_w"],
                "next": nxt and {"dur": nxt["dur"], "lo": nxt["lo"], "hi": nxt["hi"], "label": nxt["label"]},
                "intensity": p.intensity, "erg": self.erg, "ftp": p.ftp, "ceiling": self.ceiling, "cap": self.cap,
                "pm": self.pm_k if self.settings.get("power_match", True) else None,
                "trainer": tr and {"controlled": tr.controlled, "error": tr.error, "power": tr.power_w}}

    def _meta(self, s: Session) -> None:
        """The ride's sidecar (ride_summary.META): room conditions and the workout as ridden."""
        m = {"temp_c": self.settings.get("room_temp"), "rh_pct": self.settings.get("room_rh")}
        view = getattr(self.worker, "view", None)                  # which strap, and how: HRV trusts some more than others
        if view:
            status, link = view("hr")
            if status.startswith("connected"):
                m["hr_strap"] = {"name": status.split(": ", 1)[-1], "link": link}
        if self._wo_meta:
            m["workout"] = {**self._wo_meta, "log": self.step_log}
            if self.settings.get("power_match", True) and self.pm_k != 1.0:
                m["power_match_k"] = self.pm_k                     # ERG was asked for target × this
        if self.trainer_log:
            m["trainer"] = self.trainer_log                        # [ride s, trainer W, asked W]: the second meter
        if self.fuel_log:
            m["fuel"] = [list(x) for x in self.fuel_log]             # [[ride s, grams]]: what you ate, when
        try:
            rs.save_meta(self.out_dir, os.path.basename(s.base), m)
        except OSError as e:
            self.worker.log(f"meta: {e}")

    def _log_step(self) -> None:
        """Note when each workout step starts (ride second, step, intensity): the Fitness check and
        the step-by-step targets in the summary are read from it."""
        s, p = self.session, self.player
        if s is None or p is None or s.state != "recording":
            return
        a = p.at(len(s.seconds))
        if a["done"]:
            return
        if not self.step_log or self.step_log[-1][1] != a["index"] or self.step_log[-1][2] != p.intensity:
            self.step_log.append([len(s.seconds), a["index"], p.intensity])

    def _start_ride(self, now) -> None:
        self._wake("ride")
        self.notice = ""
        self.fuel_log, self.fuel_later = [], None
        self._autopaused, self._still_since = False, None
        self._a1_low_since = None
        self.step_log, self._wo_meta = [], None
        self.trainer_log, self._pm, self._pm_tgt = [], [], None
        self.cap, self._cap_t = 1.0, 0.0                 # (the ceiling toggle itself stays as you left it)
        self.player, self.workout_done, self.erg = None, False, True
        w = self.workouts().get(self.workout_id) if self.workout_id else None
        if w is not None:
            if self.settings.get("ftp"):
                self.player = wk.Player(w, self.settings["ftp"])
                self._wo_meta = {"id": self.workout_id, "name": w["name"], "ftp": self.settings["ftp"],
                                 "steps": [[st["dur"], st["lo"], st["hi"], st["label"]] for st in w["steps"]]}
            else:
                self.error = "Set your FTP on the home screen to ride a workout."
        self.session = Session(self.out_dir)
        self.session.live_target = []               # (s since t0, target W): the chart's target line
        for n in self.pmd_setup:                     # how the strap's streams were set up, from before
            self.session.log_raw(n)
        self.session.pedal_name = self.worker.pedal_name or "Assioma"
        self.session.start(now)
        self._meta(self.session)
        self.worker.log(f"ride started: {os.path.basename(self.session.base)}")
        self.screen, self.dialog, self.pedal_since = "ride", None, None

    def _leave_ride(self, screen: str, summary_id: Optional[str] = None) -> None:
        self.session, self.dialog = None, None
        self.player, self.workout_done = None, False
        self._drive_trainer()                            # off the workout's watts
        self.screen, self.summary_id, self.back_to = screen, summary_id, "home"
        self.home_dec, self.home_live = dec.Decoder(), {}
        self.armed, self.quiet_since, self.pedal_since = False, None, None

    def _save(self) -> None:
        s = self.session
        self._drain()
        self._meta(s)                                    # with the workout's step log
        result = s.finish()
        if not result:                             # nothing recorded: its meta goes aside with its logs
            keep.set_aside(rs.meta_path(self.out_dir, os.path.basename(s.base)), os.path.join(self.out_dir, s.DISCARDED))
            self._leave_ride("home")
            return
        ride_id = os.path.basename(s.base)
        self.worker.log(f"ride saved: {ride_id}")
        self.rides_version += 1
        try:                                       # the ride list's entry, ready before it's asked for
            info = rs.short(rs.summarize(rs.load(s.base + rs.SUFFIX), self.settings.get("ftp"), None,
                                         rs.load_meta(self.out_dir, ride_id)))
            with open(rs.cache_path(self.out_dir, ride_id), "w") as fh:
                json.dump(info, fh)
        except Exception as e:
            self.worker.log(f"summary: {type(e).__name__}: {e}")
        self._leave_ride("summary", ride_id)

    def _delete_ride(self, ride_id: Optional[str]) -> None:
        """Out of your rides, never off the disk: every file of the ride (the recorder's raw log,
        per-second CSV and FIT, or another app's FIT file) moves to rides/deleted/ (keep.py). Only
        the cached summary, which is worked out again from them, goes."""
        path = rs.find_ride(self.out_dir, ride_id)
        if path is None:
            return
        base = os.path.join(self.out_dir, ride_id)
        exts = (("_raw.csv", rs.SUFFIX, ".fit", "_summary.json", "_log.txt") + STRAP_FILES if path.endswith(rs.SUFFIX)
                else (".fit",)) + (rs.META,)
        keep.set_aside_all([base + e for e in exts], os.path.join(self.out_dir, "deleted"))
        try:
            os.remove(rs.cache_path(self.out_dir, ride_id))
        except OSError:
            pass
        self.worker.log(f"ride deleted: {ride_id} (kept in rides/deleted)")
        self.rides_version += 1

    def _show_file(self) -> None:
        path = os.path.join(self.out_dir, f"{self.summary_id}.fit")          # ours, or another app's file
        if self.summary_id and rs.find_ride(self.out_dir, self.summary_id) and os.path.exists(path)                 and sys.platform == "win32":
            import subprocess
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])

    def _settings(self, new: dict) -> None:
        ftp, plan, hr = new.get("ftp", self.settings.get("ftp")), new.get("plan_min"), new.get("hr")
        max_hr = new.get("max_hr", self.settings.get("max_hr"))
        vt1_hr = new.get("vt1_hr", self.settings.get("vt1_hr"))     # None: from your rides (vt1.py)
        if vt1_hr is None or (isinstance(vt1_hr, (int, float)) and 90 <= vt1_hr <= 210):
            self.settings["vt1_hr"] = vt1_hr
        if ftp is None or (isinstance(ftp, (int, float)) and 50 <= ftp <= 600):
            old = self.settings.get("ftp")
            if ftp and ftp != old:                  # remembered by day: past rides keep the FTP they were ridden at
                today = date.today().isoformat()
                hist = [h for h in (self.settings.get("ftp_history") or []) if h[0] != today]
                if not hist and old:
                    hist = [["0001-01-01", old]]   # everything before the first change
                self.settings["ftp_history"] = sorted(hist + [[today, ftp]])
            self.settings["ftp"] = ftp
        if max_hr is None or (isinstance(max_hr, (int, float)) and 120 <= max_hr <= 230):
            self.settings["max_hr"] = max_hr
        kg = new.get("weight_kg")
        if isinstance(kg, (int, float)) and 35 <= kg <= 150 and kg != self.settings.get("weight_kg"):
            today = date.today().isoformat()            # by day, like the FTP: W/kg on each day uses that day's
            hist = [h for h in (self.settings.get("weight_history") or []) if h[0] != today]
            self.settings["weight_history"] = sorted(hist + [[today, kg]])
            self.settings["weight_kg"] = kg
        g = new.get("goal_wkg")
        if isinstance(g, (int, float)) and 1.5 <= g <= 7:
            self.settings["goal_wkg"] = g
        if "goal_date" in new:
            try:
                self.settings["goal_date"] = date.fromisoformat(new["goal_date"]).isoformat() if new["goal_date"] else None
            except (TypeError, ValueError):
                pass
        for key, lo, hi in (("room_temp", 5, 40), ("room_rh", 5, 100)):     # the room, from your hygrometer
            if key in new and (new[key] is None or (isinstance(new[key], (int, float)) and lo <= new[key] <= hi)):
                self.settings[key] = new[key]
        if isinstance(plan, (int, float)) and 10 <= plan <= 600:
            self.settings["plan_min"] = plan
        trainer = new.get("trainer")
        reconnect = isinstance(trainer, bool) and trainer != self.settings.get("trainer")
        if reconnect:
            self.settings["trainer"] = trainer
        if isinstance(hr, bool) and hr != self.settings.get("hr"):
            self.settings["hr"] = hr
            reconnect = True
        if reconnect:
            self.worker.stop()                     # reconnect with or without the strap
            self.worker.join(timeout=6)
            self.worker = self._new_worker()
        if self.settings_path:
            save_settings(self.settings_path, self.settings)

    IDLE_S = 600             # awake with no ride, no morning check and no pedalling this long: let go

    def _wake(self, why: str) -> None:
        """Connect the strap and trainer you use (settings hr / trainer), searching quietly."""
        self._active = time.monotonic()
        w = self.worker
        if self.awake or not hasattr(w, "set_want"):
            return
        self.awake = True
        for dev in ("hr", "trainer"):
            if self.settings.get(dev) and not w.want.get(dev):
                w.set_want(dev, True)
                w.attempt[dev] = None                  # no 20-s "couldn't find it": it's not a click
                w.state[dev] = "searching"
        w.log(f"sensors: awake ({why})")

    def _sleep(self) -> None:
        """Let the strap and trainer go; the pedals are listened for again."""
        w = self.worker
        self.awake = False
        for dev in ("hr", "trainer"):
            if w.want.get(dev):
                w.set_want(dev, False)
        w.log(f"sensors: let go after {self.IDLE_S // 60} min idle")

    def _sensor_tick(self, now) -> None:
        w = self.worker
        if not hasattr(w, "set_want"):
            return
        pedal = w.state.get("pedal") == "connected"
        woke, self._pedal_was = pedal and not self._pedal_was, pedal
        busy = self.session is not None or self.mcheck is not None
        if busy or (pedal and self._home_value("power", now, 3)):
            self._wake("ride" if self.session is not None else "morning check" if self.mcheck is not None else "pedalling")
        elif woke:
            self._wake("pedals")
        elif self.awake and time.monotonic() - self._active > self.IDLE_S:
            self._sleep()

    def _connect(self, device: str, on: bool) -> None:
        """The Connections pane: connect (an attempt that says when it can't find the sensor) or let
        go, without touching the other sensors. The strap and trainer choice is remembered."""
        kind = {"pedals": "pedal"}.get(device, device)
        if on:
            self._wake("click")
        if hasattr(self.worker, "set_want"):
            self.worker.set_want(kind, on)
            if device in ("hr", "trainer") and self.settings.get(device) != on:
                self.settings[device] = on
                if self.settings_path:
                    save_settings(self.settings_path, self.settings)
        elif device in ("hr", "trainer"):
            self._settings({device: on})               # a worker without a lifecycle: reconnect it

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
                if n.char in pmd.CHARS:
                    self._strap_feed(n)
                    if self.session is not None:
                        self.session.log_raw(n)     # the ride's raw log keeps every frame
                elif self.session is not None:
                    self.session.add(n)
                else:
                    self._home_feed(n)
        except queue.Empty:
            pass

    def _strap_feed(self, n) -> None:
        if n.char == pmd.FEAT_CHAR:                  # a new connection: its setup starts here
            self.pmd_setup, self.pmd_off = [n], None
        elif n.char in (pmd.CMD_CHAR, pmd.CP_CHAR) and len(self.pmd_setup) < 64:
            self.pmd_setup.append(n)
        for kind, samples in self.pmd_dec.feed(n.char, n.time, n.data):
            name = {pmd.ECG: "ecg", pmd.ACC: "acc"}.get(kind)
            if not name or not samples:
                continue
            lag = n.time - pmd.strap_time(samples[-1][0]).replace(tzinfo=None)
            if self.pmd_off is None or lag < self.pmd_off:
                self.pmd_off = lag
            with self.strap_lock:
                for ns, vals in samples:
                    self.strap[name].append((pmd.strap_time(ns).replace(tzinfo=None) + self.pmd_off, vals))
                self.strap_n[name] += len(samples)
            if name == "ecg" and self.mcheck is not None:      # the morning check judges odd beats by it
                self.mcheck.add_ecg([(pmd.strap_time(ns).replace(tzinfo=None) + self.pmd_off, vals[0])
                                     for ns, vals in samples])

    def strap_since(self, name: str, k: int, t0) -> tuple:
        """(total, [[s since t0, values...]]) of the samples after the page's count k."""
        with self.strap_lock:
            dq, total = self.strap[name], self.strap_n[name]
            first = total - len(dq)
            start = first if k > total or k < first else k
            items = list(dq)[start - first:]
        return total, [[round((t - t0).total_seconds(), 3), *v] for t, v in items]

    def _home_feed(self, n) -> None:
        """Home: the latest power and heart rate, for the sensor check and auto-start."""
        for r in self.home_dec.feed(n):
            if r.get("type") == dec.CPM_CHAR:
                self.home_live["power"] = (r["cpm_power_w"], r["time"])
            elif r.get("type") == dec.HR_CHAR:
                self.home_live["hr"] = (r["heart_rate_bpm"], r["time"])
                if self.mcheck is not None:
                    self.mcheck.feed(r["time"], r.get("heart_rate_bpm"), r.get("rr_ms") or [])

    def _home_value(self, name: str, now, max_age: float = 5):
        v = self.home_live.get(name)
        return v[0] if v and (now - v[1]).total_seconds() < max_age else None

    def run(self) -> None:
        while not self.quit.is_set():
            try:
                self._drain()
                self._watch_files()
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
            time.sleep(0.05)                       # a new beat reaches the snapshot within 50 ms

    def _morning_tick(self, now) -> None:
        c = self.mcheck
        if c is None:
            return
        quiet = (now - (c.last_data or c.started)).total_seconds()
        if c.phase(now) == "done" or quiet > 20:
            res = c.result(now) if quiet <= 20 else {"ok": False, "why": "the strap went quiet: is it on and connected?"}
            self.mcheck, self.mresult = None, res and {k: v for k, v in res.items() if k != "_ecg"}
            if res.get("ok"):
                self.mornings = mo.add(self.mornings, res)
                try:
                    mo.save(self.morning_path, self.mornings)
                    mo.save_ecg(self.morning_path, res["date"], res.get("_ecg"))
                except OSError as e:
                    self.worker.log(f"morning: {e}")
            self.morning_version += 1
            self.worker.log(f"morning check: {res}")

    def _tick(self) -> None:
        now = utcnow()
        lock = getattr(self.worker, "hr_lock", None)
        if lock and lock != self.settings.get("hr_lock"):          # the worker learnt your strap: keep it
            self.settings["hr_lock"] = dict(lock)
            if self.settings_path:
                save_settings(self.settings_path, self.settings)
        self._morning_tick(now)
        self._sensor_tick(now)
        if self.keep_awake is not None:              # a ride or a morning check: the computer stays awake
            (self.keep_awake.hold if self.session is not None or self.mcheck is not None else self.keep_awake.release)()
        self._sync_poke()
        if self.screen in PAGES:
            self._plan_today()
        self._log_step()
        self._drive_trainer()
        if self.session is not None:
            self._autopause(now)
            self._log_target(now)
            self.session.tick(now)
            if self.worker.pedal_name:
                self.session.pedal_name = self.worker.pedal_name
        elif self.screen in PAGES and self.dialog is None and self.mcheck is None:   # pedalling 3 s on a page starts a ride
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

    def _log_target(self, now) -> None:
        """The workout's target each second, on the graphs' clock (s since the first notification):
        the power chart draws it as a stepped line. What the pedals should read (intensity, VT1 cap
        included; power match asks the trainer for its own equivalent); None for a free-ride step or no workout."""
        s = self.session
        lt = getattr(s, "live_target", None)
        if lt is None or s.t0 is None or s.state != "recording":
            return
        t = int((now - s.t0).total_seconds())
        if lt and lt[-1][0] >= t:
            return
        tgt = None
        if self.player is not None:
            a = self.player.at(len(s.seconds))
            if not a.get("done") and a.get("target_w") is not None:
                tgt = a["target_w"] * (self.cap if self.ceiling else 1.0)
        if tgt is not None or (lt and lt[-1][1] is not None):
            lt.append((t, tgt))

    FUEL_FROM_S, FUEL_EVERY_S, FUEL_LONG_S, FUEL_FREE_S = 45 * 60, 20 * 60, 90 * 60, 75 * 60

    def _fuel(self) -> Optional[dict]:
        """Fuelling on long rides: a workout of 90 min or more, or a free ride past 75 min. From
        45 min a quiet prompt every 20 min for a share of your hourly carbs (settings fuel_g_h,
        60 g/h = 20 g a prompt); Done logs it (kept with the ride: what you ate, when), Later asks
        again in 5 min. None when the ride is too short for it."""
        s = self.session
        if s is None or s.state == "ready":
            return None
        t = len(s.seconds)
        long = (self.player is not None and self.player.total >= self.FUEL_LONG_S) or t >= self.FUEL_FREE_S \
            or bool(self.fuel_log)
        if not long:
            return None
        g = round((self.settings.get("fuel_g_h") or 60) * self.FUEL_EVERY_S / 3600)
        due_at = self.fuel_log[-1][0] + self.FUEL_EVERY_S if self.fuel_log else self.FUEL_FROM_S
        if self.fuel_later is not None and self.fuel_later > due_at:            # "later": 5 min on
            due_at = self.fuel_later
        eaten = sum(x[1] for x in self.fuel_log)
        return {"due": t >= due_at, "in_s": max(0, due_at - t), "grams": g, "eaten": eaten,
                "g_h": round(eaten / (t / 3600)) if t >= 1800 else None}

    AUTOPAUSE_S = 20         # no pedalling this long while recording: paused for you (you mostly pause yourself)

    def _autopause(self, now) -> None:
        """The fallback for a pause you forgot: AUTOPAUSE_S without a pedal stroke pauses the ride,
        the first strokes after it resume it. Never undoes a pause of yours, never acts while a
        dialog is open, off with settings autopause = false. Nothing is lost: the raw log keeps
        every notification, paused or not."""
        s = self.session
        if not self.settings.get("autopause", True) or self.dialog is not None or s.t0 is None:
            self._still_since = None
            return
        p = self._power_3s(now)
        moving = bool(p and p > 0)
        if s.state == "recording":
            if moving:
                self._still_since = None
            else:
                self._still_since = self._still_since or now
                if (now - self._still_since).total_seconds() >= self.AUTOPAUSE_S:
                    s.pause(now)
                    self._autopaused, self._still_since = True, None
                    self.worker.log(f"auto-paused after {self.AUTOPAUSE_S} s without pedalling")
        elif s.state == "paused" and self._autopaused and moving:
            s.resume(now)
            self._autopaused = False
            self.worker.log("auto-resumed: pedalling again")

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

    ALERT_GRACE_S = 5        # a sensor gone this long during a ride: the red alert (a blip isn't one)

    def _alerts(self, s, now, devices, strap) -> list:
        """The ride screen's red flashing alert: a sensor you use that isn't connected (after
        ALERT_GRACE_S), the strap connected but silent, or pedalling while you paused the ride."""
        out = []
        if s.state not in ("recording", "paused"):
            self._lost = {}
            return out
        lost = getattr(self, "_lost", {})
        if devices:
            used = (("pedals", "Pedals", True), ("hr", "Strap", self.settings.get("hr")),
                    ("trainer", "Trainer", self.settings.get("trainer")))
            for key, label, on in used:
                if on and (devices.get(key) or {}).get("state") != "connected":
                    lost.setdefault(key, now)
                    if (now - lost[key]).total_seconds() >= self.ALERT_GRACE_S:
                        out.append(f"{label} not connected")
                else:
                    lost.pop(key, None)
        self._lost = lost
        if strap and strap.get("warn", "").startswith("no data") and "Strap not connected" not in out:
            out.append(f"Strap: {strap['warn']}")
        if s.state == "paused" and not self._autopaused and (self._power_3s(now) or 0) > 0:
            out.append("Paused, but you're pedalling: press Resume")
        return out

    def _snapshot(self) -> dict:
        w, now = self.worker, utcnow()
        view = getattr(w, "view", lambda k: (w.status.get(k, ""), ""))   # (status, "ANT+" / "Bluetooth")
        (pedal, pedal_link), (hr, hr_link) = view("pedal"), view("hr")
        trainer, trainer_link = view("trainer")
        sensor = getattr(w, "sensor", None)            # the lifecycle (BleWorker): {state, status, link, note}
        if sensor:                                     # the strap's name and "ANT+ & BT" when both links record
            x = sensor("hr")
            hr, hr_link = x["status"] or hr, x["link"] or hr_link
        base = {
            "screen": self.screen, "summary_id": self.summary_id, "back_to": self.back_to,
            "settings": dict(self.settings),
            "sensors": [{"label": "Pedals", "status": pedal, "link": pedal_link}]
                       + ([{"label": "Strap", "status": hr, "link": hr_link}] if hr else [])
                       + ([{"label": "Trainer", "status": trainer, "link": trainer_link}] if trainer else []),
            "devices": {k: sensor(kind) for k, kind in (("pedals", "pedal"), ("hr", "hr"), ("trainer", "trainer"))}
                       if sensor else None,
            "awake": self.awake or not hasattr(w, "set_want"),     # False: waiting for the pedals (_wake)
            "error": self.error, "notice": self.notice, "dialog": self.dialog, "rides_version": self.rides_version,
            # what the pages' numbers depend on: the rides, the day (form, "this week" roll over at
            # midnight with the app open) and the FTP (every TSS): a change reloads them
            "data_key": f"{self.rides_version}|{date.today().isoformat()}|{self.settings.get('ftp')}",
            "plan_version": self.plan_version, "plan_msg": self.plan_msg,
            "morning_version": self.morning_version, "morning_result": self.mresult,
            "morning": None if self.mcheck is None else {
                "phase": self.mcheck.phase(utcnow()), "left": self.mcheck.left_s(utcnow()),
                "beats": sum(len(x[1]) for x in self.mcheck.rr), "hr": self._home_value("hr", utcnow())},
            "config": {"ftp": self.settings.get("ftp"), "plan_min": self.settings.get("plan_min")},
            "sync": self._sync_view(),
        }
        s = self.session
        if s is None:
            base["vt1"] = self.vt1_ref()
            base["workout_id"] = self.workout_id
            base["home"] = {"power_w": self._home_value("power", now), "hr": self._home_value("hr", now),
                            "pedalling_s": (now - self.pedal_since).total_seconds() if self.pedal_since else None,
                            "armed": self.armed, "trainer_w": self._trainer_w()}
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
        ride = {"state": s.state, "clock": hms(len(s.seconds)), "elapsed": None, "autopaused": self._autopaused,
                "fuel": self._fuel()}
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
            "hrv": h, "strap": strap, "ride": ride, "alerts": self._alerts(s, now, base.get("devices"), strap),
            "guard": self._guard(s, now, h.get("alpha1"), h.get("cov")) if s.state != "finished" else None,
            "wbal": self._wbal_live(s),
            "warmup": self._warmup_live(s),
            "workout": self._workout_view(s),
            "beats": [[round((b.time - t0).total_seconds(), 3), b.artifact] for b in beats[-60:]
                      if (now - b.time).total_seconds() < 5.5] if t0 else [],      # the HR tile's strip
            "battery": s.battery,
            "dialog": self.dialog,
        }


def today_readiness(engine: "Engine", rides: list, pmc: dict) -> dict:
    """readiness.today with the morning trend and your normal warm-up from the rides before today."""
    today = date.today()
    earlier = [r for r in rides if r.get("source") == "Assioma" and training.local_day(r["start_utc"]) < today]
    return rd.today(engine.morning_trend(), rides, pmc, checks.warmup_normal([r.get("warmup") for r in earlier]), today)


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
    return ThreadingHTTPServer(("127.0.0.1", 0), make_handler(engine, app_mode))


def make_handler(engine: Engine, app_mode: dict):
    """The request handler (also the web view's on the server, sync_server.py)."""

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
                self._json(engine.rides())
            elif u.path == "/api/workouts":
                ftp = engine.settings.get("ftp")
                self._json([{"id": k, "name": w["name"], "description": w.get("description", ""),
                             "total": wk.total_s(w), "file": bool(w.get("file")),
                             "steps": [[st["dur"], st["lo"], st["hi"], st["label"]] for st in w["steps"]],
                             "estimate": wk.estimate(w, ftp) if ftp else None}
                            for k, w in engine.workouts().items()])
            elif u.path == "/api/parse":                     # the builder's live preview
                try:
                    steps = wk.parse_text(q.get("text", [""])[0])
                    w = {"steps": steps}
                    ftp = engine.settings.get("ftp")
                    self._json({"steps": [[st["dur"], st["lo"], st["hi"], st["label"]] for st in steps],
                                "total": wk.total_s(w), "estimate": wk.estimate(w, ftp) if ftp else None,
                                "split": pl.zone_split(w)})
                except ValueError as e:
                    self._json({"error": str(e)})
            elif u.path == "/api/workout_text":              # a workout as shorthand, to edit a copy
                w = engine.workouts().get(q.get("id", [""])[0])
                self._json({"name": w["name"], "text": wk.to_text(w["steps"])} if w else {"error": "no such workout"})
            elif u.path == "/api/morning":
                self._json(engine.morning_trend())
            elif u.path == "/api/readiness":
                rides = engine.rides()
                self._json(today_readiness(engine, rides, training.pmc(rides)))
            elif u.path == "/api/plan":
                rides = engine.rides()
                over = pl.overview(engine.plan, engine.workouts(), rides, engine.settings.get("ftp"), date.today())
                prog = training.progress(rides)
                ready = today_readiness(engine, rides, prog["pmc"])
                over["readiness"] = ready
                if ready["level"] != "go":                   # flag the next hard day, today or tomorrow
                    soon = (date.today() + timedelta(days=1)).isoformat()
                    for d in over["days"]:
                        if date.today().isoformat() <= d["day"] <= soon and d["status"] in ("today", "upcoming") and rd.is_hard(d):
                            d["warn"] = ("Your body says " + ready["level"] + " today: swap this for Z2?") if d["day"] == date.today().isoformat()                                 else "Check how you feel tomorrow before this one."
                            break
                self._json({**over, "status_text": pl.status_text(prog, rides, engine.settings, over, engine.morning_trend()),
                            "pmc_now": prog["pmc"]})
            elif u.path == "/api/progress":
                prog = training.progress(engine.rides())
                prog["goal"] = training.goal(engine.settings, prog["eftp"], date.today())
                self._json(prog)
            elif u.path == "/api/ride":
                rid = q.get("id", [""])[0]
                rides = engine.rides()
                ftp = next((r.get("ftp") for r in rides if r["id"] == rid), None) or engine.settings.get("ftp")
                s = rs.summarize_file(engine.out_dir, rid, ftp, engine.settings.get("max_hr"), engine.w_prime_at(ftp))
                if s is not None:                            # your normal warm-up from the rides before this one
                    s["warmup_normal"] = checks.warmup_normal([r.get("warmup") for r in rides if r.get("source") == "Assioma"
                                                              and r["start_utc"] < s["start_utc"] and r["id"] != rid])
                if s is None:
                    self._send(404, b"no such ride", "text/plain")
                    return
                s.pop("fit_path", None)
                self._json(s)
            elif u.path == "/api/state":
                self._json(state(engine, app_mode, *(int(q.get(k, ["0"])[0]) for k in ("p", "h", "a", "b", "e", "x", "g"))))
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

    return Handler


def state(engine: Engine, app_mode: dict, p: int, h: int, a: int, b: int, e: int = 0, x: int = 0, g: int = 0) -> dict:
    """The snapshot plus the graph points of the current ride that arrived since the page's counts
    (no ride: zero counts, which tells the page to clear its graphs)."""
    out = {**engine.snap, "app": app_mode["app"], "viewer": bool(app_mode.get("viewer")), "closed": engine.quit.is_set()}
    session = engine.session                       # may end while we read: keep this one
    if session is None or session.t0 is None:
        return {**out, "pn": 0, "hn": 0, "an": 0, "bn": 0, "gn": 0, "power": [], "hr": [], "a1": [], "beats_rr": [],
                "target": [], "strap": None}
    lp, lh, la, lb = session.live_power, session.live_hr, session.hrv.series, session.hrv.beats
    pn, hn, an, bn = len(lp), len(lh), len(la), session.hrv._judged
    t0 = session.t0
    (en, ecg), (xn, acc) = engine.strap_since("ecg", e, t0), engine.strap_since("acc", x, t0)
    lt = getattr(session, "live_target", [])
    gn = len(lt)
    return {**out, "pn": pn, "hn": hn, "an": an, "bn": bn, "gn": gn,
            "target": [[t, None if w is None else _r(w)] for t, w in lt[g:gn]] if g <= gn else [],
            # the strap's raw streams: ECG [s, uV], accelerometer [s, x, y, z mG]
            "strap": {"en": en, "xn": xn, "ecg": ecg, "acc": acc},
            # beats once judged: [s since t0, RR ms, artifact]
            "beats_rr": [[_r((x.time - t0).total_seconds()), _r(x.rr), bool(x.artifact)] for x in lb[b:bn]]
                        if b <= bn else [],
            "power": [[_r(t), _r(w), _r(av)] for t, w, av in lp[p:pn]] if p <= pn else [],
            "hr": [[_r(t), v] for t, v in lh[h:hn]] if h <= hn else [],
            # DFA alpha 1 every 5 s: [s since t0, alpha 1 or None, artifact %]
            "a1": [[_r((ts - t0).total_seconds()), _r(v), _r(art)] for ts, v, _, art in la[a:an]] if a <= an else []}


# --------------------------------------------------------------------------- #
class MonthlyLog:
    """The app's log, one file a month in rides/logs/ (app_log_2026-10.txt), never deleted; a new
    month starts a new file even with the app open. The old single app_log.txt becomes its month's
    file the first time (moved, not copied)."""

    def __init__(self, folder: str, legacy: Optional[str] = None) -> None:
        self.folder, self._month, self._fh = folder, None, None
        os.makedirs(folder, exist_ok=True)
        if legacy and os.path.exists(legacy) and not os.path.exists(self._path(self._now())):
            os.replace(legacy, self._path(self._now()))

    @staticmethod
    def _now() -> str:
        return datetime.now().strftime("%Y-%m")

    def _path(self, month: str) -> str:
        return os.path.join(self.folder, f"app_log_{month}.txt")

    def _file(self):
        m = self._now()
        if m != self._month:
            if self._fh:
                self._fh.close()
            self._fh, self._month = open(self._path(m), "a", buffering=1), m
        return self._fh

    def write(self, text: str) -> int:
        return self._file().write(text)

    def flush(self) -> None:
        self._file().flush()

    def fileno(self) -> int:                          # faulthandler writes straight to the file
        return self._file().fileno()


def _only_instance() -> bool:
    """One app at a time: two would fight over the pedals and the ANT+ stick. Windows: a named
    mutex the app owns until its process ends (even on a crash). A start right after closing the
    app waits for it to finish shutting down (disconnecting the sensors takes a few seconds);
    a start while it is really open says so and quits. Linux/macOS: an exclusive lock on a file
    next to the app, held until the process ends (the system lets go of it even on a crash)."""
    if os.name != "nt":
        import fcntl
        fh = open(os.path.join(HERE, ".app.lock"), "a")
        globals()["_LOCK"] = fh
        for _ in range(48):                        # 12 s for one that is still closing
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except OSError:
                time.sleep(0.25)
        print("Assioma Recorder is already open.", file=sys.stderr)
        import shutil
        if shutil.which("notify-send"):
            import subprocess
            subprocess.run(["notify-send", "Assioma Recorder", "Assioma Recorder is already open."], check=False)
        return False
    import ctypes
    k32 = ctypes.windll.kernel32
    k32.CreateMutexW.restype = ctypes.c_void_p
    k32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    handle = k32.CreateMutexW(None, True, "AssiomaRecorder.single")
    globals()["_MUTEX"] = handle
    if k32.GetLastError() != 183:                  # ERROR_ALREADY_EXISTS: no, it's ours
        return True
    if k32.WaitForSingleObject(handle, 12000) in (0, 0x80):    # got it (0x80: the other one crashed)
        return True
    ctypes.windll.user32.MessageBoxW(None, "Assioma Recorder is already open.", "Assioma Recorder", 0x40)
    return False


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
    if not args.replay and not args.no_open and not _only_instance():
        return

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
    log = MonthlyLog(os.path.join(args.out, "logs"), os.path.join(args.out, "app_log.txt"))
    if sys.stderr is None:                          # started without a console (pythonw): errors go to the log
        sys.stderr = log
    import faulthandler
    faulthandler.enable(log, all_threads=True)

    engine = Engine(args.out, settings, make_worker, settings_path, log)
    engine.worker.log(f"app started, {sys.version.split()[0]}")
    if not args.replay:
        import keepawake
        engine.keep_awake = keepawake.KeepAwake(engine.worker.log)
    engine.start()
    if not args.replay:                             # a ride the app closed in the middle of: finished now
        threading.Thread(target=engine.recover_rides, daemon=True).start()
    engine.start_sync()                             # your server, when sync.json says where
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
            # Linux: render with Chromium (Qt WebEngine, pip install PyQt6 PyQt6-WebEngine qtpy), like
            # WebView2 on Windows. WebKitGTK, pywebview's Linux default, draws the canvases on the CPU
            # and stutters (~53 fps with 4-5 long frames a second vs a steady 60 with Qt).
            gui = None
            if sys.platform.startswith("linux"):
                try:
                    import PyQt6.QtWebEngineWidgets  # noqa: F401
                    gui = "qt"
                except ImportError:
                    pass
            webview.start(gui=gui)
    except KeyboardInterrupt:
        pass
    finally:
        engine.quit.set()
        engine.join(timeout=2)
        engine.stop_sync()
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
