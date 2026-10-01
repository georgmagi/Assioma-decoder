"""Estimated FTP: critical power from the power-duration curve of recent rides."""
from datetime import date, timedelta

import pytest

import eftp


def _curve(cp, w_prime, noise=None):
    return {str(t): cp + w_prime / t + (noise(t) if noise else 0) for t in eftp.DURATIONS}


def test_recovers_cp_and_w_prime_from_maximal_efforts():
    f = eftp.fit_cp(_curve(250, 20000))
    assert f["cp"] == pytest.approx(250) and f["w_prime"] == pytest.approx(20000) and f["quality"] == "good"
    assert f["best20_95"] == pytest.approx(0.95 * (250 + 20000 / 1200))


def test_a_flat_curve_from_easy_rides_gives_no_estimate():
    flat = {str(t): 160 - t / 200 for t in eftp.DURATIONS}             # steady Z2: barely drops
    assert eftp.fit_cp(flat) is None
    assert eftp.fit_cp({"180": 300, "300": 280}) is None                 # too few durations


def test_mmp_is_the_best_rolling_average():
    power = [100] * 600 + [300] * 300 + [100] * 600
    m = eftp.mmp(power)
    assert m["300"] == 300 and m["600"] == pytest.approx(200) and m["3600"] is None


def test_the_window_is_42_days_and_history_follows_it():
    today = date(2026, 10, 1)
    old = {"day": today - timedelta(days=50), "mmp": _curve(300, 20000)}  # outside the window
    new = {"day": today - timedelta(days=3), "mmp": _curve(250, 20000)}
    h = eftp.history([old, new], today, lambda r: r["day"])
    assert h["current"]["method"] == "cp" and h["current"]["ftp"] == pytest.approx(250, abs=0.1)
    assert [x["ftp"] for x in h["series"]] == [pytest.approx(300, abs=0.1), pytest.approx(250, abs=0.1)]


def test_a_ramp_test_gives_its_own_estimate_and_stays_out_of_the_fit():
    today = date(2026, 10, 1)
    ramp = {"day": today - timedelta(days=20), "name": "Ramp Test", "mmp": {**_curve(150, 60000), "60": 264.7}}
    hard = {"day": today - timedelta(days=5), "mmp": _curve(220, 18000)}
    h = eftp.history([ramp, hard], today, lambda r: r["day"])
    cur = h["current"]
    assert cur["method"] == "ramp" and cur["ftp"] == pytest.approx(0.75 * 264.7, abs=0.1)
    assert cur["cp_fit"]["cp"] == pytest.approx(220, abs=0.1)              # the ramp's curve isn't in it
    later = eftp.history([ramp, hard], today + timedelta(days=30), lambda r: r["day"])
    assert later["current"]["method"] == "cp"                               # the ramp test aged out


def test_hrvt2_sets_eftp_without_a_ramp_test_only_with_every_beat():
    today = date(2026, 10, 1)
    d = lambda k: today - timedelta(days=k)
    good = {"day": d(3), "vt2_power": {"kind": "cross", "value": 205.0}, "vt1_beats_pct": 99.0}
    lossy = {"day": d(1), "vt2_power": {"kind": "cross", "value": 230.0}, "vt1_beats_pct": 80.0}
    h = eftp.history([good], today, lambda r: r["day"])
    assert h["current"]["method"] == "hrvt2" and h["current"]["ftp"] == 205
    h = eftp.history([lossy], today, lambda r: r["day"])
    assert h["current"] is None                                            # Bluetooth beats: listed, not used
    bound = {"day": d(2), "vt2_power": {"kind": "above", "value": 225.0}, "vt1_beats_pct": 86.0,
             "mmp": _curve(220, 18000)}
    ev = eftp.history([bound], today, lambda r: r["day"])["current"]["evidence"]
    assert [e["kind"] for e in ev] == ["hrvt2", "efforts"] and ev[0]["bound"] == "above" and not ev[0]["trusted"]
