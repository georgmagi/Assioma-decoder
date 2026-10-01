"""
Smart trainer control over Bluetooth FTMS (Fitness Machine Service 1.0, Bluetooth SIG), for ERG
workouts: the app sets the target power, the trainer holds it.

The protocol, from the specification (section numbers in brackets):
- Fitness Machine Control Point 0x2AD9 [4.16]: write an op code (+ parameter); the trainer answers by
  indication 0x80, <request op code>, <result code> [4.16.2.22]. Indications must be enabled before
  any write [4.16.3]. One procedure at a time: a write while one is pending is refused with an ATT
  error [4.16.3], so the next write waits for the answer.
- Request Control 0x00 first [4.16.2.1]. Control lasts until the connection ends, a Fitness Machine
  Status "Control Permission Lost" 0xFF [4.17], or a Reset 0x01 [4.16.2.2]; so after a reconnect it
  is requested again.
- Set Target Power 0x05, SINT16 W [4.16.2.6]; Start or Resume 0x07 [4.16.2.8]; Stop or Pause 0x08
  with 0x01 stop / 0x02 pause [4.16.2.9]; Set Indoor Bike Simulation Parameters 0x11: wind speed
  SINT16 0.001 m/s, grade SINT16 0.01 %, Crr UINT8 0.0001, Cw UINT8 0.01 kg/m [4.16.2.18].
- Result codes: 0x01 success, 0x02 op code not supported, 0x03 invalid parameter, 0x04 operation
  failed, 0x05 control not permitted [Table 4.24].
- Supported Power Range 0x2AD8: SINT16 min, SINT16 max, UINT16 increment, W [4.14].
- Fitness Machine Feature 0x2ACC: UINT32 machine features, UINT32 target setting features; target
  bit 3 = power target setting, bit 13 = indoor bike simulation [4.3].
- Indoor Bike Data 0x2AD2 [4.9], about once a second: flags UINT16, then the fields present.

The trainer is never written to except through TrainerControl: it only writes when the wanted
target changes (ramps at most once a second), waits for each answer, retries a few times, and
clamps every target to a safe range. Read-only towards the pedals is unaffected: this is the
trainer, a separate device.
"""
from __future__ import annotations

import asyncio
import struct
import time
from typing import Callable, Optional

_BASE = "-0000-1000-8000-00805f9b34fb"
FTMS_SERVICE = "00001826" + _BASE
FEATURE = "00002acc" + _BASE
BIKE_DATA = "00002ad2" + _BASE
POWER_RANGE = "00002ad8" + _BASE
CONTROL_POINT = "00002ad9" + _BASE
STATUS = "00002ada" + _BASE

# control point op codes [Table 4.15]
REQUEST_CONTROL, RESET, SET_TARGET_POWER, START, STOP_PAUSE, SET_SIM, RESPONSE = 0x00, 0x01, 0x05, 0x07, 0x08, 0x11, 0x80
SUCCESS, NOT_SUPPORTED, INVALID_PARAMETER, FAILED, NOT_PERMITTED = 0x01, 0x02, 0x03, 0x04, 0x05
RESULTS = {SUCCESS: "success", NOT_SUPPORTED: "op code not supported", INVALID_PARAMETER: "invalid parameter",
           FAILED: "operation failed", NOT_PERMITTED: "control not permitted"}
# Fitness Machine Status op codes [Table 4.26]
ST_RESET, ST_STOPPED_BY_USER, ST_STARTED_BY_USER, ST_TARGET_POWER, ST_SIM, ST_LOST = 0x01, 0x02, 0x04, 0x08, 0x12, 0xFF

SAFE_MIN_W, SAFE_MAX_W = 25, 600          # never asked for less / more, whatever a workout says


# --------------------------------------------------------------------------- encoding
def op_request_control() -> bytes:
    return bytes([REQUEST_CONTROL])


def op_reset() -> bytes:
    return bytes([RESET])


def op_start() -> bytes:
    return bytes([START])


def op_target_power(watts: int) -> bytes:
    return struct.pack("<Bh", SET_TARGET_POWER, int(watts))


def op_simulation(grade_pct: float, wind_mps: float = 0.0, crr: float = 0.004, cw: float = 0.51) -> bytes:
    return struct.pack("<BhhBB", SET_SIM, round(wind_mps * 1000), round(grade_pct * 100),
                       max(0, min(255, round(crr * 10000))), max(0, min(255, round(cw * 100))))


# --------------------------------------------------------------------------- decoding
def parse_response(data: bytes) -> Optional[tuple]:
    """(request op code, result code) of a control point indication, None if it isn't one."""
    if len(data) >= 3 and data[0] == RESPONSE:
        return data[1], data[2]
    return None


def parse_features(data: bytes) -> dict:
    machine, target = struct.unpack_from("<II", data.ljust(8, b"\0"))
    return {"power_measurement": bool(machine & (1 << 14)), "power_target": bool(target & (1 << 3)),
            "resistance_target": bool(target & (1 << 2)), "simulation": bool(target & (1 << 13))}


def parse_power_range(data: bytes) -> Optional[tuple]:
    if len(data) < 6:
        return None
    lo, hi, inc = struct.unpack_from("<hhH", data)
    return lo, hi, max(1, inc)


def parse_bike_data(data: bytes) -> dict:
    """Indoor Bike Data [4.9]. Flags bit 0 is inverted: 0 means instantaneous speed IS present."""
    if len(data) < 2:
        return {}
    flags = data[0] | data[1] << 8
    i, out = 2, {}

    def take(fmt):
        nonlocal i
        v = struct.unpack_from(fmt, data, i)[0]
        i += struct.calcsize(fmt)
        return v
    try:
        if not flags & 0x0001:
            out["speed_kmh"] = take("<H") / 100
        if flags & 0x0002:
            take("<H")                                   # average speed
        if flags & 0x0004:
            out["cadence_rpm"] = take("<H") / 2
        if flags & 0x0008:
            take("<H")                                   # average cadence
        if flags & 0x0010:
            take("<H"); take("<B")                       # total distance, UINT24
        if flags & 0x0020:
            out["resistance"] = take("<h")
        if flags & 0x0040:
            out["power_w"] = take("<h")
        if flags & 0x0080:
            take("<h")                                   # average power
        if flags & 0x0100:
            take("<H"); take("<H"); take("<B")           # energy total, per hour, per minute
        if flags & 0x0200:
            out["heart_rate_bpm"] = take("<B")
        if flags & 0x0400:
            take("<B")                                   # metabolic equivalent
        if flags & 0x0800:
            out["elapsed_s"] = take("<H")
    except struct.error:
        pass                                             # a short packet: keep what was there
    return out


# --------------------------------------------------------------------------- control
class TrainerError(Exception):
    """Something the link can't fix by retrying: reconnect."""


class TrainerControl:
    """What the trainer should do (`want`, set from any thread) and the async loop that makes it so
    over one connection. want: None (leave it alone), ("erg", watts) or ("sim", grade %).

    run() is called per connection with a connected bleak-like client; it returns when the
    connection ends or raises TrainerError for the caller to reconnect. It never raises for a
    refused target: that's reported in `error` and the ride goes on."""
    TIMEOUT_S = 4.0            # for an answer (the spec's ATT limit is 30 s; the trainer answers in ms)
    MIN_GAP_S = 1.0            # between two target writes (ramps)
    RETRIES = 3

    def __init__(self, log: Callable[[str], None] = lambda t: None) -> None:
        self.want: Optional[tuple] = None
        self.applied: Optional[tuple] = None
        self.features: dict = {}
        self.power_range = (SAFE_MIN_W, SAFE_MAX_W, 1)
        self.error = ""
        self.power_w: Optional[float] = None             # what the trainer reports (in ERG: about the target)
        self.cadence_rpm: Optional[float] = None
        self.data_at = 0.0
        self.controlled = False
        self.log = log
        self._answer: Optional[asyncio.Future] = None
        self._expect: Optional[int] = None               # the op code whose answer we wait for
        self._lost = False
        self._last_write = 0.0

    # ---- what to ask for
    def clamp(self, watts: float) -> int:
        lo, hi, inc = self.power_range
        lo, hi = max(lo, SAFE_MIN_W), min(hi, SAFE_MAX_W)
        w = max(lo, min(hi, watts))
        return int(round(w / inc) * inc)

    # ---- callbacks
    def on_indication(self, _sender, data: bytearray) -> None:
        r = parse_response(bytes(data))
        if r and r[0] == self._expect and self._answer is not None and not self._answer.done():
            self._answer.set_result(r[1])                # (an answer to anything else is stale: ignored)

    def on_status(self, _sender, data: bytearray) -> None:
        if not data:
            return
        op = data[0]
        if op == ST_LOST:
            self._lost = True
            self.controlled = False
            self.log("trainer: control permission lost")
        elif op == ST_STOPPED_BY_USER:
            self.log("trainer: stopped or paused on the trainer")
        elif op == ST_RESET:
            self.applied = None                          # its targets are back to defaults

    def on_data(self, _sender, data: bytearray) -> None:
        d = parse_bike_data(bytes(data))
        if "power_w" in d:
            self.power_w = d["power_w"]
        if "cadence_rpm" in d:
            self.cadence_rpm = d["cadence_rpm"]
        self.data_at = time.monotonic()

    # ---- one procedure: write, wait for its answer
    async def _call(self, client, payload: bytes) -> int:
        loop = asyncio.get_running_loop()
        for attempt in range(self.RETRIES):
            self._answer, self._expect = loop.create_future(), payload[0]
            try:
                await client.write_gatt_char(CONTROL_POINT, payload, response=True)
                return await asyncio.wait_for(self._answer, self.TIMEOUT_S)
            except asyncio.TimeoutError:
                self.log(f"trainer: no answer to op 0x{payload[0]:02X} (try {attempt + 1})")
            except Exception as e:                       # e.g. ATT "procedure already in progress"
                self.log(f"trainer: write op 0x{payload[0]:02X} failed: {type(e).__name__}: {e}")
            await asyncio.sleep(0.5 * (attempt + 1))
            if not getattr(client, "is_connected", True):
                break
        raise TrainerError(f"no answer to op 0x{payload[0]:02X}")

    async def _take_control(self, client) -> None:
        r = await self._call(client, op_request_control())
        if r != SUCCESS:
            raise TrainerError(f"control refused: {RESULTS.get(r, hex(r))}")
        self.controlled, self._lost = True, False
        r = await self._call(client, op_start())
        if r not in (SUCCESS, FAILED, NOT_SUPPORTED):    # FAILED = already started [4.16.2.22]
            self.log(f"trainer: start: {RESULTS.get(r, hex(r))}")
        self.applied = None
        self.log("trainer: in control")

    async def _apply(self, client, want: tuple) -> None:
        kind, value = want
        if kind == "erg":
            if not self.features.get("power_target", True):
                self.error = "this trainer can't hold a target power (no ERG)"
                self.applied = want
                return
            watts = self.clamp(value)
            payload = op_target_power(watts)
        else:
            grade = max(-10.0, min(10.0, float(value)))
            payload = op_simulation(grade)
        r = await self._call(client, payload)
        if r == NOT_PERMITTED:                           # lost control without being told: take it again
            await self._take_control(client)
            r = await self._call(client, payload)
        if r == SUCCESS:
            self.error = ""
        else:
            self.error = f"trainer refused {kind} {value:.0f}: {RESULTS.get(r, hex(r))}"
            self.log(self.error)
        self.applied = want                              # refused or not: don't hammer it with the same
        self._last_write = time.monotonic()

    def _due(self, want: Optional[tuple]) -> bool:
        if want is None or want == self.applied:
            return False
        if self.applied is None or want[0] != self.applied[0]:
            return True
        if time.monotonic() - self._last_write < self.MIN_GAP_S:
            return False
        step = self.power_range[2] if want[0] == "erg" else 0.1
        return abs(want[1] - self.applied[1]) >= step     # (in ERG, ramps move in whole watts)

    async def run(self, client, connected: Callable[[], bool], halt: Callable[[], bool]) -> None:
        """One connection: subscribe, read the trainer's abilities, take control, then keep the
        trainer on `want` until the connection or the app ends."""
        await client.start_notify(CONTROL_POINT, self.on_indication)   # indications first [4.16.3]
        for uuid, cb in ((STATUS, self.on_status), (BIKE_DATA, self.on_data)):
            try:
                await client.start_notify(uuid, cb)
            except Exception as e:
                self.log(f"trainer: no notifications on {uuid[4:8]}: {e}")
        try:
            self.features = parse_features(bytes(await client.read_gatt_char(FEATURE)))
        except Exception:
            self.features = {}
        try:
            self.power_range = parse_power_range(bytes(await client.read_gatt_char(POWER_RANGE))) or self.power_range
        except Exception:
            pass
        self.log(f"trainer: features {self.features}, power range {self.power_range}")
        self.applied, self.controlled = None, False
        await self._take_control(client)
        while connected() and not halt():
            if self._lost:
                await self._take_control(client)
            want = self.want
            if self._due(want):
                await self._apply(client, want)
            await asyncio.sleep(0.2)

    async def release(self, client) -> None:
        """Hand the trainer back (Reset [4.16.2.2]: default targets, control ends). Best effort."""
        if self.controlled:
            try:
                await asyncio.wait_for(client.write_gatt_char(CONTROL_POINT, op_reset(), response=True), 2.0)
            except Exception:
                pass
        self.controlled = False
