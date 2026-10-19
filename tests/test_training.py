"""Long-term numbers: fitness / fatigue / form, weekly totals, heart-rate zones."""
from datetime import date, datetime, timedelta, timezone

import ride_summary as rs
import training as tr


def _ride(day: date, tss: float, secs: int = 3600, hour: int = 12) -> dict:
    local = datetime(day.year, day.month, day.day, hour).astimezone()      # noon local time
    return {"start_utc": local.astimezone(timezone.utc).isoformat(), "tss": tss, "ride_s": secs, "work_kj": 500.0}


def test_fitness_and_fatigue_follow_the_42_and_7_day_averages():
    d0 = date(2026, 9, 1)
    p = tr.pmc([_ride(d0 + timedelta(days=i), 100) for i in range(60)], today=d0 + timedelta(days=59))
    assert len(p["days"]) == 60 and p["days"][0] == "2026-09-01"
    k42, k7 = 1 - 1 / 42, 1 - 1 / 7
    assert abs(p["ctl"][-1] - 100 * (1 - k42 ** 60)) < 0.01          # 100 TSS a day, 60 days
    assert abs(p["atl"][-1] - 100 * (1 - k7 ** 60)) < 0.01
    assert p["tsb"][0] == 0 and p["tsb"][1] < 0                         # fatigue rises faster than fitness


def test_form_comes_back_after_rest_days():
    d0 = date(2026, 9, 1)
    p = tr.pmc([_ride(d0 + timedelta(days=i), 120) for i in range(21)], today=d0 + timedelta(days=35))
    assert p["tss"][-1] == 0 and p["tsb"][20] < 0 and p["tsb"][-1] > 0   # 2 weeks off: fresh again
    assert p["ctl"][-1] < p["ctl"][20]                                  # and a bit less fit


def test_two_rides_on_one_day_add_up():
    d = date(2026, 9, 20)
    assert tr.daily_tss([_ride(d, 40, hour=8), _ride(d, 60, hour=18)]) == {d: 100}


def test_weeks_are_monday_to_sunday_and_include_empty_ones():
    today = date(2026, 9, 27)                                           # a Sunday
    w = tr.weeks([_ride(date(2026, 9, 21), 50), _ride(date(2026, 9, 27), 70), _ride(date(2026, 9, 14), 30)], today, 3)
    assert [x["start"] for x in w] == ["2026-09-07", "2026-09-14", "2026-09-21"]
    assert [x["rides"] for x in w] == [0, 1, 2] and w[-1]["tss"] == 120 and w[-1]["time_s"] == 7200


def test_heart_rate_zones_use_max_heart_rate():
    t0 = datetime(2026, 9, 28, 6)
    rows = [{"time": t0 + timedelta(seconds=i), "power_w": 150, "heart_rate_bpm": hr, "rr_ms": []}
            for i, hr in enumerate([100] * 10 + [140] * 20 + [160] * 5 + [175] * 5 + [190] * 3)]
    z = rs.summarize(rows, ftp=200, max_hr=200)["hr_zones"]
    # 200 bpm max: Z1 <= 120, Z2 <= 140, Z3 <= 160, Z4 <= 180, Z5 above
    assert z == {"Z1": 10, "Z2": 20, "Z3": 5, "Z4": 5, "Z5": 3}
    assert sum(rs.summarize(rows, ftp=200)["hr_zones"].values()) == 0   # no max set: none
