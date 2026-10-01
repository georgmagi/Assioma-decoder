#!/usr/bin/env python3
"""
Decoder for the Favero Assioma private Bluetooth stream.

Reverse engineered from an Assioma PRO RS-2 (left pedal = main unit) using
nRF Connect logs, a Garmin watch (ANT+) as ground truth and the Favero app's
Live screen. See PROTOCOL.md for the byte-level description.

Input:  an nRF Connect log export (CSV) with notifications from
        characteristic 2701000A-489A-4A48-9DA8-80097E460D77 (private stream)
        and optionally 2A63 (standard Cycling Power Measurement).
Output: a per-message CSV and a per-second CSV with every metric.

Usage:
    python assioma_decoder.py LOG.csv --date 2026-09-26 [--utc-offset 3] [-o out_prefix]

Standard library only (Python 3.8+).
"""
from __future__ import annotations

import argparse
import csv
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, Iterator, Optional

PRIVATE_CHAR = "2701000A-489A-4A48-9DA8-80097E460D77"
CPM_CHAR = "2A63"
HR_CHAR = "2A37"
ANT_HR_CHAR = "ANT+2A37"     # heart rate from the ANT+ stick (ant_hr.py), already exact: no repeat filter

DEG_PER_UNIT = 360 / 256
RIDER_POSITION = {0: "seated", 1: "to_seated", 2: "standing", 3: "to_standing"}

# --------------------------------------------------------------------------- #
# SLIP framing
# --------------------------------------------------------------------------- #
SLIP_END, SLIP_ESC, SLIP_ESC_END, SLIP_ESC_ESC = 0xC0, 0xDB, 0xDC, 0xDD


class SlipDecoder:
    """Streaming SLIP decoder: feed raw notification bytes, get whole frames."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self._esc = False

    def feed(self, data: bytes) -> Iterator[bytes]:
        for b in data:
            if self._esc:
                self._buf.append(SLIP_END if b == SLIP_ESC_END else SLIP_ESC if b == SLIP_ESC_ESC else b)
                self._esc = False
            elif b == SLIP_ESC:
                self._esc = True
            elif b == SLIP_END:
                if self._buf:
                    yield bytes(self._buf)
                self._buf.clear()
            else:
                self._buf.append(b)


# --------------------------------------------------------------------------- #
# Message parsing
# --------------------------------------------------------------------------- #
def _u8(v: int, invalid: int = 0xFF) -> Optional[int]:
    return None if v == invalid else v


def _s8(v: int) -> Optional[int]:
    """Signed byte, 0x80 = invalid."""
    if v == 0x80:
        return None
    return v - 256 if v > 127 else v


def _half(v: int) -> Optional[float]:
    """Percent in 0.5 steps, 0xFF = invalid. Values over 100 % (seen rarely at
    near-zero power) are treated as invalid too."""
    return None if v > 200 else v / 2


def parse_frame(frame: bytes) -> dict:
    """Parse one unescaped frame: [type][length][counter][payload...]."""
    if len(frame) < 3:
        return {"type": None, "error": "short frame", "raw": frame.hex()}
    mtype, length, counter = frame[0], frame[1], frame[2]
    payload = frame[3:]
    out: dict = {"type": f"{mtype:02X}", "counter": counter, "raw": frame.hex()}
    if length != len(frame) - 2:
        out["error"] = f"length byte {length} != {len(frame) - 2}"
        return out
    p = payload

    if mtype == 0x10 and len(p) == 6:          # power / cadence / balance
        valid = p[5] != 0xFF
        out.update(
            cadence_rpm=p[0],
            power_w=p[1] | p[2] << 8,
            energy_counter=p[3] | p[4] << 8,    # ~0.74 J per unit, wraps at 65536
            balance_right_pct=p[5] if valid else None,
        )
    elif mtype == 0x13 and len(p) == 4:        # torque effectiveness / smoothness
        out.update(
            te_left_pct=_half(p[0]), te_right_pct=_half(p[1]),
            ps_left_pct=_half(p[2]), ps_right_pct=_half(p[3]),
        )
    elif mtype in (0xE0, 0xE1) and len(p) == 6:  # power phase, left / right
        side = "left" if mtype == 0xE0 else "right"
        if p[:4] != b"\xc0\xc0\xc0\xc0":
            out.update({
                f"pp_{side}_start_deg": round(p[0] * DEG_PER_UNIT, 1),
                f"pp_{side}_peak_start_deg": round(p[1] * DEG_PER_UNIT, 1),
                f"pp_{side}_peak_end_deg": round(p[2] * DEG_PER_UNIT, 1),
                f"pp_{side}_end_deg": round(p[3] * DEG_PER_UNIT, 1),
            })
        out["pp_tail"] = p[4:].hex()            # constant 201c in all captures
    elif mtype == 0xE2 and len(p) == 3:        # platform centre offset / rider position
        out.update(
            pco_left_mm=_s8(p[0]), pco_right_mm=_s8(p[1]),
            rider_position=RIDER_POSITION.get(p[2], f"unknown_{p[2]}"),
        )
    elif mtype == 0x52 and len(p) == 6:        # status
        out.update(
            status_flags=p[:2].hex(),
            battery_left_pct=p[2], battery_right_pct=p[3],
            status_unknown=p[4:].hex(),
        )
    else:
        out["unparsed_payload"] = p.hex()
    return out


def parse_cpm(data: bytes) -> dict:
    """Standard Bluetooth Cycling Power Measurement (0x2A63)."""
    flags = int.from_bytes(data[0:2], "little")
    out: dict = {"type": "2A63", "cpm_power_w": int.from_bytes(data[2:4], "little", signed=True)}
    i = 4
    if flags & 0x01:                            # pedal power balance
        bal = data[i] / 2
        out["cpm_balance_pct"] = bal
        out["cpm_balance_ref"] = "left" if flags & 0x02 else "unknown"
        i += 1
    if flags & 0x04:                            # accumulated torque
        out["cpm_accum_torque_nm"] = int.from_bytes(data[i:i + 2], "little") / 32
        i += 2
    if flags & 0x10:                            # wheel revolution data
        out["cpm_wheel_revs"] = int.from_bytes(data[i:i + 4], "little")
        out["cpm_wheel_time"] = int.from_bytes(data[i + 4:i + 6], "little")
        i += 6
    if flags & 0x20:                            # crank revolution data
        out["cpm_crank_revs"] = int.from_bytes(data[i:i + 2], "little")
        out["cpm_crank_time"] = int.from_bytes(data[i + 2:i + 4], "little")  # 1/1024 s
        i += 4
    return out


# --------------------------------------------------------------------------- #
# nRF Connect log reading
# --------------------------------------------------------------------------- #
_LINE = re.compile(r"Updated Value of Characteristic (\S+) to ([0-9A-Fa-f ]*)\.?$")


@dataclass
class Notification:
    time: datetime
    char: str
    data: bytes


def read_nrf_log(path: str, date: str, utc_offset_h: float) -> Iterator[Notification]:
    """Yield notifications with UTC times. Handles midnight rollover."""
    day = datetime.strptime(date, "%Y-%m-%d")
    last: Optional[datetime] = None
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            m = _LINE.search(row.get("Line") or "")
            if not m:
                continue
            t = datetime.combine(day.date(), datetime.strptime(row["Timestamp"], "%H:%M:%S.%f").time())
            if last and t < last - timedelta(hours=12):
                day += timedelta(days=1)
                t += timedelta(days=1)
            last = t
            yield Notification(t - timedelta(hours=utc_offset_h), m.group(1).upper(), bytes.fromhex(m.group(2).replace(" ", "")))


def parse_hr(data: bytes) -> dict:
    """Standard Heart Rate Measurement (0x2A37)."""
    flags = data[0]
    hr = int.from_bytes(data[1:3], "little") if flags & 0x01 else data[1]
    i = 3 if flags & 0x01 else 2
    if flags & 0x08:                     # energy expended present
        i += 2
    out: dict = {"type": HR_CHAR, "heart_rate_bpm": hr}
    if flags & 0x10:                     # RR intervals, 1/1024 s each
        out["rr_ms"] = [round(int.from_bytes(data[j:j + 2], "little") * 1000 / 1024, 1)
                        for j in range(i, len(data) - 1, 2)]
    return out


class RRDedup:
    """Drops repeated RR intervals.

    Straps do not all behave alike: in the nRF capture the HRM 200 sent one notification per
    beat, but on a Windows connection it notified at a fixed ~2 Hz and repeated the last RR
    until the next beat. A repeat cannot be told from a genuinely equal interval by value
    alone (1/1024 s resolution: ~5 % of consecutive beats are equal), so we track the time
    window [lo, hi] in which the last beat must have happened, and how much consecutive
    intervals differ (sd = recent RMSSD). An equal value is a repeat for sure if no beat can
    have happened yet (lo + rr - 2 sd > now), a new beat if one must have happened
    (hi + rr + 2 sd <= now); in between it counts as a repeat once the strap has shown that it
    repeats. Simulated at 55-160 bpm with breathing-driven HRV: <= 0.5 % beats wrong.
    """
    SLACK = 0.05        # s, notification latency jitter

    def __init__(self) -> None:
        self.prev: Optional[tuple] = None   # (time s, rr tuple) of the previous notification
        self.lo = self.hi = None            # bounds on the time of the last accepted beat (s)
        self.repeats = False                # has this strap been seen repeating values?
        self._diffs: list = []              # recent squared successive differences (s^2)
        self._last_rr: Optional[float] = None

    def _sd(self) -> float:
        return max(0.008, (sum(self._diffs) / len(self._diffs)) ** 0.5) if self._diffs else 0.05

    def feed(self, t: float, rr: list) -> list:
        rr = [x for x in rr if x > 0]       # 0 = no valid interval
        if not rr:
            return []
        s = sum(rr) / 1000
        # a repeat carries the previous notification's interval(s) again, or just its last one
        if self.prev and tuple(rr) == self.prev[1][-len(rr):]:
            sd = 2 * self._sd() * len(rr) ** 0.5
            if t - self.prev[0] < 0.75 * s:
                self.repeats = True                      # too soon for a strap that notifies per beat
            if self.lo is not None and self.lo + s - sd > t + self.SLACK:
                self.repeats, new = True, False          # no beat can have happened yet
            elif self.hi is not None and self.hi + s + sd <= t:
                new = True                               # a beat must have happened
            else:
                new = not self.repeats
            if not new:
                self.prev = (t, tuple(rr))
                return []
        for x in rr:
            if self._last_rr is not None and abs(x - self._last_rr) < 0.25 * self._last_rr:
                self._diffs = (self._diffs + [((x - self._last_rr) / 1000) ** 2])[-30:]
            self._last_rr = x
        t_prev = self.prev[0] if self.prev else t - s
        lo, hi = t_prev, t
        if self.lo is not None:
            lo, hi = max(self.lo + s, t_prev), min(self.hi + s, t)
            if lo > hi + self.SLACK:                     # lost track (dropped packet): start over
                lo, hi = t_prev, t
        self.lo, self.hi, self.prev = lo, hi, (t, tuple(rr))
        return rr


class Decoder:
    """Stateful decoder: feed notifications one by one (frames may span notifications)."""

    def __init__(self) -> None:
        self._slip = SlipDecoder()
        self._rr = RRDedup()

    def feed(self, n: Notification) -> list[dict]:
        if n.char == PRIVATE_CHAR:
            return [{"time": n.time, **parse_frame(f)} for f in self._slip.feed(n.data)]
        if n.char == CPM_CHAR:
            return [{"time": n.time, **parse_cpm(n.data)}]
        if n.char == ANT_HR_CHAR and len(n.data) >= 2:
            return [{"time": n.time, **parse_hr(n.data)}]
        if n.char == HR_CHAR and len(n.data) >= 2:
            r = {"time": n.time, **parse_hr(n.data)}
            if "rr_ms" in r:
                r["rr_ms"] = self._rr.feed(n.time.timestamp(), r["rr_ms"])
                if not r["rr_ms"]:
                    del r["rr_ms"]
            return [r]
        return []


def decode(notes: Iterable[Notification]) -> list[dict]:
    d = Decoder()
    return [r for n in notes for r in d.feed(n)]


# --------------------------------------------------------------------------- #
# Per-second table
# --------------------------------------------------------------------------- #
SECOND_COLUMNS = [
    "power_w", "cadence_rpm", "heart_rate_bpm", "balance_right_pct", "power_left_w", "power_right_w",
    "te_left_pct", "te_right_pct", "ps_left_pct", "ps_right_pct",
    "pp_left_start_deg", "pp_left_peak_start_deg", "pp_left_peak_end_deg", "pp_left_end_deg",
    "pp_right_start_deg", "pp_right_peak_start_deg", "pp_right_peak_end_deg", "pp_right_end_deg",
    "pco_left_mm", "pco_right_mm", "rider_position", "battery_left_pct", "battery_right_pct",
]
# How long a value may be carried forward (s). The private stream sends
# 10 every ~10 s, 13/E0 ~1 Hz, E2 ~2 Hz, E1 irregularly, 52 every ~30 s.
MAX_AGE = {"battery_left_pct": 120, "battery_right_pct": 120, "cadence_rpm": 2, "_default": 5,
           **{f"pp_right_{k}": 10 for k in ("start_deg", "peak_start_deg", "peak_end_deg", "end_deg")}}


_ONE = timedelta(seconds=1)


class PerSecond:
    """Turns time-ordered messages into one row per second.

    The same rules are used offline (per_second) and live (the recorder):
    ingest() returns the whole seconds completed before the message,
    values_at() gives the current values for a live display.
    """

    def __init__(self) -> None:
        self.latest: dict = {}      # name -> (time, value)
        self.prev_crank = None      # (revs, event_time_1024, received_at) from 2A63
        self.t: Optional[datetime] = None   # start of the second being filled
        self._rr: list = []                 # beat-to-beat intervals received this second

    def advance(self, until: datetime) -> list[dict]:
        """Emit every second that ends at or before `until`."""
        out = []
        while self.t is not None and self.t + _ONE <= until:
            out.append(self._emit())
        return out

    def _emit(self) -> dict:
        rec = self.values_at(self.t + _ONE, stamp=self.t)
        if self._rr:
            rec["rr_ms"] = self._rr
            self._rr = []
        self.t += _ONE
        return rec

    def ingest(self, r: dict) -> list[dict]:
        if self.t is None:
            self.t = r["time"].replace(microsecond=0)
        out = self.advance(r["time"])
        for k, v in r.items():
            if k not in ("time", "type", "raw", "counter", "rr_ms"):
                self.latest[k] = (r["time"], v)
        self._rr += r.get("rr_ms", [])
        # standard power/cadence (1 Hz) take priority over the sparse private 10
        if r.get("type") == CPM_CHAR:
            self.latest["power_w"] = (r["time"], r["cpm_power_w"])
            if "cpm_balance_pct" in r and r["cpm_power_w"] > 0:
                self.latest["balance_right_pct"] = (r["time"], 100 - r["cpm_balance_pct"])
            if "cpm_crank_revs" in r:               # cadence from crank events (1/1024 s)
                revs, et = r["cpm_crank_revs"], r["cpm_crank_time"]
                prev = self.prev_crank
                if prev:
                    d_rev = (revs - prev[0]) % 65536
                    d_t = (et - prev[1]) % 65536
                    if d_rev and d_t:
                        if d_t <= 3 * 1024:              # skip the first event after a pause
                            self.latest["cadence_rpm"] = (r["time"], round(d_rev * 60 * 1024 / d_t))
                        self.prev_crank = (revs, et, r["time"])
                    elif (r["time"] - prev[2]).total_seconds() > 2:
                        self.latest["cadence_rpm"] = (r["time"], 0)   # no crank event for 2 s
                else:
                    self.prev_crank = (revs, et, r["time"])
        return out

    def finish(self) -> list[dict]:
        """Emit the last, partly filled second."""
        if self.t is None:
            return []
        return [self._emit()]

    def values_at(self, now: datetime, stamp: Optional[datetime] = None) -> dict:
        rec: dict = {"time": stamp if stamp is not None else now}
        for c in SECOND_COLUMNS:
            if c in self.latest:
                ts, v = self.latest[c]
                if (now - ts).total_seconds() <= MAX_AGE.get(c, MAX_AGE["_default"]):
                    rec[c] = v
        p, b = rec.get("power_w"), rec.get("balance_right_pct")
        if p is not None and b is not None:
            rec["power_right_w"] = round(p * b / 100)
            rec["power_left_w"] = p - rec["power_right_w"]
        return rec


def per_second(rows: list[dict]) -> list[dict]:
    ps = PerSecond()
    out: list[dict] = []
    for r in sorted(rows, key=lambda r: r["time"]):
        out += ps.ingest(r)
    return out + ps.finish()


def write_csv(path: str, rows: list[dict], columns: Optional[list[str]] = None) -> None:
    if columns is None:
        columns = []
        for r in rows:
            columns += [k for k in r if k not in columns]
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: (v.isoformat() + "Z" if isinstance(v, datetime)
                            else " ".join(map(str, v)) if isinstance(v, list) else v) for k, v in r.items()})


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("log", help="nRF Connect CSV log export")
    ap.add_argument("--date", required=True, help="date the log was recorded (YYYY-MM-DD, local)")
    ap.add_argument("--utc-offset", type=float, default=0.0, help="hours ahead of UTC for the log's timestamps (EEST = 3)")
    ap.add_argument("-o", "--out", default="assioma", help="output file prefix")
    args = ap.parse_args()

    rows = decode(read_nrf_log(args.log, args.date, args.utc_offset))
    sec = per_second(rows)
    write_csv(f"{args.out}_messages.csv", rows)
    write_csv(f"{args.out}_per_second.csv", sec, ["time"] + SECOND_COLUMNS)
    types: dict = {}
    for r in rows:
        types[r.get("type")] = types.get(r.get("type"), 0) + 1
    errors = sum(1 for r in rows if "error" in r)
    print(f"{len(rows)} messages {types}, {errors} errors -> {len(sec)} seconds")
    print(f"wrote {args.out}_messages.csv and {args.out}_per_second.csv")


if __name__ == "__main__":
    main()
