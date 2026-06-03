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

REPO_ROOT  = Path(__file__).parent.parent
DATA_DIR   = REPO_ROOT / "data" / "bookmarks"
INDEX_DIR  = REPO_ROOT / "index"
SITE_DIR   = REPO_ROOT / "site"
SITE_HTML  = SITE_DIR / "index.html"
SITE_JSON  = SITE_DIR / "bookmarks.json"
TAGS_JSON  = INDEX_DIR / "tags.json"

# ── Helpers ───────────────────────────────────────────────────────────────────

def load_bookmarks() -> list[dict]:
    """
    Loads all bookmark JSON files from data/bookmarks/**/*.json,
    sorted reverse-chronologically by saved_at.
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
    return dict(sorted(counts.items()))


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
            "tags"       : b.get("tags") or [],
        })
    return json.dumps(lean, indent=2, ensure_ascii=False)


def build_html(bookmarks: list[dict], tag_index: dict) -> str:
    """Generates the full static site HTML."""

    total      = len(bookmarks)
    built_at   = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    tags_json  = json.dumps(tag_index, ensure_ascii=False)

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
            "tags"       : b.get("tags") or [],
        })

    bookmarks_json = json.dumps(lean, ensure_ascii=False)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta name="generator" content="bookmarks-build/{built_at}">
  <title>Bookmarker</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600&display=swap" rel="stylesheet">
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

    :root {{
      --bg: #f7f5f1;
      --surface: #ffffff;
      --border: #e5e2dc;
      --border-mid: #d4d0c8;
      --text-primary: #1a1916;
      --text-secondary: #6b6760;
      --text-tertiary: #a09c96;
      --accent: #1a1916;
      --accent-fg: #f7f5f1;
      --tag-bg: #eeecea;
      --shadow: 0 1px 3px rgba(0,0,0,0.06), 0 1px 2px rgba(0,0,0,0.04);
      --shadow-md: 0 4px 12px rgba(0,0,0,0.08), 0 2px 4px rgba(0,0,0,0.04);
      --radius: 10px;
      --radius-sm: 6px;
      --radius-pill: 999px;
      --font-sans: 'Outfit', system-ui, sans-serif;
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
      padding: 0 20px 60px;
    }}

    /* ── Header ── */
    header {{
      display: flex;
      align-items: center;
      gap: 10px;
      padding: 18px 0 16px;
      border-bottom: 1px solid var(--border);
      margin-bottom: 20px;
      position: sticky;
      top: 0;
      background: var(--bg);
      z-index: 100;
    }}

    .site-brand {{
      display: flex;
      align-items: center;
      gap: 10px;
      margin-right: auto;
      text-decoration: none;
    }}

    .logo-mark {{
      width: 28px;
      height: 28px;
      background: var(--accent);
      border-radius: 6px;
      display: flex;
      align-items: center;
      justify-content: center;
      flex-shrink: 0;
    }}

    .logo-mark svg {{
      width: 15px;
      height: 15px;
      fill: none;
      stroke: var(--accent-fg);
      stroke-width: 2;
      stroke-linecap: round;
      stroke-linejoin: round;
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
      border-radius: var(--radius-sm);
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

    /* ── Tag Dropdown ── */
    .tag-dropdown-wrap {{ position: relative; }}

    .tag-dropdown {{
      position: absolute;
      top: calc(100% + 8px);
      right: 0;
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
      margin-bottom: 16px;
      min-height: 26px;
    }}

    .state-pill {{
      display: inline-flex;
      align-items: center;
      gap: 5px;
      font-size: 12px;
      padding: 3px 8px 3px 11px;
      border-radius: var(--radius-pill);
      border: 1px solid var(--border-mid);
      color: var(--text-secondary);
      background: var(--surface);
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
      width: 12px;
      height: 12px;
      stroke: currentColor;
      fill: none;
      stroke-width: 2.5;
      stroke-linecap: round;
    }}

    .results-count {{
      font-size: 12px;
      color: var(--text-tertiary);
    }}

    /* ── Cards ── */
    .feed {{ display: flex; flex-direction: column; gap: 12px; }}

    .card {{
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      overflow: hidden;
      display: flex;
      flex-direction: row;
      box-shadow: var(--shadow);
      transition: box-shadow 0.15s, border-color 0.15s;
      text-decoration: none;
      color: inherit;
    }}

    .card:hover {{
      box-shadow: var(--shadow-md);
      border-color: var(--border-mid);
    }}

    .card-img {{
      width: 160px;
      min-width: 160px;
      background: var(--tag-bg);
      display: flex;
      align-items: center;
      justify-content: center;
      flex-shrink: 0;
      overflow: hidden;
    }}

    .card-img img {{
      width: 100%;
      height: 100%;
      object-fit: cover;
      display: block;
    }}

    .card-img-placeholder svg {{
      width: 24px;
      height: 24px;
      stroke: var(--text-tertiary);
      fill: none;
      stroke-width: 1.5;
      stroke-linecap: round;
      stroke-linejoin: round;
    }}

    .card-body {{
      padding: 14px 16px;
      flex: 1;
      min-width: 0;
      display: flex;
      flex-direction: column;
      gap: 6px;
    }}

    .card-tags {{
      display: flex;
      gap: 4px;
      flex-wrap: wrap;
    }}

    .card-tag {{
      font-size: 11px;
      padding: 2px 8px;
      border-radius: var(--radius-pill);
      background: var(--tag-bg);
      color: var(--text-secondary);
    }}

    .card-title {{
      font-family: var(--font-sans);
      font-size: 16px;
      color: var(--text-primary);
      line-height: 1.35;
    }}

    .card-excerpt {{
      font-size: 13px;
      color: var(--text-secondary);
      line-height: 1.6;
      display: -webkit-box;
      -webkit-line-clamp: 2;
      -webkit-box-orient: vertical;
      overflow: hidden;
    }}

    .card-annotation {{
      border-left: 2px solid var(--border-mid);
      padding-left: 10px;
    }}

    .card-annotation p {{
      font-size: 12px;
      color: var(--text-secondary);
      font-style: italic;
      line-height: 1.55;
    }}

    .card-meta {{
      display: flex;
      align-items: center;
      gap: 6px;
      margin-top: auto;
      padding-top: 4px;
    }}

    .card-meta-domain,
    .card-meta-sep,
    .card-meta-date {{
      font-size: 11px;
      color: var(--text-tertiary);
    }}

    .card-meta-link {{
      margin-left: auto;
      color: var(--text-tertiary);
      display: flex;
      align-items: center;
      transition: color 0.12s;
      text-decoration: none;
    }}

    .card-meta-link:hover {{ color: var(--text-primary); }}

    .card-meta-link svg {{
      width: 14px;
      height: 14px;
      stroke: currentColor;
      fill: none;
      stroke-width: 2;
      stroke-linecap: round;
      stroke-linejoin: round;
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
        height: 160px;
      }}
      .site-name {{ display: none; }}
      .search-expand.open {{ max-width: 180px; }}
      .search-expand input {{ width: 160px; }}
    }}
  </style>
</head>
<body>
<div class="wrapper">

  <header>
    <a class="site-brand" href="/">
      <div class="logo-mark">
        <svg viewBox="0 0 24 24"><path d="M19 21l-7-5-7 5V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2z"/></svg>
      </div>
      <span class="site-name">Bookmarker</span>
    </a>
    <div class="header-controls">
      <div class="search-expand" id="searchExpand">
        <input type="text" id="searchInput" placeholder="Search bookmarks…" aria-label="Search bookmarks" autocomplete="off">
      </div>
      <button class="icon-btn" id="searchBtn" aria-label="Toggle search (⌘K)">
        <svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
      </button>
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
  </header>

  <div class="feed-state" id="feedState" aria-live="polite"></div>
  <main class="feed" id="feed" aria-label="Bookmarks feed"></main>
  <div class="load-sentinel" id="loadSentinel" style="display:none">
    <span>Loading more…</span>
  </div>
  <div class="empty-state" id="emptyState">
    <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="#a09c96" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
      <circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>
    </svg>
    <p>No results found.</p>
  </div>

</div>

<script>
// ── Data (injected at build time) ──
const BOOKMARKS  = {bookmarks_json};
const TAG_INDEX  = {tags_json};
const PAGE_SIZE  = 20;

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
    tagPillsWrap.appendChild(makePill(`${{tag}} (${{TAG_INDEX[tag]}})`, tag));
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
  resetAndRender();
}}

function closeDropdown() {{
  dropdownOpen = false;
  tagDropdown.classList.remove('open');
}}

// ── Filtering ──
function getFiltered() {{
  return BOOKMARKS.filter(b => {{
    const matchTag = activeTag === 'all' || (b.tags || []).includes(activeTag);
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
    feedState.appendChild(makeClearPill(activeTag, () => selectTag('all')));
  }}
  if (hasQuery) {{
    feedState.appendChild(makeClearPill(`"${{activeQuery}}"`, () => closeSearch()));
  }}

  const count = document.createElement('span');
  count.className = 'results-count';
  count.textContent = `${{filtered.length}} result${{filtered.length !== 1 ? 's' : ''}}`;
  feedState.appendChild(count);
}}

function makeClearPill(label, onClear) {{
  const pill = document.createElement('span');
  pill.className = 'state-pill';
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

  const imgHTML = b.image
    ? `<div class="card-img"><img src="${{b.image}}" alt="" loading="lazy"></div>`
    : `<div class="card-img card-img-placeholder"><svg viewBox="0 0 24 24"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21 15 16 10 5 21"/></svg></div>`;

  const tagsHTML = (b.tags || []).map(t => `<span class="card-tag">${{t}}</span>`).join('');
  const annotationHTML = b.annotation ? `<div class="card-annotation"><p>${{b.annotation}}</p></div>` : '';
  const excerptHTML = b.description ? `<p class="card-excerpt">${{b.description}}</p>` : '';

  article.innerHTML = `
    ${{imgHTML}}
    <div class="card-body">
      ${{tagsHTML ? `<div class="card-tags">${{tagsHTML}}</div>` : ''}}
      <h2 class="card-title">${{b.title || 'Untitled'}}</h2>
      ${{excerptHTML}}
      ${{annotationHTML}}
      <div class="card-meta">
        <span class="card-meta-domain">${{b.domain || ''}}</span>
        ${{b.domain ? '<span class="card-meta-sep">·</span>' : ''}}
        <span class="card-meta-date">${{formatDate(b.date)}}</span>
        <a class="card-meta-link" href="${{b.url}}" target="_blank" rel="noopener" aria-label="Open link">
          <svg viewBox="0 0 24 24"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>
        </a>
      </div>
    </div>`;

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

searchInput.addEventListener('input', () => {{
  activeQuery = searchInput.value.trim();
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

// ── Init ──
buildTagPills();
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

    print(f"\n✓ Built {len(bookmarks)} bookmarks → {SITE_DIR}")


if __name__ == "__main__":
    main()
