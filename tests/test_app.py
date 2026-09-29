"""The desktop app's engine (home -> ride -> summary lifecycle) and its local HTTP API."""
import json
import os
import threading
import urllib.error
import urllib.request
from datetime import timedelta

import recorder_app as app
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


def test_discard_needs_a_second_yes_and_deletes_the_files(tmp_path):
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
    assert e.screen == "home" and not [f for f in os.listdir(tmp_path) if f != ".cache"]
    assert not os.listdir(tmp_path / ".cache")


def test_rides_can_be_deleted_from_the_list_and_the_list_learns_of_it(tmp_path):
    e = _engine(tmp_path)
    s = _ride(e, 30)
    _stop_and_save(e)
    e._do("home")
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


def test_progress_opens_rides_and_their_summary_goes_back_there(tmp_path):
    e = _engine(tmp_path)
    s = _ride(e, 30)
    _stop_and_save(e)
    e._do("home")
    e._do("progress")
    assert e.screen == "progress"
    e._do("open", os.path.basename(s.base))
    assert e.screen == "summary" and e.back_to == "progress"
    e._do("home")
    assert e.screen == "progress"
    e._do("home")
    assert e.screen == "home"


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
