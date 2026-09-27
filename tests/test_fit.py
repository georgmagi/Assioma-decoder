"""FIT output: integrity and round trip. Uses Garmin's SDK if installed (pip install garmin-fit-sdk)."""
import os

import pytest

import assioma_decoder as d
import fit_writer as fw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _build(tmp_path, sample, date):
    sec = d.per_second(d.decode(d.read_nrf_log(os.path.join(ROOT, "samples", sample), date, 3)))
    path = str(tmp_path / "out.fit")
    fw.write_activity(sec, path)
    return path, sec


def test_crc(tmp_path):
    path, _ = _build(tmp_path, "ride_2026-09-26_nrf.csv", "2026-09-26")
    data = open(path, "rb").read()
    assert data[8:12] == b".FIT"
    assert fw.fit_crc(data) == 0          # CRC over the file including its CRC is 0


def test_garmin_sdk_round_trip(tmp_path):
    sdk = pytest.importorskip("garmin_fit_sdk")
    path, sec = _build(tmp_path, "ride_2026-09-26_nrf.csv", "2026-09-26")
    assert sdk.Decoder(sdk.Stream.from_file(path)).check_integrity()
    msgs, errors = sdk.Decoder(sdk.Stream.from_file(path)).read()
    assert not errors
    recs = msgs["record_mesgs"]
    assert len(recs) == len(sec)
    for s, r in zip(sec, recs):
        assert s.get("power_w") == r.get("power")
        if s.get("te_left_pct") is not None:
            assert s["te_left_pct"] == r["left_torque_effectiveness"]


def test_hrv_messages(tmp_path):
    sdk = pytest.importorskip("garmin_fit_sdk")
    path, _ = _build(tmp_path, "hrm200_rr_2026-09-27_nrf.csv", "2026-09-27")
    msgs, errors = sdk.Decoder(sdk.Stream.from_file(path)).read()
    rr = [x for h in msgs["hrv_mesgs"] for x in h["time"] if x is not None]
    assert not errors and len(rr) == 83
