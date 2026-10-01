"""Durability, the Fitness check and the heat model (checks.py)."""
from datetime import datetime, timedelta

import pytest

import checks
import training as tr
import workout as wk

T0 = datetime(2026, 10, 2, 7)


def _rows(power, hr, a1=None):
    return [{"time": T0 + timedelta(seconds=i), "power_w": p, "heart_rate_bpm": h, "dfa_a1": (a1(i) if a1 else None)}
            for i, (p, h) in enumerate(zip(power, hr))]


def test_durability_sees_heart_rate_drift_at_the_same_power():
    n = 90 * 60
    steady = _rows([150] * n, [130] * n, lambda i: 1.0)
    d = checks.durability(steady)
    assert d["score"] == "good" and abs(d["drift_pct"]) < 1e-9 and d["onset_min"] is None
    drifting = _rows([150] * n, [130 + 15 * i / n for i in range(n)], lambda i: 1.1 - 0.4 * i / n)
    d = checks.durability(drifting)
    assert d["drift_pct"] > 6 and d["a1_drop"] > 0.2 and d["score"] == "poor" and d["onset_min"] is not None
    assert checks.durability(_rows([150] * 1800, [130] * 1800)) is None          # too short


def _check_ride(hr_at_w, recovery_drop=25):
    w = wk.BUILT_IN["vt1-steps"]                                # Threshold steps: the weekly check
    power, hr = [], []
    for st in w["steps"]:
        watts = st["lo"] * 200
        for k in range(st["dur"]):
            power.append(watts)
            hr.append(hr_at_w(watts))
    # recovery: heart rate falls over the first minute of the recovery step
    rec = next(i for i, st in enumerate(w["steps"]) if st["label"] == "recovery")
    start = sum(st["dur"] for st in w["steps"][:rec])
    peak = hr[start - 1]
    for k in range(180):
        hr[start + k] = peak - min(recovery_drop, recovery_drop * k / 60)
    meta = {"workout": {"id": "vt1-steps", "steps": [[st["dur"], st["lo"], st["hi"], st["label"]] for st in w["steps"]]}}
    return _rows(power, hr), meta


def test_fitness_check_line_and_recovery():
    rows, meta = _check_ride(lambda w: 60 + 0.5 * w)
    c = checks.fitness_check(rows, meta)
    assert [round(s["power"]) for s in c["steps"]] == [120, 130, 140, 150, 160]   # the steps up to 80 %
    assert c["hr_b"] == pytest.approx(0.5) and checks.hr_at(c, 140) == pytest.approx(130)
    assert c["hrr60"] == pytest.approx(25, abs=0.6)
    assert checks.fitness_check(rows[:900], meta) is None                         # stopped early
    assert checks.fitness_check(rows, {"workout": {"id": "vo2-5x4"}}) is None


def test_heat_model_learns_bpm_per_degree_and_corrects_the_trend():
    rides = []
    for k, temp in enumerate((18, 20, 22, 24, 26, 19)):
        rows, meta = _check_ride(lambda w, t=temp: 60 + 0.5 * w + 1.5 * (t - 20))
        c = checks.fitness_check(rows, meta)
        rides.append({"id": f"r{k}", "start_utc": (T0 + timedelta(days=k)).isoformat() + "+00:00",
                      "temp_c": temp, "check": {**c, "steps": c["steps"]}})
    f = tr.fitness_checks(rides)
    assert f["heat"]["bpm_per_c"] == pytest.approx(1.5) and f["ref_w"] == 140
    assert all(x["hr_ref_20c"] == pytest.approx(130) for x in f["checks"])        # the same fitness, any room
    assert checks.heat_model([(20, 120)] * 6) is None                             # no temperature range
