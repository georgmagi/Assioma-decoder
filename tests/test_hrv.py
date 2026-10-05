"""DFA alpha 1, artifact filtering and the live/offline HRV tracker."""
import os
import random
from datetime import datetime, timedelta

import assioma_decoder as d
import hrv

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _series(kind, n=300, seed=1):
    rng = random.Random(seed)
    x, out = 0.0, []
    for _ in range(n):
        x = x + rng.gauss(0, 1) if kind == "brown" else rng.gauss(0, 1)
        out.append(700 + (x if kind == "brown" else 30 * x))
    return out


def test_dfa_known_exponents():
    # theory: white noise 0.5, brown noise (integrated white) 1.5
    assert abs(hrv.dfa_alpha1(_series("white")) - 0.5) < 0.12
    assert abs(hrv.dfa_alpha1(_series("brown")) - 1.5) < 0.15
    assert hrv.dfa_alpha1([700.0] * 20) is None          # too short


def test_rmssd():
    assert hrv.rmssd([1000, 1010, 1000]) == 10


def test_artifacts_on_real_hrm200_sample():
    rows = d.decode(d.read_nrf_log(os.path.join(ROOT, "samples", "hrm200_rr_2026-09-27_nrf.csv"), "2026-09-27", 3))
    h = hrv.analyse([(r["time"], r["rr_ms"]) for r in rows if r.get("rr_ms")])
    assert len(h.beats) == 83
    assert [round(b.rr) for b in h.beats if b.artifact] == [579, 558]


def _ride(minutes, missed_every=0, gap_at=None):
    """Synthetic strap: one notification per beat, ~125 bpm."""
    rng = random.Random(2)
    t, x, out, k = datetime(2026, 9, 28, 6), 0.0, [], 0
    while t < datetime(2026, 9, 28, 6) + timedelta(minutes=minutes):
        x = 0.9 * x + rng.gauss(0, 1)
        rr = 480 + 4 * x + rng.gauss(0, 3)
        k += 1
        if missed_every and k % missed_every == 0:
            rr *= 2
        t += timedelta(milliseconds=rr)
        if gap_at and gap_at[0] <= (t - datetime(2026, 9, 28, 6)).total_seconds() < gap_at[1]:
            continue
        out.append((t, [round(rr, 1)]))
    return out


def test_tracker_series_and_artifact_rate():
    h = hrv.analyse(_ride(10, missed_every=50))
    ok = [r for r in h.series if r[1] is not None]
    assert len(ok) > 90                                   # every 5 s once 2 min of beats exist
    assert all(0.3 < r[1] < 1.6 for r in ok)
    assert all(1 < r[3] < 4 for r in ok)                  # the doubled beats are caught (~2 %)
    assert ok[0][0] - h.beats[0].time >= timedelta(seconds=hrv.WINDOW_S * 0.9)


def test_live_matches_offline_even_with_bursts():
    beats = _ride(6)
    live = hrv.HrvTracker()
    for i, (t, rr) in enumerate(beats):
        live.add(t, rr)
        if i % 400 == 0 or i == len(beats) - 1:           # bursty updates (e.g. after a reconnect)
            live.update(t)
    off = hrv.analyse(beats)
    assert [r[:2] for r in live.series] == [r[:2] for r in off.series[:len(live.series)]]
    assert len(live.series) >= len(off.series) - 2


def test_dropout_is_not_stitched():
    h = hrv.analyse(_ride(8, gap_at=(200, 260)))
    after = [r for r in h.series if timedelta(seconds=260) < r[0] - h.beats[0].time < timedelta(seconds=300)]
    assert after == []                                    # needs 100 fresh beats after the gap


def test_columns_cover_the_whole_ride():
    h = hrv.analyse(_ride(20))
    t0 = h.beats[0].time
    secs = [t0 + timedelta(seconds=i) for i in range(20 * 60)]
    filled = [bool(h.columns_at(t)) for t in secs]
    assert sum(filled) > 20 * 60 - 140                  # everything after the first 2 min window
    assert h.columns_at(t0 + timedelta(minutes=5))["dfa_a1"] > 0


def test_coverage_flags_beats_the_strap_never_sent():
    """The HRM 200 on Windows sends one interval per ~0.54 s packet: above ~110 bpm beats go missing."""
    from assioma_decoder import RRDedup
    rng = random.Random(3)
    t0 = datetime(2026, 9, 28, 6)
    beats, t = [], 0.0
    for _ in range(3000):                         # 140 bpm
        rr = 428.6 + rng.gauss(0, 5)
        t += rr / 1000
        beats.append((t, rr))
    full = hrv.analyse([(t0 + timedelta(seconds=b), [round(rr, 1)]) for b, rr in beats])
    assert min(full.coverage) > 97
    notes, tn, i, last = [], 0.3, 0, None           # the strap: latest interval only, every ~0.5 s
    while i < len(beats):
        while i < len(beats) and beats[i][0] <= tn:
            last = beats[i][1]
            i += 1
        if last is not None:
            notes.append((tn, [round(last, 1)]))
        tn += rng.uniform(0.449, 0.541)
    d = RRDedup()
    kept = [(t0 + timedelta(seconds=tn), r) for tn, rr in notes if (r := d.feed(tn, rr))]
    seen = hrv.analyse(kept)
    cov = sum(seen.coverage) / len(seen.coverage)
    assert 80 < cov < 92, cov                     # about 86 % of the beats arrive
    assert "rr_coverage_pct" in seen.columns_at(seen.series[-1][0])


def test_a_misplaced_beat_is_split_evenly_not_left_in():
    # 160 bpm, ~3 ms beat-to-beat: one R peak timed 50 ms early (short then long, same sum)
    import random
    from datetime import datetime, timedelta
    rnd = random.Random(3)
    rr = [375 + rnd.gauss(0, 2) for _ in range(300)]
    rr[150] -= 50
    rr[151] += 50
    t, h = datetime(2026, 10, 5, 21), hrv.HrvTracker()
    for x in rr:
        t += timedelta(milliseconds=x)
        h.add(t, [x])
    h._judge(final=True)
    b = h.beats
    assert b[150].corrected and b[151].corrected and abs(b[150].rr - b[151].rr) < 1e-9
    assert sum(x.corrected for x in b) == 2 and not any(x.artifact for x in b)
    assert abs((b[151].time - b[149].time).total_seconds() * 1000 - (rr[150] + rr[151])) < 1e-3


def test_resting_variability_is_left_alone():
    # breathing at rest swings RR by ~±60 ms beat to beat: none of that is a misplaced beat
    import math
    from datetime import datetime, timedelta
    rr = [1000 + 60 * math.sin(2 * math.pi * k / 4.3) for k in range(400)]
    t, h = datetime(2026, 10, 6, 7), hrv.HrvTracker()
    for x in rr:
        t += timedelta(milliseconds=x)
        h.add(t, [x])
    h._judge(final=True)
    assert not any(x.corrected for x in h.beats)
