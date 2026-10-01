"""VT1 from a ride's alpha 1: synthetic rides with a known threshold."""
import random
from datetime import datetime, timedelta

import vt1

T0 = datetime(2026, 10, 1, 7)


def _ride(blocks, a1_of_hr, noise=0.06, seed=1, gap_after=None):
    """blocks = [(seconds, watts)]; HR follows power (slowly), alpha 1 follows HR plus noise."""
    rnd, rows, hr, t = random.Random(seed), [], 100.0, T0
    for secs, watts in blocks:
        for _ in range(secs):
            hr += ((90 + 0.4 * watts) - hr) / 40
            rows.append({"time": t, "power_w": float(watts), "heart_rate_bpm": hr,
                         "dfa_a1": a1_of_hr(hr) + rnd.gauss(0, noise), "rr_artifact_pct": 0.0})
            t += timedelta(seconds=1)
            if gap_after is not None and len(rows) == gap_after:
                t += timedelta(minutes=10)                  # a pause
    return rows


def linear(vt1_hr, slope=0.03):
    return lambda hr: max(0.3, min(1.6, 0.75 - slope * (hr - vt1_hr)))


def test_a_ramp_like_ride_finds_the_threshold():
    blocks = [(600, 100)] + [(300, w) for w in range(120, 241, 10)]   # 10 min easy, then 5-min steps
    e = vt1.estimate(_ride(blocks, linear(160)))
    hr = e["hr"]
    assert hr["kind"] == "cross" and abs(hr["value"] - 160) < 2.5
    assert hr["lo"] <= hr["value"] <= hr["hi"] and hr["hi"] - hr["lo"] < 8 and hr["p_cross"] > 0.9


def test_an_easy_ride_only_gives_a_lower_bound():
    e = vt1.estimate(_ride([(3600, 140)], linear(170)))
    assert e["hr"]["kind"] == "above" and e["hr"]["value"] < 170 and "lo" not in e["hr"]


def test_a_crossing_inside_a_gap_is_a_range_not_a_number():
    # easy and hard steady blocks only: no steady power between 120 and 240 W
    e = vt1.estimate(_ride([(1500, 120), (1200, 240)], lambda hr: 1.3 if hr < 150 else 0.5, noise=0.03))
    p = e["power"]
    assert p["kind"] == "between" and p["value"] <= 125 and p["value2"] >= 235 and "lo" not in p


def test_warm_up_unsteady_windows_and_pauses_are_left_out():
    rows = _ride([(600, 100), (1800, 150)], linear(170))
    assert all(s["i"] >= vt1.WARMUP_S for s in vt1.samples(rows))
    surges = _ride([(600, 100)] + [(20, 300), (20, 80)] * 60, linear(170))       # 20-s on / off
    assert [s for s in vt1.samples(surges) if s["i"] >= 600 + vt1.WINDOW_S] == []   # once past the steady warm-up
    paused = _ride([(1800, 150)], linear(170), gap_after=1200)
    near = [s for s in vt1.samples(paused) if 1200 <= s["i"] < 1200 + vt1.WINDOW_S - 1]
    assert near == []


def test_too_little_alpha_1_gives_nothing_and_results_repeat():
    assert vt1.estimate(_ride([(900, 150)], linear(160))) is None           # 5 min after warm-up
    rows = _ride([(600, 100)] + [(300, w) for w in range(120, 241, 10)], linear(160))
    assert vt1.estimate(rows) == vt1.estimate(rows)                         # seeded bootstrap
