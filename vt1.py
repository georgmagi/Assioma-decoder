"""
VT1 (the first ventilatory / aerobic threshold) from a ride's DFA alpha 1, with no test needed.

alpha 1 falls with intensity and crosses 0.75 near VT1 (Rogers et al. 2021). A ramp test fits a
line through that fall; an ordinary ride is messier, so:

1. Samples: alpha 1 every 5 s, each paired with the mean power and heart rate over the same 2-min
   window it was computed from. Only steady windows count (10-s powers within 20 % of each other,
   no coasting) and only settled ones: the window's first and last 30 s within 5 % in power and
   4 bpm in heart rate. alpha 1 lags a change by its whole 2-min window, so a window that spans a
   step up or an interval's start pairs a falling alpha 1 with a heart rate still on its way up:
   that put VT1 "crossings" into interval rides that weren't there. Never the first 10 minutes (alpha 1 is unreliable while warming up), never windows
   with more than 5 % artifacts.
2. Curve: the samples are binned by power (10 W) or heart rate (3 bpm); alpha 1 must fall with
   intensity, so the bin medians get a decreasing isotonic fit (pool adjacent violators, weighted
   by samples). Flat at low intensity, then falling, as alpha 1 behaves; no straight-line
   assumption.
3. VT1 = where the curve crosses 0.75 (interpolated between bins). A ride that never gets there
   gives a bound instead: "VT1 above 160 W" (or below, if alpha 1 was under 0.75 throughout).
4. Usable ("ok"): a crossing whose 80 % interval is narrow (8 bpm / 30 W) and that most resampled
   rides also cross (70 %). Anything else (a bound, a range, a shaky crossing) is shown as what it
   is but never taken as your VT1.
5. Uncertainty: a moving-block bootstrap (2-min blocks, so the overlap of neighbouring windows is
   respected) gives an 80 % interval and how often a resampled ride crosses at all. Seeded: the
   same ride always gives the same numbers.

Standard library only.
"""
from __future__ import annotations

import random
from statistics import median
from typing import Optional

A1_VT1 = 0.75                  # VT1 (HRVT1)
A1_VT2 = 0.5                   # VT2 (HRVT2): close to the second lactate threshold, i.e. near FTP
WINDOW_S = 120                 # alpha 1 is computed over 2 min of beats
STEP_S = 5                     # a new alpha 1 every 5 s
WARMUP_S = 600
MAX_ARTIFACT_PCT = 5.0
STEADY = 0.20                  # 10-s mean powers within +-20 % of the window mean
MIN_POWER = 40
MIN_BIN = 6                    # samples (30 s of alpha 1) for a bin to count
MIN_SAMPLES = 120              # 10 min of usable alpha 1 in all
BOOTS = 200
BINS = {"hr": 3.0, "power": 10.0}
SETTLE_S = 30                  # settled: the window's first and last 30 s ...
SETTLE_POWER = 0.05            # ... within 5 % in power
SETTLE_HR = 4.0                # ... and 4 bpm in heart rate
OK_WIDTH = {"hr": 8.0, "power": 30.0}   # a usable crossing's 80 % interval at most this wide
OK_P_CROSS = 0.7


def samples(rows: list[dict]) -> list[dict]:
    """(alpha 1, 2-min power, 2-min HR) every 5 s of the ride, steady windows only."""
    out = []
    for i in range(max(WARMUP_S, WINDOW_S), len(rows), STEP_S):
        r = rows[i]
        a1 = r.get("dfa_a1")
        if a1 is None or (r.get("rr_artifact_pct") or 0) > MAX_ARTIFACT_PCT:
            continue
        win = rows[i - WINDOW_S + 1: i + 1]
        if "time" in r and (r["time"] - win[0]["time"]).total_seconds() > WINDOW_S:
            continue                               # the window spans a pause
        p = [x.get("power_w") for x in win]
        h = [x.get("heart_rate_bpm") for x in win if x.get("heart_rate_bpm")]
        if any(v is None for v in p) or len(h) < 0.9 * WINDOW_S:
            continue
        mean_p = sum(p) / len(p)
        tens = [sum(p[k: k + 10]) / 10 for k in range(0, WINDOW_S, 10)]
        if mean_p < MIN_POWER or max(abs(t - mean_p) for t in tens) > STEADY * mean_p:
            continue
        p0, p1 = sum(p[:SETTLE_S]) / SETTLE_S, sum(p[-SETTLE_S:]) / SETTLE_S
        h0 = [x.get("heart_rate_bpm") for x in win[:SETTLE_S] if x.get("heart_rate_bpm")]
        h1 = [x.get("heart_rate_bpm") for x in win[-SETTLE_S:] if x.get("heart_rate_bpm")]
        if (not h0 or not h1 or abs(p1 - p0) > SETTLE_POWER * mean_p
                or abs(sum(h1) / len(h1) - sum(h0) / len(h0)) > SETTLE_HR):
            continue                               # still settling after a change
        out.append({"i": i, "a1": float(a1), "power": mean_p, "hr": sum(h) / len(h),
                    "coverage": r.get("rr_coverage_pct")})
    return out


def _pava_decreasing(y: list, w: list) -> list:
    """Weighted least-squares fit, non-increasing in order (pool adjacent violators)."""
    blocks = []                                    # [value, weight, count]
    for v, wt in zip(y, w):
        blocks.append([v, wt, 1])
        while len(blocks) > 1 and blocks[-2][0] < blocks[-1][0]:
            v2, w2, n2 = blocks.pop()
            v1, w1, n1 = blocks.pop()
            blocks.append([(v1 * w1 + v2 * w2) / (w1 + w2), w1 + w2, n1 + n2])
    out = []
    for v, _, n in blocks:
        out += [v] * n
    return out


def crossing(points: list[tuple], width: float, level: float = A1_VT1) -> Optional[dict]:
    """points = [(x, alpha 1)]: the binned isotonic curve and where it crosses 0.75.
    {kind, x, curve: [(bin centre, fitted, median, n)]}; kind "cross" (x = the crossing),
    "between" (it crosses inside a gap with no data: x, x2 = the bins either side), "above" or
    "below" (never crosses: x = the highest / lowest bin)."""
    bins: dict = {}
    for x, a in points:
        bins.setdefault(int(x // width), []).append(a)
    keys = sorted(k for k, v in bins.items() if len(v) >= MIN_BIN)
    if not keys:                                   # (one bin, a steady ride, still gives a bound)
        return None
    xs = [(k + 0.5) * width for k in keys]
    med = [median(bins[k]) for k in keys]
    n = [len(bins[k]) for k in keys]
    fit = _pava_decreasing(med, n)
    curve = list(zip(xs, fit, med, n))
    if fit[0] <= level:
        return {"kind": "below", "x": xs[0], "curve": curve}
    for j in range(1, len(xs)):
        if fit[j] <= level:
            if xs[j] - xs[j - 1] > 2.5 * width:          # no data where it crosses: a range only
                return {"kind": "between", "x": xs[j - 1], "x2": xs[j], "curve": curve}
            f0, f1 = fit[j - 1], fit[j]
            x = xs[j - 1] + (xs[j] - xs[j - 1]) * (f0 - level) / (f0 - f1)
            return {"kind": "cross", "x": x, "curve": curve}
    return {"kind": "above", "x": xs[-1], "curve": curve}


def usable(e: Optional[dict], key: str = "hr") -> bool:
    """A crossing good enough to call your VT1 (or VT2): narrow and repeatable (see 4. above)."""
    return bool(e and e.get("kind") == "cross" and e.get("lo") is not None
                and e["hi"] - e["lo"] <= OK_WIDTH[key] and e.get("p_cross", 0) >= OK_P_CROSS)


def _percentile(v: list, q: float) -> float:
    v = sorted(v)
    k = (len(v) - 1) * q
    lo = int(k)
    return v[lo] + (v[min(lo + 1, len(v) - 1)] - v[lo]) * (k - lo)


def estimate(rows: list[dict], seed: int = 1, level: float = A1_VT1, s: Optional[list] = None) -> Optional[dict]:
    """VT1 by power and by heart rate from one ride's per-second rows (with dfa_a1), or None when
    the ride has too little steady alpha 1. Each of "hr" (the main one: heart rate drifts smoothly,
    so its bins fill in) and "power": {kind, value, value2 (between), lo, hi (80 % bootstrap
    interval, crossings only), p_cross (share of resampled rides that cross), curve}.
    level=A1_VT2 gives HRVT2 the same way; pass the samples to reuse them for both."""
    s = samples(rows) if s is None else s
    if len(s) < MIN_SAMPLES:
        return None
    block = WINDOW_S // STEP_S
    starts = list(range(0, len(s) - block + 1)) or [0]
    cov = [x["coverage"] for x in s if x["coverage"] is not None]
    # beats that arrived (Bluetooth loses some at higher HR, which biases alpha 1 up near 0.75)
    out = {"samples": len(s), "minutes": round(len(s) * STEP_S / 60, 1),
           "beats_pct": round(sum(cov) / len(cov), 1) if cov else None}
    for key, width in BINS.items():
        res = crossing([(x[key], x["a1"]) for x in s], width, level)
        if res is None:
            out[key] = None
            continue
        rnd = random.Random(seed)
        xs, crossed = [], 0
        for _ in range(BOOTS):
            pick = []
            while len(pick) < len(s):
                b = rnd.choice(starts)
                pick += s[b: b + block]
            r = crossing([(x[key], x["a1"]) for x in pick[: len(s)]], width, level)
            if r and r["kind"] == "cross":
                crossed += 1
                xs.append(r["x"])
        item = {"kind": res["kind"], "value": round(res["x"], 1), "p_cross": round(crossed / BOOTS, 2),
                **({"value2": round(res["x2"], 1)} if "x2" in res else {}),
                "curve": [[round(x, 1), round(f, 3), round(m, 3), c] for x, f, m, c in res["curve"]]}
        if res["kind"] == "cross" and len(xs) >= 20:
            item["lo"], item["hi"] = round(_percentile(xs, 0.1), 1), round(_percentile(xs, 0.9), 1)
        item["ok"] = usable(item, key)
        out[key] = item
    return out
