# Bookmarker

A personal bookmarks site. Links are saved via an iOS/macOS Shortcut,
stored as individual JSON files in this repo, and published weekly as
a static site on GitHub Pages.

---

## How it works

```
Shortcut (any device)
    ↓  captures URL + tags + annotation
    ↓  writes a JSON file to data/bookmarks/YYYY/MM/{timestamp}.json
    ↓  pushes to GitHub

GitHub Actions (every Friday, midnight Eastern)
    ↓  enrich.py   — fetches OG metadata for new bookmarks
    ↓  build.py    — aggregates JSON → builds site/index.html
    ↓  deploys to GitHub Pages
```

---

## Repo structure

```
bookmarks/
├── data/
│   └── bookmarks/
│       └── YYYY/
│           └── MM/
│               └── {timestamp}.json   ← one file per bookmark
├── index/
│   └── tags.json                      ← built tag index (generated)
├── scripts/
│   ├── enrich.py                      ← fetches OG metadata
│   └── build.py                       ← builds the static site
├── site/
│   ├── index.html                     ← the website (generated)
│   └── bookmarks.json                 ← aggregated data (generated)
└── .github/
    └── workflows/
        └── build.yml                  ← weekly build + deploy
```

---

## Bookmark JSON format

Each bookmark is a single JSON file:

```json
{
  "id": "1746123456",
  "url": "https://example.com/article",
  "title": "Article Title",
  "annotation": "Optional personal note.",
  "tags": ["design", "writing"],
  "saved_at": "2025-05-14T09:32:00-04:00",
  "domain": "example.com",
  "og": {
    "title": "Article Title from OG",
    "description": "Page description from OG meta tag.",
    "image": "https://example.com/og-image.png"
  },
  "enriched_at": "2025-05-14T09:35:00Z"
}
```

Fields added by Shortcut at capture time: `id`, `url`, `title`, `annotation`, `tags`, `saved_at`

Fields added by `enrich.py`: `domain`, `og`, `enriched_at`

---

## Scripts

### enrich.py

Fetches Open Graph metadata for any bookmark not yet enriched.
Safe to re-run — skips already-enriched files.

```bash
# Run from repo root
python3 scripts/enrich.py

# Re-fetch metadata for all bookmarks
python3 scripts/enrich.py --force

# Preview without writing
python3 scripts/enrich.py --dry-run
```

### build.py

Aggregates all bookmark JSON files and builds the static site.

```bash
# Run from repo root
python3 scripts/build.py

# Preview without writing
python3 scripts/build.py --dry-run
```

---

## Changing the build schedule

Open `.github/workflows/build.yml` and edit the `cron` line.
The comment block in that file explains the format with examples.
Use [crontab.guru](https://crontab.guru) to verify your expression.

---

## Manual build

Trigger a build at any time from the **Actions** tab in GitHub →
select **Weekly Build & Deploy** → **Run workflow**.

---

## Requirements

- Python 3.10+ (no external dependencies)
- GitHub account with Pages enabled
- GitHub personal access token (for the Shortcut to push files)
