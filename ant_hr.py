"""
Heart rate and RR intervals over ANT+ (USB stick, e.g. ANTUSB2 / ANTUSB-m clones), lossless.

Over Bluetooth the strap's RR intervals arrive in notifications that can be lost or repeated.
ANT+ is better: every message (~4 a second) carries the beat count and the time of the last beat
(1/1024 s), and data page 4 also the time of the beat before. So a beat's interval is exact as long
as one message in two gets through; RR intervals are rebuilt from those times (Beats), never from
a guess about repeats.

The stick talks a small serial protocol over USB bulk endpoints; this file speaks just enough of it
to open one receive-only channel (channel type 0x40: the stick never transmits, so nothing is ever
sent to the strap). The channel pairs with the first heart-rate strap it hears (device number 0 =
any) and stays with it.

Each beat goes to the recorder as a notification shaped exactly like Bluetooth's Heart Rate
Measurement (0x2A37) but with its own char name, ANT_HR_CHAR: the decoder parses it the same way and
skips the Bluetooth repeat filter, and the raw log, --rebuild and replays work unchanged.

Needs pyusb and libusb (pip install pyusb libusb) and the stick on the WinUSB driver (Zadig).
"""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timezone
from typing import Optional

import assioma_decoder as dec

ANT_HR_CHAR = dec.ANT_HR_CHAR
STICKS = ((0x0FCF, 0x1008), (0x0FCF, 0x1009))           # ANTUSB2, ANTUSB-m
ANTPLUS_KEY = [0xB9, 0xA5, 0x21, 0xFB, 0xBD, 0x72, 0xC3, 0x45]
HR_TYPE, HR_PERIOD, ANTPLUS_FREQ = 120, 8070, 57         # device type, 32768/8070 = 4.06 Hz, 2457 MHz

# message ids and channel events (ANT Message Protocol and Usage, section 9)
SYNC = 0xA4
RESET, NETWORK_KEY, ASSIGN, CHANNEL_ID, PERIOD, SEARCH_TIMEOUT, RF_FREQ = 0x4A, 0x46, 0x42, 0x51, 0x43, 0x44, 0x45
OPEN, CLOSE, EXT_RX, LIB_CONFIG = 0x4B, 0x4C, 0x66, 0x6E
BROADCAST, CHANNEL_EVENT = 0x4E, 0x40
EV_SEARCH_TIMEOUT, EV_RX_FAIL, EV_CLOSED, EV_GO_TO_SEARCH = 0x01, 0x02, 0x07, 0x08
RECEIVE_ONLY = 0x40
# ANT+ manufacturer ids (HR data page 2), to tell the straps apart: a Garmin strap's beat times are
# smoothed in exercise (ride_summary.smoothed_rr), a Polar H10's are not
MANUFACTURERS = {1: "Garmin", 32: "Wahoo", 123: "Polar", 263: "Coospo"}
MAX_LEN = 41                                             # longest message data (ANT spec)


def message(msg_id: int, data: list) -> bytes:
    body = [SYNC, len(data), msg_id, *data]
    x = 0
    for b in body:
        x ^= b
    return bytes(body + [x])


def split_messages(buf: bytearray) -> list:
    """Whole messages (id, data) off the front of buf; a partial one stays in buf."""
    out = []
    while True:
        i = buf.find(bytes([SYNC]))
        if i < 0:
            buf.clear()
            return out
        del buf[:i]
        if len(buf) >= 2 and buf[1] > MAX_LEN:      # no message is that long: not a real start
            del buf[:1]
            continue
        if len(buf) < 4 or len(buf) < buf[1] + 4:
            return out
        n = buf[1]
        packet = bytes(buf[: n + 4])
        x = 0
        for b in packet[:-1]:
            x ^= b
        if x != packet[-1]:                       # not a real start: skip this sync byte
            del buf[:1]
            continue
        del buf[: n + 4]
        out.append((packet[2], packet[3: 3 + n]))


# --------------------------------------------------------------------------- #
# Beats: RR intervals from the HR pages
# --------------------------------------------------------------------------- #
class Beats:
    """Feed every HR data page (8 bytes); get the new beats' RR intervals in ms, exact.

    Byte 6 counts beats (mod 256), bytes 4-5 are the last beat's time and, on page 4, bytes 2-3 the
    previous beat's time (1/1024 s, mod 65536). A step of one beat gives one interval; a step of two
    (one message lost) still gives both when it is a page 4. A bigger step, or one that doesn't make
    sense (stale data in the stick after a restart, another strap), starts over: those beats are
    simply not reported, so the RR series never holds a made-up interval."""
    MIN_RR, MAX_RR = 240, 2500                   # ms: 25-250 bpm

    def __init__(self) -> None:
        self.count: Optional[int] = None
        self.time: Optional[int] = None
        self.seen = self.lost = 0                # beats reported, beats that couldn't be

    def feed(self, page: bytes) -> list:
        p = page[0] & 0x7F                       # bit 7 toggles every 4 messages
        c, t = page[6], page[4] | page[5] << 8
        prev = (page[2] | page[3] << 8) if p == 4 else None
        last_c, last_t = self.count, self.time
        self.count, self.time = c, t
        if last_c is None:
            return []
        step = (c - last_c) % 256
        if step == 0:
            if t != last_t:                      # same count, other time: not the same strap / restart
                self.count = None
            return []
        if step == 1:
            rr = [(t - last_t) % 65536]
        elif step == 2 and prev is not None:
            rr = [(prev - last_t) % 65536, (t - prev) % 65536]
        else:
            self.lost += step
            return []
        ms = [round(x * 1000 / 1024, 1) for x in rr]
        if not all(self.MIN_RR <= x <= self.MAX_RR for x in ms):
            self.lost += step
            return []
        self.seen += len(ms)
        return ms


def hr_notification(t: datetime, bpm: int, rr_ms: list) -> dec.Notification:
    """Bluetooth Heart Rate Measurement bytes: flags (0x10 = RR present), 8-bit HR, RR in 1/1024 s."""
    data = bytearray([0x10 if rr_ms else 0x00, min(255, bpm)])
    for x in rr_ms:
        data += round(x * 1024 / 1000).to_bytes(2, "little")
    return dec.Notification(t, ANT_HR_CHAR, bytes(data))


# --------------------------------------------------------------------------- #
# The stick
# --------------------------------------------------------------------------- #
def libusb_dll() -> Optional[str]:
    """The libusb DLL that the libusb package ships (found without importing the package, which
    doesn't work inside the .exe), or None: then pyusb looks on PATH."""
    import importlib.util
    import struct
    import sys
    arch = "x86_64" if struct.calcsize("P") == 8 else "x86"
    roots = [getattr(sys, "_MEIPASS", None)]
    try:
        spec = importlib.util.find_spec("libusb")
        roots += [os.path.dirname(p) for p in (spec.submodule_search_locations or [])] if spec else []
    except (ImportError, ValueError):
        pass
    for root in filter(None, roots):
        path = os.path.join(root, "libusb", "_platform", "windows", arch, "libusb-1.0.dll")
        if os.path.exists(path):
            return path
    return None


def _usb_backend():
    """pyusb's libusb backend, with the libusb package's DLL on Windows."""
    import usb.backend.libusb1 as lb
    dll = libusb_dll()
    backend = lb.get_backend(find_library=(lambda _n: dll) if dll else None)
    if backend is None:
        raise RuntimeError(f"libusb not found (DLL: {dll})")
    return backend


class AntHrWorker(threading.Thread):
    """Keeps an ANT+ heart-rate channel open on the stick and puts one notification per beat (and
    one a second between beats, for the HR number) on the recorder's queue. Unplugging and plugging
    the stick back in is fine: it is looked for every few seconds."""
    QUIET_S = 5.0                 # no HR message for this long: the strap is gone, search again

    def __init__(self, q) -> None:
        super().__init__(daemon=True)
        self.q = q
        self.status = "looking for the ANT+ stick…"
        self.stick = False                        # the stick is open
        self.opened = 0.0                         # monotonic time the channel was opened
        self.last_data = 0.0                      # monotonic time of the last HR message
        self.device: Optional[int] = None         # the strap's ANT+ device number
        self.enabled = True                       # the strap is wanted (Connections pane): else nothing is sent
        self.maker: Optional[str] = None          # from data page 2, e.g. "Garmin", "Polar"
        # your strap's device number: the channel pairs with it only, never a neighbour's strap
        # (7 Oct: the Bluetooth side connected to an Amazfit and a Galaxy Fit nearby). None = any
        self.device_lock: Optional[int] = None
        self._opened_lock: Optional[int] = None
        self.log = lambda text: None
        self._halt = threading.Event()
        self._dev = None

    def stop(self) -> None:
        self._halt.set()

    def receiving(self, within: float = QUIET_S) -> bool:
        return time.monotonic() - self.last_data < within

    def run(self) -> None:
        try:
            import usb.core  # noqa: F401
        except ImportError:
            self.status = "pyusb is not installed (pip install pyusb libusb)"
            return
        reported = None
        while not self._halt.is_set():
            if not self.enabled:                  # the strap isn't wanted (asleep, or switched off): stick closed
                self.status = ""
                self._halt.wait(0.5)
                continue
            try:
                why = self._session()
            except Exception as e:                # unplugged, driver missing, ...
                why = f"{type(e).__name__}: {e}"
            self.stick = False
            self.device = None
            if why and why != reported:
                self.log(f"ANT+: {why}")
                reported = why
            self._close()
            self._halt.wait(3.0)

    # ---- one stick session: open, pair, read until something goes wrong
    def _session(self) -> Optional[str]:
        import usb.core
        backend = _usb_backend()
        dev = None
        for vid, pid in STICKS:
            dev = usb.core.find(idVendor=vid, idProduct=pid, backend=backend)
            if dev is not None:
                break
        if dev is None:
            self.status = "no ANT+ stick"
            return "no stick"
        try:                                      # Linux: usb_serial_simple claims the stick as a tty; take it back
            if dev.is_kernel_driver_active(0):
                dev.detach_kernel_driver(0)
        except (NotImplementedError, usb.core.USBError):
            pass                                  # Windows/macOS: no kernel driver to detach
        try:
            dev.set_configuration()
        except usb.core.USBError:
            pass                                  # already configured
        self._dev = dev
        cfg = dev.get_active_configuration()
        intf = cfg[(0, 0)]
        self._out = usb.util.find_descriptor(intf, custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_OUT)
        self._in = usb.util.find_descriptor(intf, custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_IN)
        buf = bytearray()
        self._send(RESET, [0])
        time.sleep(0.6)
        self._drain()                             # whatever the stick still held from before
        for msg_id, data in ((NETWORK_KEY, [0, *ANTPLUS_KEY]), (ASSIGN, [0, RECEIVE_ONLY, 0]),
                             (CHANNEL_ID, [0, (self.device_lock or 0) & 0xFF, (self.device_lock or 0) >> 8, HR_TYPE, 0]),
                             (PERIOD, [0, HR_PERIOD & 0xFF, HR_PERIOD >> 8]),
                             (RF_FREQ, [0, ANTPLUS_FREQ]), (SEARCH_TIMEOUT, [0, 0xFF]),
                             (LIB_CONFIG, [0, 0x80])):            # extended data: the strap's device number
            self._send(msg_id, data)
            time.sleep(0.05)
        self._send(OPEN, [0])
        self._opened_lock = self.device_lock
        self.stick, self.opened = True, time.monotonic()
        self.status = "searching for the strap (ANT+)…"
        self.log("ANT+: stick open, searching")
        beats, bpm, last_sent = Beats(), 0, 0.0
        while not self._halt.is_set():
            if not self.enabled:
                return None                       # let go: closed until it's wanted again
            if self.device_lock != self._opened_lock:
                return "pairing again (strap lock changed)"
            try:
                buf += bytes(self._in.read(64, timeout=500))
            except usb.core.USBTimeoutError:
                pass
            for msg_id, data in split_messages(buf):
                if msg_id == BROADCAST and len(data) >= 9 and data[0] == 0:
                    page = data[1:9]
                    if len(data) >= 14 and data[9] & 0x80:
                        dev_no = data[10] | data[11] << 8
                        if self.device_lock and dev_no != self.device_lock:
                            continue                       # not your strap
                        if dev_no != self.device:
                            self.device = dev_no
                            self.log(f"ANT+: strap #{dev_no}")
                    self.last_data = now = time.monotonic()
                    if page[0] & 0x7F == 2:                # manufacturer page: id, serial
                        maker = MANUFACTURERS.get(page[1], f"maker {page[1]}")
                        if maker != self.maker:
                            self.maker = maker
                            self.log(f"ANT+: strap made by {maker}")
                    who = f"{self.maker} strap" if self.maker else "strap"
                    self.status = f"connected: {who} #{self.device}" if self.device else f"connected: ANT+ {who}"
                    bpm = page[7] or bpm
                    rr = beats.feed(page)
                    if self.enabled and (rr or now - last_sent >= 1.0):
                        self.q.put(hr_notification(datetime.now(timezone.utc).replace(tzinfo=None), bpm, rr))
                        last_sent = now
                elif msg_id == CHANNEL_EVENT and len(data) >= 3 and data[1] == 1:
                    if data[2] == EV_CLOSED:      # the stick gave up (it shouldn't with timeout 0xFF)
                        return "channel closed"
                    if data[2] in (EV_GO_TO_SEARCH, EV_SEARCH_TIMEOUT):
                        self.status = "searching for the strap (ANT+)…"
            if self.last_data and not self.receiving() and self.status.startswith("connected"):
                self.status = "strap lost, searching (ANT+)…"
                self.log(f"ANT+: strap quiet ({beats.seen} beats, {beats.lost} not recoverable)")
        return None

    def _send(self, msg_id: int, data: list) -> None:
        self._out.write(message(msg_id, data), timeout=1000)

    def _drain(self) -> None:
        import usb.core
        for _ in range(50):
            try:
                self._in.read(64, timeout=50)
            except usb.core.USBTimeoutError:
                return

    def _close(self) -> None:
        dev, self._dev = self._dev, None
        if dev is None:
            return
        try:
            self._send(CLOSE, [0])
        except Exception:
            pass
        try:
            import usb.util
            usb.util.dispose_resources(dev)
        except Exception:
            pass
