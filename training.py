"""
Long-term training numbers from the ride list (ride_summary.ride_list): daily training load,
fitness / fatigue / form, and weekly totals. Days and weeks are local (a ride at 00:30 belongs to
the day it was ridden on), weeks start on Monday.

Fitness (CTL) and fatigue (ATL) are exponentially weighted averages of the daily TSS with time
constants of 42 and 7 days, the usual Performance Manager model:

    CTL_d = CTL_(d-1) + (TSS_d - CTL_(d-1)) / 42
    ATL_d = ATL_(d-1) + (TSS_d - ATL_(d-1)) / 7
    Form_d = CTL_(d-1) - ATL_(d-1)          (how fresh you are going into day d)

Both start at 0 on the day of the first ride, so fitness takes weeks to mean much.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional

CTL_DAYS, ATL_DAYS = 42, 7


def local_day(start_utc: str) -> date:
    return datetime.fromisoformat(start_utc).astimezone().date()


def daily_tss(rides: list[dict]) -> dict:
    """{local date: summed TSS}; rides without TSS (no FTP set) count as 0."""
    out: dict = {}
    for r in rides:
        d = local_day(r["start_utc"])
        out[d] = out.get(d, 0.0) + (r.get("tss") or 0.0)
    return out


def pmc(rides: list[dict], today: Optional[date] = None) -> dict:
    """Day by day from the first ride to today: TSS, fitness, fatigue and form."""
    if not rides:
        return {"days": [], "tss": [], "ctl": [], "atl": [], "tsb": []}
    load = daily_tss(rides)
    first = min(load)
    today = max(today or date.today(), max(load))
    days, tss, ctl, atl, tsb = [], [], [], [], []
    c = a = 0.0
    d = first
    while d <= today:
        t = load.get(d, 0.0)
        tsb.append(c - a)                         # going into the day
        c += (t - c) / CTL_DAYS
        a += (t - a) / ATL_DAYS
        days.append(d.isoformat())
        tss.append(round(t, 1))
        ctl.append(round(c, 2))
        atl.append(round(a, 2))
        d += timedelta(days=1)
    return {"days": days, "tss": tss, "ctl": ctl, "atl": atl, "tsb": [round(x, 2) for x in tsb]}


def weeks(rides: list[dict], today: Optional[date] = None, count: int = 12) -> list[dict]:
    """The last `count` Monday-to-Sunday weeks up to today (empty ones too), oldest first:
    {start (Monday), rides, time_s, tss, work_kj}."""
    today = today or date.today()
    monday = today - timedelta(days=today.weekday())
    out = []
    for k in range(count - 1, -1, -1):
        start = monday - timedelta(weeks=k)
        out.append({"start": start.isoformat(), "rides": 0, "time_s": 0, "tss": 0.0, "work_kj": 0.0})
    index = {w["start"]: w for w in out}
    for r in rides:
        d = local_day(r["start_utc"])
        w = index.get((d - timedelta(days=d.weekday())).isoformat())
        if w is not None:
            w["rides"] += 1
            w["time_s"] += r.get("ride_s") or 0
            w["tss"] += r.get("tss") or 0.0
            w["work_kj"] += r.get("work_kj") or 0.0
    return out


def progress(rides: list[dict], today: Optional[date] = None) -> dict:
    """Everything the Progress screen shows: the rides by local day (for the calendar), the weeks,
    the fitness/fatigue/form series and all-time totals."""
    today = today or date.today()
    by_day: dict = {}
    for r in sorted(rides, key=lambda r: r["start_utc"]):
        by_day.setdefault(local_day(r["start_utc"]).isoformat(), []).append(
            {k: r.get(k) for k in ("id", "start_utc", "ride_s", "tss", "np", "avg_hr", "work_kj")})
    return {
        "today": today.isoformat(),
        "days": by_day,
        "weeks": weeks(rides, today),
        "pmc": pmc(rides, today),
        "totals": {"rides": len(rides), "time_s": sum(r.get("ride_s") or 0 for r in rides),
                   "tss": sum(r.get("tss") or 0 for r in rides), "work_kj": sum(r.get("work_kj") or 0 for r in rides)},
    }
