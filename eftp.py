"""
Estimated FTP from your rides: the critical power (CP) of your best efforts over the last 42 days.

Every ride stores its mean-maximal power (best average) for a set of durations (ride_summary). For
a date, the best of each duration over the 42 days up to it is the power-duration curve; its 3- to
20-minute points are fitted with the 2-parameter critical power model, which says the work you can
do in t seconds is

    W(t) = CP * t + W'          (CP in W, W' the anaerobic reserve in J)

a straight line in (t, W), fitted by least squares. CP sits close to FTP (a little above for most
riders); 95 % of the best 20 minutes is shown beside it as the classic cross-check.

Ramp tests are the exception: they only go all-out at the very end, so their 5- to 20-minute
averages include the easy early steps and would drag the fit down. They are kept out of the curve
and give their own estimate the standard way, 75 % of the best minute (Zwift, TrainerRoad). The
current eFTP is the newest ramp test in the window if there is one, else the critical power fit.

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


RAMP_FACTOR = 0.75


def is_ramp(r: dict) -> bool:
    """A ramp test, from its workout name (OpenCycling, Zwift and our own files say so)."""
    return "ramp" in (r.get("name") or "").lower() or "ramp" in (r.get("id") or "").lower()


def curve(rides: list, end: date, days: int = WINDOW_DAYS, day_of=None) -> dict:
    """The best of each duration over rides in the `days` days up to and including `end`
    (ramp tests left out: see the module notes)."""
    start = end - timedelta(days=days - 1)
    best: dict = {}
    for r in rides:
        d = day_of(r) if day_of else r["day"]
        if not start <= d <= end or is_ramp(r):
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


def ramp_tests(rides: list, day_of) -> list:
    """[{day, map, ftp, id}] for every ramp test, oldest first (map = best 1 min)."""
    out = []
    for r in rides:
        best1 = (r.get("mmp") or {}).get("60")
        if is_ramp(r) and best1:
            out.append({"day": day_of(r), "map": best1, "ftp": RAMP_FACTOR * best1, "id": r.get("id")})
    return sorted(out, key=lambda x: x["day"])


def hrvt2(rides: list, end: date, day_of) -> Optional[dict]:
    """The second HRV threshold (alpha 1 = 0.5, vt1.py) by power in the window: the newest ride
    that crossed it, else the highest power it was still above (a lower bound)."""
    start = end - timedelta(days=WINDOW_DAYS - 1)
    got = [(day_of(r), r["vt2_power"], r.get("id"), r.get("vt1_beats_pct")) for r in rides
           if r.get("vt2_power") and start <= day_of(r) <= end]
    cross = sorted((x for x in got if x[1]["kind"] == "cross"), key=lambda x: x[0])
    if cross:
        d, e, rid, beats = cross[-1]
        return {"bound": "cross", "value": e["value"], "lo": e.get("lo"), "hi": e.get("hi"), "day": d.isoformat(),
                "ride": rid, "beats_pct": beats}
    above = [x for x in got if x[1]["kind"] == "above"]
    if above:
        d, e, rid, beats = max(above, key=lambda x: x[1]["value"])
        return {"bound": "above", "value": e["value"], "day": d.isoformat(), "ride": rid, "beats_pct": beats}
    return None


def trusted(vt2: Optional[dict]) -> bool:
    """alpha 1 needs (nearly) every beat: with Bluetooth's lost beats it reads high near the
    thresholds (CLAUDE.md, the HRM 200 analysis), so only rides with 95 % of beats count."""
    return bool(vt2) and (vt2.get("beats_pct") is None or vt2["beats_pct"] >= 95)


def estimate_on(rides: list, end: date, day_of, ramps: list) -> Optional[dict]:
    """eFTP on a date, from the best evidence of the window: a ramp test, else the second HRV
    threshold where alpha 1 crossed 0.5 (it sits at the second lactate threshold, near FTP: Rogers
    et al. 2022, r = 0.93 for power in pro cyclists), else the critical power fit. All of them are
    listed as `evidence` so the page can show how they agree."""
    start = end - timedelta(days=WINDOW_DAYS - 1)
    recent = [x for x in ramps if start <= x["day"] <= end]
    fit = fit_cp(curve(rides, end, day_of=day_of))
    vt2 = hrvt2(rides, end, day_of)
    evidence = []
    if recent:
        x = recent[-1]
        evidence.append({"kind": "ramp", "value": x["ftp"], "day": x["day"].isoformat(), "map": x["map"], "ride": x["id"]})
    if vt2:
        evidence.append({"kind": "hrvt2", "trusted": trusted(vt2), **vt2})
    if fit:
        evidence.append({"kind": "efforts", "value": fit["cp"], "quality": fit["quality"]})
    if recent:
        x = recent[-1]
        return {"ftp": x["ftp"], "method": "ramp", "day": x["day"].isoformat(), "map": x["map"],
                "ride": x["id"], "cp_fit": fit, "evidence": evidence}
    if vt2 and vt2["bound"] == "cross" and trusted(vt2):
        return {"ftp": vt2["value"], "method": "hrvt2", "day": vt2["day"], "cp_fit": fit, "evidence": evidence}
    if fit:
        return {"ftp": fit["cp"], "method": "cp", "cp_fit": fit, "evidence": evidence}
    return None


def history(rides: list, today: date, day_of) -> dict:
    """{current: today's estimate or None, series: [{day, ftp, method}] on each ride day, curve}."""
    ramps = ramp_tests(rides, day_of)
    series = []
    for d in sorted({day_of(r) for r in rides if r.get("mmp")}):
        e = estimate_on(rides, d, day_of, ramps)
        if e:
            series.append({"day": d.isoformat(), "ftp": round(e["ftp"], 1), "method": e["method"]})
    now_curve = curve(rides, today, day_of=day_of)
    cur = estimate_on(rides, today, day_of, ramps)
    r1 = lambda v: round(v, 1) if isinstance(v, float) else v
    if cur and cur["cp_fit"]:
        cur["cp_fit"] = {k: r1(v) for k, v in cur["cp_fit"].items()}
    return {"current": cur and {k: r1(v) for k, v in cur.items()},
            "series": series, "curve": {k: round(v, 1) for k, v in now_curve.items()},
            "best20": now_curve.get("1200")}
