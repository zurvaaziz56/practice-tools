#!/usr/bin/env python3
"""Apple Health -> local daily table + summary. Standard library only; nothing leaves the Mac.

Two input lanes:
  1. Apple's own export  (Health app > profile picture > Export All Health Data > AirDrop to Mac)
     -> export.zip containing apple_health_export/export.xml (full history every time).
  2. "Health Auto Export" app files (JSON) landing in iCloud Drive on a schedule
     -> merged day by day. Parser is PROVISIONAL until tuned on a first real file;
        unrecognised metric names are reported, never guessed.

Usage:
  python3 "health_ingest.py"              # auto-discover new files, then rebuild summary
  python3 "health_ingest.py" --file PATH  # ingest one specific export.zip / export.xml / .json
  python3 "health_ingest.py" --summary-only

Outputs (all under data/, git-ignored):
  daily.csv  workouts.csv  state.json  "Health Summary.md"
"""
import argparse
import csv
import datetime as dt
import glob
import json
import os
import statistics
import sys
import zipfile
from collections import defaultdict
from xml.etree import ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
DAILY_CSV = os.path.join(DATA, "daily.csv")
WORKOUT_CSV = os.path.join(DATA, "workouts.csv")
STATE = os.path.join(DATA, "state.json")
SUMMARY = os.path.join(DATA, "Health Summary.md")

DAILY_FIELDS = [
    "date", "steps", "distance_km", "flights", "active_kcal", "exercise_min", "stand_hours",
    "resting_hr", "hrv_ms", "walking_hr", "hr_avg", "hr_max", "vo2max",
    "sleep_h", "sleep_core_h", "sleep_deep_h", "sleep_rem_h", "awake_min", "sleep_start", "sleep_end",
    "resp_rate", "spo2", "wrist_temp_c", "weight_kg", "mindful_min",
    "workouts", "workout_min", "workout_types", "source",
]
WORKOUT_FIELDS = ["date", "start", "end", "type", "duration_min", "active_kcal", "distance_km",
                  "avg_hr", "max_hr", "source"]

Q = "HKQuantityTypeIdentifier"
SUM_TYPES = {Q + "StepCount": "steps", Q + "DistanceWalkingRunning": "distance_km",
             Q + "FlightsClimbed": "flights"}
MEAN_TYPES = {Q + "RestingHeartRate": "resting_hr", Q + "HeartRateVariabilitySDNN": "hrv_ms",
              Q + "WalkingHeartRateAverage": "walking_hr", Q + "RespiratoryRate": "resp_rate",
              Q + "OxygenSaturation": "spo2", Q + "AppleSleepingWristTemperature": "wrist_temp_c"}
LAST_TYPES = {Q + "VO2Max": "vo2max", Q + "BodyMass": "weight_kg"}
FALLBACK_SUM = {Q + "ActiveEnergyBurned": "active_kcal", Q + "AppleExerciseTime": "exercise_min"}
SLEEP_TYPE = "HKCategoryTypeIdentifierSleepAnalysis"
MINDFUL_TYPE = "HKCategoryTypeIdentifierMindfulSession"
ASLEEP_STAGES = {"AsleepCore": "core", "AsleepDeep": "deep", "AsleepREM": "rem",
                 "AsleepUnspecified": "unspec", "Asleep": "unspec"}


# ---------- helpers ----------

def parse_dt(s):
    """'2026-09-30 07:15:00 +0500' -> aware datetime (kept in its recorded offset)."""
    s = s.strip()
    try:
        return dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S %z")
    except ValueError:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def fnum(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def is_watch(source):
    return "watch" in (source or "").lower()


def union_minutes(intervals):
    """Total minutes covered by a list of (start, end) datetimes, overlaps counted once."""
    total, cur_s, cur_e = 0.0, None, None
    for s, e in sorted(intervals):
        if cur_e is None or s > cur_e:
            if cur_e is not None:
                total += (cur_e - cur_s).total_seconds()
            cur_s, cur_e = s, e
        elif e > cur_e:
            cur_e = e
    if cur_e is not None:
        total += (cur_e - cur_s).total_seconds()
    return total / 60.0


def rnd(v, n=1):
    if v is None:
        return ""
    r = round(v, n)
    return int(r) if n == 0 else r


# ---------- lane 1: Apple export.xml ----------

def ingest_apple_xml(stream):
    sums = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))   # field -> date -> source -> total
    means = defaultdict(lambda: defaultdict(list))                       # field -> date -> [values]
    lasts = defaultdict(dict)                                            # field -> date -> (dt, value)
    hr = defaultdict(lambda: [0.0, 0, 0.0])                              # date -> [sum, n, max]
    fallback = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))
    activity = {}                                                        # date -> dict from ActivitySummary
    sleep = defaultdict(list)                                            # night -> [(stage, start, end, source)]
    mindful = defaultdict(float)
    workouts = []

    context = ET.iterparse(stream, events=("start", "end"))
    root = None
    n = 0
    for event, el in context:
        if event == "start":
            if root is None:
                root = el
            continue
        tag = el.tag
        if tag == "Record":
            rtype = el.get("type", "")
            start = el.get("startDate")
            if not start:
                el.clear(); continue
            day = start[:10]
            src = el.get("sourceName", "")
            if rtype == Q + "HeartRate":
                v = fnum(el.get("value"))
                if v is not None:
                    h = hr[day]; h[0] += v; h[1] += 1; h[2] = max(h[2], v)
            elif rtype in SUM_TYPES:
                v = fnum(el.get("value"))
                if v is not None:
                    if rtype.endswith("DistanceWalkingRunning") and el.get("unit") == "mi":
                        v *= 1.609344
                    sums[SUM_TYPES[rtype]][day][src] += v
            elif rtype in MEAN_TYPES:
                v = fnum(el.get("value"))
                if v is not None:
                    field = MEAN_TYPES[rtype]
                    if field == "spo2" and v <= 1.0:
                        v *= 100.0
                    if field == "wrist_temp_c" and el.get("unit") == "degF":
                        v = (v - 32) * 5 / 9
                    means[field][day].append(v)
            elif rtype in LAST_TYPES:
                v = fnum(el.get("value"))
                if v is not None:
                    if rtype.endswith("BodyMass") and el.get("unit") == "lb":
                        v *= 0.45359237
                    when = parse_dt(start)
                    prev = lasts[LAST_TYPES[rtype]].get(day)
                    if prev is None or when >= prev[0]:
                        lasts[LAST_TYPES[rtype]][day] = (when, v)
            elif rtype in FALLBACK_SUM:
                v = fnum(el.get("value"))
                if v is not None:
                    if el.get("unit") == "kJ":
                        v /= 4.184
                    fallback[FALLBACK_SUM[rtype]][day][src] += v
            elif rtype == SLEEP_TYPE:
                end = el.get("endDate")
                if end:
                    s, e = parse_dt(start), parse_dt(end)
                    night = (e + dt.timedelta(hours=6)).date().isoformat()
                    stage = el.get("value", "").replace("HKCategoryValueSleepAnalysis", "")
                    sleep[night].append((stage, s, e, src))
            elif rtype == MINDFUL_TYPE:
                end = el.get("endDate")
                if end:
                    mindful[day] += (parse_dt(end) - parse_dt(start)).total_seconds() / 60.0
            el.clear()
        elif tag == "ActivitySummary":
            day = el.get("dateComponents")
            if day:
                kcal = fnum(el.get("activeEnergyBurned"))
                if kcal is not None and el.get("activeEnergyBurnedUnit") == "kJ":
                    kcal /= 4.184
                activity[day] = {"active_kcal": kcal,
                                 "exercise_min": fnum(el.get("appleExerciseTime")),
                                 "stand_hours": fnum(el.get("appleStandHours"))}
            el.clear()
        elif tag == "Workout":
            start, end = el.get("startDate"), el.get("endDate")
            if start:
                dur = fnum(el.get("duration"))
                unit = el.get("durationUnit", "min")
                if dur is not None and unit in ("s", "sec"):
                    dur /= 60.0
                elif dur is not None and unit in ("h", "hr"):
                    dur *= 60.0
                kcal = fnum(el.get("totalEnergyBurned"))
                dist = fnum(el.get("totalDistance"))
                if dist is not None and el.get("totalDistanceUnit") == "mi":
                    dist *= 1.609344
                avg_hr = max_hr = None
                for st in el.findall("WorkoutStatistics"):
                    st_type = st.get("type", "")
                    if st_type.endswith("ActiveEnergyBurned") and fnum(st.get("sum")) is not None:
                        kcal = fnum(st.get("sum"))
                        if st.get("unit") == "kJ":
                            kcal /= 4.184
                    elif "Distance" in st_type and fnum(st.get("sum")) is not None:
                        dist = fnum(st.get("sum"))
                        if st.get("unit") == "mi":
                            dist *= 1.609344
                        elif st.get("unit") == "m":
                            dist /= 1000.0
                    elif st_type.endswith("HeartRate"):
                        avg_hr, max_hr = fnum(st.get("average")), fnum(st.get("maximum"))
                workouts.append({
                    "date": start[:10], "start": start[:16], "end": (end or "")[:16],
                    "type": el.get("workoutActivityType", "").replace("HKWorkoutActivityType", ""),
                    "duration_min": rnd(dur), "active_kcal": rnd(kcal, 0), "distance_km": rnd(dist, 2),
                    "avg_hr": rnd(avg_hr, 0), "max_hr": rnd(max_hr, 0), "source": el.get("sourceName", ""),
                })
            el.clear()
        n += 1
        if root is not None and n % 20000 == 0:
            root.clear()

    def dedupe(per_source):
        watch = [v for s, v in per_source.items() if is_watch(s)]
        return sum(watch) if watch else max(per_source.values())

    days = defaultdict(dict)
    for field, by_day in sums.items():
        for day, per_source in by_day.items():
            days[day][field] = dedupe(per_source)
    for field, by_day in means.items():
        for day, vals in by_day.items():
            days[day][field] = statistics.fmean(vals)
    for field, by_day in lasts.items():
        for day, (_, v) in by_day.items():
            days[day][field] = v
    for day, (total, count, mx) in hr.items():
        if count:
            days[day]["hr_avg"], days[day]["hr_max"] = total / count, mx
    for field, by_day in fallback.items():
        for day, per_source in by_day.items():
            days[day][field] = dedupe(per_source)
    for day, vals in activity.items():            # Apple's own de-duplicated ring totals win
        for k, v in vals.items():
            if v is not None and not (v == 0 and days[day].get(k)):
                days[day][k] = v
    for day, mins in mindful.items():
        days[day]["mindful_min"] = mins
    for night, segs in sleep.items():
        asleep = [x for x in segs if x[0] in ASLEEP_STAGES]
        if any(is_watch(x[3]) for x in asleep):
            segs = [x for x in segs if is_watch(x[3])]
            asleep = [x for x in segs if x[0] in ASLEEP_STAGES]
        if not asleep:
            continue
        d = days[night]
        d["sleep_h"] = union_minutes([(s, e) for _, s, e, _ in asleep]) / 60.0
        for stage_key, col in (("core", "sleep_core_h"), ("deep", "sleep_deep_h"), ("rem", "sleep_rem_h")):
            iv = [(s, e) for st, s, e, _ in asleep if ASLEEP_STAGES[st] == stage_key]
            if iv:
                d[col] = union_minutes(iv) / 60.0
        awake = [(s, e) for st, s, e, _ in segs if st == "Awake"]
        if awake:
            d["awake_min"] = union_minutes(awake)
        d["sleep_start"] = min(s for _, s, _, _ in asleep).strftime("%H:%M")
        d["sleep_end"] = max(e for _, _, e, _ in asleep).strftime("%H:%M")
    return days, workouts, []


def open_apple_export(path):
    if path.lower().endswith(".zip"):
        zf = zipfile.ZipFile(path)
        names = [n for n in zf.namelist() if n.lower().endswith("export.xml") and "cda" not in n.lower()]
        if not names:
            raise ValueError("no export.xml inside the zip — is this Apple's Health export?")
        return zf.open(names[0])
    return open(path, "rb")


# ---------- lane 2: Health Auto Export JSON (provisional) ----------

HAE_SUM = {"step_count": "steps", "flights_climbed": "flights", "apple_exercise_time": "exercise_min",
           "apple_stand_hour": "stand_hours", "mindful_minutes": "mindful_min"}
HAE_MEAN = {"resting_heart_rate": "resting_hr", "heart_rate_variability": "hrv_ms",
            "walking_heart_rate_average": "walking_hr", "respiratory_rate": "resp_rate",
            "blood_oxygen_saturation": "spo2", "apple_sleeping_wrist_temperature": "wrist_temp_c"}
HAE_LAST = {"vo2_max": "vo2max", "weight_body_mass": "weight_kg"}


def pick(d, *names):
    low = {k.lower(): v for k, v in d.items()}
    for nme in names:
        if nme.lower() in low and low[nme.lower()] is not None:
            return low[nme.lower()]
    return None


def ingest_hae_json(path):
    doc = json.load(open(path, encoding="utf-8"))
    data = doc.get("data", doc)
    days, workouts, unknown = defaultdict(dict), [], set()
    acc_sum = defaultdict(lambda: defaultdict(float))
    acc_mean = defaultdict(lambda: defaultdict(list))
    for m in data.get("metrics", []) or []:
        name, units = m.get("name", ""), (m.get("units") or "")
        for p in m.get("data", []) or []:
            day = str(p.get("date", ""))[:10]
            if len(day) != 10:
                continue
            qty = fnum(pick(p, "qty"))
            if name in HAE_SUM and qty is not None:
                acc_sum[HAE_SUM[name]][day] += qty
            elif name == "walking_running_distance" and qty is not None:
                acc_sum["distance_km"][day] += qty * (1.609344 if units == "mi" else 1)
            elif name == "active_energy" and qty is not None:
                acc_sum["active_kcal"][day] += qty / (4.184 if units.lower() == "kj" else 1)
            elif name in HAE_MEAN and qty is not None:
                if name == "blood_oxygen_saturation" and qty <= 1.0:
                    qty *= 100
                acc_mean[HAE_MEAN[name]][day].append(qty)
            elif name in HAE_LAST and qty is not None:
                days[day][HAE_LAST[name]] = qty * (0.45359237 if units == "lb" else 1)
            elif name == "heart_rate":
                avg, mx = fnum(pick(p, "Avg", "qty")), fnum(pick(p, "Max"))
                if avg is not None:
                    acc_mean["hr_avg"][day].append(avg)
                if mx is not None:
                    days[day]["hr_max"] = max(mx, days[day].get("hr_max", 0))
            elif name == "sleep_analysis":
                core, deep, rem = (fnum(pick(p, k)) for k in ("core", "deep", "rem"))
                total = fnum(pick(p, "totalSleep", "asleep"))
                if total is None and any(v is not None for v in (core, deep, rem)):
                    total = sum(v or 0 for v in (core, deep, rem))
                if total is not None:
                    days[day]["sleep_h"] = days[day].get("sleep_h", 0) + total
                for v, col in ((core, "sleep_core_h"), (deep, "sleep_deep_h"), (rem, "sleep_rem_h")):
                    if v is not None:
                        days[day][col] = days[day].get(col, 0) + v
                awake = fnum(pick(p, "awake"))
                if awake is not None:
                    days[day]["awake_min"] = days[day].get("awake_min", 0) + awake * 60
                for key, col in (("sleepStart", "sleep_start"), ("sleepEnd", "sleep_end")):
                    v = pick(p, key)
                    if v:
                        days[day][col] = str(v)[11:16]
            else:
                unknown.add(name)
    for field, by_day in acc_sum.items():
        for day, v in by_day.items():
            days[day][field] = v
    for field, by_day in acc_mean.items():
        for day, vals in by_day.items():
            days[day][field] = statistics.fmean(vals)
    for w in data.get("workouts", []) or []:
        start = str(pick(w, "start") or "")
        if len(start) < 10:
            continue
        dur = fnum(pick(w, "duration"))
        if dur is not None:
            dur = dur / 60.0 if dur > 600 else dur          # seconds in current app versions
        def q(*names):
            v = pick(w, *names)
            return fnum(v.get("qty")) if isinstance(v, dict) else fnum(v)
        workouts.append({
            "date": start[:10], "start": start[:16], "end": str(pick(w, "end") or "")[:16],
            "type": pick(w, "name") or "", "duration_min": rnd(dur),
            "active_kcal": rnd(q("activeEnergyBurned", "activeEnergy"), 0),
            "distance_km": rnd(q("distance"), 2), "avg_hr": rnd(q("avgHeartRate"), 0),
            "max_hr": rnd(q("maxHeartRate"), 0), "source": "Health Auto Export",
        })
    return days, workouts, sorted(n for n in unknown if n)


# ---------- storage ----------

def load_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def save_daily(days, workouts, source_label, replace_all):
    os.makedirs(DATA, exist_ok=True)
    rows = {} if replace_all else {r["date"]: r for r in load_csv(DAILY_CSV)}
    old_w = [] if replace_all else load_csv(WORKOUT_CSV)
    touched = {w["date"] for w in workouts}
    all_w = [w for w in old_w if w["date"] not in touched] + workouts
    all_w.sort(key=lambda w: w["start"])
    by_day = defaultdict(list)
    for w in all_w:
        by_day[w["date"]].append(w)

    digits = {"steps": 0, "flights": 0, "active_kcal": 0, "exercise_min": 0, "stand_hours": 0,
              "resting_hr": 0, "hrv_ms": 0, "walking_hr": 0, "hr_avg": 0, "hr_max": 0, "awake_min": 0,
              "mindful_min": 0, "distance_km": 2, "sleep_h": 2, "sleep_core_h": 2, "sleep_deep_h": 2,
              "sleep_rem_h": 2, "vo2max": 1, "resp_rate": 1, "spo2": 1, "wrist_temp_c": 2, "weight_kg": 1}
    for day, vals in days.items():
        row = rows.get(day, {f: "" for f in DAILY_FIELDS})
        row["date"] = day
        for k, v in vals.items():
            if k in digits:
                row[k] = rnd(v, digits[k])
            elif k in DAILY_FIELDS:
                row[k] = v
        row["source"] = source_label
        rows[day] = row
    for day in set(rows) | set(by_day):
        row = rows.setdefault(day, {f: "" for f in DAILY_FIELDS} | {"date": day, "source": source_label})
        ws = by_day.get(day, [])
        row["workouts"] = len(ws) if ws else ""
        row["workout_min"] = rnd(sum(fnum(w["duration_min"]) or 0 for w in ws), 0) if ws else ""
        row["workout_types"] = "; ".join(sorted({w["type"] for w in ws})) if ws else ""

    with open(DAILY_CSV, "w", newline="", encoding="utf-8") as fh:
        wr = csv.DictWriter(fh, fieldnames=DAILY_FIELDS, extrasaction="ignore")
        wr.writeheader()
        for day in sorted(rows):
            wr.writerow(rows[day])
    with open(WORKOUT_CSV, "w", newline="", encoding="utf-8") as fh:
        wr = csv.DictWriter(fh, fieldnames=WORKOUT_FIELDS, extrasaction="ignore")
        wr.writeheader()
        wr.writerows(all_w)
    return len(days), len(workouts)


# ---------- summary ----------

def avg(rows, field):
    vals = [fnum(r.get(field)) for r in rows]
    vals = [v for v in vals if v is not None]
    return statistics.fmean(vals) if vals else None


def fmt(v, n=0, unit=""):
    return "—" if v is None else f"{v:,.{n}f}{unit}"


def delta(now, before, n=0):
    if now is None or before is None:
        return "—"
    d = round(now - before, n)
    return f"{d + 0.0:+,.{n}f}"


def write_summary(notes):
    rows = load_csv(DAILY_CSV)
    if not rows:
        return False
    rows.sort(key=lambda r: r["date"])
    last = dt.date.fromisoformat(rows[-1]["date"])

    def window(days_back, length):
        hi = last - dt.timedelta(days=days_back)
        lo = hi - dt.timedelta(days=length - 1)
        return [r for r in rows if lo.isoformat() <= r["date"] <= hi.isoformat()]

    w7, p7, w30, p30 = window(0, 7), window(7, 7), window(0, 30), window(30, 30)
    metrics = [("Steps", "steps", 0, ""), ("Active energy", "active_kcal", 0, " kcal"),
               ("Exercise", "exercise_min", 0, " min"), ("Stand hours", "stand_hours", 0, ""),
               ("Sleep", "sleep_h", 1, " h"), ("Deep sleep", "sleep_deep_h", 1, " h"),
               ("REM sleep", "sleep_rem_h", 1, " h"), ("Resting heart rate", "resting_hr", 0, " bpm"),
               ("HRV", "hrv_ms", 0, " ms"), ("VO2 max", "vo2max", 1, ""),
               ("Walking distance", "distance_km", 1, " km"), ("Weight", "weight_kg", 1, " kg")]
    out = ["# Health Summary", "",
           f"> Context: generated by `health_ingest.py` on {dt.date.today().isoformat()} from Apple Health data. "
           f"Coverage: {rows[0]['date']} → {rows[-1]['date']} ({len(rows):,} days). "
           "Trends and patterns only — this is not medical advice, and nothing here diagnoses anything. "
           "Full day-by-day table: `daily.csv`; every workout: `workouts.csv`.", "",
           "## Averages: last 7 and 30 days against the period before", "",
           "| Metric | Last 7 days | vs prior 7 | Last 30 days | vs prior 30 |", "|---|---|---|---|---|"]
    for label, field, n, unit in metrics:
        a7, b7, a30, b30 = avg(w7, field), avg(p7, field), avg(w30, field), avg(p30, field)
        if a7 is None and a30 is None:
            continue
        out.append(f"| {label} | {fmt(a7, n, unit)} | {delta(a7, b7, n)} | {fmt(a30, n, unit)} | {delta(a30, b30, n)} |")
    out += ["", "## Last 14 days", "",
            "| Date | Steps | Active kcal | Exercise min | Sleep h | Resting HR | HRV | Workouts |",
            "|---|---|---|---|---|---|---|---|"]
    for r in rows[-14:]:
        wk = f"{r['workout_types']} ({r['workout_min']} min)" if r.get("workouts") else "—"
        cells = [r["date"]] + [r.get(f) or "—" for f in ("steps", "active_kcal", "exercise_min", "sleep_h", "resting_hr", "hrv_ms")] + [wk]
        out.append("| " + " | ".join(str(c) for c in cells) + " |")
    ws = [w for w in load_csv(WORKOUT_CSV) if w["date"] >= (last - dt.timedelta(days=29)).isoformat()]
    out += ["", "## Workouts, last 30 days", ""]
    if ws:
        by_type = defaultdict(lambda: [0, 0.0])
        for w in ws:
            by_type[w["type"]][0] += 1
            by_type[w["type"]][1] += fnum(w["duration_min"]) or 0
        out += ["| Type | Sessions | Total minutes |", "|---|---|---|"]
        for t, (c, m) in sorted(by_type.items(), key=lambda kv: -kv[1][1]):
            out.append(f"| {t} | {c} | {m:,.0f} |")
    else:
        out.append("No workouts recorded in this window.")
    if notes:
        out += ["", "## Ingest notes", ""] + [f"- {n}" for n in notes]
    with open(SUMMARY, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out) + "\n")
    return True


# ---------- discovery + main ----------

def discover():
    home = os.path.expanduser("~")
    md = os.path.join(home, "Library", "Mobile Documents")
    found = glob.glob(os.path.join(home, "Downloads", "export*.zip"))
    for pat in ("com~apple~CloudDocs/AutoExport/**/*.json", "com~apple~CloudDocs/Health Auto Export/**/*.json",
                "*HealthExport*/Documents/**/*.json", "*/Documents/AutoExport/**/*.json"):
        found += glob.glob(os.path.join(md, pat), recursive=True)
    return sorted(set(found), key=os.path.getmtime)


def fingerprint(path):
    st = os.stat(path)
    return f"{st.st_size}:{int(st.st_mtime)}"


def ingest_file(path, notes):
    low = path.lower()
    if low.endswith(".json"):
        days, workouts, unknown = ingest_hae_json(path)
        label, replace_all = "Health Auto Export", False
        if unknown:
            notes.append(f"{os.path.basename(path)}: metric names not mapped yet: {', '.join(unknown)}")
    elif low.endswith(".zip") or low.endswith(".xml"):
        with open_apple_export(path) as stream:
            days, workouts, _ = ingest_apple_xml(stream)
        label, replace_all = "Apple export", True
    else:
        raise ValueError(f"unsupported file type: {path}")
    n_days, n_w = save_daily(days, workouts, label, replace_all)
    print(f"ingested {os.path.basename(path)}: {n_days:,} days, {n_w:,} workouts ({label})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file")
    ap.add_argument("--summary-only", action="store_true")
    args = ap.parse_args()
    os.makedirs(DATA, exist_ok=True)
    state = json.load(open(STATE)) if os.path.exists(STATE) else {"ingested": {}}
    notes = []
    if not args.summary_only:
        targets = [args.file] if args.file else [p for p in discover()
                                                 if state["ingested"].get(p) != fingerprint(p)]
        if not targets:
            print("no new export files found")
        for path in targets:
            try:
                ingest_file(path, notes)
                state["ingested"][path] = fingerprint(path)
            except (ET.ParseError, ValueError, zipfile.BadZipFile, json.JSONDecodeError, OSError) as exc:
                notes.append(f"could not read {os.path.basename(path)}: {exc}")
                print(notes[-1])
        json.dump(state, open(STATE, "w"), indent=1)
    if write_summary(notes):
        print(f"summary written: {SUMMARY}")
    else:
        print("no health data yet — nothing to summarise")
    return 0


if __name__ == "__main__":
    sys.exit(main())
