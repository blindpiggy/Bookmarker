#!/usr/bin/env python3
"""
build.py
--------
Aggregates all bookmark JSON files into a single bookmarks.json,
rebuilds the tags index, and generates the static site HTML.

Usage:
    python3 scripts/build.py

Run from the repo root. Safe to re-run at any time — always does a
full rebuild from source JSON files.

Options:
    --dry-run   Print what would be built without writing any files
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# ── Configuration ─────────────────────────────────────────────────────────────

REPO_ROOT   = Path(__file__).parent.parent
DATA_DIR    = REPO_ROOT / "data" / "bookmarks"
INDEX_DIR   = REPO_ROOT / "index"
SITE_DIR    = REPO_ROOT / "site"
SITE_HTML   = SITE_DIR / "index.html"
SITE_JSON   = SITE_DIR / "bookmarks.json"
SITE_ROBOTS = SITE_DIR / "robots.txt"
TAGS_JSON   = INDEX_DIR / "tags.json"

# Import site config — edit config.py to customise the site
sys.path.insert(0, str(REPO_ROOT / "scripts"))
from config import (
    SITE_NAME,
    SITE_URL,
    AUTHOR,
    LINKS_NOFOLLOW,
    LINKS_NEW_TAB,
    PAGE_SIZE,
    ROBOTS_DISALLOW_ALL,
    ROBOTS_META,
)

# ── Helpers ───────────────────────────────────────────────────────────────────

def merge_tags(original: list, incoming: list) -> list:
    """
    Union of both tag lists, normalized to lowercase, original order first.
    """
    seen = []
    result = []
    for tag in (original or []) + (incoming or []):
        normalized = tag.strip().lower()
        if normalized and normalized not in seen:
            seen.append(normalized)
            result.append(normalized)
    return result


def merge_annotation(original: str | None, incoming: str | None) -> str | None:
    """
    Keep original. If incoming is non-empty and different, append it.
    """
    orig = (original or "").strip()
    inc  = (incoming or "").strip()
    if not inc:
        return original
    if not orig:
        return inc
    if inc.lower() == orig.lower():
        return original
    return f"{orig} {inc}"


def merge_og(original: dict | None, incoming: dict | None) -> dict:
    """
    Keep original OG fields; fill in any missing ones from incoming.
    """
    orig = original or {}
    inc  = incoming or {}
    return {
        "title":       orig.get("title")       or inc.get("title"),
        "description": orig.get("description") or inc.get("description"),
        "image":       orig.get("image")        or inc.get("image"),
    }


def merge_duplicates(bookmarks: list[dict]) -> list[dict]:
    """
    Merges bookmarks sharing the same URL (case-insensitive) into a single
    record. Source files are not touched — merging is in-memory only.

    Merge rules:
      - saved_at:    most recent wins (bookmark sorts to top of feed)
      - tags:        union, normalized to lowercase
      - annotation:  keep original; append incoming if non-empty and different
      - og:          keep original fields; fill missing from duplicate
      - all others:  keep from the record with the earliest saved_at (original)
    """
    seen: dict[str, dict] = {}  # normalized url -> merged record
    dupe_count = 0

    # Process oldest-first so "original" fields come from the earliest record
    for b in sorted(bookmarks, key=lambda x: x.get("saved_at") or x.get("id") or ""):
        key = b["url"].strip().lower()
        if key not in seen:
            seen[key] = dict(b)
        else:
            canon = seen[key]
            dupe_count += 1
            merged = dict(canon)
            merged["tags"]          = merge_tags(canon.get("tags"), b.get("tags"))
            merged["annotation"]    = merge_annotation(canon.get("annotation"), b.get("annotation"))
            merged["og"]            = merge_og(canon.get("og"), b.get("og"))
            # Accumulate all prior dates oldest-first before updating saved_at
            prior = list(canon.get("prior_saved_at") or [])
            prior.append(canon.get("saved_at"))
            merged["prior_saved_at"] = [d for d in prior if d]
            merged["saved_at"]      = b.get("saved_at") or canon.get("saved_at")
            seen[key] = merged

    if dupe_count:
        print(f"  {dupe_count} duplicate(s) merged (source files unchanged)")

    return list(seen.values())


def load_bookmarks() -> list[dict]:
    """
    Loads all bookmark JSON files from data/bookmarks/**/*.json,
    merges duplicates in memory, sorted reverse-chronologically by saved_at.
    Skips and warns on malformed files.
    """
    files = sorted(DATA_DIR.rglob("*.json"))
    bookmarks = []

    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            print(f"  ⚠ Skipping {path.name}: {e}", file=sys.stderr)
            continue

        # Minimum viable bookmark: must have a URL
        if not data.get("url"):
            print(f"  ⚠ Skipping {path.name}: no URL", file=sys.stderr)
            continue

        bookmarks.append(data)

    # Merge duplicates in memory before sorting
    bookmarks = merge_duplicates(bookmarks)

    # Sort reverse-chronologically by saved_at, fall back to file ID
    def sort_key(b):
        return b.get("saved_at") or b.get("id") or ""

    bookmarks.sort(key=sort_key, reverse=True)
    return bookmarks


def build_tag_index(bookmarks: list[dict]) -> dict:
    """
    Builds a tag index: { tag: count } sorted alphabetically.
    """
    counts: dict[str, int] = {}
    for b in bookmarks:
        for tag in b.get("tags") or []:
            tag = tag.strip().lower()
            if tag:
                counts[tag] = counts.get(tag, 0) + 1
    return dict(sorted(counts.items(), key=lambda x: x[1], reverse=True))


def resolve_title(b: dict) -> str:
    """Returns the best available title for a bookmark."""
    og_title = (b.get("og") or {}).get("title")
    return og_title or b.get("title") or b.get("domain") or b.get("url") or "Untitled"


def resolve_description(b: dict) -> str | None:
    """Returns the best available description."""
    return (b.get("og") or {}).get("description")


def resolve_image(b: dict) -> str | None:
    """Returns the OG image URL if available."""
    return (b.get("og") or {}).get("image")


def format_date(iso: str | None) -> str:
    """Formats an ISO date string as 'May 14, 2025'."""
    if not iso:
        return ""
    try:
        # Handle both offset-aware and naive datetimes
        iso_clean = iso[:19]  # strip timezone for parsing
        dt = datetime.fromisoformat(iso_clean)
        return dt.strftime("%-d %b %Y")
    except ValueError:
        return iso[:10] if len(iso) >= 10 else iso


def escape_html(s: str | None) -> str:
    """Escapes HTML special characters."""
    if not s:
        return ""
    return (
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
         .replace('"', "&quot;")
         .replace("'", "&#39;")
    )


def escape_js(s: str | None) -> str:
    """Escapes a string for safe embedding in a JS string literal."""
    if not s:
        return ""
    return (
        s.replace("\\", "\\\\")
         .replace('"', '\\"')
         .replace("\n", "\\n")
         .replace("\r", "")
         .replace("</", "<\\/")  # prevent </script> injection
    )


# ── Site generation ───────────────────────────────────────────────────────────

def build_bookmarks_json(bookmarks: list[dict]) -> str:
    """
    Builds a lean bookmarks.json for Fuse.js — only the fields
    needed for search and display. Keeps file size down.
    """
    lean = []
    for b in bookmarks:
        lean.append({
            "id"         : b.get("id"),
            "url"        : b.get("url"),
            "title"      : resolve_title(b),
            "description": resolve_description(b),
            "image"      : resolve_image(b),
            "annotation" : b.get("annotation"),
            "domain"     : b.get("domain"),
            "date"       : b.get("saved_at"),
            "priorDates" : b.get("prior_saved_at") or [],
            "tags"       : b.get("tags") or [],
        })
    return json.dumps(lean, indent=2, ensure_ascii=False)


def build_html(bookmarks: list[dict], tag_index: dict) -> str:
    """Generates the full static site HTML."""

    total      = len(bookmarks)
    built_at   = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    tags_json  = json.dumps(tag_index, ensure_ascii=False)

    # Config-derived template values
    robots_meta_tag = '<meta name="robots" content="noindex, nofollow">\n  ' if ROBOTS_META else ''
    link_target     = ' target="_blank"' if LINKS_NEW_TAB else ''
    link_rel_parts  = ['noopener'] + (['nofollow'] if LINKS_NOFOLLOW else [])
    link_rel        = f' rel="{" ".join(link_rel_parts)}"'  

    # Build the lean bookmarks payload for inline JS
    lean = []
    for b in bookmarks:
        lean.append({
            "id"         : b.get("id"),
            "url"        : b.get("url"),
            "title"      : resolve_title(b),
            "description": resolve_description(b),
            "image"      : resolve_image(b),
            "annotation" : b.get("annotation"),
            "domain"     : b.get("domain"),
            "date"       : b.get("saved_at"),
            "priorDates" : b.get("prior_saved_at") or [],
            "tags"       : b.get("tags") or [],
        })

    bookmarks_json = json.dumps(lean, ensure_ascii=False)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta name="generator" content="bookmarks-build/{built_at}">
  <title>{SITE_NAME}</title>
  {robots_meta_tag}<link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600&display=swap" rel="stylesheet">
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

    :root {{
      /* ── Grey scale ── */
      --gray-000: #ffffff;
      --gray-100: #f7f7f7;
      --gray-200: #f0f0f0;
      --gray-300: #e8e8e8;
      --gray-400: #e0e0e0;
      --gray-500: #c8c8c8;
      --gray-600: #a0a0a0;
      --gray-700: #6b6b6b;
      --gray-800: #1a1a1a;

      /* ── Semantic aliases ── */
      --bg:             var(--gray-100);
      --surface:        var(--gray-000);
      --border:         var(--gray-400);
      --border-mid:     var(--gray-500);
      --text-primary:   var(--gray-800);
      --text-secondary: var(--gray-700);
      --text-tertiary:  var(--gray-600);
      --accent:         var(--gray-800);
      --accent-fg:      var(--gray-000);
      --tag-bg:         var(--gray-400);

      /* ── Shadows ── */
      --shadow: 0 1px 3px rgba(0,0,0,0.06), 0 1px 2px rgba(0,0,0,0.04);
      --shadow-md: 0 4px 12px rgba(0,0,0,0.08), 0 2px 4px rgba(0,0,0,0.04);

      /* ── Radii ── */
      --radius: 10px;
      --radius-sm: 6px;
      --radius-pill: 999px;

      /* ── Typography ── */
      --font-sans: 'Outfit', system-ui, sans-serif;

      /* ── Layout ── */
      --max-w: 700px;
    }}

    body {{
      background: var(--bg);
      color: var(--text-primary);
      font-family: var(--font-sans);
      font-size: 15px;
      line-height: 1.6;
      min-height: 100vh;
      -webkit-font-smoothing: antialiased;
    }}

    .wrapper {{
      max-width: var(--max-w);
      margin: 0 auto;
      padding: 15px 20px 170px;
    }}

    /* ── Gradient blur ── */
    .gradient-blur {{
      position: fixed;
      inset: 0;
      height: 90px;
      width: 100%;
      z-index: 5;
      pointer-events: none;
      background: linear-gradient(to bottom, var(--bg) 0%, var(--bg) 10%, transparent 100%);
    }}
    .gradient-blur > div,
    .gradient-blur::before,
    .gradient-blur::after {{
      position: absolute;
      inset: 0;
    }}
    .gradient-blur::before {{
      content: "";
      z-index: 1;
      -webkit-backdrop-filter: blur(0.5px);
      backdrop-filter: blur(0.5px);
      -webkit-mask: linear-gradient(to top, rgba(255,255,255,0) 0%, rgba(255,255,255,1) 12.5%, rgba(255,255,255,1) 25%, rgba(255,255,255,0) 37.5%);
      mask: linear-gradient(to top, rgba(255,255,255,0) 0%, rgba(255,255,255,1) 12.5%, rgba(255,255,255,1) 25%, rgba(255,255,255,0) 37.5%);
    }}
    .gradient-blur > div:nth-of-type(1) {{
      z-index: 2;
      -webkit-backdrop-filter: blur(1px);
      backdrop-filter: blur(1px);
      -webkit-mask: linear-gradient(to top, rgba(255,255,255,0) 12.5%, rgba(255,255,255,1) 25%, rgba(255,255,255,1) 37.5%, rgba(255,255,255,0) 50%);
      mask: linear-gradient(to top, rgba(255,255,255,0) 12.5%, rgba(255,255,255,1) 25%, rgba(255,255,255,1) 37.5%, rgba(255,255,255,0) 50%);
    }}
    .gradient-blur > div:nth-of-type(2) {{
      z-index: 3;
      -webkit-backdrop-filter: blur(2px);
      backdrop-filter: blur(2px);
      -webkit-mask: linear-gradient(to top, rgba(255,255,255,0) 25%, rgba(255,255,255,1) 37.5%, rgba(255,255,255,1) 50%, rgba(255,255,255,0) 62.5%);
      mask: linear-gradient(to top, rgba(255,255,255,0) 25%, rgba(255,255,255,1) 37.5%, rgba(255,255,255,1) 50%, rgba(255,255,255,0) 62.5%);
    }}
    .gradient-blur > div:nth-of-type(3) {{
      z-index: 4;
      -webkit-backdrop-filter: blur(4px);
      backdrop-filter: blur(4px);
      -webkit-mask: linear-gradient(to top, rgba(255,255,255,0) 37.5%, rgba(255,255,255,1) 50%, rgba(255,255,255,1) 62.5%, rgba(255,255,255,0) 75%);
      mask: linear-gradient(to top, rgba(255,255,255,0) 37.5%, rgba(255,255,255,1) 50%, rgba(255,255,255,1) 62.5%, rgba(255,255,255,0) 75%);
    }}
    .gradient-blur > div:nth-of-type(4) {{
      z-index: 5;
      -webkit-backdrop-filter: blur(5px);
      backdrop-filter: blur(5px);
      -webkit-mask: linear-gradient(to top, rgba(255,255,255,0) 50%, rgba(255,255,255,1) 62.5%, rgba(255,255,255,1) 75%, rgba(255,255,255,0) 87.5%);
      mask: linear-gradient(to top, rgba(255,255,255,0) 50%, rgba(255,255,255,1) 62.5%, rgba(255,255,255,1) 75%, rgba(255,255,255,0) 87.5%);
    }}
    .gradient-blur > div:nth-of-type(5) {{
      z-index: 6;
      -webkit-backdrop-filter: blur(7px);
      backdrop-filter: blur(7px);
      -webkit-mask: linear-gradient(to top, rgba(255,255,255,0) 62.5%, rgba(255,255,255,1) 75%, rgba(255,255,255,1) 87.5%, rgba(255,255,255,0) 100%);
      mask: linear-gradient(to top, rgba(255,255,255,0) 62.5%, rgba(255,255,255,1) 75%, rgba(255,255,255,1) 87.5%, rgba(255,255,255,0) 100%);
    }}
    .gradient-blur > div:nth-of-type(6) {{
      z-index: 7;
      -webkit-backdrop-filter: blur(16px);
      backdrop-filter: blur(16px);
      -webkit-mask: linear-gradient(to top, rgba(255,255,255,0) 75%, rgba(255,255,255,1) 87.5%, rgba(255,255,255,1) 100%);
      mask: linear-gradient(to top, rgba(255,255,255,0) 75%, rgba(255,255,255,1) 87.5%, rgba(255,255,255,1) 100%);
    }}
    .gradient-blur::after {{
      content: "";
      z-index: 8;
      -webkit-backdrop-filter: blur(24px);
      backdrop-filter: blur(24px);
      -webkit-mask: linear-gradient(to top, rgba(255,255,255,0) 87.5%, rgba(255,255,255,1) 100%);
      mask: linear-gradient(to top, rgba(255,255,255,0) 87.5%, rgba(255,255,255,1) 100%);
    }}

    /* ── Gradient blur (bottom) ── */
    .gradient-blur-bottom {{
      position: fixed;
      bottom: 0;
      left: 0;
      right: 0;
      height: 150px;
      z-index: 5;
      pointer-events: none;
      background: linear-gradient(to top, var(--bg) 0%, var(--bg) 10%, transparent 100%);
    }}
    .gradient-blur-bottom > div,
    .gradient-blur-bottom::before,
    .gradient-blur-bottom::after {{
      position: absolute;
      inset: 0;
    }}
    .gradient-blur-bottom::before {{
      content: "";
      z-index: 1;
      -webkit-backdrop-filter: blur(0.5px);
      backdrop-filter: blur(0.5px);
      -webkit-mask: linear-gradient(to bottom, rgba(255,255,255,0) 0%, rgba(255,255,255,1) 12.5%, rgba(255,255,255,1) 25%, rgba(255,255,255,0) 37.5%);
      mask: linear-gradient(to bottom, rgba(255,255,255,0) 0%, rgba(255,255,255,1) 12.5%, rgba(255,255,255,1) 25%, rgba(255,255,255,0) 37.5%);
    }}
    .gradient-blur-bottom > div:nth-of-type(1) {{
      z-index: 2;
      -webkit-backdrop-filter: blur(1px);
      backdrop-filter: blur(1px);
      -webkit-mask: linear-gradient(to bottom, rgba(255,255,255,0) 12.5%, rgba(255,255,255,1) 25%, rgba(255,255,255,1) 37.5%, rgba(255,255,255,0) 50%);
      mask: linear-gradient(to bottom, rgba(255,255,255,0) 12.5%, rgba(255,255,255,1) 25%, rgba(255,255,255,1) 37.5%, rgba(255,255,255,0) 50%);
    }}
    .gradient-blur-bottom > div:nth-of-type(2) {{
      z-index: 3;
      -webkit-backdrop-filter: blur(2px);
      backdrop-filter: blur(2px);
      -webkit-mask: linear-gradient(to bottom, rgba(255,255,255,0) 25%, rgba(255,255,255,1) 37.5%, rgba(255,255,255,1) 50%, rgba(255,255,255,0) 62.5%);
      mask: linear-gradient(to bottom, rgba(255,255,255,0) 25%, rgba(255,255,255,1) 37.5%, rgba(255,255,255,1) 50%, rgba(255,255,255,0) 62.5%);
    }}
    .gradient-blur-bottom > div:nth-of-type(3) {{
      z-index: 4;
      -webkit-backdrop-filter: blur(4px);
      backdrop-filter: blur(4px);
      -webkit-mask: linear-gradient(to bottom, rgba(255,255,255,0) 37.5%, rgba(255,255,255,1) 50%, rgba(255,255,255,1) 62.5%, rgba(255,255,255,0) 75%);
      mask: linear-gradient(to bottom, rgba(255,255,255,0) 37.5%, rgba(255,255,255,1) 50%, rgba(255,255,255,1) 62.5%, rgba(255,255,255,0) 75%);
    }}
    .gradient-blur-bottom > div:nth-of-type(4) {{
      z-index: 5;
      -webkit-backdrop-filter: blur(5px);
      backdrop-filter: blur(5px);
      -webkit-mask: linear-gradient(to bottom, rgba(255,255,255,0) 50%, rgba(255,255,255,1) 62.5%, rgba(255,255,255,1) 75%, rgba(255,255,255,0) 87.5%);
      mask: linear-gradient(to bottom, rgba(255,255,255,0) 50%, rgba(255,255,255,1) 62.5%, rgba(255,255,255,1) 75%, rgba(255,255,255,0) 87.5%);
    }}
    .gradient-blur-bottom > div:nth-of-type(5) {{
      z-index: 6;
      -webkit-backdrop-filter: blur(7px);
      backdrop-filter: blur(7px);
      -webkit-mask: linear-gradient(to bottom, rgba(255,255,255,0) 62.5%, rgba(255,255,255,1) 75%, rgba(255,255,255,1) 87.5%, rgba(255,255,255,0) 100%);
      mask: linear-gradient(to bottom, rgba(255,255,255,0) 62.5%, rgba(255,255,255,1) 75%, rgba(255,255,255,1) 87.5%, rgba(255,255,255,0) 100%);
    }}
    .gradient-blur-bottom > div:nth-of-type(6) {{
      z-index: 7;
      -webkit-backdrop-filter: blur(16px);
      backdrop-filter: blur(16px);
      -webkit-mask: linear-gradient(to bottom, rgba(255,255,255,0) 75%, rgba(255,255,255,1) 87.5%, rgba(255,255,255,1) 100%);
      mask: linear-gradient(to bottom, rgba(255,255,255,0) 75%, rgba(255,255,255,1) 87.5%, rgba(255,255,255,1) 100%);
    }}
    .gradient-blur-bottom::after {{
      content: "";
      z-index: 8;
      -webkit-backdrop-filter: blur(24px);
      backdrop-filter: blur(24px);
      -webkit-mask: linear-gradient(to bottom, rgba(255,255,255,0) 87.5%, rgba(255,255,255,1) 100%);
      mask: linear-gradient(to bottom, rgba(255,255,255,0) 87.5%, rgba(255,255,255,1) 100%);
    }}

    /* ── Header ── */
    header {{
      background: rgba(255, 255, 255, 0.80);
      backdrop-filter: blur(8px);
      -webkit-backdrop-filter: blur(8px);
      border: 1px solid rgba(255, 255, 255, 0.50);
      border-radius: var(--radius-pill);
      box-shadow: var(--shadow);
      width: calc(100% - 32px);
      max-width: var(--max-w);
      margin: 16px auto 16px;
      position: sticky;
      top: 16px;
      z-index: 100;
    }}

    .header-inner {{
      display: grid;
      grid-template-columns: 1fr auto 1fr;
      align-items: center;
      padding: 4px;
    }}

    .header-left {{
      display: flex;
      align-items: center;
      justify-content: flex-start;
    }}

    .header-center {{
      display: flex;
      align-items: center;
      justify-content: center;
    }}

    .header-right {{
      display: flex;
      align-items: center;
      justify-content: flex-end;
      gap: 4px;
    }}

    .site-brand {{
      display: flex;
      align-items: center;
      gap: 10px;
      text-decoration: none;
    }}

    .site-name {{
      font-family: var(--font-sans);
      font-size: 17px;
      font-weight: 600;
      color: var(--text-primary);
      letter-spacing: 0.01em;
    }}

    .header-controls {{
      display: flex;
      align-items: center;
      gap: 4px;
    }}

    /* kept for JS compat — actual layout uses .header-right */

    .search-expand {{
      overflow: hidden;
      max-width: 0;
      opacity: 0;
      transition: max-width 0.22s ease, opacity 0.18s ease;
      display: flex;
      align-items: center;
    }}

    .search-expand.open {{
      max-width: 240px;
      opacity: 1;
    }}

    .search-expand input {{
      width: 210px;
      font-family: var(--font-sans);
      font-size: 13px;
      background: var(--surface);
      border: 1px solid var(--border-mid);
      border-radius: var(--radius-sm);
      padding: 6px 10px;
      color: var(--text-primary);
      outline: none;
      transition: border-color 0.15s;
    }}

    .search-expand input::placeholder {{ color: var(--text-tertiary); }}
    .search-expand input:focus {{ border-color: var(--accent); }}

    .icon-btn {{
      width: 34px;
      height: 34px;
      border: 1px solid transparent;
      border-radius: var(--radius-pill);
      background: transparent;
      display: flex;
      align-items: center;
      justify-content: center;
      cursor: pointer;
      color: var(--text-secondary);
      transition: background 0.12s, border-color 0.12s, color 0.12s;
      flex-shrink: 0;
    }}

    .icon-btn:hover {{
      background: var(--surface);
      border-color: var(--border);
      color: var(--text-primary);
    }}

    .icon-btn.active {{
      background: var(--accent);
      border-color: var(--accent);
      color: var(--accent-fg);
    }}

    .icon-btn svg {{
      width: 17px;
      height: 17px;
      stroke: currentColor;
      fill: none;
      stroke-width: 2;
      stroke-linecap: round;
      stroke-linejoin: round;
    }}

    /* ── Back to top ── */
    .to-top-btn {{
      position: fixed;
      bottom: 24px;
      left: 50%;
      transform: translateX(-50%) translateY(8px);
      z-index: 50;
      width: 38px;
      height: 38px;
      border-radius: var(--radius-pill);
      background: rgba(255, 255, 255, 0.80);
      backdrop-filter: blur(8px);
      -webkit-backdrop-filter: blur(8px);
      border: 1px solid rgba(255, 255, 255, 0.50);
      box-shadow: var(--shadow);
      display: flex;
      align-items: center;
      justify-content: center;
      cursor: pointer;
      color: var(--text-secondary);
      opacity: 0;
      visibility: hidden;
      pointer-events: none;
      transition: opacity 0.2s ease, transform 0.2s ease, color 0.12s;
    }}

    .to-top-btn.visible {{
      opacity: 1;
      visibility: visible;
      pointer-events: auto;
      transform: translateX(-50%) translateY(0);
    }}

    .to-top-btn:hover {{
      color: var(--text-primary);
    }}

    .to-top-btn svg {{
      width: 18px;
      height: 18px;
      stroke: currentColor;
      fill: none;
      stroke-width: 2;
      stroke-linecap: round;
      stroke-linejoin: round;
    }}

    /* ── Tag Dropdown ── */
    .tag-dropdown-wrap {{ position: relative; }}

    .tag-dropdown {{
      position: absolute;
      top: calc(100% + 8px);
      left: 0;
      background: var(--surface);
      border: 1px solid var(--border-mid);
      border-radius: var(--radius);
      padding: 14px;
      min-width: 240px;
      display: none;
      z-index: 200;
      box-shadow: var(--shadow-md);
    }}

    .tag-dropdown.open {{ display: block; }}

    .tag-dropdown-heading {{
      font-size: 10px;
      color: var(--text-tertiary);
      text-transform: uppercase;
      letter-spacing: 0.08em;
      margin-bottom: 10px;
      font-weight: 500;
    }}

    .tag-pills-wrap {{ display: flex; gap: 6px; flex-wrap: wrap; }}

    .tag-pill {{
      font-size: 12px;
      font-family: var(--font-sans);
      padding: 4px 11px;
      border-radius: var(--radius-pill);
      border: 1px solid var(--border-mid);
      color: var(--text-secondary);
      cursor: pointer;
      background: var(--bg);
      transition: background 0.12s, color 0.12s, border-color 0.12s;
    }}

    .tag-pill:hover {{
      border-color: var(--accent);
      color: var(--text-primary);
    }}

    .tag-pill.active {{
      background: var(--accent);
      color: var(--accent-fg);
      border-color: var(--accent);
    }}

    /* ── Feed State ── */
    .feed-state {{
      display: flex;
      align-items: center;
      gap: 6px;
      flex-wrap: wrap;
      justify-content: space-between;
      margin-bottom: 16px;
      min-height: 26px;
    }}

    .state-pill {{
      display: inline-flex;
      align-items: center;
      gap: 5px;
      font-size: 12px;
      padding: 3px 8px 3px 8px;
      border-radius: var(--radius-pill);
      border: 1px solid var(--border-mid);
      color: var(--text-secondary);
      background: var(--surface);
    }}

    .state-pill-icon {{
      width: 11px;
      height: 11px;
      stroke: currentColor;
      fill: none;
      stroke-width: 2;
      stroke-linecap: round;
      stroke-linejoin: round;
      flex-shrink: 0;
    }}

    .state-pill-x {{
      display: flex;
      align-items: center;
      background: none;
      border: none;
      cursor: pointer;
      padding: 0;
      color: var(--text-tertiary);
      transition: color 0.12s;
      line-height: 1;
    }}

    .state-pill-x:hover {{ color: var(--text-primary); }}

    .state-pill-x svg {{
      width: 10px;
      height: 10px;
      stroke: currentColor;
      fill: none;
      stroke-width: 1;
      stroke-linecap: round;
    }}

    .results-count {{
      font-size: 12px;
      font-weight: 600;
      color: var(--text-primary);
    }}

    .refresh-btn {{
      width: 26px;
      height: 26px;
      border: 1px solid transparent;
      border-radius: var(--radius-sm);
      background: transparent;
      display: flex;
      align-items: center;
      justify-content: center;
      cursor: pointer;
      color: var(--text-tertiary);
      transition: background 0.12s, border-color 0.12s, color 0.12s;
      flex-shrink: 0;
      margin-left: auto;
    }}

    .refresh-btn:hover {{
      background: var(--surface);
      border-color: var(--border);
      color: var(--text-primary);
    }}

    .refresh-btn svg {{
      width: 14px;
      height: 14px;
      stroke: currentColor;
      fill: none;
      stroke-width: 2;
      stroke-linecap: round;
      stroke-linejoin: round;
    }}

    /* ── Cards ── */
    .feed {{ display: flex; flex-direction: column; gap: 12px; }}

    .card {{
      background: var(--surface);
      border-radius: 16px;
      overflow: hidden;
      display: flex;
      flex-direction: row;
      position: relative;
      text-decoration: none;
      color: inherit;
      transition: all 0.3s ease-in-out 0.1s;
    }}

    .card::after {{
      content: '';
      position: absolute;
      inset: 0;
      border-radius: 16px;
      border: 2px solid rgba(255, 255, 255, 0.50);
      pointer-events: none;
      z-index: 1;
      transition: border-color 200ms ease;
    }}

    .card:hover::after {{
      border-color: var(--gray-400);
    }}

    .card:hover {{
      margin: 0 -0.125rem;
    }}

    .card-img {{
      width: 160px;
      min-width: 160px;
      background: var(--gray-200);
      display: flex;
      align-items: center;
      justify-content: center;
      flex-shrink: 0;
      overflow: hidden;
      order: 2;
    }}

    .card-img img {{
      width: 100%;
      height: 100%;
      object-fit: cover;
      display: block;
      mix-blend-mode: multiply;
    }}

    .card-img-link {{
      display: block;
      width: 100%;
      height: 100%;
      cursor: pointer;
    }}

    .card-body {{
      padding: 14px 16px;
      flex: 1;
      min-width: 0;
      display: flex;
      flex-direction: column;
      gap: 6px;
    }}

    .card-meta {{
      display: flex;
      align-items: center;
      gap: 6px;
    }}

    .card-meta-domain,
    .card-meta-sep,
    .card-meta-date {{
      font-size: 11px;
      color: var(--text-tertiary);
    }}

    .card-meta-date-prior {{
      font-size: 11px;
      color: var(--text-tertiary);
      opacity: 0.7;
    }}

    .card-title {{
      font-family: var(--font-sans);
      font-size: 16px;
      font-weight: 600;
      color: var(--text-primary);
      line-height: 1.35;
      display: -webkit-box;
      -webkit-line-clamp: 3;
      -webkit-box-orient: vertical;
      overflow: hidden;
    }}

    .card-title-link {{
      color: inherit;
      text-decoration: none;
    }}

    .card-title-link:hover {{
      text-decoration: underline;
      text-underline-offset: 3px;
    }}

    .card-excerpt {{
      font-size: 13px;
      color: var(--text-secondary);
      line-height: 1.6;
      display: -webkit-box;
      -webkit-line-clamp: 3;
      -webkit-box-orient: vertical;
      overflow: hidden;
    }}

    .card-annotation {{
      display: inline-flex;
      padding: 4px 12px 4px 8px;
      align-items: flex-start;
      gap: 8px;
      border-radius: 14px;
      background: var(--gray-200);
    }}

    .card-annotation-icon {{
      width: 13px;
      height: 13px;
      stroke: var(--text-tertiary);
      fill: none;
      stroke-width: 2;
      stroke-linecap: round;
      stroke-linejoin: round;
      flex-shrink: 0;
      margin-top: 2px;
    }}

    .card-annotation p {{
      font-size: 12px;
      color: var(--text-secondary);
      line-height: 1.55;
      flex: 1;
    }}

    .card-tags {{
      display: flex;
      gap: 4px;
      row-gap: 8px;
      flex-wrap: wrap;
      align-items: center;
      margin-top: auto;
      padding-top: 2px;
    }}

    .card-tag {{
      font-size: 11px;
      padding: 4px 10px;
      border-radius: var(--radius-pill);
      background: var(--tag-bg);
      color: var(--text-secondary);
      line-height: 1.55;
      cursor: pointer;
      transition: background 0.12s, color 0.12s;
    }}

    .card-tag:hover {{
      background: var(--border-mid);
      color: var(--text-primary);
    }}

    /* ── Lazy load sentinel ── */
    .load-sentinel {{
      height: 40px;
      display: flex;
      align-items: center;
      justify-content: center;
    }}

    .load-sentinel span {{
      font-size: 12px;
      color: var(--text-tertiary);
    }}

    /* ── Empty state ── */
    .empty-state {{
      text-align: center;
      padding: 60px 20px;
      color: var(--text-tertiary);
      display: none;
    }}

    .empty-state.visible {{ display: block; }}
    .empty-state p {{ font-size: 14px; margin-top: 8px; }}

    /* ── Responsive ── */
    @media (max-width: 540px) {{
      .card {{ flex-direction: column; }}
      .card-img {{
        width: 100%;
        min-width: unset;
        height: 180px;
        order: -1;
      }}
      .site-name {{ display: none; }}
      .search-expand.open {{ max-width: 180px; }}
      .search-expand input {{ width: 160px; }}
    }}
  </style>
</head>
<body>
  <header>
    <div class="header-inner">
      <div class="header-left">
        <div class="tag-dropdown-wrap">
          <button class="icon-btn" id="tagBtn" aria-label="Filter by tag">
            <svg viewBox="0 0 24 24"><path d="M20.59 13.41l-7.17 7.17a2 2 0 0 1-2.83 0L2 12V2h10l8.59 8.59a2 2 0 0 1 0 2.82z"/><line x1="7" y1="7" x2="7.01" y2="7"/></svg>
          </button>
          <div class="tag-dropdown" id="tagDropdown" role="listbox" aria-label="Filter by tag">
            <p class="tag-dropdown-heading">Filter by tag</p>
            <div class="tag-pills-wrap" id="tagPillsWrap"></div>
          </div>
        </div>
      </div>
      <div class="header-center">
        <a class="site-brand" href="/">
          <span class="site-name">{SITE_NAME}</span>
        </a>
      </div>
      <div class="header-right">
        <div class="search-expand" id="searchExpand">
          <input type="text" id="searchInput" placeholder="Search bookmarks…" aria-label="Search bookmarks" autocomplete="off">
        </div>
        <button class="icon-btn" id="searchBtn" aria-label="Toggle search (⌘K)">
          <svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
        </button>
      </div>
    </div>
  </header>

  <div class="gradient-blur">
    <div></div>
    <div></div>
    <div></div>
    <div></div>
    <div></div>
    <div></div>
  </div>

  <div class="gradient-blur-bottom">
    <div></div>
    <div></div>
    <div></div>
    <div></div>
    <div></div>
    <div></div>
  </div>

<div class="wrapper">

  <div class="feed-state" id="feedState" aria-live="polite"></div>
  <main class="feed" id="feed" aria-label="Bookmarks feed"></main>
  <div class="load-sentinel" id="loadSentinel" style="display:none">
    <span>Loading more…</span>
  </div>
  <div class="empty-state" id="emptyState">
    <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="var(--gray-600)" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
      <circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>
    </svg>
    <p>No results found.</p>
  </div>

</div>

<button class="to-top-btn" id="toTopBtn" aria-label="Back to top">
  <svg viewBox="0 0 24 24"><line x1="12" y1="19" x2="12" y2="5"/><polyline points="5 12 12 5 19 12"/></svg>
</button>

<script>
// ── Data (injected at build time) ──
const BOOKMARKS  = {bookmarks_json};
const TAG_INDEX  = {tags_json};
const PAGE_SIZE  = {PAGE_SIZE};

// ── State ──
let activeTag    = 'all';
let activeQuery  = '';
let searchOpen   = false;
let dropdownOpen = false;
let visibleCount = PAGE_SIZE;
let filtered     = [];

// ── Elements ──
const searchBtn    = document.getElementById('searchBtn');
const searchExpand = document.getElementById('searchExpand');
const searchInput  = document.getElementById('searchInput');
const tagBtn       = document.getElementById('tagBtn');
const tagDropdown  = document.getElementById('tagDropdown');
const tagPillsWrap = document.getElementById('tagPillsWrap');
const feedState    = document.getElementById('feedState');
const feed         = document.getElementById('feed');
const emptyState   = document.getElementById('emptyState');
const loadSentinel = document.getElementById('loadSentinel');

// ── Tag pills ──
function buildTagPills() {{
  tagPillsWrap.innerHTML = '';
  const allPill = makePill('All', 'all');
  tagPillsWrap.appendChild(allPill);
  Object.keys(TAG_INDEX).forEach(tag => {{
    tagPillsWrap.appendChild(makePill(tag, tag));
  }});
}}

function makePill(label, tag) {{
  const btn = document.createElement('button');
  btn.className = 'tag-pill' + (activeTag === tag ? ' active' : '');
  btn.textContent = label;
  btn.dataset.tag = tag;
  btn.addEventListener('click', () => selectTag(tag));
  return btn;
}}

function selectTag(tag) {{
  activeTag = tag;
  buildTagPills();
  tagBtn.classList.toggle('active', activeTag !== 'all');
  closeDropdown();
  pushURL();
  resetAndRender();
}}

// ── Search ──
function openSearch() {{
  searchOpen = true;
  searchExpand.classList.add('open');
  searchBtn.classList.add('active');
  setTimeout(() => searchInput.focus(), 180);
}}

function closeSearch() {{
  searchOpen = false;
  searchExpand.classList.remove('open');
  searchBtn.classList.remove('active');
  searchInput.value = '';
  activeQuery = '';
  pushURL();
  resetAndRender();
}}

function closeDropdown() {{
  dropdownOpen = false;
  tagDropdown.classList.remove('open');
}}

// ── Filtering ──
function getFiltered() {{
  return BOOKMARKS.filter(b => {{
    const matchTag = activeTag === 'all' || (b.tags || []).map(t => t.trim().toLowerCase()).includes(activeTag.toLowerCase());
    const q = activeQuery.toLowerCase();
    const matchQuery = !q || [b.title, b.description, b.annotation, b.domain, ...(b.tags || [])].some(f => f && f.toLowerCase().includes(q));
    return matchTag && matchQuery;
  }});
}}

// ── Feed state ──
function updateFeedState() {{
  feedState.innerHTML = '';
  const hasTag   = activeTag !== 'all';
  const hasQuery = activeQuery.length > 0;

  if (hasTag) {{
    feedState.appendChild(makeClearPill(activeTag, 'tag', () => selectTag('all')));
  }}
  if (hasQuery) {{
    feedState.appendChild(makeClearPill(`"${{activeQuery}}"`, 'search', () => closeSearch()));
  }}

  const count = document.createElement('span');
  count.className = 'results-count';
  count.textContent = `${{filtered.length}} result${{filtered.length !== 1 ? 's' : ''}}`;
  feedState.appendChild(count);

  const refreshBtn = document.createElement('button');
  refreshBtn.className = 'refresh-btn';
  refreshBtn.setAttribute('aria-label', 'Refresh');
  refreshBtn.innerHTML = `<svg viewBox="0 0 24 24"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>`;
  refreshBtn.addEventListener('click', () => window.location.reload());
  feedState.appendChild(refreshBtn);
}}

function makeClearPill(label, type, onClear) {{
  const pill = document.createElement('span');
  pill.className = 'state-pill';

  const icon = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  icon.setAttribute('viewBox', '0 0 24 24');
  icon.classList.add('state-pill-icon');
  if (type === 'tag') {{
    icon.innerHTML = `<path d="M20.59 13.41l-7.17 7.17a2 2 0 0 1-2.83 0L2 12V2h10l8.59 8.59a2 2 0 0 1 0 2.82z"/><line x1="7" y1="7" x2="7.01" y2="7"/>`;
  }} else {{
    icon.innerHTML = `<circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>`;
  }}
  pill.appendChild(icon);
  pill.appendChild(document.createTextNode(label));

  const btn = document.createElement('button');
  btn.className = 'state-pill-x';
  btn.setAttribute('aria-label', `Clear ${{label}}`);
  btn.innerHTML = `<svg viewBox="0 0 12 12"><line x1="2" y1="2" x2="10" y2="10"/><line x1="10" y1="2" x2="2" y2="10"/></svg>`;
  btn.addEventListener('click', onClear);
  pill.appendChild(btn);
  return pill;
}}

// ── Date formatting ──
function formatDate(iso) {{
  if (!iso) return '';
  try {{
    const d = new Date(iso);
    return d.toLocaleDateString('en-US', {{ month: 'short', day: 'numeric', year: 'numeric' }});
  }} catch(e) {{ return iso.slice(0, 10); }}
}}

// ── Card rendering ──
function renderCard(b) {{
  const article = document.createElement('article');
  article.className = 'card';
  article.setAttribute('data-source', b.id);

  const imgHTML = (b.image && !b.image.endsWith('/'))
    ? `<div class="card-img"><a class="card-img-link" href="${{b.url}}"{link_target}{link_rel}><img src="${{b.image}}" alt="" loading="lazy" onerror="this.closest('.card-img').style.display='none'"></a></div>`
    : '';

  const tagsHTML = (b.tags || [])
    .filter(t => t && t.trim())
    .map(t => `<span class="card-tag" data-tag="${{t.trim().toLowerCase()}}">${{t.trim().toLowerCase()}}</span>`).join('');
  const annotationHTML = b.annotation ? `<div class="card-annotation"><svg class="card-annotation-icon" viewBox="0 0 24 24"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg><p>${{b.annotation}}</p></div>` : '';
  const tagsRowHTML = (annotationHTML || tagsHTML)
    ? `<div class="card-tags">${{annotationHTML}}${{tagsHTML}}</div>`
    : '';
  const excerptHTML = b.description ? `<p class="card-excerpt">${{b.description}}</p>` : '';

  const priorDatesHTML = (b.priorDates || [])
    .map(d => `<span class="card-meta-date-prior">${{formatDate(d)}}</span><span class="card-meta-sep">·</span>`)
    .join('');

  article.innerHTML = `
    <div class="card-body">
      <div class="card-meta">
        ${{priorDatesHTML}}<span class="card-meta-date">${{formatDate(b.date)}}</span>
        ${{b.domain ? '<span class="card-meta-sep">·</span>' : ''}}
        <span class="card-meta-domain">${{b.domain || ''}}</span>
      </div>
      <h2 class="card-title"><a class="card-title-link" href="${{b.url}}"{link_target}{link_rel}>${{b.title || 'Untitled'}}</a></h2>
      ${{excerptHTML}}
      ${{tagsRowHTML}}
    </div>
    ${{imgHTML}}`;

  return article;
}}

function renderVisible() {{
  const slice = filtered.slice(0, visibleCount);
  feed.innerHTML = '';
  slice.forEach(b => feed.appendChild(renderCard(b)));

  const hasMore = visibleCount < filtered.length;
  loadSentinel.style.display = hasMore ? 'flex' : 'none';
  emptyState.classList.toggle('visible', filtered.length === 0);
}}

function resetAndRender() {{
  filtered     = getFiltered();
  visibleCount = PAGE_SIZE;
  updateFeedState();
  renderVisible();
}}

// ── Lazy loading via IntersectionObserver ──
const observer = new IntersectionObserver((entries) => {{
  if (entries[0].isIntersecting && visibleCount < filtered.length) {{
    visibleCount += PAGE_SIZE;
    renderVisible();
  }}
}}, {{ rootMargin: '200px' }});

observer.observe(loadSentinel);

// ── URL state ──
function pushURL() {{
  const params = new URLSearchParams();
  if (activeTag !== 'all') params.set('tag', activeTag);
  if (activeQuery) params.set('q', activeQuery);
  const search = params.toString() ? '?' + params.toString() : window.location.pathname;
  history.replaceState(null, '', search || '?');
  // if no params, clean up the trailing ?
  if (!params.toString()) history.replaceState(null, '', window.location.pathname);
}}

function readURL() {{
  const params = new URLSearchParams(window.location.search);
  activeTag   = params.get('tag') || 'all';
  activeQuery = params.get('q')   || '';
  if (activeQuery) {{
    searchInput.value = activeQuery;
    openSearch();
  }}
}}

// ── Event listeners ──
searchBtn.addEventListener('click', () => {{
  if (searchOpen) {{ closeSearch(); }} else {{ openSearch(); }}
}});

tagBtn.addEventListener('click', (e) => {{
  e.stopPropagation();
  dropdownOpen = !dropdownOpen;
  tagDropdown.classList.toggle('open', dropdownOpen);
}});

document.addEventListener('click', () => {{ if (dropdownOpen) closeDropdown(); }});
tagDropdown.addEventListener('click', e => e.stopPropagation());

const toTopBtn = document.getElementById('toTopBtn');
window.addEventListener('scroll', () => {{
  toTopBtn.classList.toggle('visible', window.scrollY > 600);
}});
toTopBtn.addEventListener('click', () => {{
  window.scrollTo({{ top: 0, behavior: 'smooth' }});
}});

feed.addEventListener('click', (e) => {{
  const tagEl = e.target.closest('.card-tag');
  if (tagEl && tagEl.dataset.tag) {{
    selectTag(tagEl.dataset.tag);
  }}
}});

searchInput.addEventListener('input', () => {{
  activeQuery = searchInput.value.trim();
  pushURL();
  resetAndRender();
}});

document.addEventListener('keydown', (e) => {{
  if ((e.metaKey || e.ctrlKey) && e.key === 'k') {{
    e.preventDefault();
    if (searchOpen) {{ searchInput.focus(); }} else {{ openSearch(); }}
    if (dropdownOpen) closeDropdown();
  }}
  if (e.key === 'Escape') {{
    if (searchOpen) closeSearch();
    if (dropdownOpen) closeDropdown();
  }}
}});

window.addEventListener('popstate', () => {{
  readURL();
  buildTagPills();
  tagBtn.classList.toggle('active', activeTag !== 'all');
  if (activeQuery) {{
    searchInput.value = activeQuery;
    openSearch();
  }} else {{
    closeSearch();
  }}
  resetAndRender();
}});

// ── Init ──
readURL();
buildTagPills();
tagBtn.classList.toggle('active', activeTag !== 'all');
resetAndRender();
</script>
</body>
</html>"""


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Build the bookmarks static site.")
    parser.add_argument("--dry-run", action="store_true", help="Print stats without writing files")
    args = parser.parse_args()

    if not DATA_DIR.exists():
        print(f"✗ Data directory not found: {DATA_DIR}", file=sys.stderr)
        sys.exit(1)

    print("Loading bookmarks…")
    bookmarks = load_bookmarks()
    print(f"  {len(bookmarks)} bookmarks loaded")

    print("Building tag index…")
    tag_index = build_tag_index(bookmarks)
    print(f"  {len(tag_index)} unique tags")

    if args.dry_run:
        print(f"\n[dry-run] Would write:")
        print(f"  {SITE_HTML}")
        print(f"  {SITE_JSON}")
        print(f"  {TAGS_JSON}")
        if ROBOTS_DISALLOW_ALL:
            print(f"  {SITE_ROBOTS}")
        print(f"\nDone (dry-run). No files written.")
        return

    # Ensure output directories exist
    SITE_DIR.mkdir(parents=True, exist_ok=True)
    INDEX_DIR.mkdir(parents=True, exist_ok=True)

    print("Writing bookmarks.json…")
    SITE_JSON.write_text(build_bookmarks_json(bookmarks), encoding="utf-8")

    print("Writing tags.json…")
    TAGS_JSON.write_text(json.dumps(tag_index, indent=2, ensure_ascii=False), encoding="utf-8")

    print("Writing index.html…")
    SITE_HTML.write_text(build_html(bookmarks, tag_index), encoding="utf-8")

    if ROBOTS_DISALLOW_ALL:
        print("Writing robots.txt…")
        SITE_ROBOTS.write_text(
            "User-agent: *\nDisallow: /\n",
            encoding="utf-8"
        )

    print(f"\n✓ Built {len(bookmarks)} bookmarks → {SITE_DIR}")


if __name__ == "__main__":
    main()
