# practice-tools

Small, standalone Python scripts behind a freelance content practice. Each one is a single file that uses the standard library only.

| Script | What it does |
|---|---|
| [`log_hours.py`](log_hours.py) | Time log by client. `log_hours.py 2h startup "homepage edits"` appends a row to `Time Log.csv` and prints week-to-date totals. `--summary week\|month\|all` prints totals. Client aliases map to category names at the top of the file. |
| [`health_ingest.py`](health_ingest.py) | Turns an Apple Health export (`export.zip`) and optional "Health Auto Export" JSON files into local tables (`daily.csv`, `workouts.csv`) plus a Markdown summary of 7- and 30-day trends. Fully offline. |
| [`remind.py`](remind.py) | Puts deadlines on your iPhone as Apple Reminders through AppleScript (synced by iCloud): one reminder 7 days and one 2 days before (configurable). `remind.py add --title … --deadline "YYYY-MM-DD HH:MM" --link URL [--days 7,2] [--dry-run]`. macOS only; asks once for Automation permission. |

## The problem they solve

Each removes one small, repeated chore that otherwise lives in someone's head: logging hours so capacity planning has real numbers, turning a phone's health export into something you can read, and not missing a competition deadline.

## Running them

```bash
python3 log_hours.py 1.5h agency "article research"
python3 log_hours.py --summary week
python3 health_ingest.py --summary-only
python3 remind.py add --title "Example hackathon" --deadline "2026-11-20 23:59" --link https://example.com --dry-run
```

## Notes

- `log_hours.py` can also add a line to an Obsidian daily note if `vault-daily-line.py` (from the `obsidian-vault-tooling` repo) is installed at `~/.claude/scripts/`. Without it, it just skips that step.
- `health_ingest.py` writes into a `data/` folder next to the script. Keep that folder out of git; the `.gitignore` here already does.
- The Health Auto Export JSON parser was only tested on synthetic data. It reports any metric names it doesn't recognise.

Built with Claude Code.
