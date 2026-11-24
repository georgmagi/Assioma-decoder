"""Live pacing for the 20-min FTP test (built-in "ftp-20", step "20-min test").

Pure: the engine hands over the test so far, second by second, and shows what comes back. You
change the power yourself (the - / + buttons); the guide only says when:

  0-3 min     settle: hold the target, heart rate is still climbing
  3-15 min    hold, +2 % or ease 2 %, from heart rate against the range a sustainable 20 min runs
              in for you (around your threshold heart rate, rising slowly) and its trend over the
              last 3 min; after a change, 90 s to let it settle before the next one
  15-18 min   +2 % if heart rate allows
  18-20 min   everything

FTP = 95 % of the 20-min average. The projection assumes the current target for the time left.
"""
from typing import Optional

TEST_LABEL = "20-min test"
SETTLE_S = 180          # heart rate still climbing: no verdicts yet
LATE_S = 900            # from 15 min: only "+2 % if you can"
FINAL_S = 120           # the last 2 min: everything
CHANGE_WAIT_S = 90      # after an intensity change, let heart rate answer first
SLOPE_S = 180           # heart-rate trend over the last 3 min
STEADY_S = 15          # a new hold / + / ease verdict shows once it has held for 15 s (no flicker)
STEP_PCT = 2            # one suggested change: 2 % (two presses)
UNDER = 0.93            # 30 s under 93 % of the target: ERG is winning, ease before cadence falls
CHART_EVERY = 5         # chart points every 5 s


def threshold_hr(max_hr: Optional[float]) -> float:
    """Threshold heart rate: 85 % of max (201 -> 171; his VT2 crossings sit at 167-169)."""
    return round(0.85 * max_hr, 1) if max_hr else 170.0


def hr_band(t: float, lt: float) -> Optional[tuple]:
    """(lo, hi) for a sustainable 20-min effort t seconds in: around threshold heart rate at 4 min,
    drifting up about 1 bpm a minute after that (a maximal 20 min ends near 94 % of max). None while heart rate is still climbing."""
    if t < SETTLE_S:
        return None
    m = t / 60 - 4
    return (round(lt - 4 + 1.0 * m, 1), round(lt + 8 + 1.1 * m, 1))


def slope(hr: list, now: int, span: int = SLOPE_S) -> Optional[float]:
    """Least-squares heart-rate trend, bpm per minute, over the last `span` seconds (None with less
    than 90 s of readings)."""
    pts = [(i, hr[i]) for i in range(max(0, now - span), now) if i < len(hr) and hr[i]]
    if len(pts) < 90:
        return None
    n = len(pts)
    mx = sum(p[0] for p in pts) / n
    my = sum(p[1] for p in pts) / n
    sxx = sum((p[0] - mx) ** 2 for p in pts)
    return round(60 * sum((p[0] - mx) * (p[1] - my) for p in pts) / sxx, 2) if sxx else None


def _mean(v: list) -> Optional[float]:
    v = [x for x in v if x is not None]
    return sum(v) / len(v) if v else None


def cue(t: float, dur: int, hr_now: Optional[float], band: Optional[tuple], trend: Optional[float],
        since_change: Optional[float], p30: Optional[float], target: Optional[float]) -> dict:
    """{kind: settle | hold | up | down | go, text, sub}."""
    left = dur - t
    if left <= 30:
        return {"kind": "go", "text": "Empty the tank", "sub": "30 seconds. Everything you have left."}
    if left <= FINAL_S:
        return {"kind": "go", "text": "Everything",
                "sub": "Last 2 minutes: press + every 30 s, or ERG off and ride it on the gears."}
    if t < SETTLE_S:
        return {"kind": "settle", "text": "Settle in",
                "sub": "Hold the target. Heart rate is still climbing. Cadence 95-100, breathe out fully."}
    if target and p30 is not None and p30 < UNDER * target and t >= 300:
        return {"kind": "down", "text": f"Ease {STEP_PCT} %",
                "sub": f"You're under the target ({p30:.0f} of {target:.0f} W): press − twice before ERG drags your cadence down."}
    if since_change is not None and since_change < CHANGE_WAIT_S:
        return {"kind": "hold", "text": "Hold", "sub": "Let the last change settle before the next one."}
    if hr_now is None or band is None:
        return {"kind": "hold", "text": "Hold", "sub": "No heart rate: pace by feel, steady, and save the push for the end."}
    lo, hi = band
    rising = trend if trend is not None else 0.0
    if t >= LATE_S:
        if hr_now <= hi and rising < 2.0:
            return {"kind": "up", "text": f"+{STEP_PCT} % if you can",
                    "sub": f"{(left - FINAL_S) / 60:.0f} min to the final push. Heart rate {hr_now:.0f} has room: press + twice if your legs agree."}
        return {"kind": "hold", "text": "Hold", "sub": f"Heart rate {hr_now:.0f}: you're near the limit. Hold it to the final 2 minutes."}
    if hr_now > hi and rising > 1.5:
        return {"kind": "down", "text": f"Ease {STEP_PCT} %",
                "sub": f"Heart rate {hr_now:.0f} is above your range ({lo:.0f}-{hi:.0f}) and climbing {rising:.1f}/min: press − twice."}
    if hr_now < lo and rising < 1.0:
        return {"kind": "up", "text": f"+{STEP_PCT} %",
                "sub": f"Heart rate {hr_now:.0f} is below your range ({lo:.0f}-{hi:.0f}) and steady: press + twice if your legs agree."}
    return {"kind": "hold", "text": "Hold", "sub": f"On pace: heart rate {hr_now:.0f} in your range ({lo:.0f}-{hi:.0f})."}


def steady(new: dict, t: int, state: Optional[dict]) -> dict:
    """Hold / + / ease verdicts change only after the new one has stood for STEADY_S, so a heart rate
    hovering at a boundary doesn't flip the screen; settle, everything and 'let it settle' after
    your own change show at once. state (kept by the caller between calls) remembers what's shown."""
    if state is None:
        return new
    shown, cand = state.get("shown"), state.get("cand")
    instant = new["kind"] in ("settle", "go") or new["sub"].startswith("Let the last change")
    if shown is None or instant or (shown["kind"], shown["text"]) == (new["kind"], new["text"]):
        state["shown"], state["cand"] = new, None
        return new
    if cand is None or (cand[0]["kind"], cand[0]["text"]) != (new["kind"], new["text"]):
        state["cand"] = (new, t)
    elif t - cand[1] >= STEADY_S:
        state["shown"], state["cand"] = new, None
        return new
    return state["shown"]


def guide(power: list, hr: list, dur: int, target: Optional[float], max_hr: Optional[float],
          since_change: Optional[float] = None, state: Optional[dict] = None) -> dict:
    """The live view of the test: power and hr are the test so far, one value per second (None
    where missing), target the current target in W; state: a dict the caller keeps for the test."""
    t = len(power)
    lt = threshold_hr(max_hr)
    band = hr_band(t, lt)
    done = sum(p or 0 for p in power)
    avg = done / t if t else None
    left = max(0, dur - t)
    proj = (done + (target or avg or 0) * left) / dur if t else None
    hr_now = _mean(hr[-10:])
    trend = slope(hr, t)
    p30 = _mean(power[-30:])
    chart = []
    for i in range(CHART_EVERY - 1, t, CHART_EVERY):
        p = _mean(power[max(0, i - 9): i + 1])
        h = _mean(hr[max(0, i - 4): i + 1])
        chart.append([i + 1, round(p) if p is not None else None, round(h) if h is not None else None])
    return {"t": t, "dur": dur, "left": left, "avg": avg and round(avg), "proj": proj and round(proj),
            "ftp": proj and round(0.95 * proj), "hr": hr_now and round(hr_now), "trend": trend,
            "band": band, "lt": lt, "target": target and round(target),
            "cue": steady(cue(t, dur, hr_now, band, trend, since_change, p30, target), t, state),
            "bands": [[s] + list(hr_band(s, lt)) for s in range(SETTLE_S, dur + 1, 60)],
            "chart": chart}
