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
    for k, w in wk.BUILT_IN.items():
        top = 1.8 if k == "ramp-test" else 1.3                   # a ramp test climbs until you stop
        assert all(s["dur"] > 0 and (s["lo"] is None or 0.3 <= s["lo"] <= top) for s in w["steps"])   # None: free
    (tmp_path / "mine.zwo").write_text(ZWO, encoding="utf-8")
    (tmp_path / "broken.zwo").write_text("<nope", encoding="utf-8")
    lib = wk.library(str(tmp_path))
    assert "zwo:mine" in lib and "zwo:broken" not in lib and "vt1-steps" in lib


def test_shorthand_reads_like_a_coach_writes():
    st = wk.parse_text('10m 50-65%, 3x(12m 90%, 4m 55%), 10m 60-40% "cool down"')
    assert [s["dur"] for s in st] == [600] + [720, 240] * 3 + [600]
    assert st[0]["lo"] == 0.5 and st[0]["hi"] == 0.65 and st[1]["label"] == "1/3" and st[-1]["label"] == "cool down"
    assert wk.parse_text("1h30m Z2, 90s free")[0] == {"dur": 5400, "lo": 0.65, "hi": 0.65, "label": ""}
    assert wk.parse_text("1h30m Z2, 90s free")[1]["lo"] is None


@pytest.mark.parametrize("bad", ["", "12m", "12x 90%", "12m 900%", "3x(12m 90%", "2m30 90%", "8h 50%"])
def test_shorthand_says_what_is_wrong(bad):
    with pytest.raises(ValueError):
        wk.parse_text(bad)


def test_shorthand_and_zwift_round_trip():
    text = '10m 50-65%, 3x(12m 90%, 4m 55%), 10m 60-40% "cool down"'
    st = wk.parse_text(text)
    assert wk.to_text(st) == text
    back = wk.parse_zwo(wk.to_zwo("3x12", "test & <check>", st))
    assert back["name"] == "3x12" and [(s["dur"], s["lo"], s["hi"]) for s in back["steps"]] == [(s["dur"], s["lo"], s["hi"]) for s in st]


def test_a_ramp_test_is_estimated_as_ridden_not_to_its_last_step():
    w = wk.BUILT_IN["ramp-test"]
    ridden = wk.as_ridden(w)
    tops = [s["lo"] for s in ridden["steps"] if s["dur"] <= 60 and s["lo"] == s["hi"]]
    assert max(tops) == pytest.approx(1.4) and ridden["steps"][-1]["label"] == "cool down"
    assert [s for s in ridden["steps"] if s["lo"] == pytest.approx(1.4)][0]["dur"] == 30
    est, written = wk.estimate(w, 240), wk.targets(w)
    assert est["duration_s"] == 5 * 60 + 9 * 60 + 30 + 600 < len(written)
    assert 20 < est["tss"] < 35                                    # Oct 19, ridden: 27
    other = wk.BUILT_IN["sweet-spot-3x12"]
    assert wk.as_ridden(other) is other
