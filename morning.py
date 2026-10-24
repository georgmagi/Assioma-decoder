"""
The morning check: resting heart rate and heart-rate variability, lying down, 30 s to settle and
1 minute to measure (longer only if the strap was noisy), with the strap (best over ANT+, every
beat), most mornings. One minute of ln RMSSD agrees closely with the classic 5 minutes (Esco &
Flatt 2014; HRV4Training measures 1 minute too). Read against your own normal, the way HRV-guided
training does it (Plews et al.; Kiviniemi et al.): the natural log of RMSSD (ln RMSSD), its 7-day
average against your 30-day baseline, with the normal range baseline +- 0.5 SD. Under the range =
strained or not recovered; over it can also mean fatigue (parasympathetic saturation), so it's
flagged, not cheered. Resting heart rate 5+ bpm over its baseline is a flag of its own.

Artifacts at rest are judged differently from the ride's filter (hrv.py). Lying down, breathing
swings your heart rate a lot: calm slow breaths at ~60 bpm move beats +-200 ms (20 %+) around the
median, which the ride's 20 % rule took for artifacts (28-34 % of beats on 6-7 Oct; simulated
breathing reproduces it). Here a beat is an artifact only if it is a missed or extra beat (over
1.6x / under 0.6x the local median, or two short ones adding up to one: breathing never does
that) or a lone spike: a jump in and a
jump straight back out, each over 5.2 quartile deviations of the beat-to-beat changes (Lipponen &
Tarvainen 2019). RMSSD then uses only the successive differences between two good beats.

With the Polar's ECG streaming (the strap on Bluetooth: polar_pmd.py), each beat the filter set
aside is checked in the ECG (ecg_beats.py): a real early beat, or the strap's error. Both stay out
of RMSSD; a beat the ECG shows to be an ordinary one (a long pause on a deep breath looks like a
missed beat by timing alone) is put back. The measured minute of ECG is kept.

Stored in morning.json next to the app: a list of {date, time, hr, rmssd, ln_rmssd, beats,
artifact_pct, rr, odd}, one per day (a second check the same day replaces the first); rr keeps the
intervals so a check can be re-read later, odd is the ECG's verdict on the set-aside beats. The
ECG itself goes to mornings/<date>_ecg.csv (s since the check started, uV).
"""
from __future__ import annotations

import json
import math
import os
from datetime import date, datetime, timedelta, timezone
from statistics import mean, median, stdev
from types import SimpleNamespace
from typing import Optional

import ecg_beats

SETTLE_S, MEASURE_S = 30, 60
MAX_MEASURE_S = 120          # a noisy strap: keep measuring up to this, then use the cleanest minute
MIN_BEATS = 40
MAX_ARTIFACT_PCT = 20.0
MISSED, EXTRA = 1.6, 0.6     # x the local median: a missed beat (two in one) or an extra one (a split)
SPIKE_QD = 5.2               # a lone spike's jump, in quartile deviations of the beat-to-beat changes


def rest_artifacts(rr: list) -> list:
    """Which beats are artifacts at rest (see the module notes), one bool per interval."""
    n = len(rr)
    bad = [False] * n
    for i in range(n):
        m = median(rr[max(0, i - 5):i + 6])
        bad[i] = bad[i] or rr[i] > MISSED * m or rr[i] < EXTRA * m
        # an extra beat splits one interval in two, not always evenly: two short ones that add up to one
        if i + 1 < n and rr[i] < 0.75 * m and rr[i + 1] < 0.75 * m and abs(rr[i] + rr[i + 1] - m) < 0.25 * m:
            bad[i] = bad[i + 1] = True
    d = [b - a for a, b in zip(rr, rr[1:])]
    if len(d) >= 8:
        q = sorted(d)
        qd = (q[3 * len(q) // 4] - q[len(q) // 4]) / 2
        th = max(SPIKE_QD * qd, 0.08 * median(rr))
        for i in range(1, n - 1):                  # d[i - 1] into beat i, d[i] out of it
            if abs(d[i - 1]) > th and abs(d[i]) > th and d[i - 1] * d[i] < 0:
                bad[i] = True
    return bad


def rest_rmssd(rr: list, bad: list) -> Optional[float]:
    """RMSSD over successive differences between two good beats only (none across a removed one)."""
    sq = [(b - a) ** 2 for a, b, x, y in zip(rr, rr[1:], bad, bad[1:]) if not x and not y]
    return math.sqrt(sum(sq) / len(sq)) if len(sq) >= 10 else None


class Check:
    """One check in progress: fed the strap's notifications (time, heart rate, RR intervals)."""

    def __init__(self, now: datetime) -> None:
        self.started = now
        self.rr: list = []                         # (time, [ms])
        self.hr: list = []
        self.last_data: Optional[datetime] = None
        self.ecg: list = []                        # (time, uV) from the strap's ECG stream, if it streams

    def add_ecg(self, samples: list) -> None:
        """[(time, uV)] as they arrive (recorder_app feeds the strap's ECG while a check runs)."""
        self.ecg.extend(samples)

    def phase(self, now: datetime) -> str:
        """settle, measure, more (a noisy minute: measuring on for a cleaner one) or done."""
        t = (now - self.started).total_seconds()
        if t < SETTLE_S:
            return "settle"
        if t < SETTLE_S + MEASURE_S:
            return "measure"
        if t < SETTLE_S + MAX_MEASURE_S and not self._clean_enough():
            return "more"
        return "done"

    def left_s(self, now: datetime) -> float:
        t = (now - self.started).total_seconds()
        end = (SETTLE_S if t < SETTLE_S else SETTLE_S + MEASURE_S if t < SETTLE_S + MEASURE_S
               else SETTLE_S + MAX_MEASURE_S)
        return max(0.0, end - t)

    def feed(self, t: datetime, bpm: Optional[float], rr_ms: list) -> None:
        self.last_data = t
        if (t - self.started).total_seconds() < SETTLE_S:
            return
        if bpm:
            self.hr.append(bpm)
        if rr_ms:
            self.rr.append((t, list(rr_ms)))

    def _intervals(self) -> list:
        """[(time of the beat, rr ms)] in order: a notification's last interval ends at its time."""
        out = []
        for t, rrs in self.rr:
            back = 0.0
            for rr in reversed(rrs):
                if 200 <= rr <= 2500:
                    out.append((t - timedelta(milliseconds=back), rr))
                back += rr
        return sorted(out, key=lambda x: x[0])

    def _best_minute(self) -> tuple:
        """(rr, bad, beat times) of the cleanest MEASURE_S of beats; the latest of equally clean ones."""
        beats = self._intervals()
        if not beats:
            return [], [], []
        rr = [x for _, x in beats]
        bad = rest_artifacts(rr)
        best, best_bad, j = (0, len(beats)), None, 0
        for i in range(len(beats)):
            while j < len(beats) and (beats[j][0] - beats[i][0]).total_seconds() < MEASURE_S:
                j += 1
            if j == len(beats) and best_bad is not None:
                break                                  # windows from here on are shorter than a minute
            n_bad = sum(bad[i:j])
            if best_bad is None or n_bad <= best_bad:
                best, best_bad = (i, j), n_bad
        i, j = best
        return rr[i:j], bad[i:j], [t for t, _ in beats[i:j]]

    def _clean_enough(self) -> bool:
        rr, bad, _ = self._best_minute()
        return bool(rr) and 100 * sum(bad) / len(rr) <= MAX_ARTIFACT_PCT

    def result(self, now: datetime) -> dict:
        """{ok, hr, rmssd, ln_rmssd, beats, artifact_pct, rr} from the cleanest minute, or {ok: False, why}."""
        rr, bad, times = self._best_minute()
        if len(rr) < MIN_BEATS:
            return {"ok": False, "why": f"only {len(rr)} beats arrived: is the strap on and connected?"}
        art = 100 * sum(bad) / len(rr)
        r = rest_rmssd(rr, bad)
        if art > MAX_ARTIFACT_PCT or r is None:
            return {"ok": False, "why": f"the strap missed or doubled {art:.0f}% of beats: wet the electrodes and try again"}
        clean = [x for x, b in zip(rr, bad) if not b]
        local = self.started.replace(tzinfo=timezone.utc).astimezone()     # started is UTC (utcnow)
        odd, ecg = self._ecg_check(rr, bad, times)
        if odd and odd.get("real_at"):
            # the ECG shows these were ordinary beats (e.g. a long pause on a deep breath): they count.
            # 7 Oct: two pauses of 1.47 and 1.60 s, flat ECG between clean beats, set aside as "missed beats"
            bad = [b and k not in odd["real_at"] for k, b in enumerate(bad)]
            art, r = 100 * sum(bad) / len(rr), rest_rmssd(rr, bad)
            clean = [x for x, b in zip(rr, bad) if not b]
        return {"ok": True, "date": local.date().isoformat(), "time": local.strftime("%H:%M"),
                "hr": round(60000 / mean(clean), 1), "rmssd": round(r, 1), "ln_rmssd": round(math.log(r), 3),
                "beats": len(rr), "artifact_pct": round(art, 1), "rr": [round(x) for x in rr], "odd": odd,
                "_ecg": ecg}

    def _ecg_check(self, rr: list, bad: list, times: list) -> tuple:
        """(the ECG's verdict on the set-aside beats or None, [[s, uV]] of the measured minute or None)."""
        t0 = self.started
        span = [(t, v) for t, v in self.ecg if times[0] - timedelta(seconds=8) <= t <= times[-1] + timedelta(seconds=2)]
        if len(span) < ecg_beats.RATE * 20:             # no ECG (ANT+ only) or too little of it
            return None, None
        ts = [(t - t0).total_seconds() for t, _ in span]
        vs = [v for _, v in span]
        beats = [SimpleNamespace(rr=x, orig=x, artifact=b, corrected=False) for x, b in zip(rr, bad)]
        try:
            odd = ecg_beats.check(beats, [(t - t0).total_seconds() for t in times], (ts, vs))
        except (ValueError, IndexError, ZeroDivisionError):
            odd = None
        return odd, [[round(t, 4), v] for t, v in zip(ts, vs)]


def save_ecg(morning_path: Optional[str], day: str, ecg: Optional[list]) -> None:
    """The check's minute of ECG in mornings/<day>_ecg.csv beside morning.json."""
    if not morning_path or not ecg:
        return
    folder = os.path.join(os.path.dirname(morning_path), "mornings")
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, f"{day}_ecg.csv"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write("s,uV\n")
        fh.writelines(f"{t},{v}\n" for t, v in ecg)


def load(path: Optional[str]) -> list:
    if not path:
        return []
    try:
        with open(path, encoding="utf-8") as fh:
            got = json.load(fh)
        return got if isinstance(got, list) else []
    except (OSError, ValueError):
        return []


def save(path: Optional[str], checks: list) -> None:
    if not path:
        return
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(checks, fh, indent=1)
    os.replace(tmp, path)


def add(checks: list, result: dict) -> list:
    """One check per day: the new one replaces any earlier one that day."""
    return sorted([c for c in checks if c["date"] != result["date"]]
                  + [{k: v for k, v in result.items() if k != "ok" and not k.startswith("_")}],
                  key=lambda c: c["date"])


SLEEP_UP = 5          # bpm: the night's lowest 30 min this far over your normal = not recovered
SLEEP_NOTE = 3        # ... this far: worth a look
SLEEP_BASE_N = 5      # nights in the 30 before a night, for a normal


def nights(garmin: Optional[dict]) -> list:
    """The watch's nights (garmin.json, from garmin_pull.py on the server), oldest first: {date (the
    morning), low30 (lowest 30-min mean while asleep, the steadiest resting number), avg, rhr
    (Garmin's resting HR), base (the mean low30 of the 30 nights before, from 5 nights), up}."""
    out = []
    days = sorted(((garmin or {}).get("days") or {}).items())
    for d, r in days:
        s = r.get("sleep_hr") or {}
        low = s.get("low30") or s.get("min")
        if low is None:
            continue
        out.append({"date": d, "low30": low, "avg": s.get("avg"), "rhr": r.get("resting_hr"),
                    "sleep_start": r.get("sleep_start"), "sleep_end": r.get("sleep_end")})
    for n in out:
        d = date.fromisoformat(n["date"])
        before = [x["low30"] for x in out if d - timedelta(days=30) <= date.fromisoformat(x["date"]) < d]
        if len(before) >= SLEEP_BASE_N:
            n["base"] = round(mean(before), 1)
            n["up"] = round(n["low30"] - n["base"], 1)
    return out


def _sleep_verdict(n: dict) -> tuple:
    if "base" not in n:
        return ("learning", f"Asleep your heart rate got down to {n['low30']:.0f}; learning your normal from the watch's nights.")
    if n["up"] >= SLEEP_UP:
        return ("below", f"Sleeping heart rate {n['low30']:.0f}, {n['up']:.0f} over your normal {n['base']:.0f}: "
                         "not recovered. Keep today easy, or move the hard session.")
    if n["up"] >= SLEEP_NOTE:
        return ("normal", f"Sleeping heart rate {n['low30']:.0f}, a little over your normal {n['base']:.0f}: "
                          "fine, go by how the warm-up feels.")
    return ("normal", f"Sleeping heart rate {n['low30']:.0f}, within your normal ({n['base']:.0f}): train as planned.")


def trend(checks: list, today: Optional[date] = None, garmin: Optional[dict] = None) -> dict:
    """The morning: the strap's checks and the watch's nights read together.
    days: each check's ln RMSSD, its 7-day mean, the 30-day baseline and normal range (mean +- 0.5
    SD over the 30 days before), resting HR and its baseline (7 checks before it judges). nights:
    the watch's sleeping heart rate against its own normal (nights()). verdict: today's word, the
    worse of the two: the strap's when you checked today (or no night came in), the night's when
    only the watch spoke; "below" from either wins."""
    today = today or date.today()
    out = _strap_trend(checks, today)
    ns = nights(garmin)
    night = next((n for n in ns if n["date"] == today.isoformat()), None)
    out["nights"], out["night"] = ns, night
    if night is not None:
        sv = _sleep_verdict(night)
        v = out["verdict"]
        checked_today = out["today"] is not None
        if v is None or not checked_today or v[0] == "learning":
            out["verdict"] = sv if not (checked_today and v and v[0] == "below") else v
        elif sv[0] == "below" and v[0] != "below":
            out["verdict"] = ("below", sv[1])
        elif sv[0] == "below":
            out["verdict"] = ("below", v[1] + " " + sv[1].split(":")[0] + " too.")
    return out


def _strap_trend(checks: list, today: date) -> dict:
    days = []
    by = {c["date"]: c for c in checks}
    for c in checks:
        d = date.fromisoformat(c["date"])
        base = [x for x in checks if d - timedelta(days=30) <= date.fromisoformat(x["date"]) < d]
        week = [x["ln_rmssd"] for x in checks if d - timedelta(days=6) <= date.fromisoformat(x["date"]) <= d]
        row = {**{k: v for k, v in c.items() if k not in ("rr", "_ecg")}, "ln7": round(mean(week), 3)}
        if len(base) >= 7:
            m, sd = mean(x["ln_rmssd"] for x in base), stdev(x["ln_rmssd"] for x in base)
            row.update(base=round(m, 3), lo=round(m - 0.5 * sd, 3), hi=round(m + 0.5 * sd, 3),
                       hr_base=round(mean(x["hr"] for x in base), 1))
        days.append(row)
    last = days[-1] if days else None
    verdict = None
    if last and "base" in last:
        hr_flag = last["hr"] >= last["hr_base"] + 5
        if last["ln7"] < last["lo"] or hr_flag:
            verdict = ("below", "Below your normal" + (": resting heart rate is up too" if hr_flag else "")
                       + ". Strained or not recovered: keep today easy, or move the hard session.")
        elif last["ln7"] > last["hi"]:
            verdict = ("above", "Above your normal: often fine, but after hard days it can mean deep fatigue. Go by how the warm-up feels.")
        else:
            verdict = ("normal", "Within your normal: train as planned.")
    elif last:
        n = len(checks)
        verdict = ("learning", f"Learning your normal: {n} of 7 checks so far.")
    return {"days": days, "today": by.get(today.isoformat()), "verdict": verdict}
