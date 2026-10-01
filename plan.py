"""
The training planner: a block of days (usually 4 weeks from a Monday), each a workout, a shorthand
workout (workout.parse_text) or rest; what it does to fitness, fatigue and form; how the days went;
and a status summary to hand to Claude when asking for the next block.

The plan lives in plan.json next to the app: {"start": "YYYY-MM-DD", "weeks": 4,
"days": {"YYYY-MM-DD": {"rest": true} | {"ref": workout id} | {"text": shorthand, "name": name}}}.

Import format (what Claude writes; one line per day, "Week N" lines move to the next week):
    Week 1
    Mon: rest
    Tue: Threshold 2x20 | 15m 50-70%, 2x(20m 90%, 5m 55%), 10m 50%
    Wed: 60m Z2
    Thu: Threshold steps
    2026-10-12: 90m Z2
A day is a weekday (in the current week of the block) or a date; an entry is "rest" / "off", a
workout in the library by name, or "Name | shorthand" / shorthand alone.

TODO (agreed with Georg, see CLAUDE.md): warnings when the body disagrees with the plan (weekly
check, form, ramp rate), suggesting a swap; never changing the plan by itself.
"""
from __future__ import annotations

import json
import os
import re
from datetime import date, timedelta
from typing import Optional

import training
import workout as wk

DAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def load(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            p = json.load(fh)
        return p if isinstance(p, dict) and "days" in p else empty()
    except (OSError, ValueError):
        return empty()


def save(path: str, plan: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(plan, fh, indent=1)
    os.replace(tmp, path)


def empty(start: Optional[date] = None, weeks: int = 4) -> dict:
    start = start or next_monday(date.today())
    return {"start": start.isoformat(), "weeks": weeks, "days": {}}


def next_monday(d: date) -> date:
    return d + timedelta(days=(7 - d.weekday()) % 7)


def parse_entry(text: str, library: dict) -> dict:
    """One day's entry; ValueError when it's neither rest, a workout nor shorthand."""
    t = text.strip()
    if not t or t.lower() in ("rest", "off", "rest day", "day off"):
        return {"rest": True}
    by_name = {w["name"].lower(): k for k, w in library.items()}
    if t.lower() in by_name:
        return {"ref": by_name[t.lower()]}
    if t in library:
        return {"ref": t}
    name, _, short = t.partition("|")
    if not short:
        name, short = "", t
    wk.parse_text(short)                                   # raises with what's wrong
    return {"text": short.strip(), "name": name.strip() or short.strip()}


def parse_import(text: str, start: date, library: dict) -> tuple:
    """The pasted plan to {date: entry}, and [(line number, problem)]."""
    days, problems, week = {}, [], 0
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip().strip("-*• ").strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^week\s*(\d+)\b", line, re.I)
        if m:
            week = int(m.group(1)) - 1
            continue
        head, sep, rest = line.partition(":")
        if not sep:
            problems.append((n, "a day needs a colon: Mon: 60m Z2"))
            continue
        head = head.strip().lower()
        try:
            if re.match(r"^\d{4}-\d{2}-\d{2}$", head):
                d = date.fromisoformat(head)
            elif head[:3] in DAYS:
                d = start + timedelta(weeks=week, days=DAYS[head[:3]])
            else:
                raise ValueError(f'"{head}" is not a weekday or a date')
            days[d.isoformat()] = parse_entry(rest, library)
        except ValueError as e:
            problems.append((n, str(e)))
    return days, problems


def resolve(entry: dict, library: dict) -> Optional[dict]:
    """A day's workout ({name, steps, ...}) or None for rest / unknown."""
    if not entry or entry.get("rest"):
        return None
    if entry.get("ref"):
        return library.get(entry["ref"])
    try:
        return {"name": entry.get("name") or entry["text"], "description": "", "steps": wk.parse_text(entry["text"])}
    except (ValueError, KeyError):
        return None


def plan_workouts(plan: dict) -> dict:
    """The plan's shorthand days as workouts ("plan:YYYY-MM-DD"), for the library and the trainer."""
    out = {}
    for d, e in plan.get("days", {}).items():
        if e.get("text"):
            w = resolve(e, {})
            if w:
                out[f"plan:{d}"] = {**w, "plan": True}
    return out


def day_id(plan: dict, d: date, library: dict) -> Optional[str]:
    """The workout id for a day of the plan (None: rest or nothing planned)."""
    e = plan.get("days", {}).get(d.isoformat())
    if not e or e.get("rest"):
        return None
    return e["ref"] if e.get("ref") in library else f"plan:{d.isoformat()}" if e.get("text") else None


def zone_split(w: dict) -> dict:
    """Seconds easy (Z1-2, up to 75 %), moderate (Z3, to 90 %), hard (above)."""
    out = {"easy": 0, "moderate": 0, "hard": 0}
    for f in wk.targets(w):
        out["easy" if f <= 0.75 else "moderate" if f <= 0.90 else "hard"] += 1
    return out


def overview(plan: dict, library: dict, rides: list, ftp: Optional[float], today: date) -> dict:
    """Everything the Plan page shows: each day (planned workout, its estimate, what was ridden),
    the weeks, and the fitness / fatigue / form forecast through the end of the block."""
    start = date.fromisoformat(plan["start"])
    end = start + timedelta(weeks=plan.get("weeks", 4)) - timedelta(days=1)
    actual = training.daily_tss(rides)
    days, weeks = [], []
    for k in range((end - start).days + 1):
        d = start + timedelta(days=k)
        e = plan["days"].get(d.isoformat())
        w = resolve(e, library) if e else None
        est = wk.estimate(w, ftp) if w and ftp else {}
        done = actual.get(d)
        status = ("rest" if e and e.get("rest") else "upcoming" if d > today
                  else "done" if done else "today" if d == today else "missed" if w else "")
        if done and not w and not (e and e.get("rest")):
            status = "extra"
        days.append({"day": d.isoformat(), "entry": e, "name": w["name"] if w else None,
                     "id": day_id(plan, d, library), "minutes": round(wk.total_s(w) / 60) if w else 0,
                     "tss": est.get("tss"), "if": est.get("if"), "done_tss": done, "status": status,
                     "split": zone_split(w) if w else None,
                     "steps": [[s["dur"], s["lo"], s["hi"]] for s in w["steps"]] if w else None})
    for k in range(0, len(days), 7):
        wd = days[k: k + 7]
        split = {"easy": 0, "moderate": 0, "hard": 0}
        for x in wd:
            for z, v in (x["split"] or {}).items():
                split[z] += v
        weeks.append({"start": wd[0]["day"], "minutes": sum(x["minutes"] for x in wd),
                      "tss": sum(x["tss"] or 0 for x in wd), "done_tss": sum(x["done_tss"] or 0 for x in wd), "split": split})
    return {"start": plan["start"], "end": end.isoformat(), "weeks_n": plan.get("weeks", 4), "days": days,
            "weeks": weeks, "forecast": forecast(rides, days, today)}


def forecast(rides: list, days: list, today: date) -> dict:
    """Fitness, fatigue and form: the real ones up to today, then the plan's TSS day by day (today's
    plan only if nothing was ridden today yet). Same model as training.pmc."""
    p = training.pmc(rides, today)
    load = training.daily_tss(rides)
    planned = {date.fromisoformat(x["day"]): x["tss"] or 0 for x in days}
    end = max(planned) if planned else today
    c = p["ctl"][-1] if p["ctl"] else 0.0
    a = p["atl"][-1] if p["atl"] else 0.0
    if p["days"] and today not in load and today in planned:     # today not ridden yet: replace today's 0
        c0, a0 = (p["ctl"][-2], p["atl"][-2]) if len(p["ctl"]) > 1 else (0.0, 0.0)
        t = planned[today]
        c, a = c0 + (t - c0) / training.CTL_DAYS, a0 + (t - a0) / training.ATL_DAYS
    out = {"days": [], "ctl": [], "atl": [], "tsb": [], "tss": []}
    d = today + timedelta(days=1)
    while d <= end:
        t = planned.get(d, 0.0)
        out["tsb"].append(round(c - a, 2))
        c += (t - c) / training.CTL_DAYS
        a += (t - a) / training.ATL_DAYS
        out["days"].append(d.isoformat()); out["ctl"].append(round(c, 2)); out["atl"].append(round(a, 2))
        out["tss"].append(round(t, 1))
        d += timedelta(days=1)
    return {"actual": p, "plan": out}


def status_text(progress: dict, rides: list, settings: dict, over: Optional[dict]) -> str:
    """What to paste when asking Claude for the next block: the numbers that matter, plainly."""
    def f(v, nd=0):
        return "--" if v is None else (f"{v:.{nd}f}" if isinstance(v, (int, float)) else str(v))
    p = progress["pmc"]
    lines = ["# Training status (from my Assioma Recorder)", ""]
    if p["days"]:
        lines.append(f"- Fitness (CTL, 42 d) {f(p['ctl'][-1])}, fatigue (ATL, 7 d) {f(p['atl'][-1])}, form (TSB) {f(p['tsb'][-1])}")
    lines.append(f"- FTP setting {f(settings.get('ftp'))} W; threshold HR {f(settings.get('lthr'))}; VT1 HR setting {f(settings.get('vt1_hr'))}")
    e = (progress.get("eftp") or {}).get("current")
    if e:
        lines.append(f"- eFTP {f(e['ftp'])} W from {e['method']}; evidence: " + "; ".join(
            f"{x['kind']} {('above ' if x.get('bound') == 'above' else '')}{f(x['value'])} W" for x in e.get("evidence", [])))
    def thr(e, unit):
        if not e:
            return "--"
        return {"cross": f"{f(e['value'])} {unit}", "between": f"{f(e['value'])}-{f(e.get('value2'))} {unit}"}.get(
            e["kind"], f"{e['kind']} {f(e['value'])} {unit}")
    vt = [v for v in progress.get("vt1") or [] if v.get("hr")]
    if vt:
        v, v2 = vt[-1], vt[-1].get("vt2") or {}
        lines.append(f"- Latest VT1 ({v['day']}, alpha 1 = 0.75): {thr(v['hr'], 'bpm')} / {thr(v.get('power'), 'W')}; "
                     f"VT2 (alpha 1 = 0.5): {thr(v2.get('hr'), 'bpm')} / {thr(v2.get('power'), 'W')}")
    fc = progress.get("fitness_checks") or {}
    if fc.get("checks"):
        lines.append(f"- Weekly checks (HR at {fc['ref_w']} W, 20 C-corrected where known): " + ", ".join(
            f"{c['day']} {f(c['hr_ref_20c'] or c['hr_ref'])} bpm (recovery {f(c['hrr60'])})" for c in fc["checks"][-4:]))
    eff = [x for x in progress.get("efficiency") or [] if x.get("ef")]
    if eff:
        lines.append("- Z2 efficiency (W per bpm, last rides): " + ", ".join(f"{x['day']} {x['ef']:.2f}" for x in eff[-5:]))
    lines += ["", "## Last 4 weeks (Mon-Sun)"]
    for w in progress["weeks"][-4:]:
        lines.append(f"- week of {w['start']}: {w['rides']} rides, {training_hm(w['time_s'])}, {f(w['tss'])} TSS")
    lines += ["", "## Rides, last 14 days"]
    cut = (date.today() - timedelta(days=14)).isoformat()
    for r in sorted(rides, key=lambda r: r["start_utc"]):
        if r["start_utc"][:10] >= cut:
            dur = r.get("durability") or {}
            lines.append(f"- {r['start_utc'][:10]} {r.get('name') or ''} {training_hm(r['ride_s'])}, NP {f(r.get('np'))} W, "
                         f"TSS {f(r.get('tss'))}, avg HR {f(r.get('avg_hr'))}, Pw:HR {f(r.get('decoupling_pct'), 1)} %"
                         + (f", durability {dur.get('score')}" if dur else "") + (f", room {f(r.get('temp_c'), 1)} C" if r.get("temp_c") is not None else ""))
    if over:
        lines += ["", f"## Current block ({over['start']} to {over['end']})"]
        for d in over["days"]:
            if d["status"] in ("done", "missed", "extra", "today") or d["day"] <= date.today().isoformat():
                lines.append(f"- {d['day']}: planned {d['name'] or 'rest'} ({f(d['tss'])} TSS), done {f(d['done_tss'])} TSS")
    lines += ["", "Please write the next 4-week block, one line per day in this format (shorthand: durations 90s/12m/1h,",
              "power as % FTP, ramps 50-65%, zones Z1-Z6, repeats 3x(...), free; or a workout name; or rest):",
              "Week 1", "Mon: rest", "Tue: Threshold 2x20 | 15m 50-70%, 2x(20m 90%, 5m 55%), 10m 50%", "Wed: 60m Z2",
              "Thu: Threshold steps", "..."]
    return "\n".join(lines)


def training_hm(s) -> str:
    s = int(s or 0)
    return f"{s // 3600}:{s // 60 % 60:02d}"
