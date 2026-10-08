"""W' balance (wbal.py) and the ECG check of set-aside beats (ecg_beats.py), on made-up data."""
import math
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import ecg_beats as eb
import hrv
import wbal

CP, WP = 200.0, 15000.0


def test_balance_drains_above_cp_and_refills_below():
    b = wbal.Balance(CP, WP)
    for _ in range(60):
        w = b.add(300)                       # 100 W over CP for a minute: 6 kJ gone
    assert w == WP - 6000
    for _ in range(600):
        w = b.add(100)                       # easy: refills, fast at first, never past full
    assert WP - 6000 < w < WP
    assert b.add(None) <= WP                 # not pedalling counts as 0 W: refilling


def test_ramp_w_prime_is_the_work_above_cp_over_the_last_climb():
    p = [100] * 300 + [150 + i for i in range(120)]        # climbs past CP at 200 W ...
    p += [0] * 60                                          # ... then stops (cool-down left out)
    above = sum(v - CP for v in p if v > CP)
    w = wbal.ramp_w_prime(p, CP)
    # the climb starts where the 30-s power crossed CP: work above CP from there on
    assert w is not None and abs(w - above) < 1e-6 or w is None and above < wbal.MIN_W_PRIME


def test_ramp_w_prime_needs_a_real_ramp():
    assert wbal.ramp_w_prime([150] * 600, CP) is None                    # never over CP
    long_ramp = [100 + i for i in range(400)]                            # ends at 499 W: W' ~ 45 kJ
    assert wbal.ramp_w_prime(long_ramp, CP) is None                      # outside 3-40 kJ: not believed


def test_efforts_are_each_rep_with_its_cost():
    p = [120] * 600
    for _ in range(3):
        p += [230] * 180 + [120] * 180
    s = wbal.summary(p, CP, WP)
    assert len(s["efforts"]) == 3
    for e in s["efforts"]:
        assert e["end"] - e["start"] == 180 and e["power"] == 230
        assert abs(e["spent"] - 30 * 180) < 1                    # 30 W over CP for 3 min
    # each rep starts with less than a full reserve, so ends lower than the one before
    lows = [e["low"] for e in s["efforts"]]
    assert lows[0] > lows[1] > lows[2] > 0 and round(s["low"]) == min(s["series"])
    assert s["above_cp_s"] == 540 and abs(s["above_cp_kj"] - 3 * 5.4) < 1e-9
    assert wbal.summary(p, None, WP) is None and wbal.summary(p, CP, None) is None


# --------------------------------------------------------------------------- ECG
def _ecg(r_times, length):
    """A clean ECG at 130 Hz: a narrow spike (QRS) at each R time, a small T wave after it."""
    ts = [i / eb.RATE for i in range(int(length * eb.RATE))]
    vs = []
    for t in ts:
        v = 0.0
        for r in r_times:
            d = t - r
            if -0.1 < d < 0.4:
                v += 1500 * math.exp(-(d / 0.012) ** 2) + 200 * math.exp(-((d - 0.22) / 0.05) ** 2)
        vs.append(v)
    return ts, vs


def _beats(rr_list):
    tr = hrv.HrvTracker()
    t = datetime(2026, 10, 6, 8, 0, 0)
    for rr in rr_list:
        t += timedelta(milliseconds=rr)
        tr.add(t, [rr])
    tr._judge(final=True)
    t0 = datetime(2026, 10, 6, 8, 0, 0)
    return tr.beats, [(b.time - t0).total_seconds() for b in tr.beats]


def _rr_pattern():
    rr = [400 + 6 * math.sin(k / 3) for k in range(60)]
    rr[30], rr[31] = 280, 520                                 # an early beat and the pause after it
    return rr


def test_a_real_early_beat_is_confirmed_by_the_ecg():
    rr = _rr_pattern()
    r_times = [sum(rr[:k + 1]) / 1000 for k in range(len(rr))]
    beats, bt = _beats(rr)
    res = eb.check(beats, bt, _ecg(r_times, r_times[-1] + 1))
    assert res["checked"] >= 1 and len(res["early"]) == 1
    e = res["early"][0]
    assert abs(e["short"] - 280) < 1 and abs(e["long"] - 520) < 1 and e["shape"] > 0.9
    assert abs(e["t"] - r_times[30]) < 0.02


def test_a_strap_mistiming_is_not_an_early_beat():
    rr = _rr_pattern()
    beats, bt = _beats(rr)                                     # the strap says early beat ...
    steady = [400 + 6 * math.sin(k / 3) for k in range(60)]   # ... the heart beat regularly
    r_times = [sum(steady[:k + 1]) / 1000 for k in range(len(steady))]
    res = eb.check(beats, bt, _ecg(r_times, r_times[-1] + 1))
    assert res["early"] == [] and res["mistimed"] >= 1
