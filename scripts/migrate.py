#!/usr/bin/env python3
"""
migrate.py
----------
One-time migration script. Reads all notes from the Apple Notes
'Bookmarks' folder via AppleScript and writes one JSON file per URL
into data/bookmarks/YYYY/MM/{timestamp}.json.

Usage (run from repo root):
    python3 scripts/migrate.py               # full migration
    python3 scripts/migrate.py --sample 10   # process 10 notes only
    python3 scripts/migrate.py --dry-run     # print parsed data, write nothing

After migration, run enrich.py to fetch OG metadata:
    python3 scripts/enrich.py

Requirements:
    - macOS only (uses AppleScript via osascript)
    - Must be run on a Mac signed into the iCloud account with your Notes
    - Run from the repo root directory

Notes:
    - Safe to re-run: skips URLs already present in data/bookmarks/
    - Handles both Pinboard-imported notes and current Share Sheet notes
    - Uses the Pinboard Unix timestamp (if present) as saved_at
    - Notes with multiple URLs produce one JSON file per URL
"""

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# ── Configuration ─────────────────────────────────────────────────────────────

REPO_ROOT      = Path(__file__).parent.parent
DATA_DIR       = REPO_ROOT / "data" / "bookmarks"
NOTES_FOLDER   = "Bookmarks"   # Name of your Notes folder — change if different
NOTES_ACCOUNT  = "iCloud"      # Notes account name — change if different

# ── AppleScript ───────────────────────────────────────────────────────────────

# Fetches all notes from the target folder.
# Returns a tab-and-newline-delimited string:
#   id \t title \t body \t creation_date
# Dates are returned as Unix timestamps via AppleScript's `time to GMT` offset trick.
APPLESCRIPT = f"""
tell application "Notes"
    set targetFolder to folder "{NOTES_FOLDER}" of account "{NOTES_ACCOUNT}"
    set allNotes to every note of targetFolder
    set output to ""
    repeat with n in allNotes
        set nID to id of n
        set nTitle to name of n
        set nBody to plaintext of n
        -- AppleScript dates: seconds since Jan 1 2001 local time
        -- Convert to Unix timestamp (Unix epoch = Jan 1 1970)
        set nDate to (creation date of n) - (date "Thursday, January 1, 1970 at 00:00:00") + (time to GMT)
        -- Escape tabs and newlines in title and body so we can split safely
        set nTitle to do shell script "echo " & quoted form of nTitle & " | tr '\\t' ' '"
        set nBody to do shell script "echo " & quoted form of nBody & " | tr '\\t' ' '"
        set output to output & nID & "\\t" & nTitle & "\\t" & nBody & "\\t" & nDate & "\\n"
    end repeat
    return output
end tell
"""

def fetch_notes_applescript() -> str:
    """Runs the AppleScript and returns raw output."""
    print("Asking Notes for your bookmarks (this may take a moment for large folders)…")
    result = subprocess.run(
        ["osascript", "-e", APPLESCRIPT],
        capture_output=True,
        text=True,
        timeout=300   # 5 minute timeout for large note collections
    )
    if result.returncode != 0:
        print(f"\n✗ AppleScript error:\n{result.stderr}", file=sys.stderr)
        print("\nCommon causes:", file=sys.stderr)
        print("  • Notes isn't open or iCloud hasn't synced yet", file=sys.stderr)
        print(f"  • Folder name isn't exactly '{NOTES_FOLDER}' — check Notes.app", file=sys.stderr)
        print(f"  • Account name isn't exactly '{NOTES_ACCOUNT}' — check Notes.app sidebar", file=sys.stderr)
        sys.exit(1)
    return result.stdout


# ── Parsing ───────────────────────────────────────────────────────────────────

URL_RE = re.compile(r'https?://[^\s\)\]\'"<>]+')

# Pinboard import patterns
PINBOARD_URL_RE   = re.compile(r'^URL:\s*(https?://\S+)', re.MULTILINE)
PINBOARD_TS_RE    = re.compile(r'^Timestamp:\s*(\d+)', re.MULTILINE)
PINBOARD_NOISE_RE = re.compile(r'^(Folder|Timestamp|URL):\s*.*$', re.MULTILINE)


def parse_note(raw_id: str, raw_title: str, raw_body: str, raw_date: str) -> list[dict]:
    """
    Parses a single note into one or more bookmark dicts.
    Returns a list (one entry per URL found).
    """
    title    = raw_title.strip()
    body     = raw_body.strip()
    date_ts  = int(raw_date.strip()) if raw_date.strip().lstrip('-').isdigit() else None

    # ── Detect format ──

    is_pinboard = bool(PINBOARD_TS_RE.search(body))

    if is_pinboard:
        return _parse_pinboard_note(raw_id, title, body, date_ts)
    else:
        return _parse_current_note(raw_id, title, body, date_ts)


def _parse_pinboard_note(note_id: str, title: str, body: str, note_ts: int) -> list[dict]:
    """Parses a Pinboard-imported note."""

    # Prefer the embedded Pinboard timestamp over the Note creation date
    ts_match = PINBOARD_TS_RE.search(body)
    saved_ts = int(ts_match.group(1)) if ts_match else note_ts

    # Extract URL — prefer the 'URL:' line, fall back to first URL in body
    url_match = PINBOARD_URL_RE.search(body)
    if url_match:
        url = url_match.group(1).strip()
    else:
        urls = URL_RE.findall(body)
        url  = urls[0] if urls else None

    if not url:
        return []

    # Strip Pinboard metadata lines to get any remaining annotation
    cleaned = PINBOARD_NOISE_RE.sub('', body)
    # Remove the URL itself from body
    cleaned = cleaned.replace(url, '')
    annotation = _clean_annotation(cleaned)

    return [_make_bookmark(
        note_id  = note_id,
        url      = url,
        title    = title,
        annotation = annotation,
        saved_ts = saved_ts,
    )]


def _parse_current_note(note_id: str, title: str, body: str, note_ts: int) -> list[dict]:
    """Parses a current (Share Sheet) note."""

    urls = URL_RE.findall(body)
    if not urls:
        return []

    # Deduplicate while preserving order
    seen = set()
    unique_urls = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            unique_urls.append(u)

    # Annotation: everything in body that isn't a URL
    body_without_urls = body
    for u in unique_urls:
        body_without_urls = body_without_urls.replace(u, '')
    annotation = _clean_annotation(body_without_urls)

    results = []
    for i, url in enumerate(unique_urls):
        results.append(_make_bookmark(
            note_id    = f"{note_id}_{i}" if i > 0 else note_id,
            url        = url,
            title      = title if i == 0 else url,
            annotation = annotation if i == 0 else None,
            saved_ts   = note_ts,
        ))

    return results


def _clean_annotation(text: str) -> str | None:
    """Strips whitespace and noise; returns None if nothing meaningful remains."""
    # Remove common noise words left over from Pinboard metadata
    cleaned = re.sub(r'\bUnread\b', '', text)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned if len(cleaned) >= 3 else None


def _make_bookmark(note_id: str, url: str, title: str,
                   annotation: str | None, saved_ts: int | None) -> dict:
    """Constructs a bookmark dict from parsed fields."""

    # Convert Unix timestamp to ISO 8601
    if saved_ts and saved_ts > 0:
        saved_at = datetime.fromtimestamp(saved_ts, tz=timezone.utc).isoformat()
    else:
        saved_at = datetime.now(timezone.utc).isoformat()

    # Use timestamp as file ID (unique, sortable)
    ts_int = saved_ts or int(datetime.now(timezone.utc).timestamp())
    file_id = str(ts_int)

    return {
        "id"          : file_id,
        "url"         : url,
        "title"       : title,
        "annotation"  : annotation,
        "tags"        : [],
        "saved_at"    : saved_at,
        "domain"      : None,       # filled by enrich.py
        "og"          : None,       # filled by enrich.py
        "enriched_at" : None,
    }


# ── File writing ──────────────────────────────────────────────────────────────

def existing_urls() -> set[str]:
    """Returns the set of URLs already written to data/bookmarks/."""
    urls = set()
    for path in DATA_DIR.rglob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("url"):
                urls.add(data["url"])
        except (json.JSONDecodeError, OSError):
            pass
    return urls


def write_bookmark(bookmark: dict, dry_run: bool) -> Path | None:
    """
    Writes a bookmark dict to data/bookmarks/YYYY/MM/{id}.json.
    Returns the path written, or None on dry-run/skip.
    """
    saved_at = bookmark["saved_at"]
    try:
        dt = datetime.fromisoformat(saved_at[:19])
    except ValueError:
        dt = datetime.now(timezone.utc)

    out_dir  = DATA_DIR / str(dt.year) / f"{dt.month:02d}"
    out_path = out_dir / f"{bookmark['id']}.json"

    # Handle timestamp collisions by appending a counter
    counter = 1
    while out_path.exists() and not dry_run:
        out_path = out_dir / f"{bookmark['id']}_{counter}.json"
        counter += 1

    if dry_run:
        return out_path

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(bookmark, indent=2, ensure_ascii=False), encoding="utf-8")
    return out_path


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Migrate Apple Notes bookmarks to JSON files.")
    parser.add_argument("--sample",  type=int, metavar="N", help="Process only the first N notes")
    parser.add_argument("--dry-run", action="store_true",   help="Parse and print without writing files")
    args = parser.parse_args()

    # ── Fetch from Notes ──
    raw_output = fetch_notes_applescript()
    lines = [l for l in raw_output.strip().split("\n") if l.strip()]

    if not lines:
        print("✗ No notes returned. Is the Bookmarks folder empty or mis-named?", file=sys.stderr)
        sys.exit(1)

    print(f"  {len(lines)} notes found in Notes › {NOTES_ACCOUNT} › {NOTES_FOLDER}\n")

    if args.sample:
        lines = lines[:args.sample]
        print(f"[--sample {args.sample}] Processing first {len(lines)} notes only.\n")

    # ── Load existing URLs to skip duplicates ──
    known_urls: set[str] = set()
    if not args.dry_run:
        known_urls = existing_urls()
        if known_urls:
            print(f"  {len(known_urls)} existing bookmarks found — duplicates will be skipped.\n")

    # ── Parse and write ──
    written   = 0
    skipped   = 0
    empty     = 0
    errors    = 0

    for line in lines:
        parts = line.split("\t")
        if len(parts) < 4:
            empty += 1
            continue

        raw_id, raw_title, raw_body, raw_date = parts[0], parts[1], parts[2], parts[3]

        try:
            bookmarks = parse_note(raw_id, raw_title, raw_body, raw_date)
        except Exception as e:
            print(f"  ✗ Parse error on '{raw_title[:60]}': {e}", file=sys.stderr)
            errors += 1
            continue

        if not bookmarks:
            empty += 1
            continue

        for bookmark in bookmarks:
            url = bookmark["url"]

            if url in known_urls:
                skipped += 1
                continue

            known_urls.add(url)

            if args.dry_run:
                path = write_bookmark(bookmark, dry_run=True)
                print(f"  [dry-run] {path.relative_to(REPO_ROOT)}")
                print(f"    url:        {bookmark['url']}")
                print(f"    title:      {bookmark['title']}")
                print(f"    annotation: {bookmark['annotation']}")
                print(f"    saved_at:   {bookmark['saved_at']}")
                print(f"    tags:       {bookmark['tags']}")
                print()
            else:
                path = write_bookmark(bookmark, dry_run=False)
                if path:
                    written += 1
                    if written % 100 == 0:
                        print(f"  … {written} written so far")

    # ── Summary ──
    print(f"{'─' * 44}")
    if args.dry_run:
        print(f"[dry-run] Would write {len(lines) - empty - skipped} bookmark(s).")
    else:
        print(f"Done.")
        print(f"  Written:  {written}")
        print(f"  Skipped:  {skipped}  (already existed)")
        print(f"  Empty:    {empty}   (no URL found)")
        print(f"  Errors:   {errors}")
        print()
        if written > 0:
            print(f"Next step: python3 scripts/enrich.py")


if __name__ == "__main__":
    main()
