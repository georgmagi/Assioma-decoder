"""The desktop app's engine (ride lifecycle) and its local HTTP API."""
import json
import os
import queue
import threading
import urllib.request
from datetime import timedelta

import assioma_recorder as ar
import recorder_app as app


class FakeWorker:
    def __init__(self):
        self.status = {"pedal": "connected: AssiomaPRO12345L", "hr": ""}
        self.pedal_name = None
        self.stopped = False

    def stop(self):
        self.stopped = True

    def log(self, _text):
        pass


def _engine(tmp_path):
    s = ar.Session(str(tmp_path))
    return app.Engine(s, FakeWorker(), queue.Queue(), ftp=200)


def _record(e, seconds=90):
    e._do("start")
    t0 = e.session.started
    for i in range(seconds):
        e.session._second({"time": t0 + timedelta(seconds=i), "power_w": 150, "heart_rate_bpm": 130})


def test_stop_asks_first_then_save_writes_the_fit(tmp_path):
    e = _engine(tmp_path)
    _record(e)
    e._do("stop")
    assert e.dialog == "finish" and e.session.state == "paused"        # the clock stops while you decide
    e._do("resume")
    assert e.dialog is None and e.session.state == "recording"
    e._do("stop")
    e._do("save")
    assert e.dialog == "saved" and e.worker.stopped and not e.quit.is_set()
    assert os.path.isfile(e.summary["path"]) and ["Avg power", "150 W"] in e.summary["stats"]
    e._do("close")
    assert e.quit.is_set()


def test_discard_needs_a_second_yes_and_deletes_the_files(tmp_path):
    e = _engine(tmp_path)
    _record(e, 10)
    base = e.session.base
    e._do("stop")
    e._do("discard")
    assert e.dialog == "discard" and os.path.isfile(base + "_raw.csv")
    e._do("back")
    assert e.dialog == "finish"
    e._do("discard")
    e._do("discard_confirm")
    assert e.quit.is_set() and not os.path.exists(base + "_raw.csv") and not os.path.exists(base + ".fit")


def test_space_does_nothing_while_a_dialog_is_open(tmp_path):
    e = _engine(tmp_path)
    _record(e, 5)
    e._do("stop")
    e._do("toggle")
    assert e.session.state == "paused" and e.dialog == "finish"


def test_window_close_mid_ride_asks_instead_of_closing(tmp_path):
    e = _engine(tmp_path)
    e.start()
    try:
        assert e.window_closing() is True                 # ready: nothing recorded, just close
        e.command("start")
        assert e.window_closing() is False and e.dialog == "finish"
    finally:
        e.quit.set()
        e.join(2)


def test_http_api_serves_state_graph_points_and_buttons(tmp_path):
    e = _engine(tmp_path)
    e.session.t0 = ar.utcnow()
    e.session.live_power += [(0.5, 100, 100.0), (1.5, 120, 110.0), (2.5, 140, 120.0)]
    e.start()
    srv = app.make_server(e, {"app": False, "window": None})
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        for _ in range(50):                                # the first snapshot
            if e.snap:
                break
            threading.Event().wait(0.05)
        st = json.load(urllib.request.urlopen(url + "/api/state?p=1&h=0"))
        assert st["pn"] == 3 and [p[1] for p in st["power"]] == [120, 140]      # only the new points
        assert st["ride"]["state"] == "ready" and st["config"]["ftp"] == 200 and st["an"] == 0 and "dyn" in st
        assert urllib.request.urlopen(url + "/api/pedal").status == 200
        req = urllib.request.Request(url + "/api/action", data=json.dumps({"name": "start"}).encode(),
                                     headers={"Content-Type": "application/json"})
        assert json.load(urllib.request.urlopen(req))["ride"]["state"] == "recording"
        assert urllib.request.urlopen(url + "/").status == 200
        try:
            urllib.request.urlopen(url + "/..%2Frecorder_app.py")
            assert False, "served a file outside web/"
        except urllib.error.HTTPError as err:
            assert err.code == 404
    finally:
        srv.shutdown()
        e.quit.set()
        e.join(2)
