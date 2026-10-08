"""Today's readiness (readiness.py), heart rate against power (checks.hr_model) and breathing from
the ECG (breathing.py), on made-up data."""
import math
import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import breathing as br
import checks
import readiness as rd

DAY = date(2026, 10, 9)


def _trend(days, verdict=None):
    return {"days": days, "verdict": verdict}


def _ride(day, **kw):
    return {"source": "Assioma", "start_utc": f"{day}T10:00:00+00:00", **kw}


def test_readiness_takes_the_strongest_signal():
    pmc = {"tsb": [-10.0]}
    good = rd.today(_trend([]), [], pmc, None, DAY)
    assert good["level"] == "go" and any("No morning check" in x["text"] for x in good["reasons"])
    # today's warm-up 9 bpm over normal: easy
    r = [_ride("2026-10-09", warmup={"hr": 135}, knee={"score": None})]
    assert rd.today(_trend([]), r, pmc, {"hr": 126}, DAY)["level"] == "easy"
    # a sore knee yesterday: easy; flared: rest
    assert rd.today(_trend([]), [_ride("2026-10-08", knee={"score": 2})], pmc, None, DAY)["level"] == "easy"
    assert rd.today(_trend([]), [_ride("2026-10-08", knee={"score": 3})], pmc, None, DAY)["level"] == "rest"
    # form
    assert rd.today(_trend([]), [], {"tsb": [-35.0]}, None, DAY)["level"] == "easy"
    assert rd.today(_trend([]), [], {"tsb": [-45.0]}, None, DAY)["level"] == "rest"
    # the morning check against your normal
    m = {"date": "2026-10-09", "ln_rmssd": 4.0, "ln7": 4.0, "lo": 4.2, "hi": 4.6, "base": 4.4, "hr": 60, "hr_base": 58}
    assert rd.today(_trend([m], ("below", "...")), [], pmc, None, DAY)["level"] == "easy"
    # while learning, a low morning is a note only
    learn = [{"date": d, "ln_rmssd": 4.8, "hr": 58} for d in ("2026-10-07", "2026-10-08")] + [{"date": "2026-10-09", "ln_rmssd": 4.0, "hr": 59}]
    r = rd.today(_trend(learn), [], pmc, None, DAY)
    assert r["level"] == "go" and any(x["level"] == "note" and "lower" in x["text"] for x in r["reasons"])


def test_a_hard_plan_day():
    assert rd.is_hard({"if": 0.85}) and rd.is_hard({"if": 0.7, "split": {"hard": 3}})
    assert not rd.is_hard({"if": 0.65, "split": {"easy": 10, "hard": 0}})


def _model_ride(tau=60, gain=0.4, base=80, drift_per_h=6, seconds=3600):
    p = [120] * 600
    while len(p) < seconds:
        p += [220] * 240 + [110] * 180
    p = p[:seconds]
    f, hr = 0.0, []
    for i, x in enumerate(p):
        f += (x - f) / tau
        hr.append(base + gain * f + drift_per_h * (i - 600) / 3600)
    return [{"power_w": x, "heart_rate_bpm": h} for x, h in zip(p, hr)]


def test_hr_model_finds_tau_gain_and_hr_at_150():
    m = checks.hr_model(_model_ride())
    assert m["tau"] == 60 and abs(m["gain"] - 0.4) < 0.01 and abs(m["drift"] - 6) < 0.5
    assert abs(m["hr150"] - (80 + 0.4 * 150)) < 0.5 and m["rmse"] < 0.5


def test_a_steady_ride_gives_hr_at_150_only():
    rows = [{"power_w": 148, "heart_rate_bpm": 130 + 3 * i / 3600} for i in range(2400)]
    m = checks.hr_model(rows)
    assert m["tau"] is None and m["gain"] is None and abs(m["hr150"] - (130.5 + 0.4 * 2)) < 0.1   # HR at minute 10, on 150 W
    assert checks.hr_model(rows[:800]) is None                                   # under 15 min


def test_breathing_rate_from_r_peak_heights():
    # 130 Hz ECG, 150 bpm, R heights swinging +-20 % at 30 breaths/min
    ts, vs, rr, beat = [], [], 0.4, 0.0
    for k in range(int(240 * br.RATE)):
        t = k / br.RATE
        ts.append(t)
        vs.append(0.0)
    while beat < 239:
        beat += rr
        amp = 1500 * (1 + 0.2 * math.sin(2 * math.pi * 0.5 * beat))
        i = int(beat * br.RATE)
        for d in range(-3, 4):
            if 0 <= i + d < len(vs):
                vs[i + d] += amp * math.exp(-(d / 1.2) ** 2)
    r = br.rates(ts, vs)
    good = [x for x in r["rate"] if x is not None]
    assert len(good) >= 0.8 * len(r["rate"]) and all(abs(x - 30) <= 1 for x in good)
