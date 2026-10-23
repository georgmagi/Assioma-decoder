"""The ride summary, computed from the per-second CSV the recorder writes."""
import os
from datetime import date, datetime, timedelta

import assioma_recorder as ar
import ride_summary as rs

T0 = datetime(2026, 9, 28, 6)


def _ride(tmp_path, seconds, row):
    """A ride through the recorder's own Session, so the CSV is exactly what a real ride leaves."""
    s = ar.Session(str(tmp_path))
    s.start(T0)
    for i in range(seconds):
        s._second({"time": T0 + timedelta(seconds=i), **row(i)})
    s.finish()
    return s.base + rs.SUFFIX


def test_steady_ride_headline_numbers(tmp_path):
    path = _ride(tmp_path, 3600, lambda i: {"power_w": 150, "heart_rate_bpm": 130, "cadence_rpm": 90})
    s = rs.summarize(rs.load(path), ftp=200)
    st = s["stats"]
    assert st["ride_s"] == 3600 and st["avg_power"] == 150 and abs(st["np"] - 150) < 1e-9
    assert abs(st["if"] - 0.75) < 1e-9 and abs(st["tss"] - 56.25) < 1e-6      # 1 h at IF 0.75
    assert st["work_kj"] == 540 and st["avg_hr"] == 130 and st["avg_cadence"] == 90
    assert abs(st["decoupling_pct"]) < 1e-9                                      # nothing drifts
    assert s["zones"]["Z2"] == 3600 and sum(s["zones"].values()) == 3600         # 75 % of FTP is still Z2
    assert s["best"]["1200"] == 150 and s["best"]["5"] == 150


def test_best_powers_and_zones_see_an_effort(tmp_path):
    path = _ride(tmp_path, 900, lambda i: {"power_w": 300 if 100 <= i < 400 else 100})
    s = rs.summarize(rs.load(path), ftp=250)
    assert s["best"]["300"] == 300 and s["best"]["5"] == 300 and s["best"]["1200"] is None
    assert s["zones"]["Z5"] == 300 and s["zones"]["Z1"] == 600                 # 120 % and 40 % of FTP


def test_phase_angles_average_across_top_dead_centre(tmp_path):
    path = _ride(tmp_path, 60, lambda i: {"power_w": 100, "pp_left_start_deg": 350 if i % 2 else 10})
    left = rs.summarize(rs.load(path))["legs"]["left"]
    assert min(left["pp_start"], 360 - left["pp_start"]) < 1e-6                  # 0°, not 180°


def test_ride_list_caches_short_summaries_newest_first(tmp_path):
    a = _ride(tmp_path, 120, lambda i: {"power_w": 100})
    os.utime(a, (1, 1))
    rides = rs.ride_list(str(tmp_path), ftp=200)
    assert len(rides) == 1 and rides[0]["avg_power"] == 100 and rides[0]["fit"]
    assert os.path.exists(rs.cache_path(str(tmp_path), rides[0]["id"]))
    assert rs.ride_list(str(tmp_path)) == rides                                  # from the cache
    # a new FTP redoes every TSS (Plan's planned TSS uses the new one: the two must agree)
    again = rs.ride_list(str(tmp_path), ftp=250)
    assert again[0]["ftp"] == 250 and abs(again[0]["tss"] - rides[0]["tss"] * (200 / 250) ** 2) < 1e-6


def test_a_new_ftp_leaves_past_rides_at_the_ftp_they_were_ridden_at(tmp_path):
    _ride(tmp_path, 3600, lambda i: {"power_w": 150})                          # a ride today (its id)
    later = (date.today() + timedelta(days=1)).isoformat()
    hist = [["0001-01-01", 200], [later, 240]]
    r = rs.ride_list(str(tmp_path), 240, hist)[0]
    assert r["ftp"] == 200 and abs(r["tss"] - 56.25) < 1e-6                     # 1 h at IF 0.75, as it was
    assert rs.ride_list(str(tmp_path), 260, hist + [["2099-01-01", 260]])[0]["tss"] == r["tss"]
    rs.save_meta(str(tmp_path), r["id"], {"workout": {"name": "Z2", "ftp": 150}})   # the workout's own FTP wins
    assert rs.ride_list(str(tmp_path), 240, hist)[0]["ftp"] == 150
    h = [["0001-01-01", 200], ["2026-10-19", 240]]
    assert rs.ftp_at(h, "2026-10-18") == 200 and rs.ftp_at(h, "2026-10-19") == 240
    assert rs.ftp_at([["2026-09-12", 199]], "2026-08-01") == 199 and rs.ftp_at([], "2026-08-01", 240) == 240


def _fit_copy(tmp_path, name, seconds, start):
    """Another app's FIT file of a steady ride (written with the recorder's own FIT writer)."""
    import fit_writer
    rows = [{"time": start + timedelta(seconds=i), "power_w": 180, "heart_rate_bpm": 140, "cadence_rpm": 85}
            for i in range(seconds)]
    fit_writer.write_activity(rows, str(tmp_path / (name + ".fit")))


def test_other_apps_fit_rides_are_listed_with_their_workout_name(tmp_path):
    _fit_copy(tmp_path, "OpenCycling_2026-09-20_3x12min_Threshold", 1200, T0 - timedelta(days=2))
    _fit_copy(tmp_path, "OpenCycling_2026-09-26_60min_Endurance", 40, T0 - timedelta(days=1))   # an aborted start
    rides = rs.ride_list(str(tmp_path), ftp=200)
    assert [(r["name"], r["source"]) for r in rides] == [("3x12min Threshold", "OpenCycling")]
    assert rides[0]["ride_s"] == 1200 and rides[0]["avg_power"] == 180 and rides[0]["tss"] > 0
    s = rs.summarize_file(str(tmp_path), rides[0]["id"], ftp=200)
    assert s["stats"]["avg_hr"] == 140 and s["name"] == "3x12min Threshold"


def test_a_fit_ride_recorded_alongside_the_recorder_is_not_counted_twice(tmp_path):
    own = _ride(tmp_path, 1800, lambda i: {"power_w": 150, "heart_rate_bpm": 130})
    _fit_copy(tmp_path, "OpenCycling_2026-09-28_Z2", 1800, T0 + timedelta(seconds=20))   # the same ride
    _fit_copy(tmp_path, "OpenCycling_2026-09-27_Z2", 1800, T0 - timedelta(days=1))       # another day
    ids = [r["id"] for r in rs.ride_list(str(tmp_path), ftp=200)]
    assert ids == [os.path.basename(own[: -len(rs.SUFFIX)]), "OpenCycling_2026-09-27_Z2"]


def test_ride_ids_are_never_paths(tmp_path):
    _ride(tmp_path, 60, lambda i: {"power_w": 100})
    for bad in ("../x", "..\\x", "C:x", ".cache", "", None):
        assert rs.find_ride(str(tmp_path), bad) is None
