"""FTMS trainer control against a simulated trainer that follows the spec (and some that don't)."""
import asyncio
import struct

import ftms


class FakeTrainer:
    """A bleak-like client for an FTMS trainer. `refuse` maps op code -> result code; `silent` op codes
    get no answer; `lose_after` sends "control permission lost" after that many target writes."""

    def __init__(self, refuse=None, silent=(), lose_after=None, power_range=(0, 600, 1), features=None):
        self.cbs, self.writes, self.connected = {}, [], True
        self.refuse, self.silent, self.lose_after = refuse or {}, set(silent), lose_after
        self.power_range, self.target, self.controlled = power_range, None, False
        self.features = features if features is not None else struct.pack("<II", 1 << 14, (1 << 3) | (1 << 13))

    @property
    def is_connected(self):
        return self.connected

    async def start_notify(self, uuid, cb):
        self.cbs[uuid] = cb

    async def read_gatt_char(self, uuid):
        if uuid == ftms.FEATURE:
            return bytearray(self.features)
        if uuid == ftms.POWER_RANGE:
            return bytearray(struct.pack("<hhH", *self.power_range))
        raise Exception("not readable")

    async def write_gatt_char(self, uuid, data, response=True):
        assert uuid == ftms.CONTROL_POINT and ftms.CONTROL_POINT in self.cbs     # indications on first
        op = data[0]
        self.writes.append(bytes(data))
        if op in self.silent:
            return
        result = self.refuse.get(op, ftms.SUCCESS)
        if op == ftms.REQUEST_CONTROL and result == ftms.SUCCESS:
            self.controlled = True
        elif op != ftms.REQUEST_CONTROL and not self.controlled:
            result = ftms.NOT_PERMITTED
        if op == ftms.SET_TARGET_POWER and result == ftms.SUCCESS:
            self.target = struct.unpack_from("<h", data, 1)[0]
        asyncio.get_running_loop().call_soon(self.cbs[ftms.CONTROL_POINT], None, bytearray([0x80, op, result]))
        targets = sum(w[0] == ftms.SET_TARGET_POWER for w in self.writes)
        if self.lose_after and op == ftms.SET_TARGET_POWER and targets == self.lose_after:
            self.controlled = False
            asyncio.get_running_loop().call_later(0.05, self.cbs[ftms.STATUS], None, bytearray([0xFF]))


def _drive(trainer, script, ctl=None, timeout=10):
    """Run the control loop while `script(ctl)` (async) changes what we want."""
    ctl = ctl or ftms.TrainerControl()
    ctl.MIN_GAP_S = 0.05
    done = {"stop": False}

    async def main():
        task = asyncio.create_task(ctl.run(trainer, lambda: trainer.connected, lambda: done["stop"]))
        await script(ctl)
        done["stop"] = True
        await asyncio.wait_for(task, timeout)
    asyncio.run(main())
    return ctl


def test_encoding_follows_the_spec():
    assert ftms.op_target_power(250) == bytes([0x05, 0xFA, 0x00])                   # SINT16 LE
    assert ftms.op_simulation(2.5) == bytes([0x11, 0, 0, 0xFA, 0x00, 40, 51])      # grade 0.01 %, Crr 0.0001, Cw 0.01
    assert ftms.op_simulation(-1.0)[3:5] == struct.pack("<h", -100)
    assert ftms.parse_response(bytes([0x80, 0x05, 0x01])) == (0x05, 0x01)
    assert ftms.parse_power_range(struct.pack("<hhH", 0, 600, 1)) == (0, 600, 1)
    f = ftms.parse_features(struct.pack("<II", 1 << 14, (1 << 3) | (1 << 13)))
    assert f["power_target"] and f["simulation"] and f["power_measurement"]


def test_indoor_bike_data_fields_in_spec_order():
    # flags: speed present (bit 0 = 0), cadence (bit 2), power (bit 6), heart rate (bit 9)
    flags = 0x0004 | 0x0040 | 0x0200
    data = struct.pack("<HHHhB", flags, 3250, 180, 212, 141)
    assert ftms.parse_bike_data(data) == {"speed_kmh": 32.5, "cadence_rpm": 90.0, "power_w": 212, "heart_rate_bpm": 141}
    # more data (bit 0 = 1: no speed), distance UINT24 then power
    data = struct.pack("<H", 0x0001 | 0x0010 | 0x0040) + bytes([1, 2, 3]) + struct.pack("<h", 150)
    assert ftms.parse_bike_data(data) == {"power_w": 150}
    assert ftms.parse_bike_data(b"\x40\x00\x01") == {}                              # truncated: no crash


def test_takes_control_then_sets_and_follows_the_target():
    t = FakeTrainer()

    async def script(ctl):
        ctl.want = ("erg", 180)
        await asyncio.sleep(0.5)
        assert t.target == 180
        ctl.want = ("erg", 181.4)                                                   # under 1 W: not re-sent...
        await asyncio.sleep(0.4)
        ctl.want = ("erg", 250)
        await asyncio.sleep(0.5)
    ctl = _drive(t, script)
    ops = [w[0] for w in t.writes]
    assert ops[:2] == [ftms.REQUEST_CONTROL, ftms.START] and t.target == 250 and ctl.error == ""


def test_targets_are_clamped_to_the_trainer_and_a_safe_range():
    t = FakeTrainer(power_range=(10, 2000, 5))

    async def script(ctl):
        ctl.want = ("erg", 900)
        await asyncio.sleep(0.4)
        assert t.target == ftms.SAFE_MAX_W
        ctl.want = ("erg", 3)
        await asyncio.sleep(0.4)
        assert t.target == ftms.SAFE_MIN_W
        ctl.want = ("erg", 202)
        await asyncio.sleep(0.4)
        assert t.target == 200                                                      # 5 W steps
    _drive(t, script)


def test_lost_control_is_taken_back_and_the_target_resent():
    t = FakeTrainer(lose_after=1)

    async def script(ctl):
        ctl.want = ("erg", 200)
        await asyncio.sleep(0.6)
        ctl.want = ("erg", 220)
        await asyncio.sleep(0.6)
    _drive(t, script)
    assert [w[0] for w in t.writes].count(ftms.REQUEST_CONTROL) == 2 and t.target == 220


def test_a_refused_target_is_reported_not_hammered():
    t = FakeTrainer(refuse={ftms.SET_TARGET_POWER: ftms.INVALID_PARAMETER})

    async def script(ctl):
        ctl.want = ("erg", 200)
        await asyncio.sleep(0.8)
        assert "invalid parameter" in ctl.error
    _drive(t, script)
    assert [w[0] for w in t.writes].count(ftms.SET_TARGET_POWER) == 1


def test_no_answer_means_reconnect_not_a_hang():
    t = FakeTrainer(silent={ftms.REQUEST_CONTROL})
    ctl = ftms.TrainerControl()
    ctl.TIMEOUT_S = 0.1

    async def main():
        try:
            await ctl.run(t, lambda: True, lambda: False)
        except ftms.TrainerError:
            return True
    assert asyncio.run(asyncio.wait_for(main(), 5)) is True
    assert len(t.writes) == ftms.TrainerControl.RETRIES


def test_start_already_running_is_fine_and_release_resets():
    t = FakeTrainer(refuse={ftms.START: ftms.FAILED})

    async def script(ctl):
        ctl.want = ("sim", 0)
        await asyncio.sleep(0.4)
        await ctl.release(t)
    ctl = _drive(t, script)
    assert t.writes[-1] == bytes([ftms.RESET]) and t.writes[2][0] == ftms.SET_SIM and not ctl.controlled
