"""
Odd beats, checked against the strap's ECG: a real early beat, or the strap mistiming a normal one?

The beat filter (hrv.py) sets aside any interval far from its neighbours: more than 20 % off, or a
short + long pair that adds up to two normal beats (a "misplaced" beat, split evenly). Both happen
when the strap mistimes an R peak, and both happen when the heart really does beat early: an early
(premature) beat comes too soon, and the pause after it makes up the time. From intervals alone the
two can't be told apart. With the Polar's ECG (polar_pmd.py, <ride>_ecg.csv) they can: around each
set-aside beat the R peaks are found in the ECG itself, and if the ECG shows the same short and long
intervals as the strap (within TOL_MS), the beat really came early. If it doesn't, the strap
mistimed it (contact, motion) and the filter was right.

For a confirmed early beat its QRS (the spike, +-60 ms around the R peak) is compared with the
average of the normal beats just before: `shape` is their correlation, 1 = the same. Early beats
from the upper chambers usually keep the normal QRS shape; a clearly different, wider shape points
to the lower chambers. Both are common, and this is a sports strap, not a medical ECG: anything that
comes with symptoms (palpitations, dizziness, chest pain, fainting) is for a doctor.

Only the windows around set-aside beats are searched, so a long ride costs well under a second.
Standard library only.
"""
from __future__ import annotations

import csv
import os
from bisect import bisect_left
from datetime import datetime, timezone
from typing import Optional

RATE = 130                  # Hz, the H10's ECG
TOL_MS = 15                 # ECG and strap intervals agree when within this
WINDOW_S = (6.0, 4.0)       # ECG searched this long before / after the beat
REFRACTORY_S = 0.22         # no two R peaks closer than this (270 bpm)
QRS_S = 0.06                # half-width of the QRS compared for shape
EARLY = 0.85                # an early beat's interval is under this share of the beats around it


def load(path: str, t0: datetime) -> Optional[tuple]:
    """(times in s since t0, uV) from a ride's _ecg.csv, None if there's none. The ride clock comes
    from the first sample's time; later samples follow the strap's own clock (ns), which is exact."""
    if not os.path.exists(path):
        return None
    ts, vs = [], []
    with open(path, newline="") as fh:
        rd = csv.reader(fh)
        next(rd, None)
        first = next(rd, None)
        if not first:
            return None
        start = datetime.fromisoformat(first[0].replace("Z", "+00:00"))
        base = (start - t0.replace(tzinfo=timezone.utc)).total_seconds()
        ns0 = int(first[1])
        ts.append(base)
        vs.append(float(first[2]))
        for row in rd:
            ts.append(base + (int(row[1]) - ns0) / 1e9)
            vs.append(float(row[2]))
    return ts, vs


def _peaks(ts: list, vs: list) -> list:
    """R peaks in a short stretch of ECG: [(time s, index)]. The signal minus its 50-ms running mean
    (removes the drift and the slow T wave), then local maxima of its size above 45 % of the
    stretch's 98th percentile, at least REFRACTORY_S apart, each timed to a fraction of a sample
    by a parabola through it and its neighbours."""
    n = len(vs)
    if n < RATE:
        return []
    h = max(1, int(0.025 * RATE))
    acc, run = 0.0, []
    pre = [0.0]
    for v in vs:
        acc += v
        pre.append(acc)
    hp = []
    for i in range(n):
        a, b = max(0, i - h), min(n, i + h + 1)
        hp.append(abs(vs[i] - (pre[b] - pre[a]) / (b - a)))
    top = sorted(hp)[int(0.98 * (n - 1))]
    if top <= 0:
        return []
    cut, gap = 0.45 * top, int(REFRACTORY_S * RATE)
    cand = [i for i in range(1, n - 1) if hp[i] >= cut and hp[i] >= hp[i - 1] and hp[i] > hp[i + 1]]
    # strongest first, then drop any within the refractory period of a stronger one
    kept: list = []
    for i in sorted(cand, key=lambda k: -hp[k]):
        if all(abs(i - k) > gap for k in kept):
            kept.append(i)
    out = []
    for i in sorted(kept):
        y0, y1, y2 = hp[i - 1], hp[i], hp[i + 1]
        den = y0 - 2 * y1 + y2
        d = 0.5 * (y0 - y2) / den if den else 0.0
        out.append((ts[i] + d * (ts[i + 1] - ts[i]), i))
    return out


def _corr(a: list, b: list) -> Optional[float]:
    n = min(len(a), len(b))
    if n < 3:
        return None
    a, b = a[:n], b[:n]
    ma, mb = sum(a) / n, sum(b) / n
    sab = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    saa = sum((x - ma) ** 2 for x in a)
    sbb = sum((y - mb) ** 2 for y in b)
    return sab / (saa * sbb) ** 0.5 if saa > 0 and sbb > 0 else None


def check(beats: list, beat_t: list, ecg: tuple) -> Optional[dict]:
    """beats: hrv.Beat list (judged), beat_t: their times in s since the ride start, ecg: load().
    Returns {checked, early: [{t, hr, short, long, usual, shape}], real (true but not early),
    mistimed, unclear (the ECG itself too noisy there), real_at (indices of the set-aside beats the
    ECG showed to be ordinary real beats)} or None."""
    ts, vs = ecg
    if not ts:
        return None
    early, mistimed, unclear, real, checked, real_at = [], 0, 0, 0, 0, []
    i, n = 0, len(beats)
    while i < n:
        b = beats[i]
        if not (b.artifact or b.corrected):
            i += 1
            continue
        # the set-aside beat and the one after it (a corrected pair is two beats), as the strap sent them
        j = i + 1 if b.corrected and i + 1 < n and beats[i + 1].corrected else i
        sent = [x.orig for x in beats[i:j + 1]] + ([beats[j + 1].orig] if j == i and j + 1 < n else [])
        around = [x.rr for x in beats[max(0, i - 8):i] if not x.artifact and not x.corrected]
        group = list(range(i, j + 1))
        i = j + 1
        if not around or beat_t[i - 1] - WINDOW_S[0] < ts[0] or beat_t[i - 1] + WINDOW_S[1] > ts[-1]:
            continue
        checked += 1
        usual = sorted(around)[len(around) // 2]
        t_end = beat_t[i - 1]
        a, z = bisect_left(ts, t_end - WINDOW_S[0]), bisect_left(ts, t_end + WINDOW_S[1])
        pk = _peaks(ts[a:z], vs[a:z])
        rr = [(1000 * (q[0] - p[0]), q) for p, q in zip(pk, pk[1:])]
        # the strap's intervals somewhere in the ECG's sequence, one after another
        hit = None
        for k in range(len(rr) - len(sent) + 1):
            if all(abs(rr[k + m][0] - s) <= TOL_MS for m, s in enumerate(sent)):
                hit = k
                break
        if hit is None:
            # the ECG beats around there are all regular: the strap mistimed a normal beat
            if rr and all(abs(x - usual) <= max(TOL_MS * 2, 0.12 * usual) for x, _ in rr):
                mistimed += 1
            else:
                unclear += 1
            continue
        short = sent[0]
        if short >= EARLY * usual or len(sent) < 2 or sent[1] <= short:
            real += 1                                  # real, but not an early beat (a quick swing, a long pause)
            real_at += group
            continue
        # its QRS against the average of the normal beats before it (relative indices in the stretch)
        idx = rr[hit][1][1] + a
        w = int(QRS_S * RATE)
        norm_idx = [p[1] + a for p in pk[:hit] if p[1] + a - w >= 0][-6:]
        shape = None
        if norm_idx and idx + w < len(vs):
            mean = [sum(vs[q + d] for q in norm_idx) / len(norm_idx) for d in range(-w, w + 1)]
            shape = _corr(mean, vs[idx - w: idx + w + 1])
        early.append({"t": round(rr[hit][1][0], 2), "hr": round(60000 / usual), "short": round(short),
                      "long": round(sent[1]), "usual": round(usual), "shape": shape and round(shape, 2)})
        i = max(i, group[0] + 2)                       # the pause after it belongs to it: not a beat of its own
    return {"checked": checked, "early": early, "real": real, "mistimed": mistimed, "unclear": unclear,
            "real_at": real_at}
