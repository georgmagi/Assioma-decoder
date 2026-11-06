"""The room's temperature and humidity from a Xiaomi Bluetooth thermometer running pvvx's firmware
(github.com/pvvx/ATC_MiThermometer; flashed onto a Mijia MJWSD06MMC, Nov 2026).

Two ways in, both unencrypted:
  - Live: the thermometer broadcasts a BTHome v2 advertisement (service data 0xFCD2) every 5 s with
    temperature and humidity in 1/100 units. A short scan picks it up; no connection.
  - History: the thermometer itself keeps a 5-minute average in its flash (~20,000 records, about
    10 weeks) stamped with its own clock. Every half hour off the bike the app connects for a few
    seconds, sets that clock, downloads the records it doesn't have yet and appends them to
    rides/room/room_YYYY-MM.csv. So the room's history is complete even while the app was closed.

pvvx protocol (characteristic 0x1F1F, write a command, the answer comes as a notification):
  0x23 [u32 utc]    get/set the clock          -> 0x23 u32 now, u32 last set
  0x35 [u16 count]  send the newest `count` records, newest first
                    -> 0x35 u16 index, u32 utc, s16 °C x100, u16 %RH x100, u16 mV; ends with 0x35 00 00

RoomSensor is a thread with its own asyncio loop, apart from the pedals' Bluetooth worker. The Engine
says how often it wants a live reading and whether a history download may happen now (not on a ride).
"""
import asyncio
import calendar
import math
import os
import threading
import time
from typing import Callable, Optional

CMD_UUID = "00001f1f-0000-1000-8000-00805f9b34fb"
BTHOME = "0000fcd2-0000-1000-8000-00805f9b34fb"
NAMES = ("MJ6_", "ATC_", "MHO_", "CGG_", "CGDK2_")    # pvvx firmware's names
FRESH_S = 35 * 60                 # older than this, a reading no longer describes the room
SYNC_S = 30 * 60                  # history download this often (off the bike)
MEMO_S = 300                      # the thermometer stores a record every 5 min
CLOCK_OK = 1.7e9                  # records stamped before this were made before the clock was set
FIRST_SYNC = 7 * 24 * 12          # the first download goes back at most a week


# ------------------------------------------------------------------------------------- history file
def log_path(folder: str, at: float) -> str:
    return os.path.join(folder, "room", time.strftime("room_%Y-%m.csv", time.gmtime(at)))


def log_reading(folder: str, at: float, temp_c: float, rh_pct: float) -> None:
    """One record, kept by month: rides/room/room_YYYY-MM.csv (utc, °C, % RH). Only appended to."""
    path = log_path(folder, at)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    new = not os.path.exists(path)
    with open(path, "a") as fh:
        if new:
            fh.write("utc,temp_c,rh_pct\n")
        fh.write(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(at))},{temp_c:.2f},{rh_pct:.1f}\n")


def _rows(path: str):
    try:
        with open(path) as fh:
            next(fh, None)
            for line in fh:
                try:
                    u, t, rh = line.strip().split(",")
                    yield calendar.timegm(time.strptime(u, "%Y-%m-%dT%H:%M:%SZ")), float(t), float(rh)
                except ValueError:
                    continue
    except OSError:
        return


def history(folder: str, hours: float = 48, now: Optional[float] = None) -> list:
    """The records of the last `hours`: [[epoch ms, °C, % RH], ...], oldest first."""
    now = time.time() if now is None else now
    since = now - hours * 3600
    out = []
    for path in sorted({log_path(folder, since), log_path(folder, now)}):
        out += [[at * 1000, t, rh] for at, t, rh in _rows(path) if since <= at <= now]
    return sorted(out)


def last_saved(folder: str, now: Optional[float] = None) -> Optional[float]:
    """Time of the newest record kept (this month or last), None if there's none."""
    now = time.time() if now is None else now
    for path in (log_path(folder, now), log_path(folder, now - 32 * 86400)):
        ats = [at for at, _, _ in _rows(path)]
        if ats:
            return max(ats)
    return None


def store(folder: str, records: list, after: Optional[float]) -> int:
    """Append the downloaded records newer than `after`, oldest first. Returns how many."""
    new = sorted(r for r in records if r[0] >= CLOCK_OK and (after is None or r[0] > after))
    for at, t, rh in new:
        log_reading(folder, at, t, rh)
    return len(new)


# ------------------------------------------------------------------------------------- decoding
def parse_bthome(data: bytes) -> Optional[dict]:
    """BTHome v2 service data -> {temp_c, rh_pct, batt_pct, volt} (what's present); None if
    encrypted, malformed or without a temperature."""
    if not data or data[0] & 0x01 or (data[0] >> 5) != 2:          # encrypted, or not v2
        return None
    sizes = {0x00: 1, 0x01: 1, 0x02: 2, 0x03: 2, 0x0C: 2, 0x10: 1, 0x11: 1, 0x2E: 1, 0x45: 2}
    out, i = {}, 1
    while i < len(data):
        oid = data[i]
        n = sizes.get(oid)
        if n is None or i + 1 + n > len(data):
            break                                                  # an object we don't know: stop
        v = data[i + 1:i + 1 + n]
        if oid == 0x02:
            out["temp_c"] = int.from_bytes(v, "little", signed=True) / 100
        elif oid == 0x45:
            out["temp_c"] = int.from_bytes(v, "little", signed=True) / 10
        elif oid == 0x03:
            out["rh_pct"] = int.from_bytes(v, "little") / 100
        elif oid == 0x2E:
            out["rh_pct"] = float(v[0])
        elif oid == 0x01:
            out["batt_pct"] = v[0]
        elif oid == 0x0C:
            out["volt"] = int.from_bytes(v, "little") / 1000
        i += 1 + n
    if "temp_c" not in out or not -20 <= out["temp_c"] <= 60:
        return None
    return out


def parse_memo(b: bytes) -> Optional[list]:
    """One history notification -> [utc, °C, % RH]; None for the end marker."""
    if len(b) < 13 or b[0] != 0x35:
        return None
    at = int.from_bytes(b[3:7], "little")
    return [at, int.from_bytes(b[7:9], "little", signed=True) / 100, int.from_bytes(b[9:11], "little") / 100]


# ------------------------------------------------------------------------------------- the thread
class RoomSensor(threading.Thread):
    def __init__(self, log: Callable[[str], None] = print, address: Optional[str] = None,
                 folder: Optional[str] = None) -> None:
        super().__init__(daemon=True, name="room")
        self.log, self.address, self.folder = log, address, folder
        self.every_s: Optional[float] = None      # None: don't read; else seconds between live readings
        self.sync_ok = False                      # may connect for the history now (not on a ride)
        self.latest: Optional[dict] = None        # {temp_c, rh_pct, volt, at (time.time())}
        self.error: Optional[str] = None
        self.synced_at: Optional[float] = None    # last history download
        self._stop = threading.Event()
        self._poke = threading.Event()
        self._fails = 0
        self._sync_fails = 0

    def want(self, every_s: Optional[float], sync_ok: bool = True) -> None:
        """How often to read (None = stop) and whether a history download may happen. A shorter
        interval than before reads now."""
        now_sooner = every_s is not None and (self.every_s is None or every_s < self.every_s)
        self.every_s, self.sync_ok = every_s, sync_ok
        if now_sooner:
            self._poke.set()

    def view(self) -> Optional[dict]:
        r = self.latest
        if r is None:
            return {"state": "failed" if self.error else "searching", "note": self.error} if self.every_s else None
        age = time.time() - r["at"]
        return {"state": "ok" if age < FRESH_S else "old", "temp_c": r["temp_c"], "rh_pct": r.get("rh_pct"),
                "age_s": round(age), "volt": r.get("volt"), "batt_pct": r.get("batt_pct")}

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
        last, last_sync_try, started = 0.0, 0.0, time.time()
        while not self._stop.is_set():
            every = self.every_s
            if every is not None and time.time() - last >= every:
                try:                                         # nothing in Bluetooth may hang this loop
                    ok = await asyncio.wait_for(self._read_once(), 30)
                except asyncio.TimeoutError:
                    self.error, ok = "no answer", False
                last = time.time()
                if not ok:                                   # try again sooner, then back off
                    self._fails += 1
                    last -= every - min(every, 30 * self._fails)
            due = self.synced_at is None or time.time() - self.synced_at >= SYNC_S
            retry = time.time() - last_sync_try >= min(SYNC_S, 30 * 2 ** self._sync_fails)   # 30 s, 1, 2, 4, 8 min
            settled = time.time() - started >= 20                    # not while the app's start-up scans run
            if (every is not None and self.sync_ok and self.address and self.folder and due and retry and settled):
                last_sync_try = time.time()
                try:
                    await asyncio.wait_for(self._sync(), 240)
                    self._sync_fails = 0
                except Exception as e:
                    self._sync_fails = min(self._sync_fails + 1, 4)
                    if self._sync_fails in (1, 4):
                        self.log(f"room sensor: history download failed: {type(e).__name__}: {e}")
            await asyncio.get_running_loop().run_in_executor(None, self._poke.wait, 5)
            self._poke.clear()

    async def _read_once(self) -> bool:
        """Listen for one broadcast (it comes every 5 s); remember the thermometer the first time."""
        from bleak import BleakScanner
        got: asyncio.Future = asyncio.get_running_loop().create_future()

        def seen(d, adv) -> None:
            sd = (adv.service_data or {}).get(BTHOME)
            if sd is None or got.done():
                return
            if self.address:
                if d.address.upper() != self.address.upper():
                    return
            elif not (d.name or adv.local_name or "").startswith(NAMES):
                return
            r = parse_bthome(bytes(sd))
            if r is not None and "rh_pct" in r:
                got.set_result((d, r))
        try:
            async with BleakScanner(seen):
                d, r = await asyncio.wait_for(got, 20)
        except asyncio.TimeoutError:
            self.error = "thermometer not found"
            return False
        except Exception as e:
            self.error = type(e).__name__
            if self._fails in (0, 3):
                self.log(f"room sensor: {type(e).__name__}: {e}")
            return False
        if not self.address:
            self.address = d.address
            self.log(f"room sensor: found {d.name} ({d.address})")
        self.latest = {**r, "at": time.time()}
        self.error, self._fails = None, 0
        return True

    async def _sync(self) -> None:
        """Connect, set the thermometer's clock if it drifted, download the records we don't have."""
        from bleak import BleakClient, BleakScanner
        after = last_saved(self.folder)
        now = time.time()
        want = FIRST_SYNC if after is None else min(FIRST_SYNC * 3, math.ceil((now - after) / MEMO_S) + 3)
        dev = await BleakScanner.find_device_by_address(self.address, timeout=20)
        if dev is None:
            raise RuntimeError("not in range")
        q: asyncio.Queue = asyncio.Queue()
        async with BleakClient(dev, timeout=20) as c:
            await c.start_notify(CMD_UUID, lambda _h, b: q.put_nowait(bytes(b)))

            async def ask(cmd: bytes, kind: int, wait: float = 4) -> bytes:
                for _ in range(3):                           # right after connecting it may not answer yet
                    await c.write_gatt_char(CMD_UUID, cmd, response=True)
                    end = time.time() + wait
                    try:
                        while True:
                            b = await asyncio.wait_for(q.get(), max(0.1, end - time.time()))
                            if b and b[0] == kind:
                                return b
                    except asyncio.TimeoutError:
                        continue
                raise TimeoutError(f"no answer to command {cmd[0]:#04x}")

            await asyncio.sleep(1.5)                         # let it settle (it sends its status first)

            clock = await ask(b"\x23", 0x23)
            dev_now = int.from_bytes(clock[1:5], "little")
            if abs(dev_now - time.time()) > 20:
                await ask(b"\x23" + int(time.time()).to_bytes(4, "little"), 0x23)
                self.log(f"room sensor: clock set (was {dev_now - time.time():+.0f} s off)")
            records = []
            await c.write_gatt_char(CMD_UUID, bytes([0x35, want & 0xFF, want >> 8]), response=True)
            while True:                                      # newest first; stop at what we have
                b = await asyncio.wait_for(q.get(), 5)
                if not b or b[0] != 0x35:
                    continue
                r = parse_memo(b)
                if r is None:
                    break
                if after is not None and r[0] <= after:
                    await c.write_gatt_char(CMD_UUID, b"\x35\x00\x00", response=True)   # enough
                    break
                records.append(r)
        n = store(self.folder, records, after)
        self.synced_at = time.time()
        if n:
            self.log(f"room sensor: {n} history records downloaded")
