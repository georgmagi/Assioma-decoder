"""
Polar Measurement Data (PMD): the H10's raw streams over Bluetooth, ECG (130 Hz, uV) and the
accelerometer (up to 200 Hz, mG, 3 axes). Written from Polar's own documentation
(polar-ble-sdk technical_documentation/online_measurement.pdf) and the SDK's parser
(BlePMDClient.kt, PmdSetting.kt, PmdDataFrame.kt, EcgData.kt, AccData.kt, PmdTimeStampUtils.kt).

Unlike the pedals, the strap has to be asked to stream: the recorder writes to the PMD control
point of the strap (never to the pedals). Every command written, every control-point answer and
every data frame goes into the ride's raw log as it arrived, so the streams can be decoded again
later at full detail:

    PMD_FEAT  the control point as read at connect (which streams the strap has)
    PMD_CMD   a command we wrote
    PMD_CP    the strap's answer (0xF0, op code, type, status, more, parameters)
    PMD       a data frame: type, timestamp (uint64 ns since 2000-01-01, of the frame's last
              sample, on the strap's own clock), frame type (bit 7 = delta compressed), samples

    Decoder().feed(char, arrival, data) -> [(type, [(strap_ns, values), ...])]
    export(raw_log_path, base) -> writes base_ecg.csv and base_acc.csv
"""
from __future__ import annotations

import csv
import math
import struct
from datetime import datetime, timedelta, timezone
from typing import Optional

SERVICE = "fb005c80-02e7-f387-1cad-8acd2d8df0c8"
CP = "fb005c81-02e7-f387-1cad-8acd2d8df0c8"
DATA = "fb005c82-02e7-f387-1cad-8acd2d8df0c8"
FEAT_CHAR, CMD_CHAR, CP_CHAR, DATA_CHAR = "PMD_FEAT", "PMD_CMD", "PMD_CP", "PMD"
CHARS = (FEAT_CHAR, CMD_CHAR, CP_CHAR, DATA_CHAR)

ECG, PPG, ACC, PPI, GYRO, MAG = 0, 1, 2, 3, 5, 6
NAMES = {ECG: "ECG", PPG: "PPG", ACC: "ACC", PPI: "PPI", GYRO: "GYRO", MAG: "MAG"}
GET_SETTINGS, START, STOP = 1, 2, 3
SAMPLE_RATE, RESOLUTION, RANGE, RANGE_MILLIUNIT, CHANNELS, FACTOR, SECURITY = 0, 1, 2, 3, 4, 5, 6
FIELD_SIZE = {SAMPLE_RATE: 2, RESOLUTION: 2, RANGE: 2, RANGE_MILLIUNIT: 4, CHANNELS: 1, FACTOR: 4, SECURITY: 16}
ERRORS = {0: "success", 1: "invalid op code", 2: "invalid measurement type", 3: "not supported",
          4: "invalid length", 5: "invalid parameter", 6: "already in state", 7: "invalid resolution",
          8: "invalid sample rate", 9: "invalid range", 10: "invalid MTU", 11: "invalid number of channels",
          12: "invalid state", 13: "device in charger"}
EPOCH = datetime(2000, 1, 1, tzinfo=timezone.utc)


# ---- the control point
def features(data: bytes) -> set:
    """Streams the strap offers, from the control point's value (byte 0 = 0x0F, byte 1 a bit field)."""
    if len(data) < 2:
        return set()
    bits = {0x01: ECG, 0x02: PPG, 0x04: ACC, 0x08: PPI, 0x20: GYRO, 0x40: MAG}
    return {t for b, t in bits.items() if data[1] & b}


def settings_cmd(kind: int) -> bytes:
    return bytes([GET_SETTINGS, kind])


def stop_cmd(kind: int) -> bytes:
    return bytes([STOP, kind])


def start_cmd(kind: int, chosen: dict) -> bytes:
    """REQUEST_MEASUREMENT_START: type, then each setting as (id, count 1, value little-endian).
    The conversion factor is never sent (the strap reports it)."""
    out = bytearray([START, kind])
    for sid in sorted(chosen):
        if sid == FACTOR:
            continue
        out += bytes([sid, 1]) + int(chosen[sid]).to_bytes(FIELD_SIZE[sid], "little")
    return bytes(out)


def response(data: bytes) -> Optional[dict]:
    """A control point answer: {op, type, status, error, more, params}; None for anything else
    (e.g. the strap telling us it stopped a stream: first byte 0x01, then the types)."""
    if len(data) < 4 or data[0] != 0xF0:
        return None
    ok = data[3] == 0
    return {"op": data[1], "type": data[2], "status": data[3], "error": ERRORS.get(data[3], f"error {data[3]}"),
            "more": ok and len(data) > 4 and data[4] != 0, "params": bytes(data[5:]) if ok else b""}


def parse_settings(params: bytes) -> dict:
    """{setting id: [values]} from a settings answer (or the start answer, which carries the factor)."""
    out, i = {}, 0
    while i + 2 <= len(params):
        sid, n = params[i], params[i + 1]
        i += 2
        size = FIELD_SIZE.get(sid)
        if size is None:
            break                                  # unknown setting: the rest can't be framed
        vals = []
        for _ in range(n):
            raw = params[i:i + size]
            i += size
            vals.append(struct.unpack("<f", raw)[0] if sid == FACTOR else int.from_bytes(raw, "little", signed=True))
        out[sid] = vals
    return out


def choose_max(available: dict) -> dict:
    """Full detail: the highest of every setting, as Polar's own maxSettings() does."""
    return {sid: max(v) for sid, v in available.items() if v and sid not in (FACTOR, SECURITY)}


# ---- data frames
def _signed(b: bytes) -> int:
    return int.from_bytes(b, "little", signed=True)


def _delta_frames(content: bytes, channels: int, resolution: int) -> list:
    """Polar's delta compression (parseDeltaFramesAll): reference sample, then blocks of
    (bit width, count, packed little-endian bit stream of signed deltas)."""
    nbytes = math.ceil(resolution / 8)
    ref = []
    for c in range(channels):
        v = _signed(content[c * nbytes:(c + 1) * nbytes])
        if v & (1 << (resolution - 1)):
            v |= -(1 << (resolution - 1))
        ref.append(v)
    samples, i = [ref], channels * nbytes
    while i + 2 <= len(content):
        width, count = content[i], content[i + 1]
        i += 2
        nbits = width * count * channels
        block = content[i:i + math.ceil(nbits / 8)]
        i += math.ceil(nbits / 8)
        bits = int.from_bytes(block, "little")
        pos = 0
        for _ in range(count):
            nxt = []
            for c in range(channels):
                d = (bits >> pos) & ((1 << width) - 1)
                pos += width
                if width and d & (1 << (width - 1)):
                    d -= 1 << width
                nxt.append(samples[-1][c] + d)
            samples.append(nxt)
    return samples


class Decoder:
    """Turns the PMD lines of a raw log back into samples. Keeps what the control point said
    (sample rate asked for, the factor the strap answered) and each stream's previous frame
    time, which spreads a frame's samples evenly up to its timestamp (PmdTimeStampUtils)."""

    def __init__(self) -> None:
        self.rate: dict = {}
        self.factor: dict = {}
        self.resolution: dict = {}
        self.channels: dict = {}
        self.prev: dict = {}                        # (type, frame type) -> previous frame timestamp
        self.offered: set = set()
        self.started: dict = {}                     # type -> chosen settings, once the strap said yes
        self.errors: list = []
        self._asked: dict = {}

    def feed(self, char: str, arrival: datetime, data: bytes) -> list:
        if char == FEAT_CHAR:
            self.offered = features(data)
        elif char == CMD_CHAR and len(data) >= 2 and data[0] == START:
            self._asked[data[1]] = parse_settings(data[2:])
        elif char == CP_CHAR:
            r = response(data)
            if r and r["op"] == START:
                t = r["type"]
                if r["status"] in (0, 6):           # started (or already streaming)
                    asked = {k: v[0] for k, v in self._asked.get(t, {}).items()}
                    self.started[t] = asked
                    self.rate[t] = asked.get(SAMPLE_RATE, 0)
                    self.resolution[t] = asked.get(RESOLUTION, 16)
                    self.channels[t] = asked.get(CHANNELS, 3 if t == ACC else 1)
                    got = parse_settings(r["params"]).get(FACTOR)
                    self.factor[t] = got[0] if got else 1.0
                    for k in [k for k in self.prev if k[0] == t]:
                        del self.prev[k]            # a new stream: no previous frame
                else:
                    self.errors.append((NAMES.get(t, t), r["error"]))
        elif char == DATA_CHAR and len(data) >= 10:
            return self._frame(data)
        return []

    def _frame(self, data: bytes) -> list:
        kind = data[0] & 0x3F
        ts = int.from_bytes(data[1:9], "little")
        ftype, compressed = data[9] & 0x7F, bool(data[9] & 0x80)
        content = data[10:]
        values = self._values(kind, ftype, compressed, content)
        if not values:
            return []
        n = len(values)
        prev = self.prev.get((kind, ftype), 0)
        rate = self.rate.get(kind) or 0
        if prev and ts > prev:
            delta = (ts - prev) / n
            first = prev + delta
        elif rate:
            delta = 1e9 / rate
            first = ts - delta * (n - 1)
        else:                                       # rate unknown: time it from the next frame
            self.prev[(kind, ftype)] = ts
            return []
        self.prev[(kind, ftype)] = ts
        times = [round(first + delta * k) for k in range(n - 1)] + [ts]
        return [(kind, list(zip(times, values)))]

    def _values(self, kind, ftype, compressed, content) -> list:
        if kind == ECG and not compressed and ftype == 0:
            return [(_signed(content[i:i + 3]),) for i in range(0, len(content) - 2, 3)]
        if kind == ACC and not compressed and ftype in (0, 1, 2):
            s = ftype + 1                           # bytes per axis
            return [tuple(_signed(content[i + a * s:i + (a + 1) * s]) for a in range(3))
                    for i in range(0, len(content) - 3 * s + 1, 3 * s)]
        if kind == ACC and compressed and ftype in (0, 1):
            f = self.factor.get(ACC, 1.0)
            raw = _delta_frames(content, 3, 16)
            if ftype == 0:                          # arrives in G
                return [tuple(int(v * f * 1000) for v in s) for s in raw]
            return [tuple(int(v * f) for v in s) if f != 1.0 else tuple(s) for s in raw]
        return []


def strap_time(ns: int) -> datetime:
    return EPOCH + timedelta(microseconds=ns / 1000)


def export(raw_path: str, base: str) -> dict:
    """The ride's ECG and accelerometer at full detail: base_ecg.csv (uV) and base_acc.csv (mG),
    each sample with the strap's own timestamp and that time on the ride's clock. The strap's
    clock is put on the ride's by the smallest gap between a frame's last sample and its arrival
    (a frame can't arrive before its last sample was taken). Returns {stream: samples}."""
    from assioma_recorder import read_raw_log
    d, frames = Decoder(), {ECG: [], ACC: []}
    for n in read_raw_log(raw_path):
        if n.char in CHARS:
            arrival = n.time if n.time.tzinfo else n.time.replace(tzinfo=timezone.utc)
            for kind, samples in d.feed(n.char, arrival, n.data):
                if kind in frames and samples:
                    frames[kind].append((arrival, samples))
    counts = {}
    for kind, name, cols in ((ECG, "ecg", ["uV"]), (ACC, "acc", ["x_mG", "y_mG", "z_mG"])):
        fr = frames[kind]
        if not fr:
            continue
        off = min(a - strap_time(s[-1][0]) for a, s in fr)
        with open(f"{base}_{name}.csv", "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["time_utc", "strap_ns"] + cols)
            n = 0
            for _, samples in fr:
                for ns, vals in samples:
                    w.writerow([(strap_time(ns) + off).strftime("%Y-%m-%dT%H:%M:%S.%fZ"), ns, *vals])
                    n += 1
        counts[name] = n
    return counts
