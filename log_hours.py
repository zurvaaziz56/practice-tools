#!/usr/bin/env python3
"""Time log: record hours per client, and total them.

Say "log 2h startup homepage edits" and Claude runs:
    python3 log_hours.py 2h startup "homepage edits"
    python3 log_hours.py 90m agency "article 2 research" --date 2026-10-01
    python3 log_hours.py --summary week      # or: month, all

Hours accept 2 / 2h / 1.5 / 1h30 / 90m. Client names are matched to the roster's
canonical names (unknown names are kept as typed, with a warning).
Appends to "Time Log.csv" in this folder and adds one line to the vault daily note.
"""
import csv
import datetime as dt
import os
import re
import subprocess
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(HERE, "Time Log.csv")
FIELDS = ["date", "client", "hours", "task", "logged_at"]
DAILY_LINE = os.path.expanduser("~/.claude/scripts/vault-daily-line.py")

# alias -> (canonical name, vault note for a wikilink or None)
CLIENTS = {
    "startup": ("AI-orchestration startup", "AI-orchestration startup"),
    "agency": ("Content agency", "Content agency"),
    "newsletter": ("Leadership consultant", "Leadership consultant"),
    "consultant": ("Leadership consultant", "Leadership consultant"),
    "annotation": ("Data-annotation contract", None),
    "pr": ("PR agency", "PR agency"),
    "gtm": ("GTM consultancy", "GTM consultancy"),
    "linkedin": ("GTM consultancy", "GTM consultancy"),
    "proposal": ("Proposals & admin", None),
    "proposals": ("Proposals & admin", None),
    "admin": ("Proposals & admin", None),
}


def parse_hours(text):
    t = text.strip().lower().replace(" ", "")
    m = re.fullmatch(r"(\d+(?:\.\d+)?)h?", t)
    if m:
        return float(m.group(1))
    m = re.fullmatch(r"(\d+)h(\d+)m?", t)
    if m:
        return int(m.group(1)) + int(m.group(2)) / 60
    m = re.fullmatch(r"(\d+)m(?:in)?", t)
    if m:
        return int(m.group(1)) / 60
    raise ValueError(f"can't read hours from '{text}' — use 2, 2h, 1.5, 1h30 or 90m")


def match_client(text):
    key = " ".join(text.lower().split())
    if key in CLIENTS:
        return CLIENTS[key], True
    for alias, val in CLIENTS.items():
        if alias in key or (len(key) >= 3 and key in alias):
            return val, True
    return (text.strip(), None), False


def load():
    if not os.path.exists(LOG):
        return []
    with open(LOG, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def totals(rows, since):
    agg = defaultdict(float)
    for r in rows:
        if r["date"] >= since:
            agg[r["client"]] += float(r["hours"])
    return agg


def print_summary(period):
    today = dt.date.today()
    since = {"week": today - dt.timedelta(days=today.weekday()),
             "month": today.replace(day=1),
             "all": dt.date(2000, 1, 1)}[period]
    agg = totals(load(), since.isoformat())
    label = {"week": "this week (from Monday)", "month": "this month", "all": "all time"}[period]
    if not agg:
        print(f"no hours logged {label}")
        return
    print(f"Hours {label}:")
    for client, hrs in sorted(agg.items(), key=lambda kv: -kv[1]):
        print(f"  {client:<26} {hrs:5.1f} h")
    print(f"  {'Total':<26} {sum(agg.values()):5.1f} h")


def main(argv):
    args = argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if args[0] == "--summary":
        print_summary(args[1] if len(args) > 1 else "week")
        return 0
    date = dt.date.today()
    if "--date" in args:
        i = args.index("--date")
        date = dt.date.fromisoformat(args[i + 1])
        args = args[:i] + args[i + 2:]
    if len(args) < 2:
        print(__doc__)
        return 1
    hours = parse_hours(args[0])
    if not 0 < hours <= 16:
        print(f"{hours} h looks wrong for one entry — nothing logged")
        return 1
    (client, note), known = match_client(args[1])
    task = " ".join(" ".join(args[2:]).split())

    new = not os.path.exists(LOG)
    with open(LOG, "a", newline="", encoding="utf-8") as fh:
        wr = csv.DictWriter(fh, fieldnames=FIELDS)
        if new:
            wr.writeheader()
        wr.writerow({"date": date.isoformat(), "client": client, "hours": round(hours, 2),
                     "task": task, "logged_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M")})
    print(f"logged {hours:g} h — {client}" + (f" — {task}" if task else "") + f" ({date.isoformat()})")
    if not known:
        print(f"note: '{client}' is not a known client name — kept as typed")

    if os.path.exists(DAILY_LINE):
        who = f"[[{note}|{client}]]" if note else client
        text = f"{hours:g} h — {who}" + (f": {task}" if task else "")
        subprocess.run([sys.executable, DAILY_LINE, "--date", date.isoformat(), "time-log", text],
                       capture_output=True, text=True)
    print_summary("week")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except ValueError as exc:
        print(exc)
        sys.exit(1)
