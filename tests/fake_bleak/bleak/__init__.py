"""
Fake `bleak` for testing the recorder's Bluetooth logic without hardware.

Put tests/fake_bleak first on sys.path. Simulates:
  scan 1: only the right pedal, advertised without its L/R suffix
  scan 2: left pedal without a name
  scan 3+: both pedals named, plus a heart-rate strap
The right pedal sends bogus 999 W standard power and no private stream.
The left pedal replays samples/ride_2026-09-26_nrf.csv and drops the connection once.
"""
import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, ROOT)
import assioma_decoder as d  # noqa: E402

NOTES = list(d.read_nrf_log(os.path.join(ROOT, "samples", "ride_2026-09-26_nrf.csv"), "2026-09-26", 3))
STATE = {"connects": 0, "scans": 0, "left": 0}
_BASE = "-0000-1000-8000-00805f9b34fb"


class Dev:
    def __init__(self, name, address):
        self.name, self.address = name, address


class Adv:
    def __init__(self, name, rssi, uuids):
        self.local_name, self.rssi, self.service_uuids = name, rssi, uuids


class Char:
    def __init__(self, uuid):
        self.uuid = uuid


class BleakScanner:
    @staticmethod
    async def discover(timeout=5.0, return_adv=False):
        STATE["scans"] += 1
        await asyncio.sleep(0.2)
        if STATE["scans"] == 1:
            return {"R": (Dev("AssiomaPRO12344", "R"), Adv("AssiomaPRO12344", -50, []))}
        left = ("AssiomaPRO12345L" if STATE["scans"] > 2 else None)
        return {"R": (Dev("AssiomaPRO12344R", "R"), Adv("AssiomaPRO12344R", -50, [])),
                "L": (Dev(left, "L"), Adv(left, -60, [])),
                "H": (Dev("HRM200:123", "H"), Adv("HRM200:123", -70, ["0000180d" + _BASE]))}


class BleakClient:
    def __init__(self, dev, disconnected_callback=None, timeout=20):
        self.dev, self.cb, self.connected, self.subs = dev, disconnected_callback, False, {}

    async def __aenter__(self):
        STATE["connects"] += 1
        self.connected = True
        self.task = asyncio.create_task(self._feed())
        return self

    async def __aexit__(self, *a):
        self.connected = False
        self.task.cancel()

    @property
    def is_connected(self):
        return self.connected

    async def start_notify(self, uuid, callback):
        if self.dev.address == "H" and "2a37" not in uuid:
            raise Exception("no such characteristic")
        self.subs[uuid.lower()] = callback

    def _send(self, uuid, data):
        if uuid in self.subs:
            self.subs[uuid](Char(uuid), bytearray(data))

    async def _feed(self):
        if self.dev.address == "H":
            while True:
                await asyncio.sleep(1)
                self._send("00002a37" + _BASE, [0x10, 70, 0x80, 0x03])
        if self.dev.address == "R":
            while True:
                await asyncio.sleep(0.5)
                self._send("00002a63" + _BASE, [0x20, 0, 0xE7, 0x03, 1, 0, 0, 4])
        STATE["left"] += 1
        first = STATE["left"] == 1
        for i, n in enumerate(NOTES[:400]):
            await asyncio.sleep(0.005)
            uuid = ("0000" + n.char.lower() + _BASE) if len(n.char) == 4 else n.char.lower()
            self._send(uuid, n.data)
            if first and i == 150:
                self.connected = False
                self.cb(self)
                return
