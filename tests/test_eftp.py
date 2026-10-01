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
    assert h["current"]["cp"] == pytest.approx(250, abs=0.1)
    assert [x["cp"] for x in h["series"]] == [pytest.approx(300, abs=0.1), pytest.approx(250, abs=0.1)]
