"""Polar PMD (polar_pmd.py): commands, answers, frames, delta compression, export, and the worker
asking a fake H10 for its streams."""
import csv
import os
import struct
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import polar_pmd as pmd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def test_start_commands_are_the_known_h10_bytes():
    # the widely used H10 commands: ECG 130 Hz 14 bit; ACC 200 Hz 16 bit 8 G
    assert pmd.start_cmd(pmd.ECG, {0: 130, 1: 14}) == bytes.fromhex("02000001820001010e00")
    assert pmd.start_cmd(pmd.ACC, {0: 200, 1: 16, 2: 8}) == bytes.fromhex("02020001c8000101100002010800")
    assert pmd.settings_cmd(pmd.ACC) == b"\x01\x02" and pmd.stop_cmd(pmd.ECG) == b"\x03\x00"


def test_settings_answer_and_the_highest_of_each():
    params = bytes.fromhex("0004190032006400c800" "01011000" "0203020004000800")
    s = pmd.parse_settings(params)
    assert s == {0: [25, 50, 100, 200], 1: [16], 2: [2, 4, 8]}
    assert pmd.choose_max(s) == {0: 200, 1: 16, 2: 8}
    with_factor = pmd.parse_settings(bytes([5, 1]) + struct.pack("<f", 0.5))
    assert with_factor == {5: [0.5]}


def test_control_point_answers():
    ok = pmd.response(bytes.fromhex("f00202000005010000803f"))
    assert ok["op"] == 2 and ok["type"] == 2 and ok["status"] == 0 and not ok["more"]
    assert pmd.parse_settings(ok["params"])[5] == [1.0]
    bad = pmd.response(bytes.fromhex("f0020008"))
    assert bad["error"] == "invalid sample rate" and bad["params"] == b""
    assert pmd.response(bytes.fromhex("0100")) is None            # the strap stopping a stream
    assert pmd.features(bytes.fromhex("0f0500")) == {pmd.ECG, pmd.ACC}


def _frame(kind, ts, ftype, content):
    return bytes([kind]) + ts.to_bytes(8, "little") + bytes([ftype]) + content


def _i24(v):
    return (v & 0xFFFFFF).to_bytes(3, "little")


def _started(d, kind, rate, factor=1.0):
    t = datetime(2026, 10, 6, tzinfo=timezone.utc)
    d.feed(pmd.CMD_CHAR, t, pmd.start_cmd(kind, {0: rate, 1: 14 if kind == pmd.ECG else 16}))
    d.feed(pmd.CP_CHAR, t, bytes([0xF0, 2, kind, 0, 0, 5, 1]) + struct.pack("<f", factor))


def test_ecg_frames_samples_and_their_times():
    d = pmd.Decoder()
    _started(d, pmd.ECG, 130)
    t = datetime(2026, 10, 6, tzinfo=timezone.utc)
    ts1 = 10_000_000_000
    a = d.feed(pmd.DATA_CHAR, t, _frame(0, ts1, 0, b"".join(_i24(v) for v in (100, -250, 7))))
    (kind, s1), = a
    assert kind == pmd.ECG and [v for _, (v,) in s1] == [100, -250, 7]
    step = 1e9 / 130
    assert s1[-1][0] == ts1 and abs((s1[1][0] - s1[0][0]) - step) < 2      # first frame: from the sample rate
    ts2 = ts1 + 4 * 7_700_000                                               # next frame: spread up to its stamp
    (_, s2), = d.feed(pmd.DATA_CHAR, t, _frame(0, ts2, 0, b"".join(_i24(v) for v in (1, 2, 3, 4))))
    assert [x for x, _ in s2] == [ts1 + 7_700_000 * k for k in (1, 2, 3, 4)]


def test_acc_raw_frames():
    d = pmd.Decoder()
    _started(d, pmd.ACC, 200)
    content = struct.pack("<hhhhhh", -12, 980, 35, -1500, 1, 0)
    (kind, s), = d.feed(pmd.DATA_CHAR, datetime.now(timezone.utc), _frame(2, 5_000_000_000, 1, content))
    assert kind == pmd.ACC and [v for _, v in s] == [(-12, 980, 35), (-1500, 1, 0)]


def _encode_delta(samples, width):
    """An independent encoder: reference sample (2 bytes per axis), then one block of deltas."""
    ref = b"".join(struct.pack("<h", v) for v in samples[0])
    bits, pos = 0, 0
    for prev, cur in zip(samples, samples[1:]):
        for c in range(3):
            bits |= ((cur[c] - prev[c]) & ((1 << width) - 1)) << pos
            pos += width
    n = len(samples) - 1
    return ref + bytes([width, n]) + bits.to_bytes((pos + 7) // 8, "little")


def test_delta_compressed_acc_round_trip():
    want = [(10, -20, 1000), (12, -25, 998), (5, -25, 1003), (-3, -10, 1003), (0, 0, 990)]
    d = pmd.Decoder()
    _started(d, pmd.ACC, 200)
    (kind, s), = d.feed(pmd.DATA_CHAR, datetime.now(timezone.utc), _frame(2, 9_000_000_000, 0x81, _encode_delta(want, 6)))
    assert [v for _, v in s] == want


def test_export_writes_both_streams_on_the_ride_clock(tmp_path):
    t0 = datetime(2026, 10, 6, 8, 0, tzinfo=timezone.utc)
    strap0 = 700_000_000_000                       # the strap's clock, nowhere near the ride's
    lines = [(t0, pmd.FEAT_CHAR, bytes.fromhex("0f0500"))]
    for kind, rate in ((pmd.ECG, 130), (pmd.ACC, 200)):
        lines.append((t0, pmd.CMD_CHAR, pmd.start_cmd(kind, {0: rate, 1: 14 if kind == 0 else 16})))
        lines.append((t0, pmd.CP_CHAR, bytes([0xF0, 2, kind, 0, 0])))
    for k in range(1, 11):                         # 10 frames each, arriving 30-80 ms after their last sample
        ts = strap0 + k * 10 * 7_692_308
        lines.append((t0 + timedelta(seconds=k * 10 / 130, milliseconds=30 + 5 * (k % 3)), pmd.DATA_CHAR,
                      _frame(0, ts, 0, b"".join(_i24(k * 10 + j) for j in range(10)))))
        ts = strap0 + k * 20 * 5_000_000
        lines.append((t0 + timedelta(seconds=k * 0.1, milliseconds=40), pmd.DATA_CHAR,
                      _frame(2, ts, 1, struct.pack("<" + "h" * 60, *range(60)))))
    lines.sort(key=lambda x: x[0])
    raw = tmp_path / "ride_raw.csv"
    raw.write_text("time_utc,char,hex\n" + "".join(f"{a.isoformat()},{c},{d.hex()}\n" for a, c, d in lines))
    counts = pmd.export(str(raw), str(tmp_path / "ride"))
    assert counts == {"ecg": 100, "acc": 200}
    rows = list(csv.DictReader(open(tmp_path / "ride_ecg.csv")))
    assert rows[0].keys() >= {"time_utc", "strap_ns", "uV"} and rows[-1]["uV"] == "109"
    last = datetime.strptime(rows[-1]["time_utc"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    assert abs((last - (t0 + timedelta(seconds=100 / 130, milliseconds=30))).total_seconds()) < 0.01
    acc = list(csv.DictReader(open(tmp_path / "ride_acc.csv")))
    assert (acc[1]["x_mG"], acc[1]["y_mG"], acc[1]["z_mG"]) == ("3", "4", "5")


WORKER = r'''
import queue, time, collections, os
import assioma_recorder as ar, assioma_decoder as dec, bleak, polar_pmd as pmd
ar.PEDAL_FILE = os.environ["PEDAL_FILE"]
q = queue.Queue(); w = ar.BleWorker(q, True, "", ""); w.start()
time.sleep(14); w.stop(); w.join(5)
notes = list(q.queue)
c = collections.Counter(n.char for n in notes)
d = pmd.Decoder(); got = collections.Counter()
for n in notes:
    for kind, s in d.feed(n.char, n.time, n.data):
        got[kind] += len(s)
print(c["PMD_FEAT"] >= 1, ",".join(x.hex() for x in bleak.STATE["pmd_writes"][:4]), got[0] > 100, got[2] > 100, c["2A37"] > 3)
'''


def test_worker_asks_a_polar_strap_for_its_streams(tmp_path):
    env = dict(os.environ, PEDAL_FILE=str(tmp_path / "pedal.txt"), FAKE_POLAR="1",
               PYTHONPATH=os.pathsep.join([os.path.join(HERE, "fake_bleak"), ROOT]))
    out = subprocess.run([sys.executable, "-c", WORKER], env=env, capture_output=True, text=True, timeout=60)
    assert out.stdout.split() == ["True", "0100,02000001820001010e00,0102,02020001c80001011000020108" + "00",
                                  "True", "True", "True"], out.stdout + out.stderr
