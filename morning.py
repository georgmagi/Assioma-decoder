"""
The morning check: resting heart rate and heart-rate variability, lying down, 1 minute to settle
and 2 to measure, with the strap (best over ANT+, every beat), most mornings. Read against your
own normal, the way HRV-guided training does it (Plews et al.; Kiviniemi et al.): the natural log
of RMSSD (ln RMSSD), its 7-day average against your 30-day baseline, with the normal range
baseline +- 0.5 SD. Under the range = strained or not recovered; over it can also mean fatigue
(parasympathetic saturation), so it's flagged, not cheered. Resting heart rate 5+ bpm over its
baseline is a flag of its own.

Stored in morning.json next to the app: a list of {date, time, hr, rmssd, ln_rmssd, beats,
artifact_pct}, one per day (a second check the same day replaces the first).
"""
from __future__ import annotations

import json
import math
import os
from datetime import date, datetime, timedelta
from statistics import mean, stdev
from typing import Optional

import hrv

SETTLE_S, MEASURE_S = 60, 120
MIN_BEATS = 60
MAX_ARTIFACT_PCT = 20.0


class Check:
    """One check in progress: fed the strap's notifications (time, heart rate, RR intervals)."""

    def __init__(self, now: datetime) -> None:
        self.started = now
        self.rr: list = []                         # (time, [ms])
        self.hr: list = []
        self.last_data: Optional[datetime] = None

    def phase(self, now: datetime) -> str:
        t = (now - self.started).total_seconds()
        return "settle" if t < SETTLE_S else "measure" if t < SETTLE_S + MEASURE_S else "done"

    def left_s(self, now: datetime) -> float:
        t = (now - self.started).total_seconds()
        return max(0.0, (SETTLE_S if t < SETTLE_S else SETTLE_S + MEASURE_S) - t)

    def feed(self, t: datetime, bpm: Optional[float], rr_ms: list) -> None:
        self.last_data = t
        if (t - self.started).total_seconds() < SETTLE_S:
            return
        if bpm:
            self.hr.append(bpm)
        if rr_ms:
            self.rr.append((t, list(rr_ms)))

    def result(self, now: datetime) -> dict:
        """{ok, hr, rmssd, ln_rmssd, beats, artifact_pct} or {ok: False, why}."""
        tr = hrv.HrvTracker()
        for t, rr in self.rr:
            tr.add(t, rr)
        tr._judge(final=True)
        beats = tr.beats
        clean = [b.rr for b in beats if not b.artifact]
        if len(beats) < MIN_BEATS:
            return {"ok": False, "why": f"only {len(beats)} beats arrived: is the strap on and connected?"}
        art = 100 * (len(beats) - len(clean)) / len(beats)
        if art > MAX_ARTIFACT_PCT:
            return {"ok": False, "why": f"{art:.0f}% of beats were artifacts: wet the strap and lie still"}
        r = hrv.rmssd(clean)
        hr = mean(self.hr) if self.hr else 60000 / mean(clean)
        return {"ok": True, "date": self.started.astimezone().date().isoformat(),
                "time": self.started.astimezone().strftime("%H:%M"), "hr": round(hr, 1), "rmssd": round(r, 1),
                "ln_rmssd": round(math.log(r), 3), "beats": len(beats), "artifact_pct": round(art, 1)}


def load(path: Optional[str]) -> list:
    if not path:
        return []
    try:
        with open(path, encoding="utf-8") as fh:
            got = json.load(fh)
        return got if isinstance(got, list) else []
    except (OSError, ValueError):
        return []


def save(path: Optional[str], checks: list) -> None:
    if not path:
        return
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(checks, fh, indent=1)
    os.replace(tmp, path)


def add(checks: list, result: dict) -> list:
    """One check per day: the new one replaces any earlier one that day."""
    return sorted([c for c in checks if c["date"] != result["date"]] + [{k: v for k, v in result.items() if k != "ok"}],
                  key=lambda c: c["date"])


def trend(checks: list, today: Optional[date] = None) -> dict:
    """Each day: ln RMSSD, its 7-day mean, the 30-day baseline and normal range (mean +- 0.5 SD over
    the 30 days before), resting HR and its baseline; and today's reading of it. Needs 7 checks in
    the baseline before it judges anything."""
    today = today or date.today()
    days = []
    by = {c["date"]: c for c in checks}
    for c in checks:
        d = date.fromisoformat(c["date"])
        base = [x for x in checks if d - timedelta(days=30) <= date.fromisoformat(x["date"]) < d]
        week = [x["ln_rmssd"] for x in checks if d - timedelta(days=6) <= date.fromisoformat(x["date"]) <= d]
        row = {**c, "ln7": round(mean(week), 3)}
        if len(base) >= 7:
            m, sd = mean(x["ln_rmssd"] for x in base), stdev(x["ln_rmssd"] for x in base)
            row.update(base=round(m, 3), lo=round(m - 0.5 * sd, 3), hi=round(m + 0.5 * sd, 3),
                       hr_base=round(mean(x["hr"] for x in base), 1))
        days.append(row)
    last = days[-1] if days else None
    verdict = None
    if last and "base" in last:
        hr_flag = last["hr"] >= last["hr_base"] + 5
        if last["ln7"] < last["lo"] or hr_flag:
            verdict = ("below", "Below your normal" + (": resting heart rate is up too" if hr_flag else "")
                       + ". Strained or not recovered: keep today easy, or move the hard session.")
        elif last["ln7"] > last["hi"]:
            verdict = ("above", "Above your normal: often fine, but after hard days it can mean deep fatigue. Go by how the warm-up feels.")
        else:
            verdict = ("normal", "Within your normal: train as planned.")
    elif last:
        n = len(checks)
        verdict = ("learning", f"Learning your normal: {n} of 7 checks so far.")
    return {"days": days, "today": by.get(today.isoformat()), "verdict": verdict}
