"""Workouts: Zwift files, the built-in ones, and the player's targets over ride time."""
import pytest

import workout as wk

ZWO = """<workout_file>
  <name>Test</name><description>for tests</description>
  <workout>
    <Warmup Duration="600" PowerLow="0.5" PowerHigh="0.7"/>
    <IntervalsT Repeat="2" OnDuration="60" OffDuration="30" OnPower="1.2" OffPower="0.5"/>
    <SteadyState Duration="300" Power="0.8"/>
    <FreeRide Duration="120" FlatRoad="1"/>
    <Cooldown Duration="300" PowerLow="0.6" PowerHigh="0.4"/>
  </workout>
</workout_file>"""


def test_zwift_file():
    w = wk.parse_zwo(ZWO)
    assert w["name"] == "Test" and [s["dur"] for s in w["steps"]] == [600, 60, 30, 60, 30, 300, 120, 300]
    assert w["steps"][0]["lo"] == 0.5 and w["steps"][0]["hi"] == 0.7 and w["steps"][6]["lo"] is None
    assert wk.total_s(w) == 1500


@pytest.mark.parametrize("bad", ["<foo/>", "<workout_file><workout/></workout_file>",
                                 '<workout_file><workout><SteadyState Duration="60" Power="250"/></workout></workout_file>'])
def test_bad_files_are_refused(bad):
    with pytest.raises(ValueError):
        wk.parse_zwo(bad)


def test_player_targets_ramps_skip_and_intensity():
    p = wk.Player(wk.parse_zwo(ZWO), ftp=200)
    assert p.at(0)["target_w"] == 100 and p.at(300)["target_w"] == pytest.approx(120)   # warm-up ramp
    a = p.at(630)
    assert a["target_w"] == pytest.approx(240) and a["left"] == 30 and a["next"]["lo"] == 0.5
    assert p.at(1000)["target_w"] == pytest.approx(160) and p.at(1100)["target_w"] is None   # steady, free ride
    p.skip(630)                                                                          # on to the recovery
    assert p.at(630)["index"] == 2 and p.at(630)["in_step"] == 0
    p.nudge(+5)
    assert p.at(630)["target_w"] == pytest.approx(105)
    p.nudge(-90)
    assert p.intensity == 0.5
    assert p.at(10_000)["done"] and p.at(10_000)["target_w"] is None


def test_built_in_workouts_are_sane_and_the_folder_adds_files(tmp_path):
    for w in wk.BUILT_IN.values():
        assert all(s["dur"] > 0 and 0.3 <= s["lo"] <= 1.3 for s in w["steps"])
    (tmp_path / "mine.zwo").write_text(ZWO, encoding="utf-8")
    (tmp_path / "broken.zwo").write_text("<nope", encoding="utf-8")
    lib = wk.library(str(tmp_path))
    assert "zwo:mine" in lib and "zwo:broken" not in lib and "vt1-steps" in lib
