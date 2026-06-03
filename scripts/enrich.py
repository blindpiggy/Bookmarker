#!/usr/bin/env python3
"""
enrich.py
---------
Scans data/bookmarks/**/*.json for files missing an 'og' block,
fetches Open Graph metadata for each URL, and writes it back in place.

Usage:
    python3 scripts/enrich.py

Run this from the repo root. Safe to re-run — already-enriched files
are skipped unless you pass --force to re-fetch everything.

Options:
    --force     Re-fetch OG metadata for all bookmarks, even enriched ones
    --dry-run   Print what would be enriched without writing any files
"""

import argparse
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

# ── Configuration ────────────────────────────────────────────────────────────

REPO_ROOT    = Path(__file__).parent.parent
DATA_DIR     = REPO_ROOT / "data" / "bookmarks"
REQUEST_DELAY = 1.0   # seconds between requests — be polite to servers
TIMEOUT       = 10    # seconds before giving up on a URL
USER_AGENT    = "Mozilla/5.0 (compatible; bookmarks-enricher/1.0)"

# ── OG Metadata Parser ────────────────────────────────────────────────────────

class OGParser(HTMLParser):
    """Extracts Open Graph and standard meta tags from HTML."""

    def __init__(self):
        super().__init__()
        self.og    = {}
        self.title = None
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)

        if tag == "title":
            self._in_title = True

        if tag == "meta":
            prop    = attrs.get("property", "") or attrs.get("name", "")
            content = attrs.get("content", "")

            if prop == "og:title"       : self.og["title"]       = content
            if prop == "og:description" : self.og["description"] = content
            if prop == "og:image"       : self.og["image"]       = content

            # Fallback: standard description meta tag
            if prop == "description" and "description" not in self.og:
                self.og["description"] = content

    def handle_data(self, data):
        if self._in_title and not self.og.get("title"):
            self.title = data.strip()

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False

    def handle_starttag_only(self, tag, attrs):
        pass


# ── Fetch helpers ─────────────────────────────────────────────────────────────

def fetch_og(url: str) -> dict:
    """
    Fetches a URL and extracts OG metadata.
    Returns a dict with keys: title, description, image (all optional).
    Returns an empty dict on any error.
    """
    # Sanitize URL — remove any non-ASCII replacement characters from
    # encoding issues during migration, and encode non-ASCII chars safely
    try:
        url = url.encode('ascii', errors='ignore').decode('ascii')
        if not url.startswith('http'):
            return {}
    except Exception:
        return {}

    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            # Only parse HTML responses
            content_type = resp.headers.get("Content-Type", "")
            if "html" not in content_type:
                return {}

            # Read up to 200KB — enough for <head> on any reasonable page
            raw = resp.read(200_000)
            charset = _detect_charset(content_type, raw)
            html = raw.decode(charset, errors="replace")

    except (urllib.error.URLError, urllib.error.HTTPError, socket.timeout, OSError) as e:
        print(f"    ⚠ Fetch error: {e}", file=sys.stderr)
        return {}

    parser = OGParser()
    try:
        parser.feed(html)
    except Exception:
        pass  # Malformed HTML — return whatever we got

    og = parser.og.copy()

    # If no og:title, fall back to <title> tag
    if not og.get("title") and parser.title:
        og["title"] = parser.title

    # Truncate long descriptions
    if og.get("description"):
        og["description"] = og["description"][:500].strip()

    return og


def _detect_charset(content_type: str, raw: bytes) -> str:
    """Best-effort charset detection from Content-Type or meta tag."""
    match = re.search(r"charset=([^\s;]+)", content_type, re.I)
    if match:
        return match.group(1).strip().lower()

    # Scan the first 2KB for a meta charset declaration
    snippet = raw[:2000].decode("ascii", errors="replace")
    match = re.search(r'charset=["\']?([^"\'\s;>]+)', snippet, re.I)
    if match:
        return match.group(1).strip().lower()

    return "utf-8"


def extract_domain(url: str) -> str:
    """Extracts the bare domain from a URL."""
    match = re.search(r"https?://([^/]+)", url)
    if match:
        domain = match.group(1)
        # Strip www.
        return re.sub(r"^www\.", "", domain)
    return url


# ── File helpers ──────────────────────────────────────────────────────────────

def find_unenriched(force: bool) -> list[Path]:
    """Returns all bookmark JSON files that need enrichment."""
    files = sorted(DATA_DIR.rglob("*.json"))
    if force:
        return files
    return [f for f in files if not _is_enriched(f)]


def _is_enriched(path: Path) -> bool:
    """Returns True if the file already has an enriched_at timestamp."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return bool(data.get("enriched_at"))
    except (json.JSONDecodeError, OSError):
        return False


def enrich_file(path: Path, dry_run: bool) -> bool:
    """
    Enriches a single bookmark JSON file with OG metadata.
    Returns True on success, False on failure.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(f"  ✗ Could not read {path.name}: {e}", file=sys.stderr)
        return False

    url = data.get("url")
    if not url:
        print(f"  ✗ No URL in {path.name} — skipping", file=sys.stderr)
        return False

    print(f"  → {url}")

    if dry_run:
        print(f"    [dry-run] would fetch OG metadata")
        return True

    og = fetch_og(url)

    # Always set domain (doesn't require a successful fetch)
    data["domain"] = extract_domain(url)

    # Write OG block — empty dict if fetch failed, so we know we tried
    data["og"] = {
        "title"      : og.get("title"),
        "description": og.get("description"),
        "image"      : og.get("image"),
    }

    data["enriched_at"] = datetime.now(timezone.utc).isoformat()

    try:
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        print(f"  ✗ Could not write {path.name}: {e}", file=sys.stderr)
        return False

    status = "✓" if og else "⚠ (no OG data found)"
    print(f"    {status}")
    return True


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Enrich bookmark JSON files with OG metadata.")
    parser.add_argument("--force",   action="store_true", help="Re-enrich already-enriched files")
    parser.add_argument("--dry-run", action="store_true", help="Print actions without writing files")
    args = parser.parse_args()

    if not DATA_DIR.exists():
        print(f"✗ Data directory not found: {DATA_DIR}", file=sys.stderr)
        sys.exit(1)

    files = find_unenriched(force=args.force)

    if not files:
        print("✓ All bookmarks already enriched. Pass --force to re-fetch.")
        sys.exit(0)

    label = "dry-run: " if args.dry_run else ""
    print(f"Enriching {len(files)} bookmark(s) {label}...\n")

    success = 0
    failure = 0

    for i, path in enumerate(files):
        ok = enrich_file(path, dry_run=args.dry_run)
        if ok:
            success += 1
        else:
            failure += 1

        # Delay between requests (skip after last file)
        if not args.dry_run and i < len(files) - 1:
            time.sleep(REQUEST_DELAY)

    print(f"\n{'─' * 40}")
    print(f"Done. {success} enriched, {failure} failed.")

    if failure > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()