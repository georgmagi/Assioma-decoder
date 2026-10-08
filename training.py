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
from statistics import median
from typing import Optional

import checks
import eftp

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


def fitness_checks(rides: list[dict]) -> dict:
    """Every Fitness check (checks.py), oldest first, compared at the same watts: the reference is the
    median of the checks' middle steps, so an FTP change doesn't move it. With the room temperature
    of each, the heat model (bpm per degree) and heart rate corrected to 20 C."""
    got = sorted((r for r in rides if r.get("check")), key=lambda r: r["start_utc"])
    if not got:
        return {"checks": [], "ref_w": None, "heat": None}
    mids = sorted(median(s["power"] for s in r["check"]["steps"]) for r in got)
    ref = round(mids[len(mids) // 2] / 5) * 5                     # the checks' typical middle step
    heat = checks.heat_model([(r.get("temp_c"), checks.hr_at(r["check"], ref)) for r in got])
    out = []
    for r in got:
        c, hr = r["check"], checks.hr_at(r["check"], ref)
        corr = hr - heat["bpm_per_c"] * (r["temp_c"] - 20) if heat and r.get("temp_c") is not None else None
        out.append({"id": r.get("id"), "day": local_day(r["start_utc"]).isoformat(), "temp_c": r.get("temp_c"),
                    "rh_pct": r.get("rh_pct"), "hr_ref": hr, "hr_ref_20c": corr, "hrr60": c.get("hrr60"),
                    "a1": [st.get("a1") for st in c["steps"]], "rmssd": [st.get("rmssd") for st in c["steps"]],
                    "a1_ref": checks.a1_at(c, ref),
                    "steps": c["steps"]})
    return {"checks": out, "ref_w": ref, "heat": heat}


def progress(rides: list[dict], today: Optional[date] = None) -> dict:
    """Everything the Progress screen shows: the rides by local day (for the calendar), the weeks,
    the fitness/fatigue/form series and all-time totals."""
    today = today or date.today()
    by_day: dict = {}
    for r in sorted(rides, key=lambda r: r["start_utc"]):
        by_day.setdefault(local_day(r["start_utc"]).isoformat(), []).append(
            {k: r.get(k) for k in ("id", "start_utc", "ride_s", "tss", "np", "avg_hr", "work_kj", "zones")})
    # VT1 ride by ride (vt1.py): crossings and bounds, oldest first
    vt1 = [{"id": r.get("id"), "day": local_day(r["start_utc"]).isoformat(), "hr": r.get("vt1_hr"),
            "power": r.get("vt1_power"), "beats_pct": r.get("vt1_beats_pct"),
            "vt2": {"hr": r.get("vt2_hr"), "power": r.get("vt2_power")}}         # alpha 1 = 0.5
           for r in sorted(rides, key=lambda r: r["start_utc"]) if r.get("vt1_hr") or r.get("vt1_power")]
    return {
        "fitness_checks": fitness_checks(rides),
        "today": today.isoformat(),
        "days": by_day,
        "vt1": vt1,
        "eftp": eftp.history(rides, today, lambda r: local_day(r["start_utc"])),
        # aerobic efficiency ride by ride: power per heartbeat in steady Z2, and Pw:HR decoupling
        "efficiency": [{"id": r.get("id"), "day": local_day(r["start_utc"]).isoformat(), "ef": r.get("ef_z2"),
                        "power": r.get("ef_z2_power"), "minutes": r.get("ef_z2_min"),
                        "decoupling": r.get("decoupling_pct")}
                       for r in sorted(rides, key=lambda r: r["start_utc"]) if r.get("ef_z2") or r.get("decoupling_pct") is not None],
        # heart rate against power, ride by ride (checks.hr_model): HR at 150 W (lower = fitter), tau
        # (faster = fitter); only the recorder's own rides, one strap, the same pedals
        "hr_fitness": [{"id": r.get("id"), "day": local_day(r["start_utc"]).isoformat(), "name": r.get("name"),
                        "temp_c": r.get("temp_c"), **(r.get("hr_model") or {})}
                       for r in sorted(rides, key=lambda r: r["start_utc"])
                       if r.get("source") == "Assioma" and (r.get("hr_model") or {}).get("hr150") is not None],
        "weeks": weeks(rides, today),
        "pmc": pmc(rides, today),
        "totals": {"rides": len(rides), "time_s": sum(r.get("ride_s") or 0 for r in rides),
                   "tss": sum(r.get("tss") or 0 for r in rides), "work_kj": sum(r.get("work_kj") or 0 for r in rides)},
    }
