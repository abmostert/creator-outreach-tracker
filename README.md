# People Tracker (local-first)

A small, schema-driven CLI for tracking people and interactions over time.

- Local-first: JSON is the source of truth
- CSV import/export with section headers (e.g., "Seniors List", "Peers List")
- Append-safe notes for long-running relationship tracking
- Supports pasted profile text in a dedicated "LinkedIn Paste" field


## Design philosophy

This tool is intentionally simple.

It is designed as a lightweight, local-first CLI for managing personal networking and outreach workflows without forcing a database, web app, or proprietary platform.

Key principles:

- **Plain data**: JSON and CSV are the source of truth. You can inspect, edit, back up, and migrate your data at any time.
- **Low friction**: No external dependencies, no accounts, no services. If Python runs, the tool runs.
- **Human-in-the-loop**: The tool assists data entry and tracking, but never assumes perfect automation or scraping.
- **Privacy by default**: Personal contact data lives locally and is never transmitted.
- **Composable workflow**: The CLI is meant to integrate with how you already work (spreadsheets, editors, notes, LinkedIn).

This project deliberately avoids over-engineering. Complexity is added only when it clearly reduces cognitive load in day-to-day use.

## Quick start

Initialize a database from a CSV:

```bash
python3 people_tracker.py init --csv examples/people.csv --out people_master.json
```

Add a person:

```bash
python3 people_tracker.py add --db people_master.json
```

Export back to a CSV:

```bash
python3 people_tracker.py export --db people_master.json --out people_export.csv
```

List / search / edit:

```bash
python3 people_tracker.py list --db people_master.json
python3 people_tracker.py search --db people_master.json --name "alice"
python3 people_tracker.py edit --db people_master.json --id 1 --pick
python3 people_tracker.py show --db people_master.json --id 1 --all
```

## Current status and future direction

This project is actively used in real networking workflows.

The current focus is on:
- running the tool end-to-end in daily use
- identifying friction points in real interactions
- validating which features actually reduce mental overhead

Future development will be guided by usage rather than speculation. Likely areas of evolution include:

- greater schema independence (supporting alternative column sets and workflows)
- improved ergonomics for rapid updates after live interactions
- optional abstractions for different networking or outreach styles

Any future expansion will preserve the core principles of simplicity, transparency, and local data ownership.



## License
MIT
