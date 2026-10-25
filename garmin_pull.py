"""
Garmin Connect -> garmin.json: your watch's resting heart rate and sleeping heart rate, pulled by
the server (sync_server.py runs pull_loop) and synced to your devices like the plan or the mornings.

Only the resting-level numbers are kept. The watch's daytime and exercise heart rate (wrist optics:
often far off when you ride) is never stored, and inside the night a reading above SLEEP_MAX is
taken for an artifact and dropped.

garmin.json = {"pulled": iso time, "days": {wake day: {
    "resting_hr":  Garmin's resting HR for the day,
    "sleep_start", "sleep_end": the night ending that morning (UTC, ISO),
    "sleep_hr": {"avg", "min", "low30": the lowest 30-min mean, "n"},
    "samples": [[minutes since falling asleep, bpm], ...]  (about every 2 min) }}}

Logging in (once, by you, on the server; the password is never stored, only Garmin's tokens):
    sudo -u assioma GARMINTOKENS=/var/lib/assioma/garmin /srv/assioma/venv/bin/python \\
        /srv/assioma/app/garmin_pull.py login
Then the server pulls every PULL_EVERY_S on its own. `... garmin_pull.py pull` pulls once now.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Optional

REL = "garmin.json"
SLEEP_MAX = 110          # bpm: above this during the night it's the optics, not you
PULL_EVERY_S = 2 * 3600
DAYS_BACK = 3            # each pull refreshes the last few days (the watch syncs late)
LOGIN_CHECK_S = 60
# mornings: the watch reaches Garmin Connect when you pick up the phone, so from 06:00 to 11:00 (your
# time) the server looks every 20 min until that morning's night is in, then back to every 2 h
MORNING = (6, 11)
MORNING_EVERY_S = 20 * 60
LOCAL_TZ = "Europe/Tallinn"


def next_wait(every: float, have: dict, now: Optional[datetime] = None) -> float:
    """Seconds to the next pull: `every`, or MORNING_EVERY_S in the morning window while today's
    night (sleep heart rate) isn't in yet; never past the window's start (so 06:00 isn't missed)."""
    from zoneinfo import ZoneInfo
    now = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(LOCAL_TZ))
    today = now.date().isoformat()
    got = bool((have.get(today) or {}).get("sleep_hr"))
    if MORNING[0] <= now.hour < MORNING[1] and not got:
        return min(every, MORNING_EVERY_S)
    start = now.replace(hour=MORNING[0], minute=0, second=0, microsecond=0)
    if start <= now:
        start += timedelta(days=1)
    return max(60.0, min(every, (start - now).total_seconds() + 30))       # before the login: look for the tokens this often
FIRST_DAYS = 14          # the first pull fills two weeks
TOKENS = os.environ.get("GARMINTOKENS", "/var/lib/assioma/garmin")


def _iso(ms) -> Optional[str]:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec="seconds") if ms else None


def _night_samples(sleep: dict, hr_days: list) -> tuple:
    """(start_ms, end_ms, [(ms, bpm)]) of the night: the sleep's own HR series, else the day's
    2-min HR values inside the sleep window."""
    dto = (sleep or {}).get("dailySleepDTO") or {}
    t0, t1 = dto.get("sleepStartTimestampGMT"), dto.get("sleepEndTimestampGMT")
    if not t0 or not t1:
        return None, None, []
    pts = [(p.get("startGMT"), p.get("value")) for p in (sleep.get("sleepHeartRate") or []) if isinstance(p, dict)]
    if not pts:
        pts = [(v[0], v[1]) for d in hr_days for v in ((d or {}).get("heartRateValues") or [])
               if isinstance(v, (list, tuple)) and len(v) >= 2]
    pts = sorted({(int(t), float(b)) for t, b in pts if t and b and t0 <= t <= t1 and 25 <= b <= SLEEP_MAX})
    return t0, t1, pts


def day_record(sleep: dict, hr_today: dict, hr_before: Optional[dict] = None) -> Optional[dict]:
    """One wake day from Garmin's sleep and heart-rate answers; None when there's nothing yet."""
    rhr = (hr_today or {}).get("restingHeartRate") or (sleep or {}).get("restingHeartRate")
    t0, t1, pts = _night_samples(sleep, [hr_before, hr_today])
    rec = {"resting_hr": rhr}
    if pts:
        bpm = [b for _, b in pts]
        low30 = None
        j = 0
        for i in range(len(pts)):                      # lowest mean over any 30 min of the night
            while pts[i][0] - pts[j][0] > 30 * 60000:
                j += 1
            if pts[i][0] - pts[j][0] >= 25 * 60000:
                m = sum(b for _, b in pts[j:i + 1]) / (i + 1 - j)
                low30 = m if low30 is None else min(low30, m)
        rec.update({"sleep_start": _iso(t0), "sleep_end": _iso(t1),
                    "sleep_hr": {"avg": round(sum(bpm) / len(bpm), 1), "min": min(bpm),
                                 "low30": round(low30, 1) if low30 is not None else None, "n": len(bpm)},
                    "samples": [[round((t - t0) / 60000), b] for t, b in pts]})
    elif t0:
        rec.update({"sleep_start": _iso(t0), "sleep_end": _iso(t1)})
    return rec if rhr or pts else None


def pull(client, known: dict, today: Optional[date] = None, log: Callable = print) -> dict:
    """known: garmin.json as it is. Returns it with the last days pulled again (all of them the
    first time). client: a logged-in garminconnect.Garmin (or anything with its two calls)."""
    today = today or date.today()
    days = dict((known or {}).get("days") or {})
    back = DAYS_BACK if days else FIRST_DAYS
    hr_cache: dict = {}

    def hr(d: date) -> dict:
        if d not in hr_cache:
            hr_cache[d] = client.get_heart_rates(d.isoformat()) or {}
        return hr_cache[d]

    for k in range(back, -1, -1):
        d = today - timedelta(days=k)
        try:
            rec = day_record(client.get_sleep_data(d.isoformat()) or {}, hr(d), hr(d - timedelta(days=1)))
        except Exception as e:                         # one bad day doesn't stop the rest
            log(f"garmin: {d}: {type(e).__name__}: {e}")
            continue
        if rec:
            days[d.isoformat()] = rec
    return {"pulled": datetime.now(timezone.utc).isoformat(timespec="seconds"), "days": dict(sorted(days.items()))}


def client_from_tokens(tokens: str = TOKENS):
    """A Garmin client from the saved tokens (refreshed on their own), or None before the login."""
    if not os.path.isdir(tokens) or not os.listdir(tokens):
        return None
    from garminconnect import Garmin
    g = Garmin()
    g.login(tokens)
    return g


def pull_loop(read: Callable[[], Optional[bytes]], write: Callable[[bytes], None], stop: threading.Event,
              log: Callable = print, every: float = PULL_EVERY_S, tokens: str = TOKENS) -> None:
    """The server's thread: pull, write garmin.json when it changed, wait, again. Quiet (one line)
    until you've logged in; a failed pull waits for the next round."""
    said = None
    while not stop.is_set():
        g, days = None, {}
        try:
            g = client_from_tokens(tokens)
            if g is None:
                msg = "garmin: not logged in (garmin_pull.py login)"
            else:
                old = read()
                new = pull(g, json.loads(old) if old else {}, log=log)
                days = new["days"]
                body = json.dumps(new, indent=1).encode()
                if old is None or json.loads(old).get("days") != new["days"]:
                    write(body)
                    msg = f"garmin: {len(new['days'])} days, latest {max(new['days'], default='-')}"
                else:
                    msg = "garmin: nothing new"
        except Exception as e:
            msg = f"garmin: {type(e).__name__}: {e}"
        if msg != said:
            log(msg)
            said = msg
        stop.wait(next_wait(every, days) if g is not None else min(every, LOGIN_CHECK_S))   # logged in a minute ago: pull now


def _login() -> None:
    import getpass
    from garminconnect import Garmin
    os.makedirs(TOKENS, mode=0o700, exist_ok=True)
    email = input("Garmin email: ").strip()
    password = getpass.getpass("Garmin password (not shown, not saved): ")
    g = Garmin(email, password, prompt_mfa=lambda: input("Garmin code (MFA): ").strip())
    g.login(TOKENS)
    try:
        g.client.dump(TOKENS)                        # make sure the tokens are on disk
    except Exception:
        pass
    for f in os.listdir(TOKENS):
        os.chmod(os.path.join(TOKENS, f), 0o600)
    print(f"Logged in. Tokens in {TOKENS}; the server pulls every {PULL_EVERY_S // 3600} h.")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "login":
        _login()
    elif cmd == "pull":
        g = client_from_tokens()
        if g is None:
            sys.exit("Not logged in: run  garmin_pull.py login  first.")
        out = pull(g, {})
        print(json.dumps({d: {k: v for k, v in r.items() if k != "samples"} for d, r in out["days"].items()}, indent=1))
    else:
        sys.exit(__doc__)
