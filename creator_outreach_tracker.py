#!/usr/bin/env python3
"""Creator outreach tracker (local-first, schema-driven).

Tracks online content creators for relationship-based outreach and guest-post
opportunities. JSON is the source of truth; CSV import/export is supported.

Examples:
  python3 creator_outreach_tracker.py add --db creator_outreach_master.json
  python3 creator_outreach_tracker.py list --db creator_outreach_master.json
  python3 creator_outreach_tracker.py search --db creator_outreach_master.json --query "Jane"
  python3 creator_outreach_tracker.py edit --db creator_outreach_master.json --id 1 --pick --editor
  python3 creator_outreach_tracker.py export --db creator_outreach_master.json --out creator_outreach.csv
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import re
import subprocess
import tempfile
from typing import Any, Dict, List, Optional, Tuple


DEFAULT_COLUMNS = [
    "First Name",
    "Last Name",
    "Publication / Site Name",
    "Site URL",
    "Creator / Profile URL",
    "Contact Channel",
    "Contact Details",
    "Topic / Niche",
    "Audience Traffic Tier",
    "Audience Fit",
    "Funnel Stage",
    "Date of Initial Outreach",
    "Date of Most Recent Interaction",
    "Next Action Date",
    "Currently Pursuing? (Y/N)",
    "Response (Positive/Negative/Neutral)",
    "Advice / Feedback Received",
    "Advice Implemented / Changes Made",
    "Guest Post Idea",
    "Guest Post Guidelines URL",
    "Guest Post URL",
    "Additional Notes",
]

# These stages are intentionally fixed to the user's outreach funnel.
# Blank is also allowed for people who have not entered the funnel yet.
FUNNEL_STAGES = (
    "punch1_sent",
    "punch1_reprompt_sent",
    "punch1_reply_received",
    "advice_implemented",
    "punch2_sent",
    "punch2_reply_received",
    "guest_post_permission_granted",
    "guest_post_declined",
    "guest_post_submitted",
    "guest_post_published",
    "closed_no_response",
)

TRAFFIC_TIERS = ("low", "medium", "high")
AUDIENCE_FITS = ("low", "medium", "high")
RESPONSE_VALUES = ("positive", "negative", "neutral")

DATE_FIELDS = {
    "Date of Initial Outreach",
    "Date of Most Recent Interaction",
    "Next Action Date",
}

CHOICE_FIELDS = {
    "Audience Traffic Tier": TRAFFIC_TIERS,
    "Audience Fit": AUDIENCE_FITS,
    "Funnel Stage": FUNNEL_STAGES,
    "Response (Positive/Negative/Neutral)": RESPONSE_VALUES,
}

AUTO_APPEND_FIELDS = {"Additional Notes"}


def now_iso() -> str:
    return dt.datetime.now().replace(microsecond=0).isoformat()


def now_stamp() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M")


def parse_yes_no(val: str) -> str:
    v = (val or "").strip().lower()
    if v in {"y", "yes", "true", "1"}:
        return "Y"
    if v in {"n", "no", "false", "0"}:
        return "N"
    return ""


def prompt(label: str, default: str = "") -> str:
    if default:
        value = input(f"{label} [{default}]: ").strip()
        return value if value else default
    return input(f"{label}: ").strip()


def prompt_choice(label: str, choices: Tuple[str, ...], default: str = "", allow_blank: bool = True) -> str:
    choice_text = "/".join(choices)
    while True:
        suffix = f" [{default}]" if default else ""
        value = input(f"{label} ({choice_text}){suffix}: ").strip().lower()
        if not value:
            if default:
                return default
            if allow_blank:
                return ""
        if value in choices:
            return value
        print(f"Invalid value. Choose one of: {', '.join(choices)}" + ("; or leave blank." if allow_blank else "."))


def parse_date_loose(s: str) -> str:
    """Return YYYY-MM-DD if parseable, otherwise the original text.

    UK-style DD/MM/YYYY is preferred for ambiguous slash-separated dates.
    """
    s0 = (s or "").strip()
    if not s0 or s0 in {"-", "--"}:
        return ""

    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s0):
        try:
            return dt.date.fromisoformat(s0).isoformat()
        except ValueError:
            return s0

    for fmt in ["%d/%m/%Y", "%d/%m/%y", "%Y/%m/%d", "%m/%d/%Y", "%m/%d/%y"]:
        try:
            return dt.datetime.strptime(s0, fmt).date().isoformat()
        except ValueError:
            pass

    return s0


def normalize_choice(value: str, choices: Tuple[str, ...]) -> str:
    v = (value or "").strip().lower()
    if not v:
        return ""
    if v not in choices:
        raise ValueError(f"Expected one of: {', '.join(choices)}")
    return v


def make_person_id(existing: List[Dict[str, Any]]) -> str:
    used = set()
    for record in existing:
        pid = str(record.get("id", ""))
        if re.fullmatch(r"P-\d{6}", pid):
            used.add(int(pid.split("-")[1]))
    n = 1
    while n in used:
        n += 1
    return f"P-{n:06d}"


def normalize_id(s: str) -> str:
    s = (s or "").strip().upper().replace(" ", "")
    if not s:
        return ""
    if s.isdigit():
        return f"P-{int(s):06d}"
    if s.startswith("P") and not s.startswith("P-"):
        s = "P-" + s[1:]
    return s


def load_db(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {
            "meta": {
                "version": "creator_outreach_db_v1",
                "created_at": now_iso(),
                "updated_at": now_iso(),
                "csv_columns": DEFAULT_COLUMNS,
            },
            "people": [],
        }
    with open(path, "r", encoding="utf-8") as f:
        db = json.load(f)
    db.setdefault("meta", {})
    db["meta"].setdefault("version", "creator_outreach_db_v1")
    db["meta"].setdefault("csv_columns", DEFAULT_COLUMNS)
    db.setdefault("people", [])
    return db


def save_db(path: str, db: Dict[str, Any]) -> None:
    db.setdefault("meta", {})
    db["meta"]["updated_at"] = now_iso()
    db["meta"]["csv_columns"] = DEFAULT_COLUMNS
    with open(path, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)


def get_fields(person: Dict[str, Any]) -> Dict[str, Any]:
    fields = person.get("fields")
    return fields if isinstance(fields, dict) else {}


def blank_record() -> Dict[str, str]:
    return {column: "" for column in DEFAULT_COLUMNS}


def dedupe_key(fields: Dict[str, Any]) -> str:
    """Conservative duplicate key using identity/contact information."""
    parts = [
        fields.get("First Name", ""),
        fields.get("Last Name", ""),
        fields.get("Publication / Site Name", ""),
        fields.get("Creator / Profile URL", ""),
        fields.get("Contact Details", ""),
    ]
    return "|".join(str(x or "").strip().lower() for x in parts)


def find_person_index(people: List[Dict[str, Any]], person_id: str) -> int:
    target = normalize_id(person_id)
    for i, person in enumerate(people):
        if normalize_id(str(person.get("id", ""))) == target:
            return i
    return -1


def edit_in_editor(initial_text: str) -> str:
    """Edit arbitrary field text using $EDITOR, falling back to nano."""
    editor = os.environ.get("EDITOR") or "nano"
    with tempfile.NamedTemporaryFile(mode="w+", suffix=".txt", delete=False, encoding="utf-8") as tf:
        path = tf.name
        tf.write(initial_text or "")
        tf.flush()
    try:
        subprocess.run([editor, path], check=False)
        with open(path, "r", encoding="utf-8") as f:
            return f.read().rstrip("\n")
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def normalize_field_value(field: str, value: str) -> str:
    if field == "Currently Pursuing? (Y/N)":
        return parse_yes_no(value)
    if field in DATE_FIELDS:
        return parse_date_loose(value)
    if field in CHOICE_FIELDS:
        return normalize_choice(value, CHOICE_FIELDS[field])
    return value


def import_from_csv(csv_path: str) -> List[Dict[str, Any]]:
    last_err: Optional[Exception] = None
    for enc in ("utf-8-sig", "latin-1"):
        try:
            people: List[Dict[str, Any]] = []
            with open(csv_path, newline="", encoding=enc) as f:
                reader = csv.DictReader(f)
                if reader.fieldnames is None:
                    return []
                for row in reader:
                    if not any((value or "").strip() for value in row.values()):
                        continue
                    fields = blank_record()
                    for column in DEFAULT_COLUMNS:
                        fields[column] = (row.get(column) or "").strip()
                    for field in DATE_FIELDS:
                        fields[field] = parse_date_loose(fields[field])
                    fields["Currently Pursuing? (Y/N)"] = parse_yes_no(fields["Currently Pursuing? (Y/N)"])
                    for field, choices in CHOICE_FIELDS.items():
                        raw = fields[field].strip().lower()
                        fields[field] = raw if raw in choices else ""
                    people.append(
                        {
                            "id": "",
                            "fields": fields,
                            "created_at": now_iso(),
                            "updated_at": now_iso(),
                        }
                    )
            return people
        except UnicodeDecodeError as exc:
            last_err = exc
    raise last_err if last_err else RuntimeError("Failed to read CSV")


def pretty_print_person(person: Dict[str, Any], show_blanks: bool = False, max_chars: Optional[int] = 240) -> None:
    fields = get_fields(person)
    print(f"ID: {person.get('id', '')}")
    print(f"Name: {fields.get('First Name', '')} {fields.get('Last Name', '')}".rstrip())
    print("-" * 70)
    for field in DEFAULT_COLUMNS:
        value = fields.get(field, "")
        if not (value or show_blanks):
            continue
        text = "" if value is None else str(value)
        if max_chars is not None and len(text) > max_chars:
            text = text[:max_chars] + f"... [truncated, {len(str(value))} chars total]"
        print(f"{field}: {text}")


def cmd_init(args: argparse.Namespace) -> int:
    imported = import_from_csv(args.csv)
    db = load_db(args.out)
    people: List[Dict[str, Any]] = db.get("people", [])

    existing_keys = {dedupe_key(get_fields(p)): p for p in people}
    added = 0
    updated = 0

    for incoming in imported:
        fields = get_fields(incoming)
        key = dedupe_key(fields)
        if key in existing_keys:
            existing = existing_keys[key]
            existing_fields = get_fields(existing)
            for column in DEFAULT_COLUMNS:
                if not str(existing_fields.get(column, "") or "").strip() and str(fields.get(column, "") or "").strip():
                    existing_fields[column] = fields[column]
            existing["updated_at"] = now_iso()
            updated += 1
        else:
            incoming["id"] = make_person_id(people)
            people.append(incoming)
            existing_keys[key] = incoming
            added += 1

    db["people"] = people
    save_db(args.out, db)
    print(f"Initialized DB: {args.out} (added {added}, updated {updated}, total {len(people)})")
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    db = load_db(args.db)
    people: List[Dict[str, Any]] = db.get("people", [])
    fields = blank_record()

    fields["First Name"] = prompt("First Name")
    fields["Last Name"] = prompt("Last Name")
    fields["Publication / Site Name"] = prompt("Publication / Site Name")
    fields["Site URL"] = prompt("Site URL")
    fields["Creator / Profile URL"] = prompt("Creator / Profile URL")
    fields["Contact Channel"] = prompt("Contact Channel (e.g. email, X, LinkedIn, contact form)")
    fields["Contact Details"] = prompt("Contact Details (email, handle, or contact URL)")
    fields["Topic / Niche"] = prompt("Topic / Niche")
    fields["Audience Traffic Tier"] = prompt_choice("Audience Traffic Tier", TRAFFIC_TIERS)
    fields["Audience Fit"] = prompt_choice("Audience Fit", AUDIENCE_FITS)
    fields["Currently Pursuing? (Y/N)"] = parse_yes_no(prompt("Currently Pursuing? (Y/N)", "Y"))
    fields["Funnel Stage"] = prompt_choice("Funnel Stage", FUNNEL_STAGES, allow_blank=True)
    fields["Date of Initial Outreach"] = parse_date_loose(prompt("Date of Initial Outreach (optional, e.g. 2026-09-12)"))
    fields["Date of Most Recent Interaction"] = parse_date_loose(prompt("Date of Most Recent Interaction (optional)"))
    fields["Next Action Date"] = parse_date_loose(prompt("Next Action Date (optional)"))
    fields["Response (Positive/Negative/Neutral)"] = prompt_choice(
        "Response", RESPONSE_VALUES, allow_blank=True
    )
    fields["Advice / Feedback Received"] = prompt("Advice / Feedback Received (optional)")
    fields["Advice Implemented / Changes Made"] = prompt("Advice Implemented / Changes Made (optional)")
    fields["Guest Post Idea"] = prompt("Guest Post Idea (optional)")
    fields["Guest Post Guidelines URL"] = prompt("Guest Post Guidelines URL (optional)")
    fields["Guest Post URL"] = prompt("Guest Post URL (optional)")
    fields["Additional Notes"] = prompt("Additional Notes (optional)")

    key = dedupe_key(fields)
    if any(dedupe_key(get_fields(p)) == key for p in people):
        print("Looks like this person already exists. Not adding duplicate.")
        return 2

    person_id = make_person_id(people)
    person = {
        "id": person_id,
        "fields": fields,
        "created_at": now_iso(),
        "updated_at": now_iso(),
    }
    people.append(person)
    db["people"] = people
    save_db(args.db, db)
    print(f"Added {person_id}: {fields['First Name']} {fields['Last Name']}".rstrip())
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    db = load_db(args.db)
    people: List[Dict[str, Any]] = db.get("people", [])

    def sort_key(person: Dict[str, Any]) -> Tuple[Any, ...]:
        fields = get_fields(person)
        pursuing = fields.get("Currently Pursuing? (Y/N)", "").upper() == "Y"
        return (
            not pursuing,
            fields.get("Last Name", ""),
            fields.get("First Name", ""),
        )

    people = sorted(people, key=sort_key)
    with open(args.out, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=DEFAULT_COLUMNS)
        writer.writeheader()
        for person in people:
            fields = get_fields(person)
            writer.writerow({column: fields.get(column, "") for column in DEFAULT_COLUMNS})

    print(f"Exported CSV: {args.out}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    db = load_db(args.db)
    rows: List[Dict[str, Any]] = list(db.get("people", []))

    if args.stage:
        rows = [p for p in rows if get_fields(p).get("Funnel Stage", "") == args.stage]
    if args.traffic:
        rows = [p for p in rows if get_fields(p).get("Audience Traffic Tier", "") == args.traffic]
    if args.fit:
        rows = [p for p in rows if get_fields(p).get("Audience Fit", "") == args.fit]
    if args.pursuing:
        wanted = parse_yes_no(args.pursuing)
        rows = [p for p in rows if get_fields(p).get("Currently Pursuing? (Y/N)", "") == wanted]

    if args.due is not None:
        cutoff = dt.date.today() + dt.timedelta(days=args.due)
        filtered: List[Dict[str, Any]] = []
        for person in rows:
            value = str(get_fields(person).get("Next Action Date", "") or "").strip()
            if not value:
                continue
            try:
                action_date = dt.date.fromisoformat(value)
            except ValueError:
                continue
            if action_date <= cutoff:
                filtered.append(person)
        rows = filtered

    rows.sort(
        key=lambda p: (
            get_fields(p).get("Next Action Date", "") or "9999-99-99",
            get_fields(p).get("Last Name", ""),
            get_fields(p).get("First Name", ""),
        )
    )

    for person in rows:
        fields = get_fields(person)
        name = f"{fields.get('First Name', '')} {fields.get('Last Name', '')}".strip()
        print(
            f"{person.get('id', ''):8} | {name:28.28} | "
            f"{fields.get('Publication / Site Name', ''):24.24} | "
            f"Traffic:{fields.get('Audience Traffic Tier', ''):6} | "
            f"Fit:{fields.get('Audience Fit', ''):6} | "
            f"Stage:{fields.get('Funnel Stage', ''):30.30} | "
            f"Next:{fields.get('Next Action Date', ''):10} | "
            f"Pursuing:{fields.get('Currently Pursuing? (Y/N)', '')}"
        )

    print(f"{len(rows)} result(s)")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    db = load_db(args.db)
    people: List[Dict[str, Any]] = db.get("people", [])
    idx = find_person_index(people, args.id)
    if idx < 0:
        print(f"Person not found: {args.id}")
        return 2

    if args.field:
        if args.field not in DEFAULT_COLUMNS:
            print(f"Unknown field: {args.field}")
            return 2
        print(f"{args.field}: {get_fields(people[idx]).get(args.field, '')}")
        return 0

    pretty_print_person(people[idx], show_blanks=args.all, max_chars=None if args.full else 240)
    return 0


def cmd_edit(args: argparse.Namespace) -> int:
    db = load_db(args.db)
    people: List[Dict[str, Any]] = db.get("people", [])
    idx = find_person_index(people, args.id)
    if idx < 0:
        print(f"Person not found: {args.id}")
        return 2

    person = people[idx]
    fields = get_fields(person)

    if not args.pick and not args.field:
        print("Provide --field, or use --pick.")
        return 2

    if args.pick:
        print("Choose a field to edit:")
        for i, column in enumerate(DEFAULT_COLUMNS, start=1):
            print(f"{i:2d}. {column}")
        choice = input("Field number: ").strip()
        if not choice:
            print("Cancelled.")
            return 0
        try:
            n = int(choice)
            if not 1 <= n <= len(DEFAULT_COLUMNS):
                raise ValueError
        except ValueError:
            print("Invalid field number.")
            return 2
        field = DEFAULT_COLUMNS[n - 1]
    else:
        field = args.field

    if field not in DEFAULT_COLUMNS:
        print(f"Unknown field: {field}")
        print("Valid fields include:")
        for column in DEFAULT_COLUMNS:
            print(f" - {column}")
        return 2

    current = str(fields.get(field, "") or "")
    preview = current.replace("\n", " ")
    if len(preview) > 100:
        preview = preview[:100] + "…"
    print(f"Editing field: {field}")
    print(f"Current value: {preview}")

    if args.editor:
        value = edit_in_editor(current)
    elif args.value is not None:
        value = args.value
    else:
        value = prompt(f"New value for '{field}'", current)

    try:
        value = normalize_field_value(field, value)
    except ValueError as exc:
        print(f"Invalid value for {field}: {exc}")
        return 2

    # Additional Notes append by default for command-line edits, preserving history.
    # Editor mode always edits the entire field directly.
    should_append = args.append or (field in AUTO_APPEND_FIELDS and not args.editor and not args.replace)
    if should_append:
        old = current.rstrip()
        new = (value or "").strip()
        if new and field in AUTO_APPEND_FIELDS and not args.editor:
            new = f"{now_stamp()}: {new}"
        if old and new:
            value = old + "\n" + new
        elif old:
            value = old
        else:
            value = new

    fields[field] = value
    person["fields"] = fields
    person["updated_at"] = now_iso()
    people[idx] = person
    db["people"] = people
    save_db(args.db, db)

    print(f"Updated {person.get('id', args.id)}: {field}")
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    db = load_db(args.db)
    people: List[Dict[str, Any]] = db.get("people", [])
    query = (args.query or "").strip().lower()
    if not query:
        print("Provide --query, e.g. --query 'Jane' or --query 'energy'.")
        return 2

    tokens = [token for token in re.split(r"\s+", query) if token]
    matches: List[Dict[str, Any]] = []

    for person in people:
        fields = get_fields(person)
        haystack = " ".join(
            [str(person.get("id", ""))]
            + [str(fields.get(column, "") or "") for column in DEFAULT_COLUMNS]
        ).lower()
        if all(token in haystack for token in tokens):
            matches.append(person)

    if not matches:
        print("No matches.")
        return 0

    matches.sort(
        key=lambda p: (
            get_fields(p).get("Last Name", ""),
            get_fields(p).get("First Name", ""),
        )
    )

    for i, person in enumerate(matches, start=1):
        fields = get_fields(person)
        name = f"{fields.get('First Name', '')} {fields.get('Last Name', '')}".strip()
        print(
            f"[{i}] {person.get('id', ''):8} | {name:28.28} | "
            f"{fields.get('Publication / Site Name', ''):24.24} | "
            f"{fields.get('Funnel Stage', '')}"
        )

    print(f"{len(matches)} match(es)")
    choice = input("Select number to show (or press Enter to cancel): ").strip()
    if not choice:
        return 0
    try:
        selected_index = int(choice) - 1
        if not 0 <= selected_index < len(matches):
            raise ValueError
    except ValueError:
        print("Invalid selection.")
        return 2

    pretty_print_person(matches[selected_index])
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Creator Outreach Tracker (local-first; JSON source of truth)"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    s_init = sub.add_parser("init", help="Initialize/merge a JSON DB from an existing CSV")
    s_init.add_argument("--csv", required=True, help="Path to CSV")
    s_init.add_argument("--out", required=True, help="Output JSON DB path")
    s_init.set_defaults(func=cmd_init)

    s_add = sub.add_parser("add", help="Interactively add a creator")
    s_add.add_argument("--db", required=True, help="Path to JSON DB")
    s_add.set_defaults(func=cmd_add)

    s_export = sub.add_parser("export", help="Export the database to CSV")
    s_export.add_argument("--db", required=True, help="Path to JSON DB")
    s_export.add_argument("--out", required=True, help="Output CSV path")
    s_export.set_defaults(func=cmd_export)

    s_list = sub.add_parser("list", help="List creators with optional filters")
    s_list.add_argument("--db", required=True, help="Path to JSON DB")
    s_list.add_argument("--stage", choices=FUNNEL_STAGES, help="Filter by funnel stage")
    s_list.add_argument("--traffic", choices=TRAFFIC_TIERS, help="Filter by traffic tier")
    s_list.add_argument("--fit", choices=AUDIENCE_FITS, help="Filter by audience fit")
    s_list.add_argument("--pursuing", choices=("Y", "N", "y", "n", "yes", "no"), help="Filter by pursuing status")
    s_list.add_argument(
        "--due",
        type=int,
        default=None,
        metavar="DAYS",
        help="Show records with Next Action Date due by today + DAYS (use 0 for today/overdue)",
    )
    s_list.set_defaults(func=cmd_list)

    s_show = sub.add_parser("show", help="Show a single creator record")
    s_show.add_argument("--db", required=True, help="Path to JSON DB")
    s_show.add_argument("--id", required=True, help="Person ID (e.g. P-000014 or 14)")
    s_show.add_argument("--all", action="store_true", help="Show blank fields too")
    s_show.add_argument("--full", action="store_true", help="Do not truncate long fields")
    s_show.add_argument("--field", help="Show only one exact field")
    s_show.set_defaults(func=cmd_show)

    s_edit = sub.add_parser("edit", help="Edit any field for a creator")
    s_edit.add_argument("--db", required=True, help="Path to JSON DB")
    s_edit.add_argument("--id", required=True, help="Person ID (e.g. P-000014 or 14)")
    s_edit.add_argument("--field", help="Exact field name")
    s_edit.add_argument("--value", default=None, help="New value (omit for interactive prompt)")
    s_edit.add_argument(
        "--editor",
        action="store_true",
        help="Edit the selected field in $EDITOR (or nano); works for every field",
    )
    s_edit.add_argument("--pick", action="store_true", help="Pick the field from a numbered list")
    s_edit.add_argument("--append", action="store_true", help="Append to the existing field value")
    s_edit.add_argument(
        "--replace",
        action="store_true",
        help="Replace existing value (overrides the default append behaviour for Additional Notes)",
    )
    s_edit.set_defaults(func=cmd_edit)

    s_search = sub.add_parser("search", help="Search across all creator fields")
    s_search.add_argument("--db", required=True, help="Path to JSON DB")
    s_search.add_argument("--query", "--name", dest="query", required=True, help="Search text")
    s_search.set_defaults(func=cmd_search)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
