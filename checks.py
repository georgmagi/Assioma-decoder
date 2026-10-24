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

import math
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
    intensity]) or, without one, from the steps' durations; a step skipped before its end is left out."""
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
        if starts.get(i + 1, end) < end - 10:
            continue                                              # skipped: not ridden to its end
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
    return {"steps": out, "hr_a": my - b * mx, "hr_b": b, "hrr60": hrr,
            "rec_s": starts[rec] if rec and starts[rec] + 63 <= len(rows) else None}


REC_MIN_PAIRS = 20


def recovery_ln_rmssd(beat_t: list, rr: list, artifact: list, start: float, dur: float = 60) -> Optional[float]:
    """ln RMSSD over the first `dur` s after the hardest step: the LSCT's recovery HRV (Lamberts et
    al. 2024: LnRMSSD60s, ICC 0.97, tracks peak power, VO2peak and 40-km TT). Only pairs of
    neighbouring clean beats count; fewer than REC_MIN_PAIRS: nothing. (The paper's riders sat still;
    here you spin easy, so compare it with your own checks, not with their numbers.)"""
    d = [rr[i + 1] - rr[i] for i in range(len(rr) - 1)
         if start <= beat_t[i] and beat_t[i + 1] <= start + dur and not artifact[i] and not artifact[i + 1]]
    if len(d) < REC_MIN_PAIRS:
        return None
    rms = math.sqrt(sum(x * x for x in d) / len(d))
    return math.log(rms) if rms > 0 else None


# ---- durability: your best efforts after work already done (Spragg et al. 2023, Mateo-March et
# al. 2024: how far the best 5 and 20 min fall after 1000-2000+ kJ separates riders that fresh
# numbers don't). Per ride, the best mean power over each duration for efforts that start once the
# ride's work has passed each level; training.durability_profile takes the best of each over 90 days.
DUR_KJ = (0, 500, 1000, 1500, 2000)
DUR_S = (60, 300, 1200)


def durable_mmp(power: list) -> dict:
    """{kJ level: {seconds: [best watts, start second]}} for every level the ride reached with the
    effort still fitting after it ("0": fresh, the ride's plain best)."""
    p = [v or 0 for v in power]
    n = len(p)
    cum = [0.0]
    for v in p:
        cum.append(cum[-1] + v)
    out = {}
    for kj in DUR_KJ:
        first = next((i for i in range(n + 1) if cum[i] >= kj * 1000), None)
        if first is None:
            break
        level = {}
        for d in DUR_S:
            if first + d > n:
                continue
            best, at = max(((cum[i + d] - cum[i]) / d, i) for i in range(first, n - d + 1))
            level[str(d)] = [round(best, 1), at]
        if level:
            out[str(kj)] = level
    return out


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


# --------------------------------------------------------------------------- warm-up readiness
# Every ride starts with easy minutes, so every ride is a small test: heart rate at a fixed easy
# power, early on, against your other rides. Higher than your normal = tired, run down or getting
# ill (6 Oct, the day after two step tests: 135 bpm against a normal of ~125). Minutes 6-15
# (heart rate has settled, drift hasn't started), seconds whose 30-s power is steady at 100-150 W;
# heart rate put on 125 W with 0.4 bpm per W (the slope of Georg's HR against power, Oct rides).
READY_W, READY_SLOPE = 125, 0.4
READY_FROM_S, READY_TO_S, READY_MIN_S = 360, 900, 180


def warmup(rows: list) -> Optional[dict]:
    """{hr: heart rate at READY_W, power, a1, seconds} from the warm-up, None without 3 min of it."""
    p = [r.get("power_w") or 0 for r in rows]
    h = [r.get("heart_rate_bpm") for r in rows]
    a = [r.get("dfa_a1") for r in rows]
    sel = []
    for i in range(READY_FROM_S, min(len(rows), READY_TO_S)):
        w = p[i - 30:i]
        m = sum(w) / 30
        if 100 <= m <= 150 and max(w) - min(w) < 0.35 * m and h[i]:
            sel.append((i, m))
    if len(sel) < READY_MIN_S:
        return None
    pm = sum(m for _, m in sel) / len(sel)
    hm = sum(h[i] for i, _ in sel) / len(sel)
    a1 = [a[i] for i, _ in sel if a[i] is not None]
    return {"hr": hm + READY_SLOPE * (READY_W - pm), "power": pm, "a1": sum(a1) / len(a1) if len(a1) >= 60 else None,
            "seconds": len(sel)}


def warmup_normal(earlier: list) -> Optional[dict]:
    """Your normal from earlier rides' warm-ups (newest first): the median of the last 10, with 3 or more.
    Only the recorder's own rides go in (the caller's job): OpenCycling's FIT rides, another strap and
    warm-ups at 140-150 W, read ~10 bpm higher (Sep 2026) and would drag the normal up."""
    hrs = [w["hr"] for w in earlier if w and w.get("hr")][:10]
    a1s = [w["a1"] for w in earlier if w and w.get("a1")][:10]
    if len(hrs) < 3:
        return None
    return {"hr": median(hrs), "a1": median(a1s) if len(a1s) >= 3 else None, "rides": len(hrs)}


# --------------------------------------------------------------------------- the knee
# The knee feels force per pedal stroke, not watts: torque = power / crank speed. The same watts at
# a lower cadence press harder on every stroke. 29 Sep 5x3 (knee flared after): 23-24 Nm per rep at
# 92-95 rpm; 6 Oct 5x4 (it didn't): 21-22 Nm at 96-98 rpm. With a 0-3 knee score per ride the
# pattern (a torque limit? the left leg's balance or PCO?) shows up across rides.
KNEE_REP_PCT = 0.85            # a rep: 30-s power over this share of FTP ...
KNEE_REP_MIN_S = 60            # ... for at least a minute


def torque(power: Optional[float], cadence: Optional[float]) -> Optional[float]:
    """Nm at the crank (both legs together) from W and rpm; None when not pedalling."""
    if not power or not cadence or cadence < 30:
        return None
    return power / (2 * math.pi * cadence / 60)


def knee(rows: list, ftp: Optional[float]) -> dict:
    """{reps: [{start, seconds, power, cadence, torque, balance_right, pco_left, pco_right, te_left,
    te_right}], peak_torque: the best 1-min mean torque (Nm), torque_z2: mean torque while
    pedalling at 56-75 % FTP}."""
    n = len(rows)
    g = lambda k: [r.get(k) for r in rows]
    p, c = g("power_w"), g("cadence_rpm")
    tq = [torque(a, b) for a, b in zip(p, c)]
    # the best minute of torque
    peak, acc, cnt = None, 0.0, 0
    for i in range(n):
        if tq[i] is not None:
            acc, cnt = acc + tq[i], cnt + 1
        if i >= 60 and tq[i - 60] is not None:
            acc, cnt = acc - tq[i - 60], cnt - 1
        if i >= 59 and cnt >= 50:
            peak = max(peak or 0, acc / cnt)
    z2 = [t for t, w in zip(tq, p) if t is not None and ftp and 0.56 * ftp <= w <= 0.75 * ftp]
    reps = []
    if ftp:
        hard = [False] * n
        acc = 0.0
        for i in range(n):
            acc += p[i] or 0
            if i >= 30:
                acc -= p[i - 30] or 0
            if i >= 29 and acc / 30 >= KNEE_REP_PCT * ftp:
                for k in range(i - 29, i + 1):
                    hard[k] = True
        i = 0
        while i < n:
            if not hard[i]:
                i += 1
                continue
            j = i
            while j < n and hard[j]:
                j += 1
            a, b = i, j
            while a < b and (p[a] or 0) < KNEE_REP_PCT * ftp:       # trim the smoothing's edges
                a += 1
            while b > a and (p[b - 1] or 0) < KNEE_REP_PCT * ftp:
                b -= 1
            if b - a >= KNEE_REP_MIN_S:
                seg = rows[a:b]
                m = lambda k: _mean([r.get(k) for r in seg])
                reps.append({"start": a, "seconds": b - a, "power": m("power_w"), "cadence": m("cadence_rpm"),
                             "torque": _mean(tq[a:b]), "balance_right": m("balance_right_pct"),
                             "pco_left": m("pco_left_mm"), "pco_right": m("pco_right_mm"),
                             "te_left": m("te_left_pct"), "te_right": m("te_right_pct")})
            i = j
    return {"reps": reps, "peak_torque": peak, "torque_z2": sum(z2) / len(z2) if len(z2) >= 300 else None}


# --------------------------------------------------------------------------- heart rate against power
# Every ride is a fitness test if heart rate is read as a lagged response to power: the heart
# follows the power a first-order system would, HR(t) = base + gain * P_f(t), where P_f is the
# power smoothed with a time constant tau (Mazzoleni et al. 2016 and later "HR kinetics" work).
# Fitted per ride (tau by grid, base and gain by least squares) it gives
#   - HR at 150 W (your Z2): base + gain * 150. Lower over the weeks = fitter (or a cooler room)
#   - tau: how fast heart rate answers a change of power. Faster = better aerobic fitness
#   - gain: bpm per W (only on rides whose power varied enough to tell gain from base)
# The first 3 minutes are left out (the heart is still waking up). 6 Oct VO2 ride: tau 70 s,
# 0.38 bpm/W; 29 Sep: 85 s, 0.42 bpm/W.
HRM_FROM_S, HRM_MIN_S = 180, 900
HRM_TAUS = range(10, 181, 5)
HRM_REF_W = 150
HRM_REF_T = 600              # s: HR at 150 W is read at minute 10


def hr_model(rows: list) -> Optional[dict]:
    """{hr150, tau, gain, drift, base, rmse, power_span, seconds} or None (under 15 min of heart
    rate, or 150 W far outside what was ridden). The model has a drift term too, heart rate creeping
    up with time at the same power (heat, fatigue), so HR at 150 W is read at minute 10 and long or
    interval rides compare with short steady ones."""
    p = [r.get("power_w") or 0.0 for r in rows]
    h = [r.get("heart_rate_bpm") for r in rows]
    idx = [i for i in range(HRM_FROM_S, len(rows)) if h[i]]
    if len(idx) < HRM_MIN_S:
        return None
    ys = [h[i] for i in idx]
    ts = [(i - HRM_REF_T) / 3600 for i in idx]                   # hours from minute 10
    n = len(idx)
    my, mt = sum(ys) / n, sum(ts) / n
    stt = sum((t - mt) ** 2 for t in ts)
    best = None
    for tau in HRM_TAUS:
        a, f, pf = 1.0 / tau, 0.0, []
        for x in p:
            f += a * (x - f)
            pf.append(f)
        xs = [pf[i] for i in idx]
        mx = sum(xs) / n
        sxx = sum((x - mx) ** 2 for x in xs)
        sxt = sum((x - mx) * (t - mt) for x, t in zip(xs, ts))
        sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
        sty = sum((t - mt) * (y - my) for t, y in zip(ts, ys))
        det = sxx * stt - sxt * sxt
        if sxx <= 0 or det <= 1e-9 * sxx * stt:
            continue
        gain, drift = (sxy * stt - sty * sxt) / det, (sty * sxx - sxy * sxt) / det
        base = my - gain * mx - drift * mt                       # HR at 0 W at minute 10
        rmse = (sum((y - base - gain * x - drift * t) ** 2 for x, t, y in zip(xs, ts, ys)) / n) ** 0.5
        if best is None or rmse < best["rmse"]:
            srt = sorted(xs)
            best = {"tau": tau, "gain": gain, "drift": drift, "base": base, "rmse": rmse,
                    "p_lo": srt[int(0.05 * (n - 1))], "p_hi": srt[int(0.95 * (n - 1))]}
    if best is None:
        return None
    b = best
    span = b["p_hi"] - b["p_lo"]
    # a steady ride can't tell gain from base, nor tau: then HR at 150 W is its mean heart rate put
    # on 150 W with your typical gain, and tau and gain are left out
    varied = span >= 60
    near = b["p_lo"] - 30 <= HRM_REF_W <= b["p_hi"] + 30
    if varied:
        hr150 = b["base"] + b["gain"] * HRM_REF_W
    else:
        xs_mean = sum(p[i] for i in idx) / n
        hr150 = my - b["drift"] * mt + READY_SLOPE * (HRM_REF_W - xs_mean)
    return {"hr150": hr150 if near else None, "tau": b["tau"] if varied else None,
            "gain": b["gain"] if varied else None, "drift": b["drift"], "base": b["base"],
            "rmse": b["rmse"], "power_span": span, "seconds": n}
