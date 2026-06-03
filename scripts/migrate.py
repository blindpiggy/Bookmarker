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
    - Reads note HTML body to extract URLs from anchor href attributes
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
from html.parser import HTMLParser

# ── Configuration ─────────────────────────────────────────────────────────────

REPO_ROOT     = Path(__file__).parent.parent
DATA_DIR      = REPO_ROOT / "data" / "bookmarks"
NOTES_FOLDER  = "Bookmarks"
NOTES_ACCOUNT = "iCloud"
BATCH_SIZE    = 100
SEP           = "\x1e"  # ASCII record separator

# ── HTML URL extractor ────────────────────────────────────────────────────────

class HrefExtractor(HTMLParser):
    """Extracts href URLs and visible text from Notes HTML body."""

    def __init__(self):
        super().__init__()
        self.urls  = []
        self.texts = []
        self._current_text = []

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            attrs_dict = dict(attrs)
            href = attrs_dict.get('href', '')
            if href.startswith('http'):
                self.urls.append(href)

    def handle_data(self, data):
        self._current_text.append(data)

    def get_text(self):
        return ' '.join(self._current_text).strip()


def extract_urls_from_html(html: str) -> tuple[list[str], str]:
    """
    Returns (urls, plain_text) from a Notes HTML body.
    URLs come from <a href> attributes.
    Plain text is everything visible, with URLs stripped out.
    """
    parser = HrefExtractor()
    try:
        parser.feed(html)
    except Exception:
        pass

    urls = list(dict.fromkeys(parser.urls))  # deduplicate, preserve order

    # Also scan raw text for any plain URLs not wrapped in anchors
    plain = parser.get_text()
    raw_urls = re.findall(r'https?://[^\s\)\]\'"<>]+', plain)
    for u in raw_urls:
        if u not in urls:
            urls.append(u)

    # Remove URLs from plain text to get annotation
    annotation_text = plain
    for u in urls:
        annotation_text = annotation_text.replace(u, '')

    return urls, annotation_text


# ── AppleScript helpers ───────────────────────────────────────────────────────

COUNT_SCRIPT = f"""
tell application "Notes"
    set targetFolder to folder "{NOTES_FOLDER}" of account "{NOTES_ACCOUNT}"
    return count of notes of targetFolder
end tell
"""

# Uses 'body of n' (HTML) instead of 'plaintext of n' to get URLs from anchors
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
            set nBody to body of n
            set nDate to (creation date of n) - (date "Thursday, January 1, 1970 at 00:00:00") + (time to GMT)
            set sep to (ASCII character 30)
            set rec to nID & sep & nTitle & sep & nBody & sep & (nDate as text) & (ASCII character 29)
            write rec to outFile
        end repeat
    end tell
    close access outFile
end run
"""


def run_applescript(script: str, args: list = [], timeout: int = 30) -> str:
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
    print("Counting notes in Bookmarks folder…")
    result = run_applescript(COUNT_SCRIPT, timeout=30)
    return int(result.strip())


def fetch_batch(start: int, end: int) -> str:
    import tempfile
    script = BATCH_SCRIPT_TEMPLATE.format(
        folder=NOTES_FOLDER,
        account=NOTES_ACCOUNT,
    )
    with tempfile.NamedTemporaryFile(mode='w', suffix='.txt',
                                     delete=False, encoding='utf-8') as of:
        tmp_path = of.name
    try:
        run_applescript(script, args=[tmp_path, str(start), str(end)], timeout=180)
        contents = Path(tmp_path).read_text(encoding='utf-8', errors='replace')
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
    return contents


# ── Parsing ───────────────────────────────────────────────────────────────────

PINBOARD_URL_RE   = re.compile(r'^URL:\s*(https?://\S+)', re.MULTILINE)
PINBOARD_TS_RE    = re.compile(r'^Timestamp:\s*(\d+)', re.MULTILINE)
PINBOARD_NOISE_RE = re.compile(r'^(Folder|Timestamp|URL):\s*.*$', re.MULTILINE)


def parse_note(raw_id, raw_title, raw_body, raw_date) -> list[dict]:
    title   = raw_title.strip()
    body    = raw_body.strip()
    try:
        date_ts = int(float(raw_date.strip()))
    except (ValueError, TypeError):
        date_ts = None

    # Detect Pinboard import by presence of Timestamp: field in plaintext
    # For Pinboard notes, body may be HTML wrapping the old plain text
    plain_body = re.sub(r'<[^>]+>', ' ', body).strip()

    if PINBOARD_TS_RE.search(plain_body):
        return _parse_pinboard(raw_id, title, plain_body, date_ts)
    else:
        return _parse_current(raw_id, title, body, date_ts)


def _parse_pinboard(note_id, title, plain_body, note_ts) -> list[dict]:
    ts_match  = PINBOARD_TS_RE.search(plain_body)
    saved_ts  = int(ts_match.group(1)) if ts_match else note_ts
    url_match = PINBOARD_URL_RE.search(plain_body)
    url       = url_match.group(1).strip() if url_match else None

    if not url:
        urls = re.findall(r'https?://[^\s\)\]\'"<>]+', plain_body)
        url  = urls[0] if urls else None
    if not url:
        return []

    cleaned    = PINBOARD_NOISE_RE.sub('', plain_body).replace(url, '')
    annotation = _clean(cleaned, title=title)
    return [_make(note_id, url, title, annotation, saved_ts)]


def _parse_current(note_id, title, html_body, note_ts) -> list[dict]:
    urls, annotation_text = extract_urls_from_html(html_body)

    if not urls:
        return []

    annotation = _clean(annotation_text, title=title)

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


def _clean(text, title=None) -> str | None:
    cleaned = re.sub(r'\bUnread\b', '', text or '')
    cleaned = re.sub(r'<[^>]+>', ' ', cleaned)  # strip any residual HTML
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    if not cleaned or len(cleaned) < 3:
        return None
    # Suppress annotation if it's just the title repeated
    if title and cleaned.strip().lower() == title.strip().lower():
        return None
    return cleaned


def _make(note_id, url, title, annotation, saved_ts) -> dict:
    import hashlib
    if saved_ts and saved_ts > 0:
        saved_at = datetime.fromtimestamp(saved_ts, tz=timezone.utc).isoformat()
    else:
        saved_at = datetime.now(timezone.utc).isoformat()
    # Use a hash of the URL for a stable, unique, collision-free file ID
    url_hash = hashlib.sha1(url.encode()).hexdigest()[:12]
    file_id  = f"{saved_ts or int(datetime.now(timezone.utc).timestamp())}_{url_hash}"
    return {
        "id"         : file_id,
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


def write_bookmark(bookmark: dict, dry_run: bool) -> Path:
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

    if not dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(bookmark, indent=2, ensure_ascii=False), encoding="utf-8")

    return out_path


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Migrate Apple Notes bookmarks to JSON.")
    parser.add_argument("--sample",  type=int, metavar="N", help="Process only the first N notes")
    parser.add_argument("--dry-run", action="store_true",   help="Parse and print without writing")
    args = parser.parse_args()

    try:
        total = get_note_count()
    except Exception as e:
        print(f"✗ Could not count notes: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"  {total} notes found in Notes › {NOTES_ACCOUNT} › {NOTES_FOLDER}\n")

    limit = min(args.sample if args.sample else total, total)

    known_urls: set = set()
    if not args.dry_run:
        known_urls = existing_urls()
        if known_urls:
            print(f"  {len(known_urls)} existing bookmarks — duplicates will be skipped.\n")

    written = skipped = empty = errors = 0
    starts  = list(range(1, limit + 1, BATCH_SIZE))

    for batch_num, start in enumerate(starts, 1):
        end = min(start + BATCH_SIZE - 1, limit)
        print(f"Batch {batch_num}/{len(starts)}: notes {start}–{end}…")

        try:
            raw = fetch_batch(start, end)
        except Exception as e:
            print(f"  ✗ Batch failed: {e}", file=sys.stderr)
            errors += (end - start + 1)
            continue

        lines = [l for l in raw.split("\x1d") if l.strip()]

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

                path = write_bookmark(bookmark, dry_run=args.dry_run)

                if args.dry_run:
                    print(f"  [dry-run] {path.relative_to(REPO_ROOT)}")
                    print(f"    url:        {bookmark['url']}")
                    print(f"    title:      {bookmark['title']}")
                    print(f"    annotation: {bookmark['annotation']}")
                    print(f"    saved_at:   {bookmark['saved_at']}")
                    print()
                else:
                    written += 1

        if not args.dry_run:
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