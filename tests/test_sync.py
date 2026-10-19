"""Cloud sync (cloud_sync.py) against the real server (sync_server.py) on localhost."""
import http.client
import json
import os
import threading
import time
from http.server import ThreadingHTTPServer

import pytest

import cloud_sync as cs
import recorder_app as app
import sync_server as srv

KEY = "k" * 40
PW = "correct horse battery"


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setattr(cs, "RECENT_S", 0)
    monkeypatch.setattr(app, "WORKOUT_DIR", app.WORKOUT_DIR)       # make_viewer moves it: put it back after
    data, var = tmp_path / "srv" / "data", tmp_path / "srv"
    store = srv.Store(str(data), str(var))
    rapp, engine = srv.make_viewer(str(data))
    env = {"SYNC_KEY": KEY, "WEB_PASSWORD_HASH": srv.hash_password(PW, 1000), "COOKIE_SECRET": "s" * 40}
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.make_handler(store, env, rapp, engine))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield {"url": f"http://127.0.0.1:{httpd.server_address[1]}", "store": store, "engine": engine,
           "data": data, "var": var, "port": httpd.server_address[1]}
    httpd.shutdown()
    engine.quit.set()


def device(tmp_path, name, url, key=KEY):
    d = tmp_path / name
    (d / "rides").mkdir(parents=True)
    return d, cs.Syncer(str(d), str(d / "rides"), cs.Client(url, key))


def write(path, text, age=100):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    t = time.time() - age
    os.utime(path, (t, t))


def test_parse_code():
    assert cs.parse_code("https://a.example.io#" + KEY) == {"url": "https://a.example.io", "key": KEY}
    assert cs.parse_code("https://a.example.io/#" + KEY)["url"] == "https://a.example.io"
    assert cs.parse_code("https://a.example.io") is None
    assert cs.parse_code("ftp://x#" + KEY) is None
    assert cs.parse_code("") is None


def test_paths():
    assert cs.valid_path("rides/ride_2026-10-19_08-17-06_per_second.csv")
    assert cs.valid_path("workouts/B2-Z2-60min.zwo") and cs.valid_path("mornings/2026-10-18_ecg.csv")
    assert cs.valid_path("settings.json") and cs.valid_path("plan.json")
    for bad in ("rides/app_log.txt", "rides/.cache/x.json", "../etc/passwd", "sync.json", "rides/x.tmp",
                "sync_state.json", "rides/../settings.json", "/settings.json", "left_pedal_address.txt"):
        assert not cs.valid_path(bad), bad


def test_merge_mornings():
    a = [{"date": "2026-10-17", "time": "07:00", "hr": 52}]
    b = [{"date": "2026-10-18", "time": "07:10", "hr": 55}, {"date": "2026-10-17", "time": "07:00", "hr": 52}]
    assert [x["date"] for x in cs.merge_mornings(a, b)] == ["2026-10-17", "2026-10-18"]


def test_two_devices_stay_the_same(tmp_path, server):
    a, sa = device(tmp_path, "a", server["url"])
    b, sb = device(tmp_path, "b", server["url"])
    write(a / "settings.json", json.dumps({"ftp": 240, "max_hr": 198}))
    write(a / "rides" / "ride_1_per_second.csv", "t,power_w\n0,100\n")
    write(a / "rides" / "app_log.txt", "this device only")
    write(a / "workouts" / "B2-Z2-60min.zwo", "<workout_file/>")
    assert sa.sync_once()["state"] == "ok" and sa.status["up"] == 3
    assert not (server["data"] / "rides" / "app_log.txt").exists()
    # the other device gets them
    assert sb.sync_once()["down"] == 3
    assert json.loads((b / "settings.json").read_text())["ftp"] == 240
    assert (b / "rides" / "ride_1_per_second.csv").read_text().startswith("t,power_w")
    # a change on b goes to a; nothing moves when nothing changed
    write(b / "settings.json", json.dumps({"ftp": 249, "max_hr": 198}), age=50)
    sb.sync_once()
    assert sa.sync_once()["down"] == 1 and json.loads((a / "settings.json").read_text())["ftp"] == 249
    assert sa.sync_once()["up"] == 0 and sa.status["down"] == 0
    # a deleted ride is deleted everywhere, and the server keeps it in its history
    os.remove(a / "rides" / "ride_1_per_second.csv")
    sa.sync_once()
    sb.sync_once()
    assert not (b / "rides" / "ride_1_per_second.csv").exists()
    assert list((server["var"] / "history").rglob("ride_1_per_second.csv"))


def test_morning_checks_from_two_devices_are_merged(tmp_path, server):
    a, sa = device(tmp_path, "a", server["url"])
    b, sb = device(tmp_path, "b", server["url"])
    write(a / "morning.json", json.dumps([{"date": "2026-10-17", "time": "07:00"}]))
    sa.sync_once()
    sb.sync_once()
    write(a / "morning.json", json.dumps([{"date": "2026-10-17", "time": "07:00"}, {"date": "2026-10-18", "time": "07:00"}]), 40)
    write(b / "morning.json", json.dumps([{"date": "2026-10-17", "time": "07:00"}, {"date": "2026-10-19", "time": "07:00"}]), 30)
    sa.sync_once()
    sb.sync_once()
    sa.sync_once()
    for d in (a, b):
        assert [x["date"] for x in json.loads((d / "morning.json").read_text())] == ["2026-10-17", "2026-10-18", "2026-10-19"]


def test_new_device_takes_the_servers_settings_and_keeps_its_own_aside(tmp_path, server):
    a, sa = device(tmp_path, "a", server["url"])
    write(a / "settings.json", json.dumps({"ftp": 240}))
    sa.sync_once()
    w, sw = device(tmp_path, "windows", server["url"])
    write(w / "settings.json", json.dumps({"ftp": 199}), age=10)        # newer, but this device never synced
    sw.sync_once()
    assert json.loads((w / "settings.json").read_text())["ftp"] == 240
    assert list((w / "sync_conflicts").rglob("settings.json"))
    assert json.loads(server["store"].path("settings.json") and open(server["store"].path("settings.json")).read())["ftp"] == 240


def test_a_ride_being_recorded_waits(tmp_path, server):
    a, sa = device(tmp_path, "a", server["url"])
    write(a / "rides" / "ride_9_raw.csv", "x")
    sa.busy = lambda: "ride_9"
    sa.sync_once()
    assert "rides/ride_9_raw.csv" not in server["store"].manifest()["files"]
    sa.busy = lambda: None
    sa.sync_once()
    assert "rides/ride_9_raw.csv" in server["store"].manifest()["files"]


def test_many_missing_files_are_not_deleted_on_the_server(tmp_path, server):
    a, sa = device(tmp_path, "a", server["url"])
    for i in range(30):
        write(a / "rides" / f"ride_{i}_per_second.csv", str(i))
    sa.sync_once()
    for i in range(30):
        os.remove(a / "rides" / f"ride_{i}_per_second.csv")         # a wrong folder, a wiped disk...
    sa.sync_once()
    assert len(server["store"].manifest()["files"]) == 30


def test_wrong_key_and_stale_base(tmp_path, server):
    _, bad = device(tmp_path, "x", server["url"], key="w" * 40)
    assert bad.sync_once()["state"] == "error"
    c = cs.Client(server["url"], KEY)
    assert c.put("plan.json", b"{}", cs.hashlib.sha256(b"{}").hexdigest(), "", 0)
    assert not c.put("plan.json", b"[]", cs.hashlib.sha256(b"[]").hexdigest(), "", 0)      # base is stale: 409
    import urllib.error
    with pytest.raises(urllib.error.HTTPError):
        c.put("plan.json", b"[]", "0" * 64, cs.hashlib.sha256(b"{}").hexdigest(), 0)      # checksum mismatch


def _req(server, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", server["port"], timeout=10)
    conn.request(method, path, body=body, headers=headers or {})
    r = conn.getresponse()
    return r.status, dict(r.getheaders()), r.read()


def test_web_view_needs_the_password_and_only_looks(tmp_path, server):
    assert _req(server, "GET", "/")[0] == 303
    assert _req(server, "GET", "/api/rides")[0] == 401
    assert _req(server, "GET", "/sync/manifest")[0] == 401
    st, _, _ = _req(server, "POST", "/login", "password=nope", {"Content-Type": "application/x-www-form-urlencoded"})
    assert st == 401
    st, h, _ = _req(server, "POST", "/login", "password=" + PW.replace(" ", "+"),
                    {"Content-Type": "application/x-www-form-urlencoded"})
    assert st == 303 and "HttpOnly" in h["Set-Cookie"] and "Secure" in h["Set-Cookie"]
    cookie = {"Cookie": h["Set-Cookie"].split(";")[0]}
    assert _req(server, "GET", "/", headers=cookie)[0] == 200
    st, _, body = _req(server, "GET", "/api/state?p=0&h=0&a=0&b=0", headers=cookie)
    assert st == 200 and json.loads(body)["viewer"] is True
    # moving between pages works; changing things doesn't
    _req(server, "POST", "/api/action", json.dumps({"name": "nav", "arg": "plan"}), {**cookie, "Content-Type": "application/json"})
    assert server["engine"].screen == "plan"
    _req(server, "POST", "/api/action", json.dumps({"name": "settings", "arg": {"ftp": 300}}), cookie)
    _req(server, "POST", "/api/action", json.dumps({"name": "start"}), cookie)
    assert server["engine"].settings.get("ftp") is None and server["engine"].session is None
    assert not (server["data"] / "settings.json").exists()
    forged = {"Cookie": "asess=9999999999.deadbeef"}
    assert _req(server, "GET", "/api/rides", headers=forged)[0] == 401


def test_web_view_follows_uploads(tmp_path, server):
    a, sa = device(tmp_path, "a", server["url"])
    write(a / "settings.json", json.dumps({"ftp": 240, "max_hr": 198}))
    sa.sync_once()
    assert server["engine"].settings["ftp"] == 240


def test_app_engine_applies_downloads_and_reloads(tmp_path):
    from test_app import FakeWorker
    folder = tmp_path / "app"
    (folder / "rides").mkdir(parents=True)
    write(folder / "settings.json", json.dumps({"ftp": 199}))
    e = app.Engine(str(folder / "rides"), app.load_settings(str(folder / "settings.json")), FakeWorker,
                   str(folder / "settings.json"))
    tmp = folder / "settings.json.part"
    tmp.write_text(json.dumps({"ftp": 240, "max_hr": 198}))
    v = e.rides_version
    e._do("sync_apply", [("settings.json", str(tmp), str(folder / "settings.json"))])
    assert e.settings["ftp"] == 240 and e.settings["max_hr"] == 198 and not tmp.exists()
    e._do("sync_apply", [("rides/ride_1_per_second.csv", None, str(folder / "rides" / "nothing"))])
    assert e.rides_version == v + 1


def test_sync_code_from_the_home_screen(tmp_path):
    from test_app import FakeWorker
    folder = tmp_path / "app"
    (folder / "rides").mkdir(parents=True)
    e = app.Engine(str(folder / "rides"), dict(app.SETTINGS_DEFAULTS), FakeWorker, str(folder / "settings.json"))
    e._do("sync_code", "not a code")
    assert not (folder / "sync.json").exists() and "isn't" in e._sync_view()["msg"]
    e._do("sync_code", "http://127.0.0.1:9#" + KEY)
    assert json.loads((folder / "sync.json").read_text()) == {"url": "http://127.0.0.1:9", "key": KEY}
    assert e._sync_view()["on"] is True
    assert "sync.json" not in json.dumps(e.settings) and KEY not in json.dumps(e._snapshot()["settings"])
    e._do("sync_code", "")
    assert not (folder / "sync.json").exists() and e._sync_view()["on"] is False
