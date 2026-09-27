"""
Minimal FIT activity writer (standard library only).

Writes an indoor-cycling activity with Garmin "Cycling Dynamics" fields, so
Garmin Connect, intervals.icu, GoldenCheetah etc. show balance, torque
effectiveness, pedal smoothness, PCO, power phase and seated/standing time.

Input: the per-second rows produced by assioma_decoder.PerSecond
(time = naive UTC datetime, values in human units).
"""
from __future__ import annotations

import math
import struct
from datetime import datetime, timedelta, timezone
from typing import Optional

FIT_EPOCH = datetime(1989, 12, 31)          # naive UTC
PROFILE_VERSION = 2132

# base type: (id, struct format, invalid value)
_BASE = {
    "enum":    (0x00, "B", 0xFF),
    "sint8":   (0x01, "b", 0x7F),
    "uint8":   (0x02, "B", 0xFF),
    "sint16":  (0x83, "h", 0x7FFF),
    "uint16":  (0x84, "H", 0xFFFF),
    "uint32":  (0x86, "I", 0xFFFFFFFF),
    "uint32z": (0x8C, "I", 0x00000000),
    "string":  (0x07, None, None),
}
_RANGE = {"b": (-128, 127), "B": (0, 255), "h": (-32768, 32767), "H": (0, 65535), "I": (0, 2**32 - 1)}

_CRC_TABLE = [0x0000, 0xCC01, 0xD801, 0x1400, 0xF001, 0x3C00, 0x2800, 0xE401,
              0xA001, 0x6C00, 0x7800, 0xB401, 0x5000, 0x9C01, 0x8801, 0x4400]


def fit_crc(data: bytes, crc: int = 0) -> int:
    for byte in data:
        tmp = _CRC_TABLE[crc & 0xF]
        crc = (crc >> 4) & 0x0FFF
        crc = crc ^ tmp ^ _CRC_TABLE[byte & 0xF]
        tmp = _CRC_TABLE[crc & 0xF]
        crc = (crc >> 4) & 0x0FFF
        crc = crc ^ tmp ^ _CRC_TABLE[(byte >> 4) & 0xF]
    return crc


def fit_time(t: datetime) -> int:
    if t.tzinfo is not None:
        t = t.astimezone(timezone.utc).replace(tzinfo=None)
    return int((t - FIT_EPOCH).total_seconds())


class FitWriter:
    """Encodes messages; field = (number, base_type, value). Value None = invalid,
    a list = array, a str = string."""

    def __init__(self) -> None:
        self.data = bytearray()
        self._locals: dict = {}      # signature -> local message number
        self._next_local = 0

    def _encode(self, typ: str, value) -> bytes:
        base, fmt, invalid = _BASE[typ]
        if typ == "string":
            return value.encode("utf-8") + b"\x00"
        values = value if isinstance(value, (list, tuple)) else [value]
        out = b""
        for v in values:
            if v is None:
                v = invalid
            else:
                v = int(round(v))
                lo, hi = _RANGE[fmt]
                if not lo <= v <= hi:
                    v = invalid
            out += struct.pack("<" + fmt, v)
        return out

    def write(self, global_num: int, fields: list) -> None:
        encoded = [(num, typ, self._encode(typ, val)) for num, typ, val in fields]
        sig = (global_num, tuple((num, typ, len(b)) for num, typ, b in encoded))
        local = self._locals.get(sig)
        if local is None:
            local = self._next_local
            self._next_local = (self._next_local + 1) % 16
            self._locals = {s: l for s, l in self._locals.items() if l != local}
            self._locals[sig] = local
            d = struct.pack("<BBBHB", 0x40 | local, 0, 0, global_num, len(encoded))
            for num, typ, b in encoded:
                d += struct.pack("<BBB", num, len(b), _BASE[typ][0])
            self.data += d
        self.data += bytes([local]) + b"".join(b for _, _, b in encoded)

    def to_bytes(self) -> bytes:
        header = struct.pack("<BBHI4s", 14, 0x20, PROFILE_VERSION, len(self.data), b".FIT")
        header += struct.pack("<H", fit_crc(header))
        body = header + bytes(self.data)
        return body + struct.pack("<H", fit_crc(body))


# --------------------------------------------------------------------------- #
# Activity
# --------------------------------------------------------------------------- #
_RIDER_POSITION_FIT = {"seated": 0, "standing": 1, "to_seated": 2, "to_standing": 3}


def _deg_raw(deg: Optional[float]) -> Optional[int]:
    # 255 (358.6 deg) is FIT's "invalid" for uint8, so nudge it to 254 (357.2 deg)
    return None if deg is None else min(int(round(deg * 256 / 360)) % 256, 254)


def _mean(vals) -> Optional[float]:
    vals = [v for v in vals if v is not None]
    return sum(vals) / len(vals) if vals else None


def _circ_mean(degs) -> Optional[float]:
    degs = [d for d in degs if d is not None]
    if not degs:
        return None
    s = sum(math.sin(math.radians(d)) for d in degs)
    c = sum(math.cos(math.radians(d)) for d in degs)
    return math.degrees(math.atan2(s, c)) % 360


def _half(pct: Optional[float]) -> Optional[int]:
    return None if pct is None else round(pct * 2)


def normalized_power(powers: list) -> Optional[float]:
    p = [v or 0 for v in powers]
    if len(p) < 30 or all(v is None for v in powers):
        return None
    s, rolling = sum(p[:30]), []
    rolling.append(s / 30)
    for i in range(30, len(p)):
        s += p[i] - p[i - 30]
        rolling.append(s / 30)
    return (sum(r ** 4 for r in rolling) / len(rolling)) ** 0.25


def summary(rows: list[dict]) -> dict:
    col = lambda k: [r.get(k) for r in rows]
    power = col("power_w")
    pedalling = [r for r in rows if (r.get("power_w") or 0) > 0]
    cad = [c for c in col("cadence_rpm") if c]
    hr = [h for h in col("heart_rate_bpm") if h]
    s = {
        "avg_power": _mean(power), "max_power": max([p for p in power if p is not None], default=None),
        "np": normalized_power(power), "work_j": sum(p or 0 for p in power),
        "avg_cadence": _mean(cad), "max_cadence": max(cad, default=None),
        "avg_hr": _mean(hr), "max_hr": max(hr, default=None),
        "balance_right": _mean(r.get("balance_right_pct") for r in pedalling),
    }
    for k in ("te_left_pct", "te_right_pct", "ps_left_pct", "ps_right_pct", "pco_left_mm", "pco_right_mm"):
        s[k] = _mean(r.get(k) for r in pedalling)
    for side in ("left", "right"):
        for part in ("start", "peak_start", "peak_end", "end"):
            k = f"pp_{side}_{part}_deg"
            s[k] = _circ_mean(r.get(k) for r in pedalling)
    return s


def write_activity(rows: list[dict], path: str, pedal_name: str = "Assioma",
                   battery: Optional[tuple] = None) -> dict:
    """Write per-second rows as a FIT activity. Returns the summary.

    A gap between consecutive rows is a pause: the timer is stopped and restarted around it, so
    the timer (moving) time counts only the recorded seconds while the elapsed time spans all."""
    if not rows:
        raise ValueError("no data to write")
    w = FitWriter()
    t0, t1 = rows[0]["time"], rows[-1]["time"]
    ts0, ts1 = fit_time(t0), fit_time(t1)
    elapsed_ms = (ts1 - ts0 + 1) * 1000
    timer_ms = len(rows) * 1000
    s = summary(rows)

    # file_id, creator, pedals
    w.write(0, [(0, "enum", 4), (1, "uint16", 255), (2, "uint16", 1),
                (3, "uint32z", ts0), (4, "uint32", ts0), (8, "string", "Assioma Recorder")])
    w.write(23, [(253, "uint32", ts0), (0, "uint8", 0), (2, "uint16", 255), (4, "uint16", 1),
                 (27, "string", "Assioma Recorder")])
    w.write(23, [(253, "uint32", ts0), (0, "uint8", 1), (2, "uint16", 263), (4, "uint16", 0),
                 (27, "string", pedal_name),
                 (32, "uint8", battery[0] if battery else None)])
    # timer start
    w.write(21, [(253, "uint32", ts0), (0, "enum", 0), (1, "enum", 0), (4, "uint8", 0)])

    position = None
    prev_ts = None
    for r in rows:
        ts = fit_time(r["time"])
        if prev_ts is not None and ts - prev_ts > 1:      # a pause: timer stop ... timer start
            w.write(21, [(253, "uint32", prev_ts), (0, "enum", 0), (1, "enum", 4), (4, "uint8", 0)])
            w.write(21, [(253, "uint32", ts), (0, "enum", 0), (1, "enum", 0), (4, "uint8", 0)])
        prev_ts = ts
        pos = r.get("rider_position")
        if pos in _RIDER_POSITION_FIT and pos != position:
            w.write(21, [(253, "uint32", ts), (0, "enum", 44), (1, "enum", 3),
                         (3, "uint32", _RIDER_POSITION_FIT[pos])])
            position = pos
        b = r.get("balance_right_pct")
        w.write(20, [
            (253, "uint32", ts),
            (7, "uint16", r.get("power_w")),
            (4, "uint8", r.get("cadence_rpm")),
            (3, "uint8", r.get("heart_rate_bpm")),
            (30, "uint8", None if b is None else 0x80 | int(round(b))),
            (43, "uint8", _half(r.get("te_left_pct"))),
            (44, "uint8", _half(r.get("te_right_pct"))),
            (45, "uint8", _half(r.get("ps_left_pct"))),
            (46, "uint8", _half(r.get("ps_right_pct"))),
            (67, "sint8", r.get("pco_left_mm")),
            (68, "sint8", r.get("pco_right_mm")),
            (69, "uint8", [_deg_raw(r.get("pp_left_start_deg")), _deg_raw(r.get("pp_left_end_deg"))]),
            (70, "uint8", [_deg_raw(r.get("pp_left_peak_start_deg")), _deg_raw(r.get("pp_left_peak_end_deg"))]),
            (71, "uint8", [_deg_raw(r.get("pp_right_start_deg")), _deg_raw(r.get("pp_right_end_deg"))]),
            (72, "uint8", [_deg_raw(r.get("pp_right_peak_start_deg")), _deg_raw(r.get("pp_right_peak_end_deg"))]),
        ])
        rr = r.get("rr_ms") or []
        for i in range(0, len(rr), 5):       # hrv message: beat-to-beat intervals (s, scale 1000)
            chunk = rr[i:i + 5]
            w.write(78, [(0, "uint16", chunk + [None] * (5 - len(chunk)))])

    # timer stop
    w.write(21, [(253, "uint32", ts1), (0, "enum", 0), (1, "enum", 4), (4, "uint8", 0)])

    bal = s["balance_right"]
    bal100 = None if bal is None else 0x8000 | int(round(bal * 100))
    common = [
        (2, "uint32", ts0), (7, "uint32", elapsed_ms), (8, "uint32", timer_ms),   # elapsed, timer (moving)
    ]
    # lap
    w.write(19, [(253, "uint32", ts1), (254, "uint16", 0), (0, "enum", 9), (1, "enum", 1), *common,
                 (15, "uint8", s["avg_hr"]), (16, "uint8", s["max_hr"]),
                 (17, "uint8", s["avg_cadence"]), (18, "uint8", s["max_cadence"]),
                 (19, "uint16", s["avg_power"]), (20, "uint16", s["max_power"]),
                 (33, "uint16", s["np"]), (34, "uint16", bal100),
                 (25, "enum", 2), (39, "enum", 6), (41, "uint32", s["work_j"]), (24, "enum", 7)])
    # session
    pp = lambda side, a, b: [_deg_raw(s[f"pp_{side}_{a}_deg"]), _deg_raw(s[f"pp_{side}_{b}_deg"])]
    w.write(18, [(253, "uint32", ts1), (254, "uint16", 0), (0, "enum", 8), (1, "enum", 1), *common,
                 (5, "enum", 2), (6, "enum", 6),
                 (16, "uint8", s["avg_hr"]), (17, "uint8", s["max_hr"]),
                 (18, "uint8", s["avg_cadence"]), (19, "uint8", s["max_cadence"]),
                 (20, "uint16", s["avg_power"]), (21, "uint16", s["max_power"]),
                 (25, "uint16", 0), (26, "uint16", 1), (28, "enum", 0),
                 (34, "uint16", s["np"]), (37, "uint16", bal100), (48, "uint32", s["work_j"]),
                 (101, "uint8", _half(s["te_left_pct"])), (102, "uint8", _half(s["te_right_pct"])),
                 (103, "uint8", _half(s["ps_left_pct"])), (104, "uint8", _half(s["ps_right_pct"])),
                 (114, "sint8", s["pco_left_mm"]), (115, "sint8", s["pco_right_mm"]),
                 (116, "uint8", pp("left", "start", "end")), (117, "uint8", pp("left", "peak_start", "peak_end")),
                 (118, "uint8", pp("right", "start", "end")), (119, "uint8", pp("right", "peak_start", "peak_end"))])
    # activity
    local_offset = datetime.now().astimezone().utcoffset() or timedelta(0)
    w.write(34, [(253, "uint32", ts1), (0, "uint32", timer_ms), (1, "uint16", 1), (2, "enum", 0),
                 (3, "enum", 26), (4, "enum", 1), (5, "uint32", ts1 + int(local_offset.total_seconds()))])

    with open(path, "wb") as fh:
        fh.write(w.to_bytes())
    return s
