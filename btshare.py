"""One Bluetooth radio, two threads: the sensors' worker (assioma_recorder) and the room thermometer
(room) each run their own asyncio loop. BlueZ refuses a second scan or connection attempt while one is
starting ("org.bluez.Error.InProgress"), so both take this lock around scanning and connecting.

    async with btshare.held():
        ...

Waiting polls instead of blocking, so a cancelled task can never leave the lock taken.
"""
import asyncio
import contextlib
import threading

LOCK = threading.Lock()


@contextlib.asynccontextmanager
async def held(poll_s: float = 0.05):
    while not LOCK.acquire(blocking=False):
        await asyncio.sleep(poll_s)
    try:
        yield
    finally:
        LOCK.release()
