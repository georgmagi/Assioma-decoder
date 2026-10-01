"""
Ride summary: everything the app's summary screen shows, computed from a ride's per-second CSV
(the recorder writes it live), so a ride that just ended and one from months ago look the same.
Other apps' rides (FIT files dropped into the rides folder, e.g. from OpenCycling) are read too.

    summarize(load(path), ftp)  -> dict (JSON-ready): headline stats, best powers, time in zones,
                                   pedalling per leg, the whole-ride series and a Poincaré plot
    ride_list(folder)           -> [short summary per ride], newest first, cached in the rides
                                   folder's hidden .cache folder

Only recorded seconds are in the CSV (pauses are left out), so ride time = the number of rows.
"""
from __future__ import annotations

import csv
import glob
import json
import math
import os
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Optional

import eftp
import hrv
import vt1

SUFFIX = "_per_second.csv"
ZONES = [(0.55, "Z1"), (0.75, "Z2"), (0.90, "Z3"), (1.05, "Z4"), (1.20, "Z5"), (float("inf"), "Z6")]  # Coggan
# Coggan heart-rate zones, share of threshold heart rate (LTHR): Z1 up to 68 %, Z2 69-83 %, Z3 84-94 %,
# Z4 95-105 %, Z5 106 % and up
HR_ZONES = [(0.685, "Z1"), (0.835, "Z2"), (0.945, "Z3"), (1.055, "Z4"), (float("inf"), "Z5")]
TEXT = {"time", "rider_position", "rr_ms"}
SUMMARY_VERSION = 6                     # bump when the cached short summary changes


def load(path: str) -> list[dict]:
    """The per-second CSV as typed rows: numbers as float (None when empty), time as UTC datetime,
    rr_ms as a list of floats."""
    rows = []
    with open(path, newline="") as fh:
        for r in csv.DictReader(fh):
            row: dict = {}
            for k, v in r.items():
                if k == "time":
                    row[k] = datetime.fromisoformat(v.replace("Z", ""))
                elif k == "rr_ms":
                    row[k] = [float(x) for x in v.split()] if v else []
                elif k in TEXT:
                    row[k] = v or None
                else:
                    row[k] = float(v) if v not in ("", None) else None
            rows.append(row)
    return rows


def _mean(vals) -> Optional[float]:
    vals = [v for v in vals if v is not None]
    return sum(vals) / len(vals) if vals else None


def _pct(vals, q: float) -> Optional[float]:
    vals = sorted(v for v in vals if v is not None)
    if not vals:
        return None
    return vals[min(len(vals) - 1, max(0, round(q * (len(vals) - 1))))]


def _circ_mean(degs) -> Optional[float]:
    """Mean of angles in degrees (0-360), the right way round 0/360."""
    degs = [d for d in degs if d is not None]
    if not degs:
        return None
    s = sum(math.sin(math.radians(d)) for d in degs)
    c = sum(math.cos(math.radians(d)) for d in degs)
    return math.degrees(math.atan2(s, c)) % 360


def _rolling(vals: list, n: int) -> list:
    """Trailing n-second means of vals (None counts as 0 W), one per second from the n-th."""
    out, acc = [], 0.0
    for i, v in enumerate(vals):
        acc += v or 0
        if i >= n:
            acc -= vals[i - n] or 0
        if i >= n - 1:
            out.append(acc / n)
    return out


def _decoupling(power: list, hr: list, warmup: int = 600, min_s: int = 600) -> dict:
    """Pw:HR as in the recorder: EF = NP / avg HR over the first vs the second half after a 10 min
    warm-up (30 s rolling power), on the seconds with heart rate."""
    p30 = _rolling(power, 30)
    pairs = [(p30[i - 29] ** 4, hr[i]) for i in range(29, len(power)) if hr[i]]
    idx = [i for i in range(29, len(power)) if hr[i]]
    pairs = [(p, h) for (p, h), i in zip(pairs, idx) if i >= warmup]
    if len(pairs) < min_s:
        return {"decoupling_pct": None, "ef": None}
    half = len(pairs) // 2

    def ef(part):
        return (sum(p for p, _ in part) / len(part)) ** 0.25 / (sum(h for _, h in part) / len(part))
    e1, e2 = ef(pairs[:half]), ef(pairs[half:])
    return {"decoupling_pct": 100 * (e1 - e2) / e1, "ef": ef(pairs)}


def _best(power: list, secs: int) -> Optional[float]:
    if len(power) < secs:
        return None
    r = _rolling(power, secs)
    return max(r) if r else None


def _poincare(rr: list) -> dict:
    """Successive clean intervals (RR_n, RR_n+1) and the ellipse: SD1 (beat-to-beat) and SD2."""
    pairs = [(a, b) for a, b in zip(rr, rr[1:])]
    if len(pairs) < 10:
        return {"points": [], "sd1": None, "sd2": None}
    d = [(b - a) / math.sqrt(2) for a, b in pairs]
    s = [(b + a) / math.sqrt(2) for a, b in pairs]

    def sd(x):
        m = sum(x) / len(x)
        return math.sqrt(sum((v - m) ** 2 for v in x) / (len(x) - 1))
    step = max(1, len(pairs) // 4000)          # plenty for a scatter, light on the page
    return {"points": [[round(a), round(b)] for a, b in pairs[::step]], "sd1": sd(d), "sd2": sd(s),
            "mean": sum(rr) / len(rr)}


def summarize(rows: list[dict], ftp: Optional[float] = None, lthr: Optional[float] = None) -> dict:
    if not rows:
        raise ValueError("no recorded seconds")
    t0 = rows[0]["time"]
    n = len(rows)
    g = lambda k: [r.get(k) for r in rows]
    power, hr, cad = g("power_w"), g("heart_rate_bpm"), g("cadence_rpm")
    pedalling = [i for i, p in enumerate(power) if p]
    at = lambda k: [rows[i].get(k) for i in pedalling]

    p30 = _rolling(power, 30)
    np_ = (sum(x ** 4 for x in p30) / len(p30)) ** 0.25 if p30 else None
    avg_p = _mean(power)
    if_ = np_ / ftp if np_ and ftp else None
    tss = n * np_ * if_ / (ftp * 3600) * 100 if if_ else None

    # the beats, judged for artifacts the same way as live (alpha 1 itself is already in the CSV)
    tracker = hrv.HrvTracker()
    for r in rows:
        if r.get("rr_ms"):
            tracker.add(r["time"] + timedelta(seconds=1), r["rr_ms"])
    tracker._judge(final=True)
    beats = tracker.beats
    clean = [b.rr for b in beats if not b.artifact]
    a1 = [r.get("dfa_a1") for r in rows]
    a1_known = [v for v in a1 if v is not None]

    zone_s = {name: 0 for _, name in ZONES}
    if ftp:
        for p in power:
            if p is not None:
                zone_s[next(name for top, name in ZONES if p <= top * ftp)] += 1
    hr_zone_s = {name: 0 for _, name in HR_ZONES}
    if lthr:
        for h in hr:
            if h:
                hr_zone_s[next(name for top, name in HR_ZONES if h <= top * lthr)] += 1
    a1_bands = {"above_0.75": sum(v > 0.75 for v in a1_known), "0.5_to_0.75": sum(0.5 <= v <= 0.75 for v in a1_known),
                "below_0.5": sum(v < 0.5 for v in a1_known)}

    legs = {}
    for side in ("left", "right"):
        pco = at(f"pco_{side}_mm")
        legs[side] = {
            "power_w": _mean(at(f"power_{side}_w")),
            "te_pct": _mean(at(f"te_{side}_pct")), "ps_pct": _mean(at(f"ps_{side}_pct")),
            "pco_mm": _mean(pco), "pco_p10": _pct(pco, 0.1), "pco_p90": _pct(pco, 0.9),
            "pp_start": _circ_mean(at(f"pp_{side}_start_deg")), "pp_end": _circ_mean(at(f"pp_{side}_end_deg")),
            "peak_start": _circ_mean(at(f"pp_{side}_peak_start_deg")),
            "peak_end": _circ_mean(at(f"pp_{side}_peak_end_deg")),
        }
    positions = [p for p in at("rider_position") if p]
    standing = sum(p in ("standing", "to_standing") for p in positions)

    ts = [round((r["time"] - t0).total_seconds()) for r in rows]
    beat_t = [round((b.time - t0).total_seconds(), 2) for b in beats]
    return {
        "start_utc": t0.replace(tzinfo=timezone.utc).isoformat(),
        "stats": {
            "ride_s": n, "elapsed_s": ts[-1] + 1,
            "avg_power": avg_p, "np": np_, "max_power": max((p for p in power if p is not None), default=None),
            "if": if_, "tss": tss, "work_kj": sum(p or 0 for p in power) / 1000, "ftp": ftp, "lthr": lthr,
            "avg_hr": _mean(hr), "max_hr": max((h for h in hr if h), default=None),
            "avg_cadence": _mean(at("cadence_rpm")), "balance_right": _mean(at("balance_right_pct")),
            "avg_a1": _mean(a1_known), "rmssd": hrv.rmssd(clean) if len(clean) > 2 else None,
            "beats": len(beats), "artifact_pct": 100 * (len(beats) - len(clean)) / len(beats) if beats else None,
            "standing_pct": 100 * standing / len(positions) if positions else None,
            **_decoupling(power, hr),
        },
        "best": {str(s): _best(power, s) for s in (5, 60, 300, 1200)},
        "mmp": eftp.mmp(power),
        "zones": zone_s, "hr_zones": hr_zone_s, "a1_bands": a1_bands,
        "legs": legs,
        "series": {"t": ts, "power": power, "hr": hr, "cadence": cad, "a1": a1},
        "beats": {"t": beat_t, "rr": [round(b.rr, 1) for b in beats], "artifact": [bool(b.artifact) for b in beats]},
        "poincare": _poincare(clean),
        "vt1": vt1.estimate(rows),
    }


def short(s: dict) -> dict:
    """The few numbers the ride list shows."""
    st, v = s["stats"], s.get("vt1") or {}
    brief = lambda e: e and {k: e[k] for k in ("kind", "value", "value2", "lo", "hi") if k in e}
    return {"v": SUMMARY_VERSION, "start_utc": s["start_utc"], **{k: st[k] for k in
            ("ride_s", "elapsed_s", "avg_power", "np", "avg_hr", "avg_a1", "tss", "if", "ftp", "work_kj",
             "decoupling_pct")}, "vt1_hr": brief(v.get("hr")), "vt1_power": brief(v.get("power")),
            "vt1_beats_pct": v.get("beats_pct"), "zones": s["zones"], "mmp": s["mmp"]}


# --------------------------------------------------------------------------- #
# Rides from other apps: FIT files (OpenCycling, Garmin, ...) in the rides folder
# --------------------------------------------------------------------------- #
MIN_FIT_S = 180                         # shorter FIT files are aborted starts: not listed


def load_fit(path: str) -> list[dict]:
    """A FIT activity's records as per-second rows like load() gives (missing fields are None).
    Left/right balance comes from the record's left_right_balance (bit 7 set = the value is the
    right leg's share); RR intervals from hrv messages when the file has them."""
    from garmin_fit_sdk import Decoder, Stream
    msgs, _errors = Decoder(Stream.from_file(path)).read(convert_datetimes_to_dates=True)
    rows, seen = [], set()
    for r in msgs.get("record_mesgs", []):
        t = r.get("timestamp")
        if t is None:
            continue
        t = t.astimezone(timezone.utc).replace(tzinfo=None) if t.tzinfo else t
        if t in seen:
            continue
        seen.add(t)
        bal = r.get("left_right_balance")
        right = None
        if isinstance(bal, int) and bal != 0xFF:
            right = (bal & 0x7F) if bal & 0x80 else None
        rows.append({"time": t, "power_w": _num(r.get("power")), "cadence_rpm": _num(r.get("cadence")),
                     "heart_rate_bpm": _num(r.get("heart_rate")), "balance_right_pct": right, "rr_ms": []})
    rows.sort(key=lambda x: x["time"])
    # RR intervals (hrv messages, in s, several per message) go on the rows in order, a second's worth each
    rr = [v * 1000 for m in msgs.get("hrv_mesgs", []) for v in (m.get("time") or []) if v and v < 65]
    if rr and rows:
        acc, i = 0.0, 0
        for v in rr:
            acc += v / 1000
            while i < len(rows) - 1 and acc > (rows[i]["time"] - rows[0]["time"]).total_seconds() + 1:
                i += 1
            rows[i]["rr_ms"].append(round(v, 1))
    return rows


def _num(v) -> Optional[float]:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _is_own_fit(path: str) -> bool:
    return os.path.exists(path[:-4] + SUFFIX)          # our FIT: the per-second CSV is the source


def find_ride(folder: str, ride_id: str) -> Optional[str]:
    """The source file of a ride id (the recorder's per-second CSV, or another app's FIT file),
    None for anything that isn't a ride in this folder (ids come from the page: never a path)."""
    if not isinstance(ride_id, str) or not ride_id or any(c in ride_id for c in "/\\:") or ride_id.startswith("."):
        return None
    for path in (os.path.join(folder, ride_id + SUFFIX), os.path.join(folder, ride_id + ".fit")):
        if os.path.isfile(path) and not (path.endswith(".fit") and _is_own_fit(path)):
            return path
    return None


def load_ride(path: str) -> list[dict]:
    return load_fit(path) if path.endswith(".fit") else load(path)


def ride_name(ride_id: str) -> tuple:
    """(workout name, source) from another app's file name, e.g. OpenCycling_2026-09-13_45min_Z2_Spin
    -> ("45min Z2 Spin", "OpenCycling"); the recorder's own rides -> (None, "Assioma")."""
    if ride_id.startswith("ride_"):
        return None, "Assioma"
    parts = ride_id.split("_")
    source = parts[0] if len(parts) > 1 else "FIT"
    rest = [p for p in parts[1:] if not (len(p) == 10 and p[4] == "-" and p[7] == "-")]   # drop the date
    return (" ".join(rest).strip() or None), source


def summarize_file(folder: str, ride_id: str, ftp=None, lthr=None) -> Optional[dict]:
    path = find_ride(folder, ride_id)
    if path is None:
        return None
    rows = load_ride(path)
    if not rows:
        return None
    s = summarize(rows, ftp, lthr)
    name, source = ride_name(ride_id)
    own_fit = os.path.join(folder, ride_id + ".fit")
    return {"id": ride_id, "name": name, "source": source, "fit": os.path.exists(own_fit), "fit_path": own_fit, **s}


def cache_path(folder: str, ride_id: str) -> str:
    """Where a ride's short summary is cached: one hidden .cache folder, not next to the rides."""
    d = os.path.join(folder, ".cache")
    if not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
        if os.name == "nt":                                       # hide it in Explorer
            try:
                import ctypes
                ctypes.windll.kernel32.SetFileAttributesW(d, 0x02)
            except Exception:
                pass
    return os.path.join(d, ride_id + ".json")


def ride_list(folder: str, ftp: Optional[float] = None) -> list[dict]:
    """Every ride in folder, newest first: {id, name, source, fit, ...short summary}. The recorder's
    rides and other apps' FIT files. A FIT ride
    recorded at the same time as one of the recorder's (both apps running) is left out: the
    recorder's own recording has the pedals' full data, and counting both would double the load.
    Short summaries are cached in the folder's hidden .cache folder."""
    sources = [p[: -len(SUFFIX)] for p in glob.glob(os.path.join(folder, "*" + SUFFIX))]
    sources += [p[:-4] for p in glob.glob(os.path.join(folder, "*.fit")) if not _is_own_fit(p)]
    out = []
    for base in sources:
        ride_id = os.path.basename(base)
        path = find_ride(folder, ride_id)
        if path is None:
            continue
        cache = cache_path(folder, ride_id)
        info = None
        try:
            with open(cache) as fh:
                info = json.load(fh)
            if (info.get("v") != SUMMARY_VERSION or os.path.getmtime(cache) < os.path.getmtime(path)
                    or (info.get("ftp") is None and ftp)):         # an FTP set since: now it has TSS
                info = None
        except (OSError, ValueError):
            pass
        if info is None:
            try:
                rows = load_ride(path)
            except Exception:                                     # a file we can't read: not a ride
                continue
            info = short(summarize(rows, ftp)) if rows else {"v": SUMMARY_VERSION, "empty": True}
            try:
                with open(cache, "w") as fh:
                    json.dump(info, fh)
            except OSError:
                pass
        if info.get("empty") or (path.endswith(".fit") and info.get("ride_s", 0) < MIN_FIT_S):
            continue
        name, source = ride_name(ride_id)
        out.append({"id": ride_id, "name": name, "source": source, "fit": os.path.exists(base + ".fit"), **info})
    own = [(_ts(r["start_utc"]), _ts(r["start_utc"]) + r.get("elapsed_s", r["ride_s"])) for r in out if r["source"] == "Assioma"]
    out = [r for r in out if r["source"] == "Assioma" or not _overlaps(r, own)]
    return sorted(out, key=lambda r: r["start_utc"], reverse=True)


def _ts(iso: str) -> float:
    return datetime.fromisoformat(iso).timestamp()


def _overlaps(r: dict, own: list) -> bool:
    a0 = _ts(r["start_utc"])
    a1 = a0 + r.get("elapsed_s", r["ride_s"])
    for b0, b1 in own:
        shared = min(a1, b1) - max(a0, b0)
        if shared > 0.5 * min(a1 - a0, b1 - b0):
            return True
    return False
