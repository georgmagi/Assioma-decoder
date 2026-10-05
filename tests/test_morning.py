"""The morning check: resting HR and ln RMSSD lying down, read against your own normal."""
import math
import random
from datetime import date, datetime, timedelta, timezone

import ant_hr
import morning as mo
import recorder_app as app
from test_app import FakeWorker

T0 = datetime(2026, 10, 3, 6, 30)


def _beats(check, rr_ms, start, artifacts=0):
    t = start
    for k, rr in enumerate(rr_ms):
        t += timedelta(milliseconds=rr)
        bad = k % 3 == 0 and k < 3 * artifacts                 # scattered single bad beats, as a loose strap gives
        check.feed(t, 60000 / rr, [rr * (1.6 if bad else 1.0)])
    return t


def test_a_good_check_gives_resting_hr_and_ln_rmssd():
    rnd = random.Random(1)
    rr = [1000 + 40 * (-1) ** k + rnd.gauss(0, 5) for k in range(200)]       # ~60 bpm, alternating 80 ms
    c = mo.Check(T0)
    _beats(c, rr, T0)
    r = c.result(T0 + timedelta(seconds=200))
    assert r["ok"] and 55 < r["hr"] < 65 and 70 < r["rmssd"] < 90 and abs(r["ln_rmssd"] - math.log(r["rmssd"])) < 0.01
    assert r["beats"] < 200                                                  # the first minute is settling


def test_too_few_beats_or_too_many_artifacts_say_why():
    c = mo.Check(T0)
    _beats(c, [1000] * 70, T0)                                              # 10 s after settling
    assert not c.result(T0)["ok"] and "beats" in c.result(T0)["why"]
    c = mo.Check(T0)
    rr = [1000 + 30 * (-1) ** k for k in range(200)]
    t = _beats(c, rr[:60], T0)
    _beats(c, rr[60:], t, artifacts=40)
    assert not c.result(T0)["ok"] and "artifact" in c.result(T0)["why"]


def _hist(values, hr=55, start=date(2026, 9, 1)):
    return [{"date": (start + timedelta(days=k)).isoformat(), "time": "06:30", "hr": hr, "rmssd": math.exp(v),
             "ln_rmssd": v, "beats": 120, "artifact_pct": 0} for k, v in enumerate(values)]


def test_trend_learns_then_reads_against_your_normal():
    assert mo.trend(_hist([4.0] * 3))["verdict"][0] == "learning"
    normal = [4.0 + 0.1 * (-1) ** k for k in range(20)]
    assert mo.trend(_hist(normal))["verdict"][0] == "normal"
    low = mo.trend(_hist(normal + [3.6] * 6))
    assert low["verdict"][0] == "below" and low["days"][-1]["ln7"] < low["days"][-1]["lo"]
    hot = _hist(normal) + [{**_hist([4.0])[0], "date": "2026-09-21", "hr": 62}]
    assert mo.trend(hot)["verdict"][0] == "below" and "heart rate" in mo.trend(hot)["verdict"][1]
    again = mo.add(_hist(normal), {"ok": True, **_hist([3.0], start=date(2026, 9, 20))[0]})
    assert len(again) == 20 and again[-1]["ln_rmssd"] == 3.0                    # same day: replaced


def test_engine_runs_a_check_and_keeps_it(tmp_path):
    e = app.Engine(str(tmp_path), {**app.SETTINGS_DEFAULTS, "ftp": 200}, FakeWorker, str(tmp_path / "settings.json"))
    e._do("morning_start")
    assert e.mcheck is not None
    start = app.utcnow() - timedelta(seconds=200)
    e.mcheck.started = start
    t = start + timedelta(seconds=61)
    for k in range(140):
        rr = 950 + 35 * (-1) ** k
        t += timedelta(milliseconds=rr)
        e._home_feed(ant_hr.hr_notification(t, 63, [rr]))
    e._morning_tick(app.utcnow())
    assert e.mcheck is None and e.mresult["ok"] and e.morning_version == 1
    saved = mo.load(str(tmp_path / "morning.json"))
    assert len(saved) == 1 and saved[0]["hr"] == 63
    e._do("morning_start"); e._do("morning_cancel")
    assert e.mcheck is None
