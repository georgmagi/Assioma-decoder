"""
What a controlled indoor setup can measure that a ride outside can't: the same load every time.

- durability(rows): how well a long ride held up. Steady 2-min windows after the warm-up, matched
  in intensity (within 10 % of the ride's typical steady power), first third against last third:
  the drift of heart rate against power (as Pw:HR decoupling, + = HR rose) and of DFA alpha 1 (it
  falls with accumulating fatigue in prolonged exercise, Gronwald et al. 2024), and when the
  power-per-beat first fell 5 % below its start (the onset).
- fitness_check(rows, meta): the weekly check, ridden as the Threshold steps workout (workout.py):
  its 5-min ERG steps up to 80 % FTP give, from their last 3 minutes, power, heart rate, alpha 1
  and RMSSD; the heart-rate line through them (HR = a + b * W) so checks compare at the same watts
  even after an FTP change; and heart-rate recovery, the drop in the first 60 s after the hardest
  step (as in the Lamberts submaximal cycle test, LSCT). The same ride gives VT1 and VT2 (vt1.py).
- heat_model(points): your own heart-rate cost per degree of room temperature, from checks (or
  rides) at different temperatures, to correct trends for conditions. Needs 5 of them over at
  least 3 degrees C; a plausible slope (0-4 bpm per degree) or nothing.

Standard library only.
"""
from __future__ import annotations

from statistics import median
from typing import Optional

WARMUP_S, WIN, STEP = 600, 120, 30
CHECK_IDS = ("vt1-steps", "fitness-check")   # Threshold steps is the weekly check (fitness-check: older rides)


def _windows(rows: list) -> list:
    """Steady 2-min windows every 30 s after the warm-up: (end second, power, HR, alpha 1 or None)."""
    out = []
    for end in range(WARMUP_S + WIN, len(rows) + 1, STEP):
        win = rows[end - WIN: end]
        p = [r.get("power_w") for r in win]
        h = [r.get("heart_rate_bpm") for r in win]
        if any(v is None for v in p) or any(not v for v in h):
            continue
        m = sum(p) / WIN
        tens = [sum(p[k: k + 10]) / 10 for k in range(0, WIN, 10)]
        if m < 40 or max(abs(t - m) for t in tens) > 0.2 * m:
            continue
        a = [r.get("dfa_a1") for r in win[-30:] if r.get("dfa_a1") is not None]
        out.append((end, m, sum(h) / WIN, sum(a) / len(a) if a else None))
    return out


def durability(rows: list) -> Optional[dict]:
    """Drift over a long steady ride; None for rides with under 45 min of steady riding."""
    w = _windows(rows)
    if len(w) * STEP < 45 * 60:
        return None
    mid = median(x[1] for x in w)
    w = [x for x in w if abs(x[1] - mid) <= 0.1 * mid]           # matched intensity
    if len(w) < 30:
        return None
    third = len(w) // 3
    first, last = w[:third], w[-third:]
    ef = lambda xs: sum(x[1] for x in xs) / sum(x[2] for x in xs)
    drift = (ef(first) / ef(last) - 1) * 100
    a_first = [x[3] for x in first if x[3] is not None]
    a_last = [x[3] for x in last if x[3] is not None]
    a_drop = (sum(a_first) / len(a_first) - sum(a_last) / len(a_last)) if len(a_first) >= 10 and len(a_last) >= 10 else None
    # the onset: the first 10 min (20 windows) whose power per beat is 5 % under the first 10 min's
    onset = None
    base = ef(w[:20]) if len(w) >= 40 else None
    if base:
        for k in range(20, len(w) - 19):
            if ef(w[k: k + 20]) < 0.95 * base:
                onset = round(w[k][0] / 60)
                break
    score = ("good" if drift < 3 and (a_drop is None or a_drop < 0.1)
             else "fair" if drift < 6 and (a_drop is None or a_drop < 0.2) else "poor")
    return {"drift_pct": drift, "a1_drop": a_drop, "onset_min": onset, "score": score,
            "power": mid, "minutes": len(w) * STEP / 60,
            "hr_first": sum(x[2] for x in first) / len(first), "hr_last": sum(x[2] for x in last) / len(last)}


def _mean(vals):
    vals = [v for v in vals if v is not None]
    return sum(vals) / len(vals) if vals else None


def fitness_check(rows: list, meta: dict) -> Optional[dict]:
    """The weekly check, from a Threshold steps ride (or an older Fitness check ride): every steady
    ERG step from 50 to 80 % of FTP (aerobic, so heart rate settles) gives its last 3 minutes; heart
    rate recovery is the drop over the first 60 s of the "recovery" step that follows the hardest
    step. Step starts come from the log the app kept (meta["workout"]["log"], [ride second, step,
    intensity]) or, without one, from the steps' durations."""
    wo = (meta or {}).get("workout") or {}
    if wo.get("id") not in CHECK_IDS:
        return None
    steps = wo.get("steps") or []
    starts = {}
    for entry in wo.get("log") or []:
        starts.setdefault(entry[1], entry[0])
    t = 0
    for i, st in enumerate(steps):
        starts.setdefault(i, t)
        t += st[0]
    out = []
    for i, st in enumerate(steps):
        lo, hi = st[1], st[2]
        if lo is None or lo != hi or not 0.5 <= lo <= 0.8 or st[0] < 240:
            continue
        end = starts[i] + st[0]
        if end > len(rows):
            break                                                 # stopped before this step ended
        win = rows[end - 180: end]
        out.append({"label": st[3] if len(st) > 3 else "", "power": _mean(r.get("power_w") for r in win),
                    "hr": _mean(r.get("heart_rate_bpm") for r in win), "a1": _mean(r.get("dfa_a1") for r in win),
                    "rmssd": _mean(r.get("rmssd_ms") for r in win)})
    if len(out) < 3 or any(s["power"] is None or s["hr"] is None for s in out):
        return None
    # the heart-rate line through the steps
    xs, ys = [s["power"] for s in out], [s["hr"] for s in out]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx if sxx else 0
    # recovery: the first 60 s of the recovery step, from the end of the step before it
    hrr = None
    rec = next((i for i, st in enumerate(steps) if len(st) > 3 and st[3] == "recovery"), None)
    if rec and starts[rec] + 63 <= len(rows):
        end = starts[rec]
        at = _mean(r.get("heart_rate_bpm") for r in rows[end - 5: end])
        after = _mean(r.get("heart_rate_bpm") for r in rows[end + 57: end + 63])
        hrr = at - after if at and after else None
    return {"steps": out, "hr_a": my - b * mx, "hr_b": b, "hrr60": hrr}


def a1_at(check: dict, watts: float) -> Optional[float]:
    """alpha 1 at a power, between the two steps either side of it (or the nearest step)."""
    pts = sorted((s["power"], s["a1"]) for s in check["steps"] if s.get("a1") is not None)
    if not pts:
        return None
    if watts <= pts[0][0]:
        return pts[0][1]
    for (p0, a0), (p1, a1) in zip(pts, pts[1:]):
        if p0 <= watts <= p1:
            return a0 + (a1 - a0) * (watts - p0) / (p1 - p0) if p1 > p0 else a0
    return pts[-1][1]


def hr_at(check: dict, watts: float) -> float:
    return check["hr_a"] + check["hr_b"] * watts


def heat_model(points: list) -> Optional[dict]:
    """points = [(room temp C, heart rate at the same watts)]: bpm per degree, or None."""
    pts = [(t, h) for t, h in points if t is not None and h is not None]
    if len(pts) < 5:
        return None
    ts = [t for t, _ in pts]
    if max(ts) - min(ts) < 3:
        return None
    mt, mh = sum(ts) / len(ts), sum(h for _, h in pts) / len(pts)
    stt = sum((t - mt) ** 2 for t in ts)
    slope = sum((t - mt) * (h - mh) for t, h in pts) / stt
    if not 0 <= slope <= 4:
        return None
    return {"bpm_per_c": slope, "n": len(pts), "range_c": (min(ts), max(ts))}
