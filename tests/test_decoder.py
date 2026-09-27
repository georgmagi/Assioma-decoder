"""Decoder checks against real frames captured from the pedals."""
import json
import os

import assioma_decoder as d

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def test_vectors():
    for v in json.load(open(os.path.join(HERE, "test_vectors.json"))):
        frames = list(d.SlipDecoder().feed(bytes.fromhex(v["wire_hex"])))
        assert [f.hex(" ").upper() for f in frames] == [v["frame_hex"]]
        p = d.parse_frame(frames[0])
        got = {k: x for k, x in p.items() if k not in ("raw", "counter", "type") and x is not None}
        assert got == v["expected"], v["wire_hex"]


def test_sample_ride_decodes_cleanly():
    rows = d.decode(d.read_nrf_log(os.path.join(ROOT, "samples", "ride_2026-09-26_nrf.csv"), "2026-09-26", 3))
    assert rows and not any("error" in r for r in rows)
    sec = d.per_second(rows)
    assert len(sec) == 405
    assert sum(1 for s in sec if s.get("power_w") is not None) > 350


def test_heart_rate_rr_intervals():
    rows = d.decode(d.read_nrf_log(os.path.join(ROOT, "samples", "hrm200_rr_2026-09-27_nrf.csv"), "2026-09-27", 3))
    rr = [x for r in rows for x in r.get("rr_ms", [])]
    assert len(rr) == 83 and abs(rr[0] - 914.1) < 0.1


def _strap_2hz(hr, seconds=600, seed=0):
    """A strap that notifies every ~0.5 s and repeats the last RR until the next beat
    (seen from the HRM 200 on Windows); rest first, then `hr`."""
    import math
    import random
    rng = random.Random(seed)
    beats, t, x = [], 0.0, 0.0
    while t < seconds:
        h = 70 if t < 180 else hr
        base = 60 / h
        amp = base * (0.07 if h < 80 else 0.01)
        x = 0.9 * x + rng.gauss(0, 1)
        r = round((base + amp * math.sin(2 * math.pi * t / 5) + 0.004 * x + rng.gauss(0, 0.004)) * 1024) / 1024
        t += r
        beats.append((t, r))
    notes, tn, i, last = [], 0.3, 0, [0.0]
    while i < len(beats):
        new = []
        while i < len(beats) and beats[i][0] <= tn:
            new.append(round(beats[i][1] * 1000, 1))
            i += 1
        last = new or last
        notes.append((tn + rng.uniform(0, 0.04), new or last[-1:]))
        tn += rng.uniform(0.46, 0.56)
    return len(beats), notes


def test_rr_repeats_are_dropped():
    for hr in (110, 125, 140):
        n, notes = _strap_2hz(hr)
        dd = d.RRDedup()
        got = sum(len(dd.feed(t, rr)) for t, rr in notes)
        assert abs(got - n) <= 0.005 * n, (hr, got, n)
        naive = sum(len(rr) for _, rr in notes)
        assert naive > 1.1 * n                      # without de-duplication the repeats would count


def test_rr_per_beat_strap_keeps_equal_intervals():
    # one notification per beat: consecutive equal intervals are real and must be kept
    dd = d.RRDedup()
    t, got = 0.0, 0
    for rr in [800.8, 800.8, 812.5, 800.8, 800.8, 800.8, 790.0] * 20:
        t += rr / 1000
        got += len(dd.feed(t + 0.02, [rr]))
    assert got == 140
