"""
Beat-to-beat (RR) analysis: artifact filtering, RMSSD and DFA alpha 1.

DFA alpha 1 follows the usual exercise-physiology recipe (Gronwald, Rogers,
Altini): a rolling 2-minute window of RR intervals, artifacts removed,
detrended fluctuation analysis over box sizes 4..16 beats, boxes taken from
both ends of the series. Around 0.75 marks the aerobic threshold (VT1),
around 0.5 the anaerobic threshold. Standard library only.
"""
from __future__ import annotations

import math
from bisect import bisect_right
from datetime import datetime, timedelta
from statistics import median
from typing import Optional

ARTIFACT_THRESHOLD = 0.20   # a beat more than 20 % away from its local median is an artifact
# a misplaced beat (one R peak timed early or late, e.g. the strap shifting under heavy breathing):
# a short interval and a long one that add up to two normal ones. Corrected by splitting the pair
# evenly (Lipponen & Tarvainen 2019), not removed, when each is off by more than 5 % of the local
# median and by more than 3x the neighbours' typical beat-to-beat change, and the sum is within 3 %.
# At 160 bpm with RMSSD ~3 ms a 50 ms shift is far under 20 % but swamps alpha 1 (5 Oct ride: 0.4
# instead of ~0.8).
MISPLACED_MIN = 0.05
MISPLACED_SUM = 0.03
MISPLACED_K = 3.0
MEDIAN_HALF = 5             # local median over 5 beats either side (so a beat is judged 5 beats late)
WINDOW_S = 120              # DFA window (seconds of beats)
STEP_S = 5                  # recompute alpha 1 this often
MIN_BEATS = 100             # below this many clean beats alpha 1 is not reported
SCALES = range(4, 17)
COLUMNS = ["dfa_a1", "rmssd_ms", "rr_artifact_pct", "rr_coverage_pct"]    # added to the per-second CSV


def dfa_alpha1(rr: list) -> Optional[float]:
    """Short-term scaling exponent of an RR series (ms). None if too short."""
    n = len(rr)
    if n < 2 * SCALES[-1]:
        return None
    mean = sum(rr) / n
    y, acc = [], 0.0
    for x in rr:
        acc += x - mean
        y.append(acc)
    log_n, log_f = [], []
    for s in SCALES:
        boxes = n // s
        # detrend each box with a least-squares line; x = 0..s-1 is the same for every box
        xm = (s - 1) / 2
        sxx = sum((i - xm) ** 2 for i in range(s))
        total = 0.0
        for start in [b * s for b in range(boxes)] + [n - (b + 1) * s for b in range(boxes)]:
            seg = y[start:start + s]
            ym = sum(seg) / s
            slope = sum((i - xm) * (v - ym) for i, v in enumerate(seg)) / sxx
            total += sum((v - ym - slope * (i - xm)) ** 2 for i, v in enumerate(seg))
        f = math.sqrt(total / (2 * boxes * s))
        if f <= 0:
            return None
        log_n.append(math.log(s))
        log_f.append(math.log(f))
    mx, my = sum(log_n) / len(log_n), sum(log_f) / len(log_f)
    return sum((a - mx) * (b - my) for a, b in zip(log_n, log_f)) / sum((a - mx) ** 2 for a in log_n)


def rmssd(rr: list) -> Optional[float]:
    d = [(b - a) ** 2 for a, b in zip(rr, rr[1:])]
    return math.sqrt(sum(d) / len(d)) if d else None


class Beat:
    __slots__ = ("time", "rr", "artifact", "corrected", "orig")

    def __init__(self, time: datetime, rr: float) -> None:
        self.time, self.rr, self.artifact = time, rr, None   # artifact: None until judged
        self.corrected = False                                # rr changed by the misplaced-beat fix
        self.orig = rr                                        # the interval as the strap sent it


class HrvTracker:
    """Collects beats live and keeps a rolling alpha 1 / RMSSD.

    add() takes the RR intervals of one heart-rate notification; the last one
    is the beat that just happened, earlier ones are placed back in time.
    """

    def __init__(self) -> None:
        self.beats: list[Beat] = []
        self.series: list[tuple] = []     # (time, alpha1, rmssd, artifact %)
        # share of the window's time covered by the received intervals. Under 100 % means beats never
        # arrived: the HRM 200 on Windows sends one interval per ~0.54 s packet, so above ~110 bpm
        # some beats are simply not sent (15 % at 140 bpm). Parallel to `series`.
        self.coverage: list[float] = []
        self._judged = 0
        self._next: Optional[datetime] = None

    def add(self, time: datetime, rr_ms: list) -> None:
        """Beat times: chained from the previous beat by the intervals themselves (the strap may
        notify on a fixed grid, up to ~0.5 s after the beat), kept within 0.55 s before the
        notification that reported them."""
        total = timedelta(milliseconds=sum(rr_ms))
        prev = self.beats[-1].time if self.beats else None
        # the newest beat happened at most one notification interval (~0.5 s) before it was
        # reported: follow the chain, but never let it fall further behind (e.g. after a
        # dropped beat) or run ahead of the notification
        end = time if prev is None else min(time, max(prev + total, time - timedelta(seconds=0.55)))
        t = end - total
        for rr in rr_ms:
            t += timedelta(milliseconds=rr)
            if 200 <= rr <= 2500:          # anything else is not a heartbeat at all
                self.beats.append(Beat(t, rr))
        self._judge()

    def _misplaced(self, i: int) -> None:
        """Beats i and i+1: a misplaced R peak? Then split the pair evenly (beat i moves in time)."""
        b = self.beats
        if i < MEDIAN_HALF or i + 1 + MEDIAN_HALF >= len(b) or b[i].corrected or b[i + 1].corrected:
            return
        around = [x.rr for x in b[i - MEDIAN_HALF:i]] + [x.rr for x in b[i + 2:i + 2 + MEDIAN_HALF]]
        m = median(around)
        steps = [abs(q.rr - p.rr) for p, q in zip(b[i - MEDIAN_HALF:i], b[i - MEDIAN_HALF + 1:i])]
        steps += [abs(q.rr - p.rr) for p, q in zip(b[i + 2:i + 1 + MEDIAN_HALF], b[i + 3:i + 2 + MEDIAN_HALF])]
        typical = median(steps) if steps else 0.0
        da, db = b[i].rr - m, b[i + 1].rr - m
        if (da * db < 0 and abs(da) > MISPLACED_MIN * m and abs(db) > MISPLACED_MIN * m
                and min(abs(da), abs(db)) > MISPLACED_K * typical and abs(da + db) < MISPLACED_SUM * m):
            half = (b[i].rr + b[i + 1].rr) / 2
            b[i].time += timedelta(milliseconds=half - b[i].rr)
            b[i].rr = b[i + 1].rr = half
            b[i].corrected = b[i + 1].corrected = True

    def _judge(self, final: bool = False) -> None:
        b = self.beats
        while self._judged + (0 if final else MEDIAN_HALF + 2) < len(b):
            i = self._judged
            self._misplaced(i)
            m = median(x.rr for x in b[max(0, i - MEDIAN_HALF):i + MEDIAN_HALF + 1])
            b[i].artifact = abs(b[i].rr - m) > ARTIFACT_THRESHOLD * m
            self._judged += 1

    def update(self, now: Optional[datetime] = None) -> list:
        """Compute alpha 1 every STEP_S seconds of beat time, catching up after a burst of beats.

        Returns the new (time, alpha1, rmssd, artifact %) rows."""
        new = []
        judged = self.beats[:self._judged]
        if not judged:
            return new
        if self._next is None:
            self._next = judged[0].time + timedelta(seconds=STEP_S)
        while self._next <= judged[-1].time:
            t = self._next
            self._next += timedelta(seconds=STEP_S)
            end = bisect_right(judged, t, key=lambda x: x.time)
            win = [x for x in judged[max(0, end - 600):end] if x.time > t - timedelta(seconds=WINDOW_S)]
            # never stitch beats across a dropout (strap lost for > 3 s)
            for i in range(len(win) - 1, 0, -1):
                if (win[i].time - win[i - 1].time).total_seconds() > 3:
                    win = win[i:]
                    break
            clean = [x.rr for x in win if not x.artifact]
            # a full window of beats, recent, and enough of them clean
            if (len(clean) < MIN_BEATS or (t - win[-1].time).total_seconds() > 15
                    or (t - win[0].time).total_seconds() < 0.9 * WINDOW_S):
                continue
            row = (t, dfa_alpha1(clean), rmssd(clean), 100 * (len(win) - len(clean)) / len(win))
            span_ms = (win[-1].time - win[0].time).total_seconds() * 1000 + win[0].rr
            self.series.append(row)
            self.coverage.append(min(100.0, 100 * sum(x.rr for x in win) / span_ms) if span_ms > 0 else 100.0)
            new.append(row)
        return new

    def latest(self, now: datetime, max_age: float = 20) -> Optional[tuple]:
        if self.series and (now - self.series[-1][0]).total_seconds() <= max_age:
            return self.series[-1]
        return None

    def columns_at(self, t: datetime, max_age: float = 20) -> dict:
        """Per-second CSV columns: the newest result computed at or before t."""
        i = bisect_right(self.series, t, key=lambda r: r[0])
        if i:
            row = self.series[i - 1]
            if (t - row[0]).total_seconds() <= max_age and row[1] is not None:
                return {"dfa_a1": round(row[1], 3), "rmssd_ms": round(row[2], 1), "rr_artifact_pct": round(row[3], 1),
                        "rr_coverage_pct": round(self.coverage[i - 1], 1)}
        return {}


def analyse(beats_in: list) -> HrvTracker:
    """Offline: [(time, [rr_ms...]), ...] in time order -> tracker with the full alpha 1 series."""
    h = HrvTracker()
    for t, rr in beats_in:
        h.add(t, rr)
    h._judge(final=True)
    h.update()
    return h
