"""
Estimated FTP from your rides: the critical power (CP) of your best efforts over the last 42 days.

Every ride stores its mean-maximal power (best average) for a set of durations (ride_summary). For
a date, the best of each duration over the 42 days up to it is the power-duration curve; its 3- to
20-minute points are fitted with the 2-parameter critical power model, which says the work you can
do in t seconds is

    W(t) = CP * t + W'          (CP in W, W' the anaerobic reserve in J)

a straight line in (t, W), fitted by least squares. CP sits close to FTP (a little above for most
riders); 95 % of the best 20 minutes is shown beside it as the classic cross-check.

The estimate is only as good as the efforts behind it: steady Z2 rides give a curve that's flat,
which the model reads as a tiny W' and a CP near your Z2 power. So a result is only given when the
fit is sound: at least 3 durations, W' between 3 and 40 kJ, and the curve dropping by at least
12 % from 3 to 20 minutes (otherwise none of the efforts was near maximal). Standard library only.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

DURATIONS = (5, 15, 30, 60, 120, 180, 300, 420, 600, 900, 1200, 1800, 3600)   # s, stored per ride
FIT_DURATIONS = (180, 300, 420, 600, 900, 1200)
WINDOW_DAYS = 42
MIN_W_PRIME, MAX_W_PRIME = 3000, 40000      # J
MIN_DROP = 0.12                             # 3-min vs 20-min power, as a share of the 3-min


def mmp(power: list) -> dict:
    """Best average power for each of DURATIONS, {str(seconds): W or None}."""
    out = {}
    for d in DURATIONS:
        if len(power) < d:
            out[str(d)] = None
            continue
        acc, best = 0.0, 0.0
        for i, v in enumerate(power):
            acc += v or 0
            if i >= d:
                acc -= power[i - d] or 0
            if i >= d - 1 and acc > best:
                best = acc
        out[str(d)] = best / d
    return out


def curve(rides: list, end: date, days: int = WINDOW_DAYS, day_of=None) -> dict:
    """The best of each duration over rides in the `days` days up to and including `end`."""
    start = end - timedelta(days=days - 1)
    best: dict = {}
    for r in rides:
        d = day_of(r) if day_of else r["day"]
        if not start <= d <= end:
            continue
        for k, v in (r.get("mmp") or {}).items():
            if v is not None and v > best.get(k, 0):
                best[k] = v
    return best


def fit_cp(best: dict) -> Optional[dict]:
    """CP and W' from a power-duration curve, None when the efforts can't support it."""
    pts = [(t, best[str(t)]) for t in FIT_DURATIONS if best.get(str(t))]
    if len(pts) < 3:
        return None
    p3, p20 = best.get("180"), best.get("1200")
    if not p3 or not p20 or (p3 - p20) / p3 < MIN_DROP:
        return None                                   # nothing near maximal
    xs, ys = [t for t, _ in pts], [t * p for t, p in pts]          # time, work
    n, mx, my = len(xs), sum(xs) / len(xs), sum(ys) / len(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    cp = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    w_prime = my - cp * mx
    if not MIN_W_PRIME <= w_prime <= MAX_W_PRIME or cp <= 0:
        return None
    resid = [y - (cp * x + w_prime) for x, y in zip(xs, ys)]
    see = (sum(e * e for e in resid) / max(1, n - 2)) ** 0.5
    # how well the line fits, as the typical miss in watts at 10 min: under 3 W good, under 8 W fair
    miss_w = see / 600
    quality = "good" if miss_w < 3 else "fair" if miss_w < 8 else "rough"
    return {"cp": cp, "w_prime": w_prime, "points": n, "see_j": see, "quality": quality,
            "best20_95": 0.95 * p20, "durations": [t for t, _ in pts]}


def history(rides: list, today: date, day_of) -> dict:
    """{current: fit on today's curve or None, series: [{day, cp, w_prime}] on each ride day}."""
    days = sorted({day_of(r) for r in rides if r.get("mmp")})
    series = []
    for d in days:
        f = fit_cp(curve(rides, d, day_of=day_of))
        if f:
            series.append({"day": d.isoformat(), "cp": round(f["cp"], 1), "w_prime": round(f["w_prime"])})
    now_curve = curve(rides, today, day_of=day_of)
    cur = fit_cp(now_curve)
    return {"current": cur and {k: (round(v, 1) if isinstance(v, float) else v) for k, v in cur.items()},
            "series": series, "curve": {k: round(v, 1) for k, v in now_curve.items()},
            "best20": now_curve.get("1200")}
