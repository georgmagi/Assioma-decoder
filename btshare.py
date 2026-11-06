"""One Bluetooth radio, two threads: the sensors' worker (assioma_recorder) and the room thermometer
(room) each run their own asyncio loop. BlueZ refuses a second scan or connection attempt while one is
starting ("org.bluez.Error.InProgress"), so both take this lock around scanning and connecting.

    async with btshare.held():                 # the worker: scans back to back
    async with btshare.held(priority=True):    # the thermometer: rare, short, goes next

The worker's jobs scan one after another, so a plain lock would almost never be free when the
thermometer asks; a waiting priority holder makes the others hold back until it has had its turn.
Waiting polls instead of blocking, so a cancelled task can never leave the lock taken.
"""
import asyncio
import contextlib
import threading

LOCK = threading.Lock()
_waiting = 0                       # priority holders waiting for LOCK
_count = threading.Lock()


@contextlib.asynccontextmanager
async def held(priority: bool = False, poll_s: float = 0.05):
    global _waiting
    if priority:
        with _count:
            _waiting += 1
    try:
        while True:
            if priority or _waiting == 0:
                if LOCK.acquire(blocking=False):
                    break
            await asyncio.sleep(poll_s)
    finally:
        if priority:
            with _count:
                _waiting -= 1
    try:
        yield
    finally:
        LOCK.release()
