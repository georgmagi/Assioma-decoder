"""The training planner: import, forecast, plan against what was ridden, the status for Claude."""
from datetime import date, datetime, timedelta, timezone

import plan as pl
import training as tr
import workout as wk

MON = date(2026, 10, 5)
LIB = dict(wk.BUILT_IN)

TEXT = """Week 1
Mon: rest
Tue: Threshold 2x20 | 15m 50-70%, 2x(20m 90%, 5m 55%), 10m 50%
Wed: 60m Z2
Thu: Threshold steps
- Sat: 2h Z2
Week 2
Mon: off
Tue: 3x(12m 90%, 4m 55%)
2026-10-25: 90m Z2
Fri 12m 90%
Sun: 12m 900%
"""


def test_import_reads_weeks_days_and_says_what_is_wrong():
    days, problems = pl.parse_import(TEXT, MON, LIB)
    assert days["2026-10-05"] == {"rest": True} and days["2026-10-08"] == {"ref": "vt1-steps"}
    assert days["2026-10-06"]["name"] == "Threshold 2x20" and days["2026-10-10"]["text"] == "2h Z2"
    assert days["2026-10-13"]["text"] == "3x(12m 90%, 4m 55%)" and "2026-10-25" in days
    assert [n for n, _ in problems] == [11, 12]                     # no colon; a power of 900 %


def _ride(d, tss):
    t = datetime(d.year, d.month, d.day, 8, tzinfo=timezone.utc)
    return {"start_utc": t.isoformat(), "tss": tss, "ride_s": 3600, "work_kj": 500.0}


def test_forecast_continues_from_today_with_the_planned_load():
    today = MON + timedelta(days=2)                                  # Wednesday of week 1
    rides = [_ride(MON - timedelta(days=k), 60) for k in range(1, 30)] + [_ride(MON + timedelta(days=1), 80)]
    plan = {"start": MON.isoformat(), "weeks": 2, "days": pl.parse_import(TEXT, MON, LIB)[0]}
    o = pl.overview(plan, LIB, rides, 200, today)
    by = {d["day"]: d for d in o["days"]}
    assert by["2026-10-06"]["status"] == "done" and by["2026-10-05"]["status"] == "rest"
    assert by["2026-10-07"]["status"] == "today" and by["2026-10-08"]["status"] == "upcoming"
    f = o["forecast"]["plan"]
    assert f["days"][0] == "2026-10-08" and f["days"][-1] == "2026-10-18"
    # by hand: the model from today's values with each day's planned TSS
    p = tr.pmc(rides, today)
    c, a = p["ctl"][-2], p["atl"][-2]
    t = by["2026-10-07"]["tss"]
    c, a = c + (t - c) / 42, a + (t - a) / 7
    for d, tss in zip(f["days"], f["tss"]):
        c, a = c + (tss - c) / 42, a + (tss - a) / 7
    assert abs(f["ctl"][-1] - c) < 0.02 and abs(f["atl"][-1] - a) < 0.02
    assert o["weeks"][0]["tss"] > 0 and o["weeks"][0]["split"]["hard"] > 0


def test_plan_shorthand_days_become_workouts_and_today_resolves():
    plan = {"start": MON.isoformat(), "weeks": 2, "days": pl.parse_import(TEXT, MON, LIB)[0]}
    ws = pl.plan_workouts(plan)
    assert ws["plan:2026-10-06"]["name"] == "Threshold 2x20" and wk.total_s(ws["plan:2026-10-06"]) == 75 * 60
    lib = {**LIB, **ws}
    assert pl.day_id(plan, MON + timedelta(days=1), lib) == "plan:2026-10-06"
    assert pl.day_id(plan, MON + timedelta(days=3), lib) == "vt1-steps" and pl.day_id(plan, MON, lib) is None


def test_status_for_claude_has_the_numbers_and_the_format():
    rides = [{**_ride(date.today() - timedelta(days=k), 50), "id": f"r{k}", "np": 150, "avg_hr": 135,
              "decoupling_pct": 2.0, "name": "Z2"} for k in range(1, 10)]
    prog = tr.progress(rides)
    text = pl.status_text(prog, rides, {"ftp": 199}, None)
    assert "Fitness (CTL" in text and "FTP setting 199" in text and "Week 1" in text and "Mon: rest" in text


def test_a_second_block_follows_the_first_and_removing_it_keeps_its_days():
    from datetime import date
    import plan as pl2
    p = {"start": "2026-10-19", "weeks": 4, "days": {"2026-10-19": {"rest": True}, "2026-11-17": {"text": "60m Z2", "name": "Z2"}}}
    assert [b["start"] for b in pl2.blocks(p)] == ["2026-10-19"]                 # an old plan: one block
    pl2.add_block(p, None, 4)
    assert pl2.blocks(p)[1] == {"start": "2026-11-16", "weeks": 4}               # the Monday after block 1
    assert p["start"] == "2026-10-19" and p["weeks"] == 4                       # older copies still read block 1
    pl2.add_block(p, date(2026, 11, 20), 2)                                       # a date inside: after the last instead
    assert pl2.blocks(p)[2]["start"] == "2026-12-14"
    o = pl2.overview(p, {}, [], 240, date(2026, 10, 25))
    assert [b["n"] for b in o["blocks"]] == [1, 2, 3] and o["end"] == "2026-12-27" and len(o["days"]) == 70
    assert o["blocks"][1]["count"] == 28 and o["days"][o["blocks"][1]["first"] + 1]["name"] == "Z2"
    pl2.remove_last_block(p); pl2.remove_last_block(p)
    assert len(pl2.blocks(p)) == 1 and "2026-11-17" in p["days"]                  # the days stay
    pl2.remove_last_block(p)
    assert len(pl2.blocks(p)) == 1                                                # never the last one
