"""The 20-min test's live guide (pacing.py) and the engine wiring it to the ftp-20 workout."""
from datetime import timedelta

import pacing
import recorder_app as app
import workout as wk
from test_app import FakeWorker


def _test(t, hr=None, power=280, dur=1200):
    return [power] * t, [hr] * t if hr is not None else [None] * t


def test_phases_settle_then_verdicts_then_everything():
    p, h = _test(60, 165)
    assert pacing.guide(p, h, 1200, 280, 201)["cue"]["kind"] == "settle"       # first 3 min: no verdicts
    p, h = _test(600, 178)                                                       # in range, flat
    assert pacing.guide(p, h, 1200, 280, 201)["cue"]["text"] == "Hold"
    p, h = _test(1100, 185)
    assert pacing.guide(p, h, 1200, 280, 201)["cue"]["text"] == "Everything"
    p, h = _test(1180, 190)
    assert pacing.guide(p, h, 1200, 280, 201)["cue"]["text"] == "Empty the tank"


def test_heart_rate_decides_up_and_ease():
    p, h = _test(600, 160)                                                       # well under the range, flat
    assert pacing.guide(p, h, 1200, 280, 201)["cue"]["kind"] == "up"
    p = [280] * 600
    h = [170 + 0.05 * i for i in range(600)]                                     # 190 at 10 min, +3/min
    g = pacing.guide(p, h, 1200, 280, 201)
    assert g["cue"]["kind"] == "down" and g["trend"] > 2.5
    p, h = _test(1000, 175)                                                      # 15-18 min with room: + if you can
    assert pacing.guide(p, h, 1200, 280, 201)["cue"]["text"] == "+2 % if you can"
    assert pacing.guide(*_test(600, 160), 1200, 280, 201, since_change=30)["cue"]["text"] == "Hold"   # wait after a change


def test_falling_under_the_target_says_ease_and_no_hr_paces_by_feel():
    p = [280] * 570 + [240] * 30
    assert pacing.guide(p, [178] * 600, 1200, 280, 201)["cue"]["kind"] == "down"
    g = pacing.guide(*_test(600), 1200, 280, 201)
    assert g["cue"]["text"] == "Hold" and "by feel" in g["cue"]["sub"]


def test_projection_counts_the_target_for_the_time_left():
    g = pacing.guide([270] * 600, [175] * 600, 1200, 290, 201)
    assert g["avg"] == 270 and g["proj"] == 280 and g["ftp"] == 266 and g["left"] == 600
    assert g["chart"][0] == [5, 270, 175] and len(g["chart"]) == 120
    assert g["bands"][0][0] == pacing.SETTLE_S and g["lt"] == 170.8


def test_a_verdict_must_stand_15_s_before_it_shows():
    st = {}
    assert pacing.guide(*_test(600, 178), 1200, 280, 201, None, st)["cue"]["text"] == "Hold"
    p, h = _test(601, 178)
    h[-10:] = [160] * 10                                                         # heart rate dips for a moment
    assert pacing.guide(p, h, 1200, 280, 201, None, st)["cue"]["text"] == "Hold"
    for t in range(602, 620):                                                    # and stays down
        p, h = [280] * t, [178] * (t - 80) + [150] * 80
        out = pacing.guide(p, h, 1200, 280, 201, None, st)["cue"]["text"]
    assert out == "+2 %"


def test_engine_guides_the_ftp_20_test_step(tmp_path):
    e = app.Engine(str(tmp_path), {**app.SETTINGS_DEFAULTS, "ftp": 280, "max_hr": 201}, FakeWorker)
    e._do("workout", "ftp-20")
    e._do("start")
    s = e.session
    w = wk.BUILT_IN["ftp-20"]
    pre = sum(st["dur"] for st in w["steps"][:5])                                # warm-up and openers
    for i in range(pre - 30):
        s._second({"time": s.started + timedelta(seconds=i), "power_w": 150, "heart_rate_bpm": 130})
    assert e._pacing(s) == {"pre": 30}                                           # the step before: a countdown
    for i in range(pre - 30, pre + 600):
        s._second({"time": s.started + timedelta(seconds=i), "power_w": 280, "heart_rate_bpm": 178})
    g = e._pacing(s)
    assert g["t"] == 600 and g["target"] == 280 and g["cue"]["text"] == "Hold"
    e._do("wo_nudge", 1)                                                         # you press +
    g = e._pacing(s)
    assert g["target"] == round(280 * 1.01) and "Let the last change" in g["cue"]["sub"]
    assert e._snapshot()["pacing"]["t"] == 600
