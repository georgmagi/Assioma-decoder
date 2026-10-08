"""
Today's readiness: one answer (go / easy / rest) from everything the app knows, with the reasons.

The signals, each with how strongly it speaks (research on athlete monitoring: Plews et al. for
HRV, Buchheit 2014 for heart rate at a fixed load, Coggan's form for training load):

- the morning check (morning.py): 7-day ln RMSSD under your normal, or resting HR 5+ bpm over it.
  While it is still learning your normal (under 7 checks) a big drop against the checks so far
  (> 0.4 in ln RMSSD) or a resting HR 5 up is shown, but only as a note: one noisy morning
  shouldn't cancel a session.
- the warm-up (checks.warmup): heart rate at 125 W in today's ride, against your normal. 8+ bpm up
  = easy (6 Oct: 135 vs 126 after two step tests the evening before); 4+ = a note.
- form (training.pmc): under -30 = easy, under -40 = rest; -20 to -30 = a note.
- the knee: your 0-3 score on the last 2 days' rides: 2 (sore) = no hard intervals today, 3
  (flared) = rest from intensity.

The answer is the strongest signal: any "rest" = rest, any "easy" = easy, else go. It suggests;
the plan is never changed by itself. Standard library only.
"""
from __future__ import annotations

from datetime import date, timedelta
from statistics import mean
from typing import Optional

import training

LEVELS = ("go", "easy", "rest")


def _add(out: list, level: str, text: str) -> None:
    out.append({"level": level, "text": text})


def today(morning_trend: dict, rides: list, pmc: dict, warm_normal: Optional[dict], day: Optional[date] = None) -> dict:
    """{level: go/easy/rest, reasons: [{level: go/note/easy/rest, text}], hard_ok: bool}."""
    day = day or date.today()
    reasons: list = []

    # the morning check
    days = morning_trend.get("days") or []
    m = next((x for x in days if x["date"] == day.isoformat()), None)
    if m is None:
        _add(reasons, "note", "No morning check today.")
    elif "base" in m:
        v = (morning_trend.get("verdict") or ["", ""])[0]
        if v == "below":
            _add(reasons, "easy", f"Morning HRV under your normal (7-day ln RMSSD {m['ln7']:.2f}, normal from {m['lo']:.2f})"
                 + (f", resting HR {m['hr']:.0f} vs {m['hr_base']:.0f}" if m["hr"] >= m["hr_base"] + 5 else "") + ".")
        elif v == "above":
            _add(reasons, "note", "Morning HRV above your normal: usually fine, after hard days it can mean deep fatigue.")
        else:
            _add(reasons, "go", f"Morning check within your normal (ln RMSSD {m['ln_rmssd']:.2f}, HR {m['hr']:.0f}).")
    else:
        before = [x for x in days if x["date"] < m["date"]]
        if len(before) >= 2:
            ln0, hr0 = mean(x["ln_rmssd"] for x in before), mean(x["hr"] for x in before)
            if m["ln_rmssd"] < ln0 - 0.4 or m["hr"] >= hr0 + 5:
                _add(reasons, "note", f"Morning check lower than your checks so far (ln RMSSD {m['ln_rmssd']:.2f} vs "
                     f"{ln0:.2f}, HR {m['hr']:.0f} vs {hr0:.0f}); still learning your normal.")
            else:
                _add(reasons, "go", f"Morning check like your others (ln RMSSD {m['ln_rmssd']:.2f}, HR {m['hr']:.0f}); still learning your normal.")
        else:
            _add(reasons, "note", f"Morning check done (ln RMSSD {m['ln_rmssd']:.2f}); your normal needs a few more.")

    # today's warm-up
    mine = [r for r in rides if r.get("source") == "Assioma" and training.local_day(r["start_utc"]) == day]
    w = next((r["warmup"] for r in mine if r.get("warmup")), None)        # newest first: today's latest
    if w and warm_normal:
        up = w["hr"] - warm_normal["hr"]
        if up >= 8:
            _add(reasons, "easy", f"Today's warm-up {w['hr']:.0f} bpm at 125 W, {up:.0f} over your normal {warm_normal['hr']:.0f}.")
        elif up >= 4:
            _add(reasons, "note", f"Today's warm-up {w['hr']:.0f} bpm at 125 W, a little over your normal {warm_normal['hr']:.0f}.")
        else:
            _add(reasons, "go", f"Today's warm-up {w['hr']:.0f} bpm at 125 W (normal {warm_normal['hr']:.0f}).")

    # form
    if pmc.get("tsb"):
        f = pmc["tsb"][-1]
        if f < -40:
            _add(reasons, "rest", f"Form {f:.0f}: very deep fatigue.")
        elif f < -30:
            _add(reasons, "easy", f"Form {f:.0f}: deep fatigue.")
        elif f < -20:
            _add(reasons, "note", f"Form {f:.0f}: a heavy load, fine if the rest agrees.")
        else:
            _add(reasons, "go", f"Form {f:+.0f}.")

    # the knee: your score on the last 2 days' rides
    recent = [r for r in rides if (r.get("knee") or {}).get("score") is not None
              and day - training.local_day(r["start_utc"]) <= timedelta(days=2)]
    if recent:
        k = max(r["knee"]["score"] for r in recent)
        if k >= 3:
            _add(reasons, "rest", "Knee flared on a recent ride: no intensity until it settles.")
        elif k == 2:
            _add(reasons, "easy", "Knee sore after a recent ride: no hard intervals today.")
        elif k == 1:
            _add(reasons, "note", "A knee niggle recently: keep the cadence up, watch the torque.")

    strongest = "rest" if any(x["level"] == "rest" for x in reasons) else \
        "easy" if any(x["level"] == "easy" for x in reasons) else "go"
    return {"level": strongest, "reasons": reasons, "hard_ok": strongest == "go"}


def is_hard(day_entry: dict) -> bool:
    """A plan day (plan.overview) with intervals: IF 0.8+ or anything over 90 % FTP."""
    return bool((day_entry.get("if") or 0) >= 0.8 or ((day_entry.get("split") or {}).get("hard") or 0) > 0)
