"""The room's temperature and humidity from a Xiaomi Bluetooth thermometer (Mijia MJWSD06MMC,
LYWSD03MMC and their relatives on stock firmware).

Stock firmware encrypts its advertisements, but the GATT characteristic ebe0ccc1 hands out the
reading unencrypted: 5 bytes, temperature int16 LE in 1/100 °C, humidity % (uint8), battery
voltage uint16 LE in mV. So the sensor is read by connecting briefly, reading that one
characteristic and letting go again: no pairing, no key, and the thermometer's battery barely
notices one connection every couple of minutes.

RoomSensor is a thread with its own asyncio loop, apart from the pedals' Bluetooth worker. The
Engine says how often it wants a reading (every 2 min on a ride, every 10 min otherwise); the
newest reading is in .latest. The first time it finds a thermometer by name it remembers the
address (.address, kept in settings by the Engine)."""
import asyncio
import threading
import time
from typing import Callable, Optional

DATA_UUID = "ebe0ccc1-7a0a-4b0c-8a1a-6ff2997da3a6"
NAMES = ("MJWSD06MMC", "MJWSD05MMC", "LYWSD03MMC", "MHO-C401", "MHO-C122", "LYWSDCGQ")
FRESH_S = 35 * 60                 # older than this, a reading no longer describes the room


def parse(data: bytes) -> Optional[dict]:
    """ebe0ccc1's 5 bytes -> {temp_c, rh_pct, volt}; None if they can't be a room."""
    if len(data) < 3:
        return None
    t = int.from_bytes(data[0:2], "little", signed=True) / 100
    rh = data[2]
    volt = int.from_bytes(data[3:5], "little") / 1000 if len(data) >= 5 else None
    if not (-20 <= t <= 60 and 0 < rh <= 100):
        return None
    return {"temp_c": t, "rh_pct": rh, "volt": volt}


class RoomSensor(threading.Thread):
    def __init__(self, log: Callable[[str], None] = print, address: Optional[str] = None) -> None:
        super().__init__(daemon=True, name="room")
        self.log, self.address = log, address
        self.every_s: Optional[float] = None      # None: don't read; else seconds between readings
        self.latest: Optional[dict] = None        # {temp_c, rh_pct, volt, at (time.time())}
        self.error: Optional[str] = None
        self._stop = threading.Event()
        self._poke = threading.Event()
        self._fails = 0

    def want(self, every_s: Optional[float]) -> None:
        """How often to read (None = stop). A shorter interval than before reads now."""
        now_sooner = every_s is not None and (self.every_s is None or every_s < self.every_s)
        self.every_s = every_s
        if now_sooner:
            self._poke.set()

    def view(self) -> Optional[dict]:
        r = self.latest
        if r is None:
            return {"state": "failed" if self.error else "searching", "note": self.error} if self.every_s else None
        age = time.time() - r["at"]
        return {"state": "ok" if age < FRESH_S else "old", "temp_c": r["temp_c"], "rh_pct": r["rh_pct"],
                "age_s": round(age), "volt": r.get("volt")}

    def stop(self) -> None:
        self._stop.set()
        self._poke.set()

    def run(self) -> None:
        try:
            from bleak.backends.winrt.util import uninitialize_sta   # Windows only; harmless elsewhere
            uninitialize_sta()
        except Exception:
            pass
        try:
            asyncio.run(self._main())
        except Exception as e:
            self.log(f"room sensor: stopped: {type(e).__name__}: {e}")

    async def _main(self) -> None:
        last = 0.0
        while not self._stop.is_set():
            every = self.every_s
            if every is not None and time.time() - last >= every:
                try:                                         # nothing in Bluetooth may hang this loop
                    ok = await asyncio.wait_for(self._read_once(), 75)
                except asyncio.TimeoutError:
                    self.error, ok = "no answer", False
                    self.log("room sensor: no answer in 75 s")
                last = time.time()
                if not ok:                                   # try again sooner, then back off
                    self._fails += 1
                    last -= every - min(every, 30 * self._fails)
            await asyncio.get_running_loop().run_in_executor(None, self._poke.wait, 5)
            self._poke.clear()

    async def _read_once(self) -> bool:
        from bleak import BleakClient, BleakScanner
        try:
            dev = None
            if self.address:
                dev = await BleakScanner.find_device_by_address(self.address, timeout=15)
            else:
                dev = await BleakScanner.find_device_by_filter(
                    lambda d, adv: any((d.name or adv.local_name or "").startswith(n) for n in NAMES), timeout=15)
                if dev is not None:
                    self.address = dev.address
                    self.log(f"room sensor: found {dev.name} ({dev.address})")
            if dev is None:
                self.error = "thermometer not found"
                return False
            async with BleakClient(dev, timeout=20) as c:
                r = parse(bytes(await c.read_gatt_char(DATA_UUID)))
            if r is None:
                self.error = "odd reading"
                return False
            self.latest = {**r, "at": time.time()}
            self.error, self._fails = None, 0
            return True
        except Exception as e:
            self.error = f"{type(e).__name__}"
            if self._fails in (0, 3):
                self.log(f"room sensor: {type(e).__name__}: {e}")
            return False
