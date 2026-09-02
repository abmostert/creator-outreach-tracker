#!/usr/bin/env python3
"""People tracker (local-first, schema-driven).

- Stores a master JSON file as source of truth.
- Imports/exports a sectioned CSV format with group headers (e.g. "Seniors List", "Peers List").
- Supports contacts sourced from LinkedIn OR email/other channels.
- Stores pasted profile text (e.g. LinkedIn) in dedicated fields for later reference.

Usage:
  python people_tracker.py init --csv "people.csv" --out people_master.json
  python people_tracker.py add --db people_master.json
  python people_tracker.py export --db people_master.json --out people_export.csv
  python people_tracker.py list --db people_master.json --group seniors --due 14
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


def open_text(path: str):
    """Open a text file with a best-effort encoding strategy.

    CSVs can contain non-UTF8 characters (common with names).
    We probe UTF-8 (with BOM) and fall back to Latin-1 if decoding fails.
    """
    f = open(path, "r", newline="", encoding="utf-8-sig")
    try:
        f.read(4096)
        f.seek(0)
        return f
    except UnicodeDecodeError:
        try:
            f.close()
        except Exception:
            pass
        return open(path, "r", newline="", encoding="latin-1")


# Public-facing schema
DEFAULT_COLUMNS = [
    "First Name",
    "Last Name",
    "Relation to me",
    "Data of initial outreach",
    "Date of most recent interaction",
    "Currently Persuing? (Y/N)",
    "Networking Channel",
    "Links/Info",
    "LinkedIn Paste",
    "Company",
    "Seniority",
    "Job Title",
    "Full Time/Contract/Internship",
    "Track",
    "Engagement Stage",
    "Response (Positive/Negative/Neutral)",
    "Salary",
    "Informational Interview Date",
    "Shared resume?",
    "Referral?",
    "Networking Recommendations",
    "Additional Notes",
]

# Internal group keys (kept stable for backward compatibility)
SECTION_SENIORS = "hero"   # legacy internal key
SECTION_PEERS = "tribe"    # legacy internal key


def now_iso() -> str:
    return dt.datetime.now().replace(microsecond=0).isoformat()


def now_stamp() -> str:
    # Local time; nice human log stamp
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M")


def parse_yes_no(val: str) -> str:
    v = (val or "").strip().lower()
    if v in {"y", "yes", "true", "1"}:
        return "Y"
    if v in {"n", "no", "false", "0"}:
        return "N"
    return ""  # allow blank / unknown


def prompt(label: str, default: str = "") -> str:
    """Prompt for a single-line input with an optional default."""
    if default:
        value = input(f"{label} [{default}]: ").strip()
        return value if value else default
    return input(f"{label}: ").strip()


def prompt_multiline(label: str) -> str:
    """
    Robust multi-line paste input.

    User ends input by typing a sentinel line: <<END>>
    This avoids accidental termination due to trailing blank lines in clipboard pastes.
    """
    print(label)
    print("Paste text. When finished, type <<END>> on its own line and press Enter.")
    lines = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if line.strip() == "<<END>>":
            break
        lines.append(line)
    # Strip leading/trailing blank lines but preserve internal formatting
    return "\n".join(lines).strip("\n")


def parse_date_loose(s: str) -> str:
    """Return YYYY-MM-DD if parseable, else original string."""
    s0 = (s or "").strip()
    if not s0 or s0 in {"-", "--"}:
        return ""

    # Already ISO
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s0):
        return s0

    # Common US/UK style: M/D/YYYY or MM/DD/YYYY
    for fmt in ["%m/%d/%Y", "%m/%d/%y", "%d/%m/%Y", "%d/%m/%y", "%Y/%m/%d"]:
        try:
            d = dt.datetime.strptime(s0, fmt).date()
            return d.isoformat()
        except ValueError:
            pass

    # Try to pull a date substring
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{2,4})", s0)
    if m:
        mm, dd, yy = m.groups()
        if len(yy) == 2:
            yy = "20" + yy
        try:
            d = dt.date(int(yy), int(mm), int(dd))
            return d.isoformat()
        except ValueError:
            return s0

    return s0


def parse_header_block(text: str) -> Dict[str, str]:
    """
    Best-effort parsing of a pasted profile header block.
    Never raises, never overwrites user input.
    """
    result: Dict[str, str] = {}
    if not text:
        return result

    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if not lines:
        return result

    # First line is often the headline
    result["Headline"] = lines[0]

    # Heuristic: look for "Title | Company" or "Title at Company"
    m = re.search(r"(.+?)\s+\|\s+(.+)", lines[0])
    if m:
        result["Job Title"] = m.group(1).strip()
        result["Company"] = m.group(2).strip()
        return result

    m = re.search(r"(.+?)\s+at\s+(.+)", lines[0], re.IGNORECASE)
    if m:
        result["Job Title"] = m.group(1).strip()
        result["Company"] = m.group(2).strip()

    return result


def normalize_group(s: str) -> str:
    """Map user-facing group labels to internal keys."""
    s2 = (s or "").strip().lower()
    # Preferred labels
    if s2 in {"seniors", "senior", "seniors list"}:
        return SECTION_SENIORS
    if s2 in {"peers", "peer", "peers list"}:
        return SECTION_PEERS
    # Legacy aliases
    if s2 in {"hero", "heroes", "hero list"}:
        return SECTION_SENIORS
    if s2 in {"tribe", "my tribe", "tribe list"}:
        return SECTION_PEERS
    raise ValueError(f"Unknown group: {s}")


def group_label(internal_key: str) -> str:
    """Display label for internal group keys."""
    return "seniors" if internal_key == SECTION_SENIORS else "peers" if internal_key == SECTION_PEERS else (internal_key or "")


def make_person_id(existing: List[Dict[str, Any]]) -> str:
    used = set()
    for r in existing:
        pid = r.get("id", "")
        if isinstance(pid, str) and re.fullmatch(r"P-\d{6}", pid):
            used.add(int(pid.split("-")[1]))
    n = 1
    while n in used:
        n += 1
    return f"P-{n:06d}"


def load_db(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {
            "meta": {
                "version": "people_db_v1",
                "created_at": now_iso(),
                "updated_at": now_iso(),
                "csv_columns": DEFAULT_COLUMNS,
            },
            "people": [],
        }
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_db(path: str, db: Dict[str, Any]) -> None:
    db.setdefault("meta", {})
    db["meta"]["updated_at"] = now_iso()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)


def merge_columns(csv_cols: List[str]) -> List[str]:
    """Preserve CSV order, but append any missing DEFAULT_COLUMNS."""
    cols = list(csv_cols) if csv_cols else []
    seen = set(cols)
    for c in DEFAULT_COLUMNS:
        if c not in seen:
            cols.append(c)
            seen.add(c)
    return cols


def row_is_blank(row: List[str]) -> bool:
    return all((c or "").strip() == "" for c in row)


def detect_section(first_cell: str) -> Optional[str]:
    """Detect group headers in the CSV body (supports preferred + legacy labels)."""
    t = (first_cell or "").strip().lower()
    if t in {"seniors list", "senior list"}:
        return SECTION_SENIORS
    if t in {"peers list", "peer list"}:
        return SECTION_PEERS
    # Legacy section headers
    if t == "hero list":
        return SECTION_SENIORS
    if t == "tribe list":
        return SECTION_PEERS
    return None


def dedupe_key(rec: Dict[str, Any]) -> str:
    """A conservative dedupe key: networking channel + link/info + name."""
    ch = (rec.get("Networking Channel") or "").strip().lower()
    link = (rec.get("Links/Info") or "").strip().lower()
    fn = (rec.get("First Name") or "").strip().lower()
    ln = (rec.get("Last Name") or "").strip().lower()
    return "|".join([ch, link, fn, ln])


def import_from_csv(csv_path: str) -> Tuple[List[str], List[Dict[str, Any]]]:
    """Import a sectioned CSV with Seniors/Peers group headers.

    The CSV may contain non-UTF8 characters. We try UTF-8 (with BOM),
    and if decoding fails during read, we retry using Latin-1.
    """
    last_err: Optional[Exception] = None
    for enc in ("utf-8-sig", "latin-1"):
        try:
            people: List[Dict[str, Any]] = []
            with open(csv_path, newline="", encoding=enc) as f:
                reader = csv.reader(f)
                header = next(reader)

                # keep only named columns up to last non-empty header
                last_named = 0
                for i, h in enumerate(header):
                    if (h or "").strip():
                        last_named = i + 1

                cols = header[:last_named] if last_named else DEFAULT_COLUMNS
                cols = merge_columns(cols)
                cols = cols[: len(DEFAULT_COLUMNS)]  # operate on canonical columns only

                current_section = SECTION_SENIORS
                for row in reader:
                    if row_is_blank(row):
                        continue

                    sec = detect_section(row[0] if row else "")
                    if sec:
                        current_section = sec
                        continue

                    row2 = (row + [""] * len(cols))[: len(cols)]
                    first = (row2[0] or "").strip()
                    if first.lower().startswith("example:"):
                        row2[0] = first.split(":", 1)[1].strip()

                    rec = {cols[i]: row2[i].strip() for i in range(len(cols))}

                    # normalize a few fields (only when schema includes them)
                    if "Currently Persuing? (Y/N)" in rec:
                        rec["Currently Persuing? (Y/N)"] = parse_yes_no(rec.get("Currently Persuing? (Y/N)", ""))
                    if "Shared resume?" in rec:
                        rec["Shared resume?"] = parse_yes_no(rec.get("Shared resume?", ""))
                    if "Data of initial outreach" in rec:
                        rec["Data of initial outreach"] = parse_date_loose(rec.get("Data of initial outreach", ""))
                    if "Date of most recent interaction" in rec:
                        rec["Date of most recent interaction"] = parse_date_loose(rec.get("Date of most recent interaction", ""))
                    if "Informational Interview Date" in rec:
                        rec["Informational Interview Date"] = parse_date_loose(rec.get("Informational Interview Date", ""))

                    wrapped = {
                        "id": rec.get("id", ""),
                        "group": current_section,
                        "fields": rec,
                        "created_at": now_iso(),
                        "updated_at": now_iso(),
                        "raw": {},
                    }
                    people.append(wrapped)

            return cols, people
        except UnicodeDecodeError as e:
            last_err = e
            continue
    raise last_err if last_err else RuntimeError("Failed to read CSV")


def get_fields(p: Dict[str, Any]) -> Dict[str, Any]:
    """Return the record fields dict regardless of storage shape."""
    f = p.get("fields")
    return f if isinstance(f, dict) else p


def normalize_id(s: str) -> str:
    s = (s or "").strip().upper().replace(" ", "")
    if not s:
        return ""
    # Allow '12' -> 'P-000012'
    if s.isdigit():
        return f"P-{int(s):06d}"
    # Allow 'P000012' -> 'P-000012'
    if s.startswith("P") and not s.startswith("P-"):
        s = "P-" + s[1:]
    return s


def find_person_index(people: List[Dict[str, Any]], person_id: str) -> int:
    target = normalize_id(person_id)
    for i, p in enumerate(people):
        if normalize_id(p.get("id", "")) == target:
            return i
    return -1


def pretty_print_person(
    p: Dict[str, Any],
    show_blanks: bool = False,
    columns: Optional[List[str]] = None,
    max_chars: int = 240,
    no_truncate_fields: Optional[set] = None,
) -> None:
    fields = p.get("fields", {}) if isinstance(p.get("fields"), dict) else {}
    no_truncate_fields = no_truncate_fields or set()

    print(f"ID: {p.get('id','')}")
    print(f"Group: {group_label(p.get('group',''))}")
    print(f"Name: {fields.get('First Name','')} {fields.get('Last Name','')}")
    print("-" * 60)

    keys = columns if columns else sorted(fields.keys())

    for k in keys:
        v = fields.get(k, "")
        if not (v or show_blanks):
            continue

        s = "" if v is None else str(v)

        if (k not in no_truncate_fields) and max_chars is not None and len(s) > max_chars:
            s = s[:max_chars] + f"... [truncated, {len(str(v))} chars total]"

        print(f"{k}: {s}")


def edit_in_editor(initial_text: str) -> str:
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
        except Exception:
            pass


def cmd_init(args: argparse.Namespace) -> int:
    """Initialize (or update) a JSON DB from an existing CSV."""
    cols, imported_people = import_from_csv(args.csv)

    db = load_db(args.out)
    db.setdefault("meta", {})
    db["meta"]["csv_columns"] = merge_columns(cols)

    existing: List[Dict[str, Any]] = db.get("people", [])

    # Build dedupe map from existing
    dedupe_map: Dict[str, Dict[str, Any]] = {}
    for p in existing:
        fields = p.get("fields", {})
        dedupe_map[dedupe_key(fields)] = p

    added = 0
    updated = 0

    for p in imported_people:
        key = dedupe_key(p.get("fields", {}))
        if key in dedupe_map:
            # Merge into existing: fill blanks only
            ex = dedupe_map[key]
            ex_fields = ex.get("fields", {})
            new_fields = p.get("fields", {})
            for k, v in new_fields.items():
                if (ex_fields.get(k) or "").strip() == "" and (v or "").strip() != "":
                    ex_fields[k] = v
            ex["fields"] = ex_fields
            ex["group"] = ex.get("group") or p.get("group")
            ex["updated_at"] = now_iso()
            updated += 1
        else:
            existing.append(p)
            dedupe_map[key] = p
            added += 1

    db["people"] = existing

    # Reassign clean sequential IDs (stable ordering for a personal tracker)
    n = 1
    for p in db["people"]:
        p["id"] = f"P-{n:06d}"
        n += 1

    save_db(args.out, db)
    print(f"Initialized DB: {args.out} (added {added}, updated {updated}, total {len(existing)})")
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    db = load_db(args.db)
    people = db.get("people", [])

    group = normalize_group(prompt("Group (seniors/peers)", "peers"))

    linkedin_url = prompt("LinkedIn URL (optional)")
    email = prompt("Email (optional)")

    # Basic identification
    first_name = prompt("First Name")
    last_name = prompt("Last Name")

    header_text = ""
    about_text = ""
    exp_text = ""
    if linkedin_url:
        header_text = prompt_multiline("Paste profile HEADER block (optional).")
        about_text = prompt_multiline("Paste profile ABOUT section (optional).")
        exp_text = prompt_multiline("Paste profile EXPERIENCE section (optional).")

    rec: Dict[str, Any] = {c: "" for c in db.get("meta", {}).get("csv_columns", DEFAULT_COLUMNS)}
    # Ensure canonical columns exist
    for c in DEFAULT_COLUMNS:
        rec.setdefault(c, "")

    rec["First Name"] = first_name
    rec["Last Name"] = last_name

    # channel + links
    channel_default = "LinkedIn" if linkedin_url else ("Email" if email else "Other")
    rec["Networking Channel"] = prompt("Networking Channel", channel_default)
    if linkedin_url and not rec.get("Links/Info"):
        rec["Links/Info"] = linkedin_url
    elif email and not rec.get("Links/Info"):
        rec["Links/Info"] = email
    else:
        rec["Links/Info"] = prompt("Links/Info (url/email/anything)", rec.get("Links/Info", ""))

    rec["Relation to me"] = prompt("Relation to me (how you know them)")

    # User-entered (manual)
    rec["Company"] = prompt("Company")
    rec["Job Title"] = prompt("Job Title")
    rec["Seniority"] = prompt("Seniority", "")

    rec["Full Time/Contract/Internship"] = prompt("Full Time/Contract/Internship", "")
    rec["Track"] = prompt("Track", "")

    rec["Currently Persuing? (Y/N)"] = parse_yes_no(prompt("Currently Pursuing? (Y/N)", "Y"))
    rec["Data of initial outreach"] = parse_date_loose(prompt("Date of initial outreach (optional, e.g. 2026-01-20)", ""))
    rec["Date of most recent interaction"] = parse_date_loose(prompt("Date of most recent interaction (optional)", ""))

    rec["Engagement Stage"] = prompt("Engagement Stage (optional)", "")
    rec["Response (Positive/Negative/Neutral)"] = prompt("Response (Positive/Negative/Neutral)", "")

    rec["Informational Interview Date"] = parse_date_loose(prompt("Informational Interview Date (optional)", ""))
    rec["Shared resume?"] = parse_yes_no(prompt("Shared resume? (Y/N)", ""))
    rec["Referral?"] = prompt("Referral?", "")
    rec["Salary"] = prompt("Salary (optional)", "")

    rec["Networking Recommendations"] = prompt("Networking Recommendations", "")

    # Pasted profile text goes into its own column
    paste_parts = []
    if header_text:
        paste_parts.append(f"[HEADER]\n{header_text}")
    if about_text:
        paste_parts.append(f"[ABOUT]\n{about_text}")
    if exp_text:
        paste_parts.append(f"[EXPERIENCE]\n{exp_text}")
    rec["LinkedIn Paste"] = "\n\n".join(paste_parts).strip()

    # Additional Notes are for your own notes only
    rec["Additional Notes"] = prompt("Additional Notes", "")

    # System fields
    rec["group"] = group
    rec["id"] = make_person_id(people)
    rec["created_at"] = now_iso()
    rec["updated_at"] = now_iso()

    # Deduping check
    key = dedupe_key(rec)
    for p in people:
        if dedupe_key(p.get("fields", {})) == key:
            print(f"Looks like this person already exists (id={p.get('id')}). Not adding duplicate.")
            return 2

    person = {
        "id": rec["id"],
        "group": group,
        "fields": rec,
        "created_at": rec["created_at"],
        "updated_at": rec["updated_at"],
        "raw": {
            "header": header_text,
            "about": about_text,
            "experience": exp_text,
        },
    }

    people.append(person)
    db["people"] = people
    save_db(args.db, db)
    print(f"Added {rec['id']}: {first_name} {last_name} ({group_label(group)})")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    db = load_db(args.db)
    cols = db.get("meta", {}).get("csv_columns", DEFAULT_COLUMNS)
    cols = merge_columns(cols)
    cols = cols[: len(DEFAULT_COLUMNS)]
    people = db.get("people", [])

    seniors = [p for p in people if p.get("group") == SECTION_SENIORS]
    peers = [p for p in people if p.get("group") == SECTION_PEERS]

    def sort_key(p: Dict[str, Any]) -> Tuple:
        fields = get_fields(p)
        pursuing = (fields.get("Currently Persuing? (Y/N)") or "").strip().upper() == "Y"
        recent = (fields.get("Date of most recent interaction") or "")
        return (not pursuing, recent, (fields.get("Last Name") or ""), (fields.get("First Name") or ""))

    seniors.sort(key=sort_key)
    peers.sort(key=sort_key)

    with open(args.out, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        header = cols + [""] * (38 - len(cols))
        writer.writerow(header)

        def write_section(title: str, rows: List[Dict[str, Any]]):
            writer.writerow([title] + [""] * (len(header) - 1))
            for p in rows:
                fields = get_fields(p)
                row = [(fields.get(c) or "") for c in cols]
                row = row + [""] * (len(header) - len(row))
                writer.writerow(row)
            writer.writerow([""] * len(header))

        write_section("Seniors List", seniors)
        write_section("Peers List", peers)

    print(f"Exported CSV: {args.out}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    db = load_db(args.db)
    people = db.get("people", [])

    group = normalize_group(args.group) if args.group else None

    rows = []
    for p in people:
        if group and p.get("group") != group:
            continue
        rows.append(p)

    # Due filter: people with no recent interaction OR older than N days
    if args.due is not None:
        cutoff = dt.date.today() - dt.timedelta(days=args.due)
        filtered = []
        for p in rows:
            fields = get_fields(p)
            s = (fields.get("Date of most recent interaction") or "").strip()
            if not s:
                filtered.append(p)
                continue
            try:
                d = dt.date.fromisoformat(s)
                if d <= cutoff:
                    filtered.append(p)
            except ValueError:
                filtered.append(p)
        rows = filtered

    def fmt(p: Dict[str, Any]) -> str:
        fields = get_fields(p)
        pid = p.get("id", "")
        grp = group_label(p.get("group", ""))
        return (
            f"{pid:16} | {grp:7} | {fields.get('First Name','')} {fields.get('Last Name','')} | "
            f"{fields.get('Job Title','')} @ {fields.get('Company','')} | "
            f"Pursuing:{fields.get('Currently Persuing? (Y/N)','')} | "
            f"Last:{fields.get('Date of most recent interaction','')} | "
            f"Channel:{fields.get('Networking Channel','')}"
        )

    rows.sort(key=lambda p: (p.get("group", ""), (get_fields(p).get("Last Name", "") or ""), (get_fields(p).get("First Name", "") or "")))
    for p in rows:
        print(fmt(p))
    print(f"{len(rows)} result(s)")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    db = load_db(args.db)
    people = db.get("people", [])

    idx = find_person_index(people, args.id)
    if idx < 0:
        print(f"Person not found: {args.id}")
        return 2

    cols = merge_columns(db.get("meta", {}).get("csv_columns", DEFAULT_COLUMNS))

    if args.field:
        f = people[idx].get("fields", {})
        print(f"{args.field}: {f.get(args.field, '')}")
        return 0

    max_chars = None if args.full else 240
    pretty_print_person(
        people[idx],
        show_blanks=args.all,
        columns=cols,
        max_chars=max_chars,
    )
    return 0


def cmd_edit(args: argparse.Namespace) -> int:
    db = load_db(args.db)
    people = db.get("people", [])

    auto_append_fields = {"Additional Notes", "Networking Recommendations", "Referral?", "LinkedIn Paste"}

    idx = find_person_index(people, args.id)
    if idx < 0:
        print(f"Person not found: {args.id}")
        return 2

    person = people[idx]
    fields = person.get("fields", {})
    if not isinstance(fields, dict):
        print("Corrupt record: missing fields dict.")
        return 3

    if not args.pick and not args.field:
        print("Provide --field, or use --pick.")
        return 2

    cols = merge_columns(db.get("meta", {}).get("csv_columns", DEFAULT_COLUMNS))

    if args.pick:
        print("Choose a field to edit:")
        for i, col in enumerate(cols, start=1):
            print(f"{i:2d}. {col}")

        choice = input("Field number: ").strip()
        if not choice:
            print("Cancelled.")
            return 0
        try:
            n = int(choice)
            if n < 1 or n > len(cols):
                raise ValueError
        except ValueError:
            print("Invalid field number.")
            return 2

        field = cols[n - 1]
        print(f"Editing field: {field}")

        current_preview = (fields.get(field, "") or "").replace("\n", " ")
        if len(current_preview) > 80:
            current_preview = current_preview[:80] + "…"
        print(f"Current value (first 80 chars): {current_preview}")

        proceed = input("Proceed? [Y/n]: ").strip().lower()
        if proceed.startswith("n"):
            print("Cancelled.")
            return 0
    else:
        field = args.field

    if field not in cols:
        print(f"Unknown field: {field}")
        print("Valid fields include:")
        for k in cols:
            print(f" - {k}")
        return 2

    fields.setdefault(field, "")

    # Default to append for these fields when NOT using editor, unless user forces replace
    if (field in auto_append_fields) and (not args.editor) and (not args.replace):
        args.append = True

    if args.editor:
        current = fields.get(field, "") or ""
        value = edit_in_editor(current)
    else:
        if args.value is not None:
            value = args.value
        else:
            value = prompt(f"New value for '{field}'", fields.get(field, ""))

    # Normalization for common fields
    if field in {"Currently Persuing? (Y/N)", "Shared resume?"}:
        value = parse_yes_no(value)
    if field in {"Data of initial outreach", "Date of most recent interaction", "Informational Interview Date"}:
        value = parse_date_loose(value)

    # If appending, add timestamp prefix (non-editor only) for selected fields
    if args.append and (not args.editor) and (field in auto_append_fields):
        new = (value or "").strip()
        if new:
            value = f"{now_stamp()}: {new}"

    # Append merge
    if args.append:
        old = (fields.get(field) or "").rstrip()
        new = (value or "").strip()
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

    print(f"Updated {args.id}: {field}")
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    db = load_db(args.db)
    people = db.get("people", [])

    q = (args.name or "").strip().lower()
    if not q:
        print("Provide --name, e.g. --name 'Jane Doe'")
        return 2

    tokens = [t for t in re.split(r"\s+", q) if t]

    matches = []
    for p in people:
        fields = p.get("fields", {}) if isinstance(p.get("fields"), dict) else {}
        hay = " ".join([
            str(p.get("id", "")),
            str(fields.get("First Name", "")),
            str(fields.get("Last Name", "")),
            str(fields.get("Company", "")),
            str(fields.get("Job Title", "")),
            str(fields.get("Links/Info", "")),
        ]).lower()

        if all(t in hay for t in tokens):
            matches.append(p)

    if not matches:
        print("No matches.")
        return 0

    matches.sort(key=lambda p: (
        (p.get("fields", {}) or {}).get("Last Name", ""),
        (p.get("fields", {}) or {}).get("First Name", ""),
    ))

    for i, p in enumerate(matches, start=1):
        f = p.get("fields", {}) if isinstance(p.get("fields"), dict) else {}
        print(
            f"[{i}] {p.get('id',''):8} | {group_label(p.get('group','')):7} | "
            f"{f.get('First Name','')} {f.get('Last Name','')} | "
            f"{f.get('Job Title','')} @ {f.get('Company','')}"
        )

    print(f"{len(matches)} match(es)")

    choice = input("Select number (or press Enter to cancel): ").strip()
    if not choice:
        return 0

    try:
        idx = int(choice) - 1
        if idx < 0 or idx >= len(matches):
            raise ValueError
    except ValueError:
        print("Invalid selection.")
        return 2

    selected = matches[idx]
    print(f"Selected: {selected.get('id')}")
    pretty_print_person(selected)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="People Tracker (local-first, sectioned CSV, JSON source-of-truth)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s_init = sub.add_parser("init", help="Initialize/merge a JSON DB from an existing CSV")
    s_init.add_argument("--csv", required=True, help="Path to CSV")
    s_init.add_argument("--out", required=True, help="Output JSON DB path")
    s_init.set_defaults(func=cmd_init)

    s_add = sub.add_parser("add", help="Interactively add a person")
    s_add.add_argument("--db", required=True, help="Path to JSON DB")
    s_add.set_defaults(func=cmd_add)

    s_export = sub.add_parser("export", help="Export a sectioned CSV")
    s_export.add_argument("--db", required=True, help="Path to JSON DB")
    s_export.add_argument("--out", required=True, help="Output CSV path")
    s_export.set_defaults(func=cmd_export)

    s_list = sub.add_parser("list", help="List people (optional filters)")
    s_list.add_argument("--db", required=True, help="Path to JSON DB")
    s_list.add_argument("--group", required=False, help="Filter by group (seniors/peers)")
    s_list.add_argument("--due", type=int, default=None, help="Show those with last interaction older than N days (or blank)")
    s_list.set_defaults(func=cmd_list)

    s_show = sub.add_parser("show", help="Show a single person record")
    s_show.add_argument("--db", required=True, help="Path to JSON DB")
    s_show.add_argument("--id", required=True, help="Person ID (e.g. P-000014 or 14)")
    s_show.add_argument("--all", action="store_true", help="Show all fields including blank ones")
    s_show.add_argument("--full", action="store_true", help="Do not truncate long fields")
    s_show.add_argument("--field", help="Show only a single field (exact column name)")
    s_show.set_defaults(func=cmd_show)

    s_edit = sub.add_parser("edit", help="Edit a single field for a person")
    s_edit.add_argument("--db", required=True, help="Path to JSON DB")
    s_edit.add_argument("--id", required=True, help="Person ID (e.g. P-000014 or 14)")
    s_edit.add_argument("--field", required=False, help="Exact column name (e.g. 'Company')")
    s_edit.add_argument("--value", default=None, help="New value (omit for interactive prompt)")
    s_edit.add_argument("--editor", action="store_true", help="Edit the field in your editor ($EDITOR or nano)")
    s_edit.add_argument("--pick", action="store_true", help="Pick the field from a numbered list")
    s_edit.add_argument("--append", action="store_true", help="Append to existing value (adds newline)")
    s_edit.add_argument("--replace", action="store_true", help="Replace existing value (overrides auto-append defaults)")
    s_edit.set_defaults(func=cmd_edit)

    s_search = sub.add_parser("search", help="Search people by name/company/title/link text")
    s_search.add_argument("--db", required=True, help="Path to JSON DB")
    s_search.add_argument("--name", required=True, help="Search string, e.g. 'Jane Doe' or 'doe'")
    s_search.set_defaults(func=cmd_search)

    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
