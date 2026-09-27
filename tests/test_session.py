"""Ride statistics computed live by the recorder's Session."""
from datetime import datetime, timedelta

import assioma_recorder as ar

T0 = datetime(2026, 9, 28, 6)


def _ride(tmp_path, seconds, hr_at):
    s = ar.Session(str(tmp_path))
    s.start(T0)
    for i in range(seconds):
        s._second({"time": T0 + timedelta(seconds=i), "power_w": 150, "heart_rate_bpm": hr_at(i)})
    return s


def test_decoupling_measures_hr_drift(tmp_path):
    # same power, heart rate 5 % higher in the second half (after the 10 min warm-up): EF drops 1 - 1/1.05
    s = _ride(tmp_path, 3600, lambda i: 130 if i < 2100 else 136.5)
    d = s.decoupling()
    assert abs(d["decoupling"] - 100 * (1 - 1 / 1.05)) < 0.05
    assert abs(d["ef"] - 150 / ((130 + 136.5) / 2)) < 0.01


def test_decoupling_waits_for_enough_data(tmp_path):
    d = _ride(tmp_path, 900, lambda i: 130).decoupling()
    assert d["decoupling"] is None and d["decoupling_at"] == 1200


def test_pause_is_left_out_of_the_ride_and_the_fit(tmp_path):
    import fit_writer
    import pytest
    s = ar.Session(str(tmp_path))
    t = lambda sec: T0 + timedelta(seconds=sec)
    s.start(t(10))
    s.pause(t(70))
    s.resume(t(130))
    for i in range(200):                        # rows arrive for every second, recorded or not
        s._second({"time": t(i), "power_w": 150, "heart_rate_bpm": 130})
    recorded = [r["time"] for r in s.seconds]
    assert len(recorded) == 60 + 70 and recorded[0] == t(10) and t(100) not in recorded
    path = str(tmp_path / "p.fit")
    fit_writer.write_activity(s.seconds, path)
    sdk = pytest.importorskip("garmin_fit_sdk")
    msgs, errors = sdk.Decoder(sdk.Stream.from_file(path)).read()
    assert not errors
    ses = msgs["session_mesgs"][0]
    assert ses["total_timer_time"] == 130 and ses["total_elapsed_time"] == 190
    timer = [e["event_type"] for e in msgs["event_mesgs"] if e["event"] == "timer"]
    assert timer == ["start", "stop_all", "start", "stop_all"]


def test_rebuild_honours_pause_markers(tmp_path):
    import assioma_decoder as dec
    ev = lambda sec, word: dec.Notification(T0 + timedelta(seconds=sec), ar.EVENT, word.encode())
    segs = ar.recording_segments([(n.time, n.data.decode()) for n in
                                  [ev(10, "start"), ev(70, "pause"), ev(130, "resume"), ev(200, "stop")]])
    assert segs == [(T0 + timedelta(seconds=10), T0 + timedelta(seconds=70)),
                    (T0 + timedelta(seconds=130), T0 + timedelta(seconds=200))]
    assert ar.in_segments(T0 + timedelta(seconds=69), segs) and not ar.in_segments(T0 + timedelta(seconds=100), segs)
    assert ar.recording_segments([]) is None and ar.in_segments(T0, None)
