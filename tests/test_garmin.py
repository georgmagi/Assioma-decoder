"""Garmin pull (garmin_pull.py): only resting-level heart rate is kept; the server writes garmin.json."""
import json
import threading
from datetime import date, datetime, timedelta, timezone

import cloud_sync as cs
import garmin_pull as gp

NIGHT0 = datetime(2026, 10, 23, 21, 30, tzinfo=timezone.utc)      # asleep 00:30 local


def _ms(dt):
    return int(dt.timestamp() * 1000)


def _sleep(start=NIGHT0, hours=8, with_series=True):
    end = start + timedelta(hours=hours)
    pts = [{"startGMT": _ms(start + timedelta(minutes=2 * i)), "value": 60 - min(i, 120) / 12}
           for i in range(hours * 30)]
    pts[10]["value"] = 160                                  # an optics spike mid-night
    return {"dailySleepDTO": {"sleepStartTimestampGMT": _ms(start), "sleepEndTimestampGMT": _ms(end)},
            **({"sleepHeartRate": pts} if with_series else {})}


def test_a_night_keeps_resting_heart_rate_and_drops_the_spikes():
    rec = gp.day_record(_sleep(), {"restingHeartRate": 49})
    assert rec["resting_hr"] == 49 and rec["sleep_start"].startswith("2026-10-23T21:30")
    s = rec["sleep_hr"]
    assert s["min"] == 50 and s["n"] == 239 and max(b for _, b in rec["samples"]) < gp.SLEEP_MAX
    assert 50 <= s["low30"] < 50.5 and s["low30"] < s["avg"] < 60


def test_without_the_sleep_series_the_days_values_inside_the_night_are_used():
    day = {"restingHeartRate": 51, "heartRateValues": [
        [_ms(NIGHT0 - timedelta(hours=3)), 150],             # a ride the evening before: never kept
        [_ms(NIGHT0 + timedelta(hours=1)), 55], [_ms(NIGHT0 + timedelta(hours=2)), 52],
        [_ms(NIGHT0 + timedelta(hours=10)), 95]]}            # the next day: not the night
    rec = gp.day_record(_sleep(with_series=False), {"restingHeartRate": 51}, day)
    assert [b for _, b in rec["samples"]] == [55, 52] and rec["sleep_hr"]["low30"] is None
    assert gp.day_record({}, {}) is None                     # nothing synced yet


class FakeGarmin:
    def __init__(self):
        self.asked = []

    def get_sleep_data(self, d):
        self.asked.append(d)
        night = datetime.fromisoformat(d).replace(tzinfo=timezone.utc) - timedelta(hours=2, minutes=30)
        return _sleep(night)

    def get_heart_rates(self, d):
        return {"restingHeartRate": 50}


def test_the_first_pull_fills_two_weeks_then_only_the_last_days():
    g = FakeGarmin()
    first = gp.pull(g, {}, today=date(2026, 10, 24))
    assert len(first["days"]) == gp.FIRST_DAYS + 1 and max(first["days"]) == "2026-10-24"
    g.asked.clear()
    again = gp.pull(g, first, today=date(2026, 10, 25))
    assert len(g.asked) == gp.DAYS_BACK + 1 and len(again["days"]) == gp.FIRST_DAYS + 2


def test_the_server_writes_garmin_json_as_a_synced_file(tmp_path, monkeypatch):
    import sync_server
    monkeypatch.setattr(gp, "client_from_tokens", lambda tokens=None: FakeGarmin())
    store = sync_server.Store(str(tmp_path / "data"), str(tmp_path / "var"))
    stop = sync_server.start_garmin(store, every=3600)
    for _ in range(200):
        if gp.REL in store.files:
            break
        threading.Event().wait(0.02)
    stop.set()
    assert gp.REL in store.manifest()["files"] and cs.valid_path(gp.REL)
    days = json.load(open(store.path(gp.REL)))["days"]
    assert days and all("samples" in r for r in days.values())


def test_before_the_login_nothing_is_written(tmp_path):
    lines, wrote = [], []
    stop = threading.Event()
    t = threading.Thread(target=gp.pull_loop, args=(lambda: None, wrote.append, stop, lines.append, 3600, str(tmp_path / "none")))
    t.start()
    threading.Event().wait(0.2)
    stop.set(); t.join(2)
    assert wrote == [] and lines == ["garmin: not logged in (garmin_pull.py login)"]


def test_mornings_look_every_20_minutes_until_the_night_is_in():
    from datetime import datetime, timezone
    import garmin_pull as gp
    utc = lambda h, m=0: datetime(2026, 10, 25, h, m, tzinfo=timezone.utc)      # Tallinn = UTC+2 that day
    two_h = 2 * 3600
    assert gp.next_wait(two_h, {}, utc(5, 10)) == 20 * 60                       # 07:10, no night yet
    assert gp.next_wait(two_h, {"2026-10-25": {"sleep_hr": {"low30": 50}}}, utc(5, 10)) == two_h   # in: back to 2 h
    assert gp.next_wait(two_h, {}, utc(9, 30)) == two_h                         # 11:30: the window is over
    w = gp.next_wait(two_h, {}, utc(3, 30))                                     # 05:30: wait only to 06:00
    assert 29 * 60 < w < 32 * 60
