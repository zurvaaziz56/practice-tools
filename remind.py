#!/usr/bin/env python3
"""Put opportunity deadlines on your iPhone as Apple Reminders (synced through iCloud).

  remind.py check
  remind.py add --title "Devpost: Example Hackathon" --deadline "2026-10-20 23:59" --link URL
                [--days 7,2] [--hour 10] [--dry-run]

Creates one reminder per offset in the Reminders list "Opportunities", at --hour (default
10:00, Mac local time) on each chosen day before the deadline. Offsets that are already
in the past are skipped. The deadline is given in local time. Needs macOS Automation permission for
this app to control Reminders (asked once; an app update can reset it — re-allow it in
System Settings > Privacy & Security > Automation).
"""
import argparse
import datetime as dt
import subprocess
import sys

LIST = "Opportunities"


def esc(text):
    """Quote-safe AppleScript string body; line breaks become linefeed joins."""
    text = text.replace("\\", "\\\\").replace('"', '\\"')
    return text.replace("\n", '" & linefeed & "')


def osa(script):
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=90)
    except subprocess.TimeoutExpired:
        return 1, "", "timed out — a macOS permission prompt is probably waiting on screen"
    return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()


def explain(err):
    if "-1743" in err or "not authorized" in err.lower() or "not allowed" in err.lower():
        return ("macOS blocked access to Reminders. Allow it: System Settings > Privacy & Security > "
                "Automation > Claude > Reminders (turn on), then run again.")
    return err


def date_block(var, when):
    """AppleScript date built from components, so it never depends on the Mac's locale."""
    return "\n".join([
        f"set {var} to current date",
        f"set day of {var} to 1",
        f"set year of {var} to {when.year}",
        f"set month of {var} to {when.month}",
        f"set day of {var} to {when.day}",
        f"set hours of {var} to {when.hour}",
        f"set minutes of {var} to {when.minute}",
        f"set seconds of {var} to 0",
    ])


def parse_deadline(text):
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            d = dt.datetime.strptime(text.strip(), fmt)
            return d.replace(hour=23, minute=59) if fmt == "%Y-%m-%d" else d
        except ValueError:
            continue
    raise SystemExit(f"can't read deadline '{text}' — use YYYY-MM-DD HH:MM (local time)")


def cmd_check():
    code, out, err = osa('tell application "Reminders" to get name of every list')
    if code != 0:
        print("NOT READY:", explain(err))
        return 1
    names = [n.strip() for n in out.split(",") if n.strip()]
    print(f"ready — Reminders reachable ({len(names)} lists; "
          f"'{LIST}' {'exists' if LIST in names else 'will be created on first use'})")
    return 0


def cmd_add(args):
    deadline = parse_deadline(args.deadline)
    now = dt.datetime.now()
    offsets = sorted({int(x) for x in args.days.split(",") if x.strip()}, reverse=True)
    planned = []
    for d in offsets:
        when = (deadline - dt.timedelta(days=d)).replace(hour=args.hour, minute=0)
        if when > now:
            planned.append((d, when))
    if not planned:
        print("nothing to schedule — every reminder date is already in the past")
        return 0
    lines = [
        'tell application "Reminders"',
        f'  if not (exists list "{LIST}") then make new list with properties {{name:"{LIST}"}}',
        "end tell",
    ]
    body = f"Deadline: {deadline:%a %d %b %Y, %H:%M} (local time)\n{args.link}".strip()
    for i, (d, when) in enumerate(planned):
        var = f"r{i}"
        name = f"{args.title} — {d} day{'s' if d != 1 else ''} left"
        lines.append(date_block(var, when))
        lines += [
            'tell application "Reminders"',
            f'  tell list "{LIST}"',
            f'    make new reminder with properties {{name:"{esc(name)}", body:"{esc(body)}", '
            f'remind me date:{var}, due date:{var}}}',
            "  end tell",
            "end tell",
        ]
    script = "\n".join(lines)
    if args.dry_run:
        print(script)
        return 0
    code, _, err = osa(script)
    if code != 0:
        print("FAILED:", explain(err))
        return 1
    for d, when in planned:
        print(f"reminder set: {when:%a %d %b %Y %H:%M} local time ({d} days before)")
    return 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    a = sub.add_parser("add")
    a.add_argument("--title", required=True)
    a.add_argument("--deadline", required=True, help="local time, YYYY-MM-DD HH:MM")
    a.add_argument("--link", default="")
    a.add_argument("--days", default="7,2")
    a.add_argument("--hour", type=int, default=10)
    a.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    return cmd_check() if args.cmd == "check" else cmd_add(args)


if __name__ == "__main__":
    sys.exit(main())
