#!/usr/bin/env python3
"""
migrate.py
----------
One-time migration script. Reads all notes from the Apple Notes
'Bookmarks' folder via AppleScript in batches of 100, and writes
one JSON file per URL into data/bookmarks/YYYY/MM/{timestamp}.json.

Usage (run from repo root):
    python3.12 scripts/migrate.py               # full migration
    python3.12 scripts/migrate.py --sample 10   # process 10 notes only
    python3.12 scripts/migrate.py --dry-run     # parse and print, no files written

After migration, run enrich.py to fetch OG metadata:
    python3.12 scripts/enrich.py

Requirements:
    - macOS only (uses AppleScript via osascript)
    - Must be run on a Mac signed into the iCloud account with your Notes
    - Run from the repo root directory

Notes:
    - Batches notes 100 at a time to avoid AppleScript timeouts
    - Safe to re-run: skips URLs already written to data/bookmarks/
    - Handles both Pinboard-imported notes and current Share Sheet notes
    - Uses the Pinboard Unix timestamp (if present) as saved_at
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

REPO_ROOT     = Path(__file__).parent.parent
DATA_DIR      = REPO_ROOT / "data" / "bookmarks"
NOTES_FOLDER  = "Bookmarks"
NOTES_ACCOUNT = "iCloud"
BATCH_SIZE    = 100
SEP           = "\x1e"  # ASCII record separator

# ── AppleScript helpers ───────────────────────────────────────────────────────

# Step 1: get total note count
COUNT_SCRIPT = f"""
tell application "Notes"
    set targetFolder to folder "{NOTES_FOLDER}" of account "{NOTES_ACCOUNT}"
    return count of notes of targetFolder
end tell
"""

# Step 2: fetch a batch by index range (1-based, inclusive)
BATCH_SCRIPT_TEMPLATE = """\
on run argv
    set outPath to item 1 of argv
    set startIdx to item 2 of argv as integer
    set endIdx to item 3 of argv as integer
    set outFile to open for access POSIX file outPath with write permission
    set eof of outFile to 0
    tell application "Notes"
        set targetFolder to folder "{folder}" of account "{account}"
        set allNotes to every note of targetFolder
        set batchEnd to endIdx
        if batchEnd > (count of allNotes) then set batchEnd to (count of allNotes)
        repeat with i from startIdx to batchEnd
            set n to item i of allNotes
            set nID to id of n
            set nTitle to name of n
            set nBody to plaintext of n
            set nDate to (creation date of n) - (date "Thursday, January 1, 1970 at 00:00:00") + (time to GMT)
            set sep to (ASCII character 30)
            set rec to nID & sep & nTitle & sep & nBody & sep & (nDate as text) & (ASCII character 10)
            write rec to outFile
        end repeat
    end tell
    close access outFile
end run
"""


def run_applescript(script: str, args: list[str] = [], timeout: int = 120) -> str:
    """Writes script to a temp file and runs it via osascript."""
    import tempfile
    with tempfile.NamedTemporaryFile(mode='w', suffix='.applescript',
                                     delete=False, encoding='utf-8') as sf:
        sf.write(script)
        script_path = sf.name
    try:
        result = subprocess.run(
            ["osascript", script_path] + args,
            capture_output=True, text=True, timeout=timeout
        )
    finally:
        os.unlink(script_path)

    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip())
    return result.stdout.strip()


def get_note_count() -> int:
    """Returns the total number of notes in the Bookmarks folder."""
    print("Counting notes in Bookmarks folder…")
    result = run_applescript(COUNT_SCRIPT, timeout=30)
    return int(result.strip())


def fetch_batch(start: int, end: int, out_path: str) -> str:
    """
    Fetches notes from index start to end (1-based, inclusive)
    and writes them to out_path. Returns file contents.
    """
    import tempfile

    script = BATCH_SCRIPT_TEMPLATE.format(
        folder=NOTES_FOLDER,
        account=NOTES_ACCOUNT,
    )

    with tempfile.NamedTemporaryFile(mode='w', suffix='.txt',
                                     delete=False, encoding='utf-8') as of:
        tmp_path = of.name

    try:
        run_applescript(script, args=[tmp_path, str(start), str(end)], timeout=120)
        contents = Path(tmp_path).read_text(encoding='utf-8', errors='replace')
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    return contents


# ── Parsing ───────────────────────────────────────────────────────────────────

URL_RE            = re.compile(r'https?://[^\s\)\]\'"<>]+')
PINBOARD_URL_RE   = re.compile(r'^URL:\s*(https?://\S+)', re.MULTILINE)
PINBOARD_TS_RE    = re.compile(r'^Timestamp:\s*(\d+)', re.MULTILINE)
PINBOARD_NOISE_RE = re.compile(r'^(Folder|Timestamp|URL):\s*.*$', re.MULTILINE)


def parse_note(raw_id, raw_title, raw_body, raw_date) -> list[dict]:
    title   = raw_title.strip()
    body    = raw_body.strip()
    date_ts = int(raw_date.strip()) if raw_date.strip().lstrip('-').isdigit() else None

    if PINBOARD_TS_RE.search(body):
        return _parse_pinboard(raw_id, title, body, date_ts)
    else:
        return _parse_current(raw_id, title, body, date_ts)


def _parse_pinboard(note_id, title, body, note_ts) -> list[dict]:
    ts_match  = PINBOARD_TS_RE.search(body)
    saved_ts  = int(ts_match.group(1)) if ts_match else note_ts
    url_match = PINBOARD_URL_RE.search(body)
    url       = url_match.group(1).strip() if url_match else None

    if not url:
        urls = URL_RE.findall(body)
        url  = urls[0] if urls else None
    if not url:
        return []

    cleaned    = PINBOARD_NOISE_RE.sub('', body).replace(url, '')
    annotation = _clean(cleaned)
    return [_make(note_id, url, title, annotation, saved_ts)]


def _parse_current(note_id, title, body, note_ts) -> list[dict]:
    urls = list(dict.fromkeys(URL_RE.findall(body)))  # deduplicate, preserve order
    if not urls:
        return []

    body_without = body
    for u in urls:
        body_without = body_without.replace(u, '')
    annotation = _clean(body_without)

    results = []
    for i, url in enumerate(urls):
        results.append(_make(
            f"{note_id}_{i}" if i > 0 else note_id,
            url,
            title if i == 0 else url,
            annotation if i == 0 else None,
            note_ts,
        ))
    return results


def _clean(text) -> str | None:
    cleaned = re.sub(r'\bUnread\b', '', text or '')
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned if len(cleaned) >= 3 else None


def _make(note_id, url, title, annotation, saved_ts) -> dict:
    if saved_ts and saved_ts > 0:
        saved_at = datetime.fromtimestamp(saved_ts, tz=timezone.utc).isoformat()
    else:
        saved_at = datetime.now(timezone.utc).isoformat()
    return {
        "id"         : str(saved_ts or int(datetime.now(timezone.utc).timestamp())),
        "url"        : url,
        "title"      : title,
        "annotation" : annotation,
        "tags"       : [],
        "saved_at"   : saved_at,
        "domain"     : None,
        "og"         : None,
        "enriched_at": None,
    }


# ── File writing ──────────────────────────────────────────────────────────────

def existing_urls() -> set:
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
    saved_at = bookmark["saved_at"]
    try:
        dt = datetime.fromisoformat(saved_at[:19])
    except ValueError:
        dt = datetime.now(timezone.utc)

    out_dir  = DATA_DIR / str(dt.year) / f"{dt.month:02d}"
    out_path = out_dir / f"{bookmark['id']}.json"

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
    parser = argparse.ArgumentParser(description="Migrate Apple Notes bookmarks to JSON.")
    parser.add_argument("--sample",  type=int, metavar="N", help="Process only the first N notes")
    parser.add_argument("--dry-run", action="store_true",   help="Parse and print without writing")
    args = parser.parse_args()

    # Get total count
    try:
        total = get_note_count()
    except Exception as e:
        print(f"✗ Could not count notes: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"  {total} notes found in Notes › {NOTES_ACCOUNT} › {NOTES_FOLDER}\n")

    limit = args.sample if args.sample else total
    limit = min(limit, total)

    # Load existing URLs
    known_urls: set = set()
    if not args.dry_run:
        known_urls = existing_urls()
        if known_urls:
            print(f"  {len(known_urls)} existing bookmarks — duplicates will be skipped.\n")

    written = skipped = empty = errors = 0

    # Process in batches
    batches = range(1, limit + 1, BATCH_SIZE)
    num_batches = len(batches)

    for batch_num, start in enumerate(batches, 1):
        end = min(start + BATCH_SIZE - 1, limit)
        print(f"Batch {batch_num}/{num_batches}: notes {start}–{end}…")

        try:
            raw = fetch_batch(start, end, "")
        except Exception as e:
            print(f"  ✗ Batch failed: {e}", file=sys.stderr)
            errors += (end - start + 1)
            continue

        lines = [l for l in raw.split("\n") if l.strip()]

        for line in lines:
            parts = line.split(SEP)
            if len(parts) < 4:
                empty += 1
                continue

            raw_id, raw_title, raw_body, raw_date = parts[0], parts[1], parts[2], parts[3]

            try:
                bookmarks = parse_note(raw_id, raw_title, raw_body, raw_date)
            except Exception as e:
                print(f"  ✗ Parse error on '{raw_title[:50]}': {e}", file=sys.stderr)
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
                    print()
                else:
                    write_bookmark(bookmark, dry_run=False)
                    written += 1

        print(f"  ✓ done  (written so far: {written})")

    print(f"\n{'─' * 44}")
    if args.dry_run:
        print(f"[dry-run] complete.")
    else:
        print(f"Done.")
        print(f"  Written:  {written}")
        print(f"  Skipped:  {skipped}  (already existed)")
        print(f"  Empty:    {empty}  (no URL found)")
        print(f"  Errors:   {errors}")
        if written > 0:
            print(f"\nNext step: python3.12 scripts/enrich.py")


if __name__ == "__main__":
    main()
# ── Configuration ─────────────────────────────────────────────────────────────

REPO_ROOT      = Path(__file__).parent.parent
DATA_DIR       = REPO_ROOT / "data" / "bookmarks"
NOTES_FOLDER   = "Bookmarks"   # Name of your Notes folder — change if different
NOTES_ACCOUNT  = "iCloud"      # Notes account name — change if different

# ── AppleScript ───────────────────────────────────────────────────────────────

# Writes each note as a line to a temp file using ASCII record separator (0x1e)
# to delimit fields. No shell calls inside the loop — much faster for large folders.
# Output is written directly to disk to avoid subprocess pipe buffer limits.

APPLESCRIPT_TEMPLATE = """\
on run argv
    set outPath to item 1 of argv
    set outFile to open for access POSIX file outPath with write permission
    set eof of outFile to 0
    tell application "Notes"
        set targetFolder to folder "{folder}" of account "{account}"
        set allNotes to every note of targetFolder
        repeat with n in allNotes
            set nID to id of n
            set nTitle to name of n
            set nBody to plaintext of n
            set nDate to (creation date of n) - (date "Thursday, January 1, 1970 at 00:00:00") + (time to GMT)
            set sep to (ASCII character 30)
            set rec to nID & sep & nTitle & sep & nBody & sep & (nDate as text) & (ASCII character 10)
            write rec to outFile
        end repeat
    end tell
    close access outFile
end run
"""

SEP = "\x1e"  # ASCII record separator — same as used in AppleScript above


def fetch_notes_applescript() -> str:
    """
    Runs the AppleScript, writing output to a temp file to avoid
    subprocess pipe buffer limits. Returns file contents as a string.
    """
    import tempfile

    script = APPLESCRIPT_TEMPLATE.format(
        folder=NOTES_FOLDER,
        account=NOTES_ACCOUNT,
    )

    # Write AppleScript to a temp file so we can pass args via osascript
    with tempfile.NamedTemporaryFile(mode='w', suffix='.applescript',
                                     delete=False, encoding='utf-8') as sf:
        sf.write(script)
        script_path = sf.name

    # Output goes to a separate temp file
    with tempfile.NamedTemporaryFile(mode='w', suffix='.txt',
                                     delete=False, encoding='utf-8') as of:
        out_path = of.name

    print("Asking Notes for your bookmarks (this may take a few minutes for large folders)…")
    print("Notes may ask for permission — click OK if a dialog appears.\n")

    try:
        result = subprocess.run(
            ["osascript", script_path, out_path],
            capture_output=True,
            text=True,
            timeout=1800   # 30 minute timeout for very large folders
        )
    finally:
        os.unlink(script_path)

    if result.returncode != 0:
        print(f"\n✗ AppleScript error:\n{result.stderr}", file=sys.stderr)
        print("\nCommon causes:", file=sys.stderr)
        print("  • Notes isn't open or iCloud hasn't synced yet", file=sys.stderr)
        print(f"  • Folder name isn't exactly '{NOTES_FOLDER}' — check Notes.app", file=sys.stderr)
        print(f"  • Account name isn't exactly '{NOTES_ACCOUNT}' — check Notes.app sidebar", file=sys.stderr)
        sys.exit(1)

    try:
        contents = Path(out_path).read_text(encoding='utf-8', errors='replace')
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            pass

    return contents


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
        parts = line.split(SEP)
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
