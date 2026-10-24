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
        bad = k % 3 == 0 and k < 3 * artifacts                 # scattered missed beats, as a loose strap gives
        check.feed(t, 60000 / rr, [rr * (2.0 if bad else 1.0)])
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
    _beats(c, [1000] * 50, T0)                                              # 20 s after settling
    assert not c.result(T0)["ok"] and "beats" in c.result(T0)["why"]
    c = mo.Check(T0)
    rr = [1000 + 30 * (-1) ** k for k in range(160)]
    _beats(c, rr, T0, artifacts=60)                                         # every 3rd beat doubled, throughout
    assert not c.result(T0)["ok"] and "missed or doubled" in c.result(T0)["why"]


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
    assert len(saved) == 1 and abs(saved[0]["hr"] - 63.2) < 0.3          # from the beats: 60000 / 950 ms
    e._do("morning_start"); e._do("morning_cancel")
    assert e.mcheck is None


def _breathing(amp, period_s, seconds=100, seed=1):
    """Resting beats swung by breathing: amp ms either way, one breath every period_s."""
    rnd, rr, t = random.Random(seed), [], 0.0
    while t < seconds:
        x = 1000 + amp * math.sin(2 * math.pi * t / period_s) + rnd.gauss(0, 10)
        rr.append(x)
        t += x / 1000
    return rr


def test_big_calm_breathing_swings_are_not_artifacts():
    # slow deep breaths at rest swing beats +-250 ms: the ride's 20 % rule threw out ~40 % of them
    rr = _breathing(250, 10)
    c = mo.Check(T0)
    _beats(c, rr, T0)
    r = c.result(T0)
    measured = r["rr"]
    true = math.sqrt(sum((b - a) ** 2 for a, b in zip(measured, measured[1:])) / (len(measured) - 1))
    assert r["ok"] and r["artifact_pct"] == 0 and abs(r["rmssd"] - true) < 0.5


def test_missed_and_extra_beats_are_still_caught():
    rr = _breathing(150, 6)
    rr[60] = rr[60] + rr[61]; del rr[61]                                   # a missed beat
    rr[80:81] = [rr[80] * 0.45, rr[80] * 0.55]                             # an extra beat (a split)
    bad = mo.rest_artifacts(rr)
    assert bad[60] and bad[80] and bad[81] and sum(bad) <= 4


def test_a_noisy_first_minute_measures_on_for_a_clean_one():
    rr = [1000 + 30 * (-1) ** k for k in range(200)]
    c = mo.Check(T0)
    start = T0
    t = _beats(c, rr[:80], start, artifacts=27)                           # settle + a noisy first minute
    assert c.phase(T0 + timedelta(seconds=SETTLE + 61)) == "more"
    _beats(c, rr[80:], t)                                                  # then a clean one
    assert c.phase(T0 + timedelta(seconds=SETTLE + 121)) == "done"
    r = c.result(T0)
    assert r["ok"] and r["artifact_pct"] < 5


def test_the_time_saved_is_local_time():
    c = mo.Check(datetime(2026, 10, 7, 6, 0))                              # 06:00 UTC
    _beats(c, [1000 + 30 * (-1) ** k for k in range(120)], c.started)
    r = c.result(c.started)
    local = datetime(2026, 10, 7, 6, 0, tzinfo=timezone.utc).astimezone()
    assert r["time"] == local.strftime("%H:%M") and r["date"] == local.date().isoformat()


SETTLE = mo.SETTLE_S


def test_with_the_ecg_an_early_beat_is_named_and_the_minute_kept(tmp_path):
    from test_wbal_ecg import _ecg
    rr = [1000 + 40 * math.sin(k / 2) for k in range(100)]
    rr[70], rr[71] = 640, 1360                                            # an early beat and its pause
    c = mo.Check(T0)
    _beats(c, rr, T0)
    r_times = [sum(rr[:k + 1]) / 1000 for k in range(len(rr))]
    ts, vs = _ecg(r_times, r_times[-1] + 1)
    c.add_ecg([(T0 + timedelta(seconds=t), v) for t, v in zip(ts, vs)])
    r = c.result(T0)
    assert r["ok"] and len(r["odd"]["early"]) == 1 and r["odd"]["early"][0]["short"] == 640
    assert r["artifact_pct"] > 0 and r["_ecg"] and abs(r["rmssd"] - mo.rest_rmssd(r["rr"], mo.rest_artifacts(r["rr"]))) < 0.1
    saved = mo.add([], r)
    assert "_ecg" not in saved[0] and saved[0]["odd"]["early"]
    path = str(tmp_path / "morning.json")
    mo.save_ecg(path, r["date"], r["_ecg"])
    lines = open(tmp_path / "mornings" / f"{r['date']}_ecg.csv").read().splitlines()
    assert lines[0] == "s,uV" and len(lines) > 60 * 130


def test_without_the_ecg_it_still_works():
    c = mo.Check(T0)
    _beats(c, [1000 + 30 * (-1) ** k for k in range(120)], T0)
    r = c.result(T0)
    assert r["ok"] and r["odd"] is None and r["_ecg"] is None


def test_a_long_pause_the_ecg_shows_is_real_counts():
    from test_wbal_ecg import _ecg
    rr = [850 + 40 * math.sin(k / 2) for k in range(100)]
    rr[70] = 1550                                                         # a long vagal pause: 1.8x, no hidden beat
    c = mo.Check(T0)
    _beats(c, rr, T0)
    assert mo.rest_artifacts(c.result(T0)["rr"])[c.result(T0)["rr"].index(1550)]   # by timing alone: "missed"
    r_times = [sum(rr[:k + 1]) / 1000 for k in range(len(rr))]
    ts, vs = _ecg(r_times, r_times[-1] + 1)
    c.add_ecg([(T0 + timedelta(seconds=t), v) for t, v in zip(ts, vs)])
    r = c.result(T0)
    assert r["odd"]["real"] == 1 and r["artifact_pct"] == 0
    assert abs(r["rmssd"] - mo.rest_rmssd(r["rr"], [False] * len(r["rr"]))) < 0.1


def test_the_watchs_nights_join_the_morning_and_speak_on_days_without_a_check():
    nights = {"days": {f"2026-09-{d:02d}": {"resting_hr": 50, "sleep_hr": {"low30": 50 + (d % 2), "avg": 54}}
                       for d in range(1, 11)}}
    nights["days"]["2026-09-11"] = {"resting_hr": 58, "sleep_hr": {"low30": 57, "avg": 61}}
    t = mo.trend([], date(2026, 9, 11), nights)
    assert t["night"]["base"] == 50.5 and t["night"]["up"] == 6.5
    assert t["verdict"][0] == "below" and "Sleeping heart rate 57" in t["verdict"][1]
    calm = mo.trend(_hist([4.0 + 0.1 * (-1) ** k for k in range(11)]), date(2026, 9, 11),
                    {"days": {**nights["days"], "2026-09-11": {"sleep_hr": {"low30": 51}}}})
    assert calm["verdict"][0] == "normal" and calm["today"] is not None      # strap and watch agree
    assert mo.trend(_hist([4.0] * 3), date(2026, 9, 3), {"days": {}})["verdict"][0] == "learning"
    import readiness as rd
    r = rd.today(t, [], {}, None, date(2026, 9, 11))
    assert r["level"] == "easy" and any("Sleeping heart rate 57" in x["text"] for x in r["reasons"])
    assert not any("No morning check" in x["text"] for x in r["reasons"])
