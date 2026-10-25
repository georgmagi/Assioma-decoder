"""The desktop app's engine (home -> ride -> summary lifecycle) and its local HTTP API."""
import json

import pytest
import os
import threading
import urllib.error
import urllib.request
from datetime import date, timedelta

import recorder_app as app
import plan as pl
import ride_summary as rs


class FakeWorker:
    made = 0

    def __init__(self, q, want_hr):
        FakeWorker.made += 1
        self.q, self.want_hr = q, want_hr
        self.status = {"pedal": "connected: AssiomaPRO12345L", "hr": "connected: HRM" if want_hr else ""}
        self.pedal_name = None
        self.stopped = False
        self.log_file = None

    def start(self):
        pass

    def stop(self):
        self.stopped = True

    def join(self, timeout=None):
        pass

    def log(self, _text):
        pass


@pytest.fixture(autouse=True)
def _no_quick_stop(monkeypatch):
    """The tests' rides are seconds long: Stop asks as for a real ride (QUICK_S has its own test)."""
    monkeypatch.setattr(app, "QUICK_S", 0)


def _engine(tmp_path, **settings):
    return app.Engine(str(tmp_path), {**app.SETTINGS_DEFAULTS, "ftp": 200, **settings}, FakeWorker)


def _ride(e, seconds=90):
    e._do("start")
    s = e.session
    for i in range(seconds):
        s._second({"time": s.started + timedelta(seconds=i), "power_w": 150, "heart_rate_bpm": 130})
    return s


def test_start_stop_save_goes_to_the_summary_and_home(tmp_path):
    e = _engine(tmp_path)
    assert e.screen == "home" and e.session is None
    s = _ride(e)
    assert e.screen == "ride" and s.state == "recording"
    e._do("stop")
    assert e.dialog == "finish" and s.state == "paused"          # the clock stops while you decide
    e._do("resume")
    assert e.dialog is None and s.state == "recording"
    e._do("stop")
    e._do("save")
    assert e.screen == "summary" and e.session is None and not e.quit.is_set()
    assert e.summary_id == os.path.basename(s.base) and os.path.exists(s.base + ".fit")
    assert json.load(open(rs.cache_path(str(tmp_path), e.summary_id)))["avg_power"] == 150   # the list's entry
    assert not e.worker.stopped                                               # sensors stay connected
    e._do("home")
    assert e.screen == "home"
    s2 = _ride(e, 5)                                                          # and the next ride
    assert s2 is not s and e.screen == "ride"


def test_discard_needs_a_second_yes_and_keeps_the_files_aside(tmp_path):
    e = _engine(tmp_path)
    s = _ride(e, 10)
    e._do("stop")
    e._do("discard")
    assert e.dialog == "discard" and os.path.isfile(s.base + "_raw.csv")
    e._do("back")
    assert e.dialog == "finish"
    e._do("discard")
    e._do("discard_confirm")
    assert e.screen == "home" and not os.path.exists(s.base + "_raw.csv") and not os.path.exists(s.base + ".fit")
    kept = os.path.join(str(tmp_path), "discarded", os.path.basename(s.base))
    for ext in ("_raw.csv", "_per_second.csv", ".fit", "_meta.json"):         # all of it, out of the ride list
        assert os.path.isfile(kept + ext), ext
    assert rs.ride_list(str(tmp_path), 200) == []
    for f in os.listdir(os.path.dirname(kept)):                              # moved back: a ride again
        os.replace(os.path.join(os.path.dirname(kept), f), os.path.join(str(tmp_path), f))
    assert [r["id"] for r in rs.ride_list(str(tmp_path), 200)] == [os.path.basename(s.base)]


def test_discarding_an_empty_ride_still_keeps_its_log_aside(tmp_path):
    e = _engine(tmp_path)
    s = _ride(e, 0)
    e._do("stop"); e._do("discard"); e._do("discard_confirm")
    assert os.path.exists(os.path.join(str(tmp_path), "discarded", os.path.basename(s.base) + "_raw.csv"))
    assert not os.path.exists(s.base + "_raw.csv") and rs.ride_list(str(tmp_path), 200) == []


def test_space_does_nothing_while_a_dialog_is_open(tmp_path):
    e = _engine(tmp_path)
    s = _ride(e, 5)
    e._do("stop")
    e._do("toggle")
    assert s.state == "paused" and e.dialog == "finish"


def test_settings_are_checked_saved_and_the_strap_switch_reconnects(tmp_path):
    path = str(tmp_path / "settings.json")
    e = app.Engine(str(tmp_path), dict(app.SETTINGS_DEFAULTS), FakeWorker, path)
    before = FakeWorker.made
    e._do("settings", {"ftp": 210, "plan_min": 75, "hr": False})
    saved = app.load_settings(path)
    assert (saved["ftp"], saved["plan_min"], saved["hr"]) == (210, 75, False)
    assert FakeWorker.made == before + 1 and not e.worker.want_hr
    e._do("settings", {"ftp": 5000, "plan_min": -1})                          # nonsense is ignored
    assert e.settings["ftp"] == 210 and e.settings["plan_min"] == 75


def test_spinning_on_after_a_ride_does_not_start_the_next(tmp_path):
    e = _engine(tmp_path)
    now = app.utcnow()
    feed = lambda watts, t: e.home_live.__setitem__("power", (watts, t))
    feed(150, now)
    e.pedal_since = now - timedelta(seconds=5)                   # pedalling at launch: armed, starts
    e._tick()
    assert e.screen == "ride"
    _stop_and_save(e)
    e._do("home")
    feed(150, app.utcnow())
    e.pedal_since = app.utcnow() - timedelta(seconds=10)
    e._tick()
    assert e.screen == "home" and not e.armed                     # still spinning: no new ride
    e.home_live.clear()
    e.quiet_since = app.utcnow() - timedelta(seconds=app.REARM_S + 1)
    e._tick()
    assert e.armed                                                # still for 5 s: armed again


def _stop_and_save(e):
    for _ in range(3):
        e.session._second({"time": e.session.started + timedelta(seconds=_), "power_w": 150})
    e._do("stop")
    e._do("save")


def test_deleting_a_ride_from_its_summary_asks_first(tmp_path):
    e = _engine(tmp_path)
    s = _ride(e, 30)
    _stop_and_save(e)
    assert e.screen == "summary"
    e._do("delete")
    assert e.dialog == "delete" and os.path.exists(s.base + ".fit")
    e._do("back")
    assert e.dialog is None and os.path.exists(s.base + ".fit")
    e._do("delete")
    e._do("delete_confirm")
    assert e.screen == "home" and not [f for f in os.listdir(tmp_path) if f not in (".cache", "deleted")]
    assert not os.listdir(tmp_path / ".cache")
    kept = os.listdir(tmp_path / "deleted")                     # never off the disk
    assert os.path.basename(s.base) + ".fit" in kept and os.path.basename(s.base) + "_raw.csv" in kept


def test_rides_can_be_deleted_from_the_history_and_the_list_learns_of_it(tmp_path):
    e = _engine(tmp_path)
    s = _ride(e, 30)
    _stop_and_save(e)
    e._do("home")
    e._do("delete", os.path.basename(s.base))                    # not from the home page
    assert e.dialog is None
    e._do("nav", "stats")
    version = e.rides_version
    ride = os.path.basename(s.base)
    e._do("delete", "../settings")                               # only ride ids
    assert e.dialog is None
    e._do("delete", ride)
    assert e.dialog == "delete" and e.delete_id == ride
    e._do("delete_confirm")
    assert e.dialog is None and not os.path.exists(s.base + ".fit") and e.rides_version == version + 1


def test_theme_span_and_power_mode_are_remembered(tmp_path):
    path = str(tmp_path / "settings.json")
    e = app.Engine(str(tmp_path), dict(app.SETTINGS_DEFAULTS), FakeWorker, path)
    e._do("prefs", {"theme": "lcd", "span": 0, "power3s": True})
    assert app.load_settings(path)["theme"] == "lcd"
    assert app.load_settings(path)["span"] == 0 and app.load_settings(path)["power3s"] is True
    e._do("prefs", {"theme": "neon", "span": False, "power3s": 1})           # nonsense is ignored
    assert app.load_settings(path) == {**app.SETTINGS_DEFAULTS, "theme": "lcd", "span": 0, "power3s": True}


def test_statistics_opens_rides_and_their_summary_goes_back_there(tmp_path):
    e = _engine(tmp_path)
    s = _ride(e, 30)
    _stop_and_save(e)
    e._do("home")
    e._do("nav", "stats")
    assert e.screen == "stats"
    e._do("open", os.path.basename(s.base))
    assert e.screen == "summary" and e.back_to == "stats"
    e._do("home")
    assert e.screen == "stats"
    e._do("home")
    assert e.screen == "home"


def test_pages_are_reachable_but_never_mid_ride(tmp_path):
    e = _engine(tmp_path)
    for page in ("workouts", "stats", "home"):
        e._do("nav", page)
        assert e.screen == page
    for bad in ("ride", "settings"):                              # not pages
        e._do("nav", bad)
        assert e.screen == "home"
    e._do("nav", "workouts")
    _ride(e, 5)                                                   # a ride can start from Workouts
    e._do("nav", "stats")
    assert e.screen == "ride"
    e._do("connect", {"device": "trainer", "on": True})          # nor reconnect sensors mid-ride
    assert not e.settings["trainer"]


def test_connections_pane_switches_the_strap_and_trainer(tmp_path):
    e = _engine(tmp_path)
    before = FakeWorker.made
    e._do("connect", {"device": "trainer", "on": True})
    assert e.settings["trainer"] and FakeWorker.made == before + 1          # reconnected with it
    e._do("connect", {"device": "hr", "on": False})
    assert not e.settings["hr"] and FakeWorker.made == before + 2
    e._do("connect", {"device": "pedals", "on": False})                     # the pedals are always on
    assert FakeWorker.made == before + 2


def test_only_rides_in_the_folder_can_be_opened(tmp_path):
    e = _engine(tmp_path)
    s = _ride(e, 30)
    _stop_and_save(e)
    e._do("home")
    for bad in ("../../etc/passwd", "ride_2026-09-28_06-00-00"):           # a path, a ride that isn't there
        e._do("open", bad)
        assert e.screen == "home"
    e._do("open", os.path.basename(s.base))
    assert e.screen == "summary" and e.summary_id == os.path.basename(s.base)


def test_window_close_mid_ride_asks_instead_of_closing(tmp_path):
    e = _engine(tmp_path)
    e.start()
    try:
        assert e.window_closing() is True                 # home: just close
        e.command("start")
        assert e.window_closing() is False and e.dialog == "finish"
    finally:
        e.quit.set()
        e.join(2)


def test_http_api_state_rides_and_buttons(tmp_path):
    e = _engine(tmp_path)
    s = _ride(e, 120)
    e._do("stop")
    e._do("save")
    e._do("home")
    e.start()
    srv = app.make_server(e, {"app": False, "window": None})
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    get = lambda path: json.load(urllib.request.urlopen(url + path))
    try:
        for _ in range(50):                                # the first snapshot
            if e.snap:
                break
            threading.Event().wait(0.05)
        st = get("/api/state?p=0&h=0&a=0&b=0")
        assert st["screen"] == "home" and st["pn"] == 0 and st["settings"]["ftp"] == 200
        prog = get("/api/progress")
        assert prog["totals"]["rides"] == 1 and len(prog["weeks"]) == 12 and prog["pmc"]["days"]
        rides = get("/api/rides")
        assert [r["id"] for r in rides] == [os.path.basename(s.base)] and rides[0]["avg_power"] == 150
        ride = get("/api/ride?id=" + rides[0]["id"])
        assert ride["stats"]["ride_s"] == 120 and ride["fit"]
        for bad in ("/api/ride?id=..%2Frecorder_app", "/..%2Frecorder_app.py"):
            try:
                urllib.request.urlopen(url + bad)
                assert False, "served " + bad
            except urllib.error.HTTPError as err:
                assert err.code == 404
        req = urllib.request.Request(url + "/api/action", data=json.dumps({"name": "start"}).encode(),
                                     headers={"Content-Type": "application/json"})
        assert json.load(urllib.request.urlopen(req))["screen"] == "ride"
        assert urllib.request.urlopen(url + "/api/pedal").status == 200
    finally:
        srv.shutdown()
        e.quit.set()
        e.join(2)


def test_z2_guard_against_your_vt1(tmp_path):
    e = _engine(tmp_path, vt1_hr=150)                       # set by hand: wins over the rides
    s = _ride(e, 0)
    s.t0 = s.started
    for i in range(60):
        s._second({"time": s.started + timedelta(seconds=i), "power_w": 180, "heart_rate_bpm": 155})
    now = s.started + timedelta(seconds=60)
    for hr, state in ((155, "above"), (148, "near"), (140, "below")):
        s.live_hr[:] = [(t, hr) for t in range(30, 61)]
        g = e._guard(s, now, alpha1=1.0, coverage=100)
        assert g["state"] == state and g["ref"]["hr"] == 150 and abs(g["headroom"] - (150 - hr)) < 1e-9
    assert g["above_s"] == 60                                # the ride's seconds above VT1
    # alpha 1 under 0.75 for 30 s says "above" whatever the heart rate says; not with too few beats
    assert e._guard(s, now, alpha1=0.7, coverage=100)["state"] == "below"
    assert e._guard(s, now + timedelta(seconds=31), alpha1=0.7, coverage=100)["state"] == "above"
    assert e._guard(s, now + timedelta(seconds=40), alpha1=0.7, coverage=80)["a1_low_s"] == 0


def test_vt1_reference_comes_from_your_latest_crossing(tmp_path, monkeypatch):
    # only a usable crossing counts (vt1.usable): not a bound, not a shaky crossing
    rides = [{"start_utc": "2026-10-03T07:00:00+00:00", "vt1_hr": {"kind": "cross", "value": 165, "lo": 158, "hi": 172, "ok": False}},
             {"start_utc": "2026-10-02T07:00:00+00:00", "vt1_hr": {"kind": "above", "value": 150}},
             {"start_utc": "2026-10-01T07:00:00+00:00", "vt1_hr": {"kind": "cross", "value": 158.4, "lo": 156, "hi": 161, "ok": True}},
             {"start_utc": "2026-09-20T07:00:00+00:00", "vt1_hr": {"kind": "cross", "value": 163, "ok": True}}]
    monkeypatch.setattr(rs, "ride_list", lambda folder, ftp=None, history=None: rides)
    e = _engine(tmp_path)
    e.vt1_ref()                                              # found in the background
    for _ in range(100):
        if e._vt1_auto:
            break
        threading.Event().wait(0.02)
    ref = e.vt1_ref()
    assert ref["hr"] == 158.4 and ref["lo"] == 156 and ref["day"] == "2026-10-01" and ref["source"] == "ride"
    e._do("settings", {"vt1_hr": 155})
    assert e.vt1_ref() == {"hr": 155, "source": "setting"}
    e._do("settings", {"vt1_hr": 300})                       # out of range: ignored
    assert e.settings["vt1_hr"] == 155


def test_max_heart_rate_setting(tmp_path):
    e = _engine(tmp_path)
    e._do("settings", {"max_hr": 198})
    assert e.settings["max_hr"] == 198
    e._do("settings", {"max_hr": 300})                         # out of range: ignored
    assert e.settings["max_hr"] == 198
    e._do("settings", {"ftp": 240})                            # other settings leave it alone
    assert e.settings["max_hr"] == 198 and e.settings["ftp"] == 240


class _Trainer:
    want = None
    controlled, error, power_w = True, "", None


def _pedal(s, secs, watts=150):
    """Notifications' worth of pedal power up to now (what _pedalling looks at)."""
    now = (app.utcnow() - s.t0).total_seconds()
    s.live_power[:] = [(now - secs + k * 0.25, watts, watts) for k in range(int(secs * 4) + 1)]


def test_workout_drives_the_trainer_safely(tmp_path):
    e = _engine(tmp_path)                                    # FTP 200
    e.worker.trainer = _Trainer()
    e._do("workout", "vt1-steps")
    assert e.workout_id == "vt1-steps"
    s = _ride(e, 0)
    s.t0 = app.utcnow()
    _pedal(s, 1)                                             # just started pedalling: not yet
    e._drive_trainer()
    assert e.worker.trainer.want == ("sim", 0.0)
    _pedal(s, 2.5)                                           # 2 s of pedalling: ERG on, warm-up at 45 %
    e._drive_trainer()
    kind, w = e.worker.trainer.want
    assert kind == "erg" and abs(w - 90) < 1e-6
    e._do("wo_nudge", 5)
    e._drive_trainer()
    assert abs(e.worker.trainer.want[1] - 94.5) < 1e-6
    e._do("wo_skip")
    e._drive_trainer()
    assert abs(e.worker.trainer.want[1] - 0.60 * 200 * 1.05) < 1e-6
    s.live_power[:] = []                                     # stopped pedalling: flat at once
    e._drive_trainer()
    assert e.worker.trainer.want == ("sim", 0.0)
    _pedal(s, 3)
    e._drive_trainer()
    e._do("stop")                                            # paused while deciding: flat
    e._drive_trainer()
    assert e.worker.trainer.want == ("sim", 0.0) and e.dialog == "finish"
    e._do("resume"); _pedal(s, 3)
    e._drive_trainer()
    assert e.worker.trainer.want[0] == "erg"
    e.player.shift = 10_000                                  # past the end: done, flat
    e._drive_trainer()
    assert e.player is None and e.workout_done and e.worker.trainer.want == ("sim", 0.0)


def test_leaving_a_ride_never_leaves_erg_on_and_no_ftp_no_workout(tmp_path):
    e = _engine(tmp_path)
    e.worker.trainer = _Trainer()
    e._do("workout", "vo2-5x4")
    s = _ride(e, 5)
    s.t0 = app.utcnow()
    _pedal(s, 3); e._drive_trainer()
    assert e.worker.trainer.want[0] == "erg"
    e._do("stop"); e._do("discard"); e._do("discard_confirm")
    assert e.screen == "home" and e.worker.trainer.want == ("sim", 0.0)
    e2 = _engine(tmp_path, ftp=None)
    e2._do("workout", "vo2-5x4")
    _ride(e2, 0)
    assert e2.player is None and "FTP" in e2.error
    e2._do("workout", "../../etc")                          # only known workouts
    assert e2.workout_id == "vo2-5x4" or e2.workout_id is None


def test_rides_keep_the_room_and_the_workout_steps(tmp_path):
    e = _engine(tmp_path)
    e._do("settings", {"room_temp": 21.5, "room_rh": 48, "ftp": 200})
    e._do("settings", {"room_temp": 99})                          # nonsense: kept as it was
    assert e.settings["room_temp"] == 21.5
    e._do("workout", "vt1-steps")
    s = _ride(e, 0)
    for i in range(601):
        e._log_step()
        s._second({"time": s.started + timedelta(seconds=i), "power_w": 100, "heart_rate_bpm": 120})
    _stop_and_save(e)
    ride = os.path.basename(s.base)
    m = rs.load_meta(str(tmp_path), ride)
    assert m["temp_c"] == 21.5 and m["rh_pct"] == 48 and m["workout"]["id"] == "vt1-steps"
    assert [x[1] for x in m["workout"]["log"]] == [0, 1]                      # warm-up, then the 60 % step at 600 s
    entry = next(r for r in rs.ride_list(str(tmp_path), 200) if r["id"] == ride)
    assert entry["temp_c"] == 21.5 and entry["name"] == "Threshold steps"
    # corrected afterwards from the summary
    e._do("ride_meta", {"temp_c": 23})
    assert rs.load_meta(str(tmp_path), ride)["temp_c"] == 23
    e._do("nav", "stats"); e._do("delete", ride); e._do("delete_confirm")
    assert not os.path.exists(rs.meta_path(str(tmp_path), ride))             # deleted with the ride


def test_vt1_ceiling_trims_slowly_and_gives_back(tmp_path):
    e = _engine(tmp_path, vt1_hr=150)
    e.worker.trainer = _Trainer()
    e._do("workout", "endurance-60")
    s = _ride(e, 0)
    s.t0 = app.utcnow()
    _pedal(s, 3)
    hr_now = lambda bpm: s.live_hr.__setitem__(slice(None), [((app.utcnow() - s.t0).total_seconds() - k, bpm) for k in range(30)])
    hr_now(158)                                               # well above VT1
    e._drive_trainer()
    full = e.worker.trainer.want[1]
    assert e.cap == 1.0                                       # off: never trimmed
    e._do("wo_ceiling")
    e._drive_trainer()
    assert e.cap == 0.97 and e.worker.trainer.want[1] == pytest.approx(full * 0.97)
    e._drive_trainer()
    assert e.cap == 0.97                                      # waits for heart rate to answer
    for _ in range(20):
        e._cap_t -= 30
        e._drive_trainer()
    assert e.cap == e.CAP_MIN                                 # never below 70 %
    hr_now(140)                                               # clearly under: given back, slowly
    e._cap_t -= 31
    e._drive_trainer()
    assert e.cap == pytest.approx(0.72)
    e._do("wo_ceiling")                                       # off again: the full target at once
    e._drive_trainer()
    assert e.cap == 1.0 and e.worker.trainer.want[1] == pytest.approx(full)


def test_plan_puts_todays_workout_up_and_the_builder_saves_files(tmp_path, monkeypatch):
    from datetime import date
    monkeypatch.setattr(app, "WORKOUT_DIR", str(tmp_path / "workouts"))
    e = app.Engine(str(tmp_path), {**app.SETTINGS_DEFAULTS, "ftp": 200}, FakeWorker, str(tmp_path / "settings.json"))
    today = date.today()
    e._do("nav", "plan")
    e._do("plan_new", {"start": today.isoformat(), "weeks": 4})
    monday = today.toordinal() - today.weekday()
    assert e.plan["start"] == date.fromordinal(monday).isoformat()
    e._do("plan_import", {"text": f"{today.isoformat()}: Sweet 3x15 | 10m 55%, 3x(15m 90%, 5m 55%), 10m 50%\nFri 1h"})
    assert "Imported 1 day" in e.plan_msg and "line 2" in e.plan_msg
    e._tick()
    assert e.workout_id == f"plan:{today.isoformat()}" and e.workouts()[e.workout_id]["name"] == "Sweet 3x15"
    e._do("workout", "endurance-60")                            # you can still change it today
    e._tick()
    assert e.workout_id == "endurance-60"
    e._do("plan_set", {"date": today.isoformat(), "text": "12m 900%"})
    assert "300" in e.plan_msg                                   # explained, the day unchanged
    assert app.pl.load(str(tmp_path / "plan.json"))["days"][today.isoformat()]["name"] == "Sweet 3x15"
    e._do("wo_save", {"name": "3x15 threshold", "text": "15m 50-70%, 3x(15m 92%, 5m 55%), 10m 50%"})
    assert (tmp_path / "workouts" / "3x15-threshold.zwo").exists() and "zwo:3x15-threshold" in e.workouts()
    e._do("wo_delete", "zwo:../settings")                       # only workout files
    e._do("wo_delete", "zwo:3x15-threshold")
    assert "zwo:3x15-threshold" not in e.workouts()


def test_an_ftp_change_is_remembered_by_day(tmp_path):
    e = _engine(tmp_path, ftp=199)
    e._do("settings", {"ftp": 240})
    today = date.today().isoformat()
    assert e.settings["ftp_history"] == [["0001-01-01", 199], [today, 240]]
    e._do("settings", {"ftp": 235})                              # the same day again: one entry a day
    assert e.settings["ftp_history"] == [["0001-01-01", 199], [today, 235]]
    e._do("settings", {"max_hr": 198})                           # other settings leave it alone
    assert e.settings["ftp_history"][-1] == [today, 235]


def test_files_changed_on_disk_are_read_again_before_a_click_saves_over_them(tmp_path):
    path = str(tmp_path / "settings.json")
    app.save_settings(path, {**app.SETTINGS_DEFAULTS, "ftp": 200})
    e = app.Engine(str(tmp_path), app.load_settings(path), FakeWorker, path)
    e._do("plan_new", {"start": date.today().isoformat(), "weeks": 4})
    e._watch_files(every=0)                                      # our own save: nothing to read
    v = e.plan_version
    e._watch_files(every=0)
    assert e.plan_version == v
    plan = pl.load(e.plan_path)                                  # an edit from outside the app
    plan["days"][date.today().isoformat()] = {"ref": "vt1-steps"}
    pl.save(e.plan_path, plan)
    st = os.stat(e.plan_path)
    os.utime(e.plan_path, ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000))
    e._watch_files(every=0)
    assert e.plan["days"][date.today().isoformat()] == {"ref": "vt1-steps"} and e.plan_version == v + 1
    s = app.load_settings(path)
    s["ftp"] = 250
    app.save_settings(path, s)
    st = os.stat(path)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000))
    e._watch_files(every=0)
    assert e.settings["ftp"] == 250


class LifeWorker(FakeWorker):
    """A worker with the per-sensor lifecycle (BleWorker.set_want)."""
    def __init__(self, q, want_hr):
        super().__init__(q, want_hr)
        self.want = {"pedal": True, "hr": want_hr, "trainer": False}
        self.state = {"pedal": "searching", "hr": "searching" if want_hr else "off", "trainer": "off"}
        self.attempt = {"pedal": None, "hr": None, "trainer": None}
        self.want_trainer = False

    def set_want(self, kind, on):
        self.want[kind] = on
        self.state[kind] = "connecting" if on else "off"


def test_sensors_wait_for_the_pedals_and_let_go_when_idle(tmp_path):
    e = app.Engine(str(tmp_path), {**app.SETTINGS_DEFAULTS, "ftp": 200, "hr": True, "trainer": True}, LifeWorker)
    w, now = e.worker, app.utcnow()
    assert not e.awake and w.want == {"pedal": True, "hr": False, "trainer": False} and not w.want_trainer
    assert e._snapshot()["awake"] is False
    e._sensor_tick(now)                                          # looking at the pages: still asleep
    assert not w.want["hr"]
    w.state["pedal"] = "connected"                               # the cranks turned
    e._sensor_tick(now)
    assert e.awake and w.want["hr"] and w.want["trainer"] and w.state["hr"] == "searching"
    e._active -= e.IDLE_S + 1                                    # 10 min of nothing
    e._sensor_tick(now)
    assert not e.awake and not w.want["hr"] and not w.want["trainer"] and w.want["pedal"]
    e._sensor_tick(now)                                          # pedals still connected: no new wake-up
    assert not e.awake
    e._do("morning_start")                                       # the check wakes the strap
    assert e.awake and w.want["hr"]
    assert e.settings["hr"] is True and e.settings["trainer"] is True   # your choices are untouched


def test_a_strap_you_dont_use_stays_off_when_the_sensors_wake(tmp_path):
    e = app.Engine(str(tmp_path), {**app.SETTINGS_DEFAULTS, "ftp": 200, "hr": False, "trainer": False}, LifeWorker)
    e.worker.state["pedal"] = "connected"
    e._sensor_tick(app.utcnow())
    assert e.awake and not e.worker.want["hr"] and not e.worker.want["trainer"]


def test_stopping_a_ride_under_a_minute_asks_nothing_and_keeps_it_aside(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "QUICK_S", 60)
    e = _engine(tmp_path)
    s = _ride(e, 30)
    e._do("stop")
    assert e.screen == "home" and e.dialog is None and e.session is None
    assert os.path.isfile(os.path.join(str(tmp_path), "discarded", os.path.basename(s.base) + "_per_second.csv"))
    assert rs.ride_list(str(tmp_path), 200) == []
    s = _ride(e, 60)                                             # a minute or more: asks as always
    e._do("stop")
    assert e.screen == "ride" and e.dialog == "finish"



def test_auto_pause_is_a_fallback_that_never_undoes_your_own_pause(tmp_path, monkeypatch):
    from datetime import datetime
    e = _engine(tmp_path)
    s = _ride(e, 5)
    s.t0 = s.t0 or datetime.now()
    t, power = datetime.now(), [150]
    monkeypatch.setattr(e, "_power_3s", lambda now: power[0])
    at = lambda sec: t + timedelta(seconds=sec)
    e._autopause(at(0))
    power[0] = 0
    e._autopause(at(1)); e._autopause(at(1 + e.AUTOPAUSE_S - 1))
    assert s.state == "recording"                               # a short stop is nothing
    e._autopause(at(1 + e.AUTOPAUSE_S))
    assert s.state == "paused" and e._snapshot()["ride"]["autopaused"]
    power[0] = 160
    e._autopause(at(40))
    assert s.state == "recording" and not e._autopaused          # pedalling again: going on by itself
    e._do("pause")                                                # yours: stays paused while you pedal
    e._autopause(at(41)); e._autopause(at(60))
    assert s.state == "paused"


def test_auto_pause_can_be_turned_off(tmp_path, monkeypatch):
    from datetime import datetime
    e = _engine(tmp_path, autopause=False)
    s = _ride(e, 5)
    s.t0 = s.t0 or datetime.now()
    monkeypatch.setattr(e, "_power_3s", lambda now: 0)
    t = datetime.now()
    for i in range(0, 200, 5):
        e._autopause(t + timedelta(seconds=i))
    assert s.state == "recording"


def test_fuelling_prompts_on_long_rides_and_is_kept_with_the_ride(tmp_path):
    e = _engine(tmp_path, fuel_g_h=60)
    s = _ride(e, 30 * 60)
    assert e._fuel() is None                                     # a free ride under 75 min: nothing
    for i in range(30 * 60, 76 * 60):
        s._second({"time": s.started + timedelta(seconds=i), "power_w": 150, "heart_rate_bpm": 130})
    f = e._fuel()
    assert f["due"] and f["grams"] == 20
    e._do("fuel_later")
    assert not e._fuel()["due"] and e._fuel()["in_s"] == 300
    e._do("fuel_done")
    f = e._fuel()
    assert not f["due"] and f["eaten"] == 20 and f["in_s"] == 20 * 60
    meta = json.load(open(os.path.join(str(tmp_path), os.path.basename(s.base) + rs.META)))
    assert meta["fuel"] == [[76 * 60, 20]]


def test_the_log_is_one_file_a_month_and_the_old_one_is_kept(tmp_path):
    legacy = tmp_path / "app_log.txt"
    legacy.write_text("old lines\n")
    log = app.MonthlyLog(str(tmp_path / "logs"), str(legacy))
    log.write("new line\n"); log.flush()
    month = app.datetime.now().strftime("%Y-%m")
    text = (tmp_path / "logs" / f"app_log_{month}.txt").read_text()
    assert text == "old lines\nnew line\n" and not legacy.exists()


def test_an_unfinished_ride_is_found_and_never_touched_when_there_is_nothing_in_it(tmp_path):
    import assioma_recorder as ar
    raw = tmp_path / "ride_2026-10-24_10-00-00_raw.csv"
    raw.write_text("time_utc,char,hex\n")
    (tmp_path / "ride_2026-10-23_10-00-00_raw.csv").write_text("x")
    (tmp_path / "ride_2026-10-23_10-00-00.fit").write_text("x")                 # finished: not one of them
    assert ar.unfinished(str(tmp_path)) == [str(raw)[: -len("_raw.csv")]]
    assert ar.unfinished(str(tmp_path), busy=str(raw)[: -len("_raw.csv")]) == []
    e = _engine(tmp_path)
    assert e.recover_rides() == [] and raw.read_text() == "time_utc,char,hex\n" and not e.notice


def test_an_older_app_is_refused_by_the_sync_server():
    import cloud_sync as cs
    assert cs.FORMAT >= 2 and "update" in cs.OUTDATED


def test_red_alert_for_a_lost_sensor_after_a_grace_and_for_pedalling_while_you_paused(tmp_path, monkeypatch):
    from datetime import datetime
    e = _engine(tmp_path)
    s = _ride(e, 5)
    t = datetime.now()
    at = lambda sec: t + timedelta(seconds=sec)
    dev = {"pedals": {"state": "connected"}, "hr": {"state": "searching"}, "trainer": {"state": "off"}}
    e.settings.update(hr=True, trainer=False)
    monkeypatch.setattr(e, "_power_3s", lambda now: 0)
    assert e._alerts(s, at(0), dev, None) == []                       # a blip: nothing yet
    assert e._alerts(s, at(e.ALERT_GRACE_S), dev, None) == ["Strap not connected"]
    dev["hr"] = {"state": "connected"}                                # trainer off and not used: fine
    assert e._alerts(s, at(6), dev, None) == []
    e._do("pause")
    assert e._alerts(s, at(7), dev, None) == []                       # paused and still: fine
    monkeypatch.setattr(e, "_power_3s", lambda now: 150)
    assert e._alerts(s, at(8), dev, None) == ["Paused, but you're pedalling: press Resume"]
    e._autopaused = True                                              # auto-pause resumes by itself
    assert e._alerts(s, at(9), dev, None) == []


def test_weight_and_goal_settings_are_checked_and_weight_is_kept_by_day(tmp_path):
    e = _engine(tmp_path)
    e._settings({"weight_kg": 73, "goal_wkg": 4.0, "goal_date": "2027-06-01"})
    assert e.settings["weight_kg"] == 73 and e.settings["weight_history"][-1][1] == 73
    assert e.settings["goal_date"] == "2027-06-01"
    e._settings({"weight_kg": 500, "goal_wkg": 20, "goal_date": "soon"})        # nonsense: ignored
    assert e.settings["weight_kg"] == 73 and e.settings["goal_wkg"] == 4.0 and e.settings["goal_date"] == "2027-06-01"
    e._settings({"goal_date": None})
    assert e.settings["goal_date"] is None


def test_power_match_learns_the_trainer_to_pedal_ratio_and_steers_by_the_pedals(tmp_path):
    e = _engine(tmp_path)                                    # FTP 200
    e.worker.trainer = _Trainer()
    e._do("workout", "endurance-60")
    s = _ride(e, 0)
    s.t0 = app.utcnow()
    _pedal(s, 3)
    e._drive_trainer()
    target = e.worker.trainer.want[1]
    assert e.pm_k == 1.0                                     # nothing learnt yet: asked for the target
    # the trainer reads 4 % above the pedals: after settling it learns, slowly, and keeps it
    t0, e._pm_tgt = 1000.0, None                            # (a clock of the test's own)
    for k in range(200):
        e._power_match(target, target, target * 1.04, t0 + k)
    assert 1.02 < e.pm_k <= 1.05
    for k in range(200, 900):
        e._power_match(target, target, target * 1.04, t0 + k)
    assert e.pm_k == pytest.approx(1.04, abs=0.01)           # within the 1 % dead zone: about 1 W at 228
    assert e.settings["power_match_k"] == pytest.approx(e.pm_k, abs=0.005)
    # a sprint or a dropout window: ignored
    before = e.pm_k
    for k in range(900, 1000):
        e._power_match(target, target, target * 1.6, t0 + k)
    assert e.pm_k == before
    # a new target: waits PM_SETTLE_S before using samples
    e._power_match(target + 50, target + 50, (target + 50) * 1.04, 3000.0)
    e._power_match(target + 50, target + 50, (target + 50) * 1.04, 3005.0)
    assert e._pm == []
    # the trainer is asked for target × k, so the pedals read the target
    e._pm_tgt = None
    e._drive_trainer()
    assert e.worker.trainer.want[1] == pytest.approx(target * e.pm_k)
    e._do("wo_match")                                        # off (the ride screen's button): the trainer's own reading
    assert e.settings["power_match"] is False
    e._drive_trainer()
    assert e.worker.trainer.want[1] == pytest.approx(target)
    e._do("wo_match")
    assert e.settings["power_match"] is True


def test_power_match_holds_still_on_swinging_pedals_and_restarts_after_a_stop(tmp_path):
    import math
    e = _engine(tmp_path)
    e.pm_k, e._pm_tgt = 1.04, None
    for k in range(3000):                                    # ±15 W swing every 5 s, trainer 4 % up and lagging
        ped = 160 + 15 * math.sin(2 * math.pi * k / 5)
        tr = 1.04 * (160 + 15 * math.sin(2 * math.pi * (k - 2) / 5))
        e._power_match(163, ped, tr, 1000.0 + k)
    assert e.pm_k == 1.04                                    # settled = still
    e._power_match(163, 5, 150, 5000.0)                      # stopped pedalling: starts over
    assert e._pm == [] and e._pm_tgt == (163, 5000.0)


def test_full_summary_is_cached_and_redone_when_anything_it_depends_on_changes(tmp_path, monkeypatch):
    import ride_summary as rs2
    e = _engine(tmp_path)
    s = _ride(e, 90)
    _stop_and_save(e)
    rid = os.path.basename(s.base)
    calls = []
    real = rs2.summarize_file
    monkeypatch.setattr(rs2, "summarize_file", lambda *a, **k: calls.append(1) or real(*a, **k))
    a = rs2.summary_cached(str(tmp_path), rid, 200, 190, None)
    b = rs2.summary_cached(str(tmp_path), rid, 200, 190, None)
    assert len(calls) == 1 and a == b                                       # the second time from the cache
    rs2.summary_cached(str(tmp_path), rid, 210, 190, None)                  # another FTP: worked out again
    assert len(calls) == 2
    import time as _t
    _t.sleep(0.01)
    rs2.save_meta(str(tmp_path), rid, {"temp_c": 22})                       # the ride's meta changed
    c = rs2.summary_cached(str(tmp_path), rid, 210, 190, None)
    assert len(calls) == 3 and c["meta"]["temp_c"] == 22


def test_editing_a_workout_changes_it_in_place_and_keeps_the_old_version(tmp_path, monkeypatch):
    import workout as wk
    wd = tmp_path / "workouts"
    monkeypatch.setattr(app, "WORKOUT_DIR", str(wd))
    e = app.Engine(str(tmp_path), {**app.SETTINGS_DEFAULTS, "ftp": 200}, FakeWorker, str(tmp_path / "settings.json"))
    wd.mkdir()
    (wd / "B2-Z2-90min.zwo").write_text(wk.to_zwo("B2-Z2-90min", "Long Z2.", wk.parse_text("90m 68%")), encoding="utf-8")
    e._do("wo_save", {"id": "zwo:B2-Z2-90min", "name": "B2-Z2-90min", "text": "90m 71%"})
    w = e.workouts()["zwo:B2-Z2-90min"]                                  # the same id: the plan still points at it
    assert w["steps"][0]["lo"] == 0.71 and w["description"] == "Long Z2."
    assert len(list((wd / "history").glob("B2-Z2-90min*.zwo"))) == 1    # the 68 % version kept
    e._do("wo_save", {"id": "vt1-steps", "name": "Threshold steps", "text": "10m 50%, 5m 60%"})
    assert e.workouts()["vt1-steps"]["steps"][1]["lo"] == 0.6 and e.workouts()["vt1-steps"].get("edited")
    assert "zwo:vt1-steps" not in e.workouts()
    e._do("wo_save", {"name": "Brand new", "text": "30m Z2"})            # no id: a new one, as before
    assert "zwo:brand-new" in e.workouts()
