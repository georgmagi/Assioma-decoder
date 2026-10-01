"""
Structured workouts: steps in % of FTP, built in or imported from Zwift .zwo files, and the player
that turns ride time into a target power for the trainer (ftms.py).

A step is {dur, lo, hi, label}: power goes from lo to hi (fractions of FTP; equal for a steady
step, different for a ramp, None for free riding). The player runs on recorded ride time, so a
pause freezes the workout; skipping moves on to the next step; the intensity scales every target
(like the -/+ on a head unit).
"""
from __future__ import annotations

import glob
import os
import xml.etree.ElementTree as ET


def _step(dur, lo, hi=None, label=""):
    return {"dur": int(dur), "lo": lo, "hi": lo if hi is None else hi, "label": label}


def _repeat(n, on_dur, on, off_dur, off):
    out = []
    for k in range(n):
        out += [_step(on_dur, on, label=f"{k + 1}/{n}"), _step(off_dur, off, label="recover")]
    return out


# Built in. The threshold steps give vt1.py what it needs for both HRV thresholds in watts: steady
# 5-min blocks climbing through the top of Z2 (alpha 1 crosses 0.75: VT1) to FTP (0.5: VT2, near
# FTP), without going all-out. Best with the ANT+ strap (every beat).
BUILT_IN = {
    "endurance-60": {"name": "Endurance 60", "description": "Steady Z2 for an hour.",
                     "steps": [_step(600, 0.50, 0.65, "warm up"), _step(2700, 0.68, label="Z2"), _step(300, 0.65, 0.45, "cool down")]},
    "vt1-steps": {"name": "Threshold steps", "description": "The weekly check: 5-min steps from 60 to 80 % FTP, 3-min steps on to 100 %, then 3 min easy. Heart rate, α1 and recovery at the same watts each week, plus VT1 and VT2 (near FTP) from α1, with no all-out effort. Set the room temperature first.",
                  # 5 min up to 80 % (heart rate settles: the check), 3 min above (enough for alpha 1 to find VT2)
                  "steps": [_step(600, 0.45, 0.60, "warm up")] + [_step(300 if p <= 80 else 180, p / 100, label=f"{p}%") for p in range(60, 101, 5)]
                  + [_step(180, 0.40, label="recovery"), _step(420, 0.50, 0.40, "cool down")]},
    # a ramp test: 1-min steps of +10 % FTP (about +20 W a minute at 200 W, like the usual protocol)
    # until you can't hold one: stop pedalling and the trainer goes flat (recorder_app). eftp.py
    # recognises it by its name and takes 75 % of the best minute.
    "ramp-test": {"name": "Ramp test", "description": "1-min steps from 50 % FTP, +10 % each minute, until you can't hold it: then stop pedalling (the trainer goes flat) and spin easy. eFTP = 75 % of your best minute.",
                  "steps": [_step(300, 0.40, 0.50, "warm up")] + [_step(60, p / 100, label=f"{p}%") for p in range(50, 181, 10)]
                  + [_step(600, 0.40, label="cool down")]},
    "sweet-spot-3x12": {"name": "Sweet spot 3×12", "description": "3 × 12 min at 90 % with 4 min easy.",
                        "steps": [_step(600, 0.50, 0.70, "warm up")] + _repeat(3, 720, 0.90, 240, 0.55)[:-1]
                        + [_step(600, 0.60, 0.45, "cool down")]},
    "vo2-5x4": {"name": "VO2max 5×4", "description": "5 × 4 min at 115 % with 4 min easy.",
                "steps": [_step(900, 0.50, 0.75, "warm up")] + _repeat(5, 240, 1.15, 240, 0.50)[:-1]
                + [_step(600, 0.60, 0.45, "cool down")]},
}


def _f(el, *names, default=None):
    for n in names:
        for k, v in el.attrib.items():
            if k.lower() == n.lower():
                try:
                    return float(v)
                except ValueError:
                    pass
    return default


def parse_zwo(text: str) -> dict:
    """A Zwift workout file: SteadyState, Warmup / Cooldown / Ramp, IntervalsT, FreeRide.
    Powers are fractions of FTP. Raises ValueError for anything that isn't a usable workout."""
    root = ET.fromstring(text)
    if root.tag.lower() != "workout_file":
        raise ValueError("not a Zwift workout file")
    name = (root.findtext("name") or "").strip()
    desc = (root.findtext("description") or "").strip()
    w = root.find("workout")
    if w is None:
        raise ValueError("no <workout>")
    steps = []
    for el in w:
        tag, dur = el.tag.lower(), _f(el, "Duration", default=0)
        if tag == "steadystate":
            steps.append(_step(dur, _f(el, "Power", "PowerLow")))
        elif tag in ("warmup", "cooldown", "ramp"):
            lo, hi = _f(el, "PowerLow", "Power"), _f(el, "PowerHigh", "Power")
            steps.append(_step(dur, lo, hi, {"warmup": "warm up", "cooldown": "cool down"}.get(tag, "")))
        elif tag == "intervalst":
            n = int(_f(el, "Repeat", default=1))
            steps += _repeat(n, _f(el, "OnDuration", default=0), _f(el, "OnPower", "PowerOnLow"),
                             _f(el, "OffDuration", default=0), _f(el, "OffPower", "PowerOffLow"))
        elif tag in ("freeride", "maxeffort"):
            steps.append({"dur": int(dur), "lo": None, "hi": None, "label": "free ride"})
    steps = [s for s in steps if s["dur"] > 0]
    for s in steps:
        for k in ("lo", "hi"):
            if s[k] is not None and not 0.2 <= s[k] <= 3.0:     # watts instead of fractions, or garbage
                raise ValueError(f"power {s[k]} is not a fraction of FTP")
    if not steps:
        raise ValueError("the workout has no steps")
    return {"name": name or "Workout", "description": desc, "steps": steps}


def library(folder: str) -> dict:
    """id -> workout: the built-in ones and every .zwo in folder (files that don't parse are left out)."""
    out = dict(BUILT_IN)
    for path in sorted(glob.glob(os.path.join(folder, "*.zwo"))):
        try:
            with open(path, encoding="utf-8") as fh:
                w = parse_zwo(fh.read())
        except (OSError, ValueError, ET.ParseError):
            continue
        out["zwo:" + os.path.basename(path)[:-4]] = {**w, "file": True}
    return out


def total_s(w: dict) -> int:
    return sum(s["dur"] for s in w["steps"])


class Player:
    """Where a workout is at a given ride time. Pure: the engine asks, nothing runs by itself."""

    def __init__(self, workout: dict, ftp: float) -> None:
        self.w, self.ftp = workout, float(ftp)
        self.intensity = 1.0                      # scales every target (-/+ buttons)
        self.shift = 0.0                          # seconds skipped
        self.starts = []
        t = 0
        for s in workout["steps"]:
            self.starts.append(t)
            t += s["dur"]
        self.total = t

    def at(self, ride_s: float) -> dict:
        """{index, step, in_step, left, target_w (None: free ride), next, done}."""
        t = ride_s + self.shift
        if t >= self.total:
            return {"done": True, "index": len(self.w["steps"]), "target_w": None, "t": t}
        i = max(k for k, st in enumerate(self.starts) if st <= t)
        s = self.w["steps"][i]
        into = t - self.starts[i]
        target = None
        if s["lo"] is not None:
            frac = s["lo"] + (s["hi"] - s["lo"]) * (into / s["dur"])
            target = frac * self.ftp * self.intensity
        nxt = self.w["steps"][i + 1] if i + 1 < len(self.w["steps"]) else None
        return {"done": False, "index": i, "step": s, "in_step": into, "left": s["dur"] - into,
                "target_w": target, "next": nxt, "t": t}

    def skip(self, ride_s: float) -> None:
        """On to the next step."""
        now = self.at(ride_s)
        if not now["done"]:
            self.shift += now["left"]

    def nudge(self, pct: int) -> None:
        self.intensity = max(0.5, min(1.5, round(self.intensity + pct / 100, 2)))


FREE_RIDE_FRAC = 0.5          # for the estimate only: a free-ride step counted as easy riding


def targets(w: dict) -> list:
    """The target as a fraction of FTP, second by second (free ride: FREE_RIDE_FRAC)."""
    out = []
    for s in w["steps"]:
        for k in range(s["dur"]):
            out.append(FREE_RIDE_FRAC if s["lo"] is None else s["lo"] + (s["hi"] - s["lo"]) * (k + 0.5) / s["dur"])
    return out


def estimate(w: dict, ftp: float) -> dict:
    """What the workout will be if ridden as written: average and normalized power, IF, TSS, kJ."""
    f = targets(w)
    n = len(f)
    if not n:
        return {}
    roll = [sum(f[i - 29: i + 1]) / 30 for i in range(29, n)] or [sum(f) / n]
    np_frac = (sum(x ** 4 for x in roll) / len(roll)) ** 0.25
    return {"duration_s": n, "avg_w": ftp * sum(f) / n, "np_w": ftp * np_frac, "if": np_frac,
            "tss": n * np_frac ** 2 / 36, "kj": ftp * sum(f) / 1000}


# --------------------------------------------------------------------------- the shorthand
# A workout in one line, the way a coach (or Claude) writes it:
#     10m 50-65%, 3x(12m 90%, 4m 55%), 10m 60-40%
# Steps are separated by commas (or new lines); "N x (...)" repeats a group; a duration is 90s, 12m,
# 1h or 1h30m (also 2m30s); a power is a share of FTP ("90%"), a ramp ("50-65%"), a zone ("Z2",
# the zone's middle) or "free" (free ride). An optional label goes last in quotes: 5m 55% "easy".
import re as _re

ZONE_PCT = {"z1": 50, "z2": 65, "z3": 83, "z4": 98, "z5": 113, "z6": 130}
_DUR = _re.compile(r"^(?:(\d+(?:\.\d+)?)h)?(?:(\d+(?:\.\d+)?)m)?(?:(\d+)s)?$")


def _duration(tok: str) -> int:
    m = _DUR.match(tok.lower())
    if not m or not any(m.groups()):
        raise ValueError(f'"{tok}" is not a duration (like 90s, 12m, 1h30m)')
    h, mi, se = (float(x) if x else 0 for x in m.groups())
    return round(h * 3600 + mi * 60 + se)


def _power(tok: str):
    t = tok.lower().rstrip("%")
    if t in ("free", "freeride", "free-ride"):
        return None, None
    if t in ZONE_PCT:
        return ZONE_PCT[t] / 100, ZONE_PCT[t] / 100
    parts = t.split("-")
    try:
        vals = [float(p) for p in parts]
    except ValueError:
        raise ValueError(f'"{tok}" is not a power (like 90%, 50-65%, Z2 or free)')
    if len(vals) not in (1, 2) or not all(20 <= v <= 300 for v in vals):
        raise ValueError(f'"{tok}": powers are % of FTP, 20-300')
    return vals[0] / 100, vals[-1] / 100


def _split_top(text: str) -> list:
    """Split on commas / new lines that aren't inside brackets."""
    out, depth, cur = [], 0, ""
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                raise ValueError("a ) without its (")
        if ch in ",\n;" and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    if depth:
        raise ValueError("a ( without its )")
    out.append(cur)
    return [x.strip() for x in out if x.strip()]


_REPEAT = _re.compile(r"^(\d+)\s*[x×]\s*\((.*)\)$", _re.S)


def parse_text(text: str) -> list:
    """The shorthand to steps ({dur, lo, hi, label}); ValueError says what's wrong."""
    steps = []
    for item in _split_top(text):
        m = _REPEAT.match(item)
        if m:
            n = int(m.group(1))
            if not 1 <= n <= 50:
                raise ValueError(f"{n} repeats? 1-50")
            inner = parse_text(m.group(2))
            for k in range(n):
                steps += [{**st, "label": st["label"] or f"{k + 1}/{n}"} for st in inner]
            continue
        label = ""
        lm = _re.search(r'"([^"]*)"\s*$', item)
        if lm:
            label, item = lm.group(1), item[: lm.start()].strip()
        toks = item.split()
        if len(toks) != 2:
            raise ValueError(f'"{item}": a step is a duration and a power, like "12m 90%"')
        lo, hi = _power(toks[1])
        steps.append({"dur": _duration(toks[0]), "lo": lo, "hi": hi, "label": label})
    if not steps:
        raise ValueError("no steps")
    if sum(s["dur"] for s in steps) > 6 * 3600:
        raise ValueError("over 6 hours")
    return steps


def _fmt_dur(s: int) -> str:
    h, rest = divmod(s, 3600)
    m, sec = divmod(rest, 60)
    return (f"{h}h" if h else "") + (f"{m}m" if m else "") + (f"{sec}s" if sec else "") or "0s"


def to_text(steps: list) -> str:
    """Steps back to the shorthand (repeats folded where the steps repeat exactly)."""
    def one(st):
        p = "free" if st["lo"] is None else (f"{round(st['lo'] * 100)}%" if st["lo"] == st["hi"]
                                             else f"{round(st['lo'] * 100)}-{round(st['hi'] * 100)}%")
        auto = _re.match(r"^\d+/\d+$", st.get("label") or "")
        return f"{_fmt_dur(st['dur'])} {p}" + (f' "{st["label"]}"' if st.get("label") and not auto else "")
    parts, i = [], 0
    while i < len(steps):
        for size in (2, 3, 1):                               # an on/off pair, a triple, a single step
            n = 1
            key = lambda k: [(s["dur"], s["lo"], s["hi"]) for s in steps[k: k + size]]
            while i + (n + 1) * size <= len(steps) and key(i + n * size) == key(i):
                n += 1
            if n > 1:
                parts.append(f"{n}x({', '.join(one(s) for s in steps[i: i + size])})")
                i += n * size
                break
        else:
            parts.append(one(steps[i]))
            i += 1
    return ", ".join(parts)


def to_zwo(name: str, description: str, steps: list) -> str:
    """A Zwift workout file (readable by parse_zwo, Zwift and most apps)."""
    from xml.sax.saxutils import escape, quoteattr
    rows = []
    for s in steps:
        if s["lo"] is None:
            rows.append(f'    <FreeRide Duration="{s["dur"]}" FlatRoad="1"/>')
        elif s["lo"] == s["hi"]:
            rows.append(f'    <SteadyState Duration="{s["dur"]}" Power="{s["lo"]:.3f}"/>')
        else:
            rows.append(f'    <Ramp Duration="{s["dur"]}" PowerLow="{s["lo"]:.3f}" PowerHigh="{s["hi"]:.3f}"/>')
    return (f"<workout_file>\n  <name>{escape(name)}</name>\n  <description>{escape(description)}</description>\n"
            f"  <sportType>bike</sportType>\n  <workout>\n" + "\n".join(rows) + "\n  </workout>\n</workout_file>\n")
