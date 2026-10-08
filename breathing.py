"""
Breathing rate from the strap's ECG (ECG-derived respiration, EDR).

Breathing moves the chest and the heart's axis against the electrodes, so the height of the R
peaks rises and falls with every breath. The R peaks' heights, put on an even 4 Hz grid, swing at
the breathing rate; the rate in each minute is the strongest rhythm in that signal between 10 and 60
breaths/min (a Welch spectrum: 30-s Hann-windowed pieces, averaged). The accelerometer was tried and dropped:
pedalling (1.5 Hz and its harmonics) and slow torso sway swamp the breathing in it.

Checked on Georg's data (Oct 2026): at rest (morning checks) EDR and the beat intervals' breathing
swing agree at ~6/min (his slow morning breathing); riding Z2 it reads 26-32/min, steady minute to
minute. Breathing rate rises with effort more faithfully than heart rate (Nicolò et al. 2017), and
its sharp rise marks the second threshold: a second marker beside alpha 1, which runs high for him.

Standard library only; a ride's ECG (~490k samples an hour) takes a second or two, so the result is
cached beside the ride summaries.
"""
from __future__ import annotations

import json
import math
import os
from datetime import datetime, timezone
from typing import Optional

RATE = 130                      # Hz, the H10's ECG
GRID = 4                        # Hz, the R heights resampled
WIN_S, STEP_S = 60, 30          # the rate per minute, every 30 s
BREATHS = [14 + 0.5 * k for k in range(93)]    # breaths/min looked at: 14-60 (riding is rarely slower; under it drift leaks in)
MIN_SHARE = 0.03                # the peak's share of the band's power; under it the minute says nothing


def r_peaks(vs: list) -> list:
    """(index, height) of each R peak: the signal minus its 50-ms running mean, local maxima over
    35 % of the 98th percentile of their 10-s stretch, 220 ms apart at least."""
    n = len(vs)
    h = max(1, int(0.025 * RATE))
    pre = [0.0]
    acc = 0.0
    for v in vs:
        acc += v
        pre.append(acc)
    hp = [0.0] * n
    for i in range(n):
        a, b = max(0, i - h), min(n, i + h + 1)
        hp[i] = vs[i] - (pre[b] - pre[a]) / (b - a)
    out = []
    chunk, gap = 10 * RATE, int(0.22 * RATE)
    last = -gap
    for c0 in range(0, n, chunk):
        seg = [abs(x) for x in hp[c0:c0 + chunk]]
        if len(seg) < RATE:
            continue
        cut = 0.35 * sorted(seg)[int(0.98 * (len(seg) - 1))]
        for k in range(1, len(seg) - 1):
            i = c0 + k
            if seg[k] >= cut and seg[k] >= seg[k - 1] and seg[k] > seg[k + 1]:
                if i - last >= gap:
                    out.append((i, hp[i]))
                    last = i
                elif abs(hp[i]) > abs(out[-1][1]):        # a bigger peak within the gap: it's that one
                    out[-1] = (i, hp[i])
                    last = i
    return out


def _smooth(xs: list, half: int) -> list:
    n, pre, acc = len(xs), [0.0], 0.0
    for v in xs:
        acc += v
        pre.append(acc)
    return [(pre[min(n, i + half + 1)] - pre[max(0, i - half)]) / (min(n, i + half + 1) - max(0, i - half)) for i in range(n)]


def rates(ts: list, vs: list) -> dict:
    """{t: [s], rate: [breaths/min or None]} every STEP_S from an ECG (ts in s on the ride clock)."""
    pk = r_peaks(vs)
    if len(pk) < 60:
        return {"t": [], "rate": []}
    pt = [ts[i] for i, _ in pk]
    ph = [abs(v) for _, v in pk]
    # heights on an even grid, then band-passed: minus an 8-s mean (drift, posture), plus a 0.5-s mean
    g0, g1 = pt[0], pt[-1]
    grid, j = [], 0
    t = g0
    while t <= g1:
        while j + 1 < len(pt) - 1 and pt[j + 1] < t:
            j += 1
        a, b = pt[j], pt[j + 1]
        f = 0.0 if b == a else min(1.0, max(0.0, (t - a) / (b - a)))
        grid.append(ph[j] + f * (ph[j + 1] - ph[j]))
        t += 1 / GRID
    slow = _smooth(grid, 4 * GRID)
    sig = _smooth([x - s for x, s in zip(grid, slow)], GRID // 4)
    out_t, out_r = [], []
    w, sub = WIN_S * GRID, 30 * GRID
    hann = [0.5 - 0.5 * math.cos(2 * math.pi * k / (sub - 1)) for k in range(sub)]
    freqs = [b / 60 for b in BREATHS]
    tables = [([math.cos(2 * math.pi * f * k / GRID) for k in range(sub)],
               [math.sin(2 * math.pi * f * k / GRID) for k in range(sub)]) for f in freqs]
    for start in range(0, len(sig) - w + 1, STEP_S * GRID):
        # Welch: 30-s pieces overlapping by half, each Hann-windowed, their power spectra averaged
        power = [0.0] * len(freqs)
        for s0 in range(start, start + w - sub + 1, sub // 2):
            seg = sig[s0:s0 + sub]
            m = sum(seg) / sub
            seg = [(x - m) * hk for x, hk in zip(seg, hann)]
            for k, (cs, sn) in enumerate(tables):
                re = sum(x * c for x, c in zip(seg, cs))
                im = sum(x * c for x, c in zip(seg, sn))
                power[k] += re * re + im * im
        top = max(range(len(power)), key=power.__getitem__)
        share = power[top] / sum(power) if sum(power) > 0 else 0
        # a clear peak: a breathing rhythm; a flat spectrum (noise, moving about) says nothing
        out_t.append(round(g0 + (start + w / 2) / GRID))
        out_r.append(BREATHS[top] if share >= MIN_SHARE and 0 < top < len(power) - 1 else None)
    return {"t": out_t, "rate": out_r}


def load_ecg(path: str, t0: datetime) -> Optional[tuple]:
    import ecg_beats
    return ecg_beats.load(path, t0)


def for_ride(folder: str, ride_id: str, t0: datetime, cache_dir: Optional[str] = None) -> Optional[dict]:
    """The ride's breathing rate (rates()) from its _ecg.csv, cached by the file's time; None without ECG."""
    path = os.path.join(folder, ride_id + "_ecg.csv")
    if not os.path.exists(path):
        return None
    cache = os.path.join(cache_dir, ride_id + "_breathing.json") if cache_dir else None
    mtime = os.path.getmtime(path)
    if cache and os.path.exists(cache):
        try:
            with open(cache) as fh:
                got = json.load(fh)
            if got.get("mtime") == mtime:
                return got["breathing"]
        except (OSError, ValueError, KeyError):
            pass
    ecg = load_ecg(path, t0)
    if not ecg:
        return None
    res = rates(*ecg)
    if cache:
        try:
            with open(cache, "w") as fh:
                json.dump({"mtime": mtime, "breathing": res}, fh)
        except OSError:
            pass
    return res
