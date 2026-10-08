"""Warm-up readiness, the knee's torque per rep (checks.py) and the strap lock (BleWorker)."""
import math
import os
import queue
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import assioma_recorder as ar
import checks


def _rows(power, hr=None, cad=90.0):
    return [{"power_w": p, "heart_rate_bpm": hr(i) if hr else None, "cadence_rpm": cad if p else 0,
             "balance_right_pct": 52.0, "pco_left_mm": 1.0, "pco_right_mm": 2.0, "te_left_pct": 70.0,
             "te_right_pct": 78.0, "dfa_a1": 1.3} for i, p in enumerate(power)]


def test_warmup_puts_heart_rate_on_125_w():
    rows = _rows([135] * 1200, hr=lambda i: 130)
    w = checks.warmup(rows)
    assert abs(w["power"] - 135) < 1e-9 and abs(w["hr"] - (130 - 0.4 * 10)) < 1e-9 and w["a1"] == 1.3
    assert checks.warmup(_rows([135] * 500, hr=lambda i: 130)) is None                # too short
    assert checks.warmup(_rows([200] * 1200, hr=lambda i: 150)) is None               # no easy warm-up


def test_warmup_normal_is_the_median_of_recent_rides():
    assert checks.warmup_normal([{"hr": 120}, None, {"hr": 124}]) is None              # 3 needed
    n = checks.warmup_normal([{"hr": h, "a1": 1.3} for h in (135, 122, 124, 126)])
    assert n["hr"] == 125 and n["rides"] == 4 and n["a1"] == 1.3


def test_torque_and_reps():
    assert abs(checks.torque(219, 96) - 219 / (2 * math.pi * 96 / 60)) < 1e-12
    assert checks.torque(200, 0) is None and checks.torque(None, 90) is None
    power = [120] * 600 + ([226] * 180 + [100] * 180) * 3
    k = checks.knee(_rows(power), 199)
    assert len(k["reps"]) == 3 and all(r["seconds"] == 180 for r in k["reps"])
    assert abs(k["reps"][0]["torque"] - checks.torque(226, 90)) < 1e-9
    assert abs(k["peak_torque"] - checks.torque(226, 90)) < 1e-9
    assert k["reps"][0]["balance_right"] == 52 and k["reps"][0]["pco_left"] == 1
    assert checks.knee(_rows(power), None)["reps"] == []                             # no FTP: no reps


def _adv(address, name):
    return SimpleNamespace(address=address, name=name), SimpleNamespace(local_name=name, rssi=-50,
                                                                        service_uuids=[ar.HR_SERVICE])


def test_the_strap_lock_leaves_other_straps_alone():
    w = ar.BleWorker(queue.Queue(), True, "", "")
    found = {a: _adv(a, n) for a, n in (("EA:00:00:00:00:01", "Amazfit Helio Strap"),
                                        ("24:AC:AC:12:34:56", "Polar H10 12345678"))}
    found["EA:00:00:00:00:01"][1].rssi = -30                                       # the neighbour is closer
    assert w._pick_hr(found)[0].address == "EA:00:00:00:00:01"                     # no lock: any strap
    w.set_hr_lock({"address": "24:AC:AC:12:34:56", "name": "Polar H10 12345678", "ant": 13398})
    assert w._pick_hr(found)[0].address == "24:AC:AC:12:34:56"
    del found["24:AC:AC:12:34:56"]
    assert w._pick_hr(found) is None                                               # yours not around: none
    assert ar.BleWorker.ant_number("24:AC:AC:12:34:56") == 13398
    assert ar.BleWorker.ant_number("not-a-mac") is None
