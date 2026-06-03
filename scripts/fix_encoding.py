#!/usr/bin/env python3
"""
fix_encoding.py
---------------
One-time script to fix garbled \ufffd replacement characters in bookmark
JSON files caused by mac_roman encoding issues during migration.

Strategy:
  - If file has \ufffd in title AND has a clean og.title → use og.title
  - If file has \ufffd in title but no og.title → strip \ufffd characters
  - Same logic for annotation field
  - Files without \ufffd are untouched

Usage (run from repo root):
    python3.12 scripts/fix_encoding.py --dry-run   # preview changes
    python3.12 scripts/fix_encoding.py              # apply fixes
"""

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
DATA_DIR  = REPO_ROOT / "data" / "bookmarks"
GARBLE    = "\ufffd"


def is_clean(text: str | None) -> bool:
    """Returns True if text exists and has no replacement characters."""
    return bool(text and GARBLE not in text)


def titles_are_related(original: str, og: str) -> bool:
    """
    Returns True if the OG title shares meaningful words with the original.
    Prevents using a completely unrelated OG title as a replacement.
    """
    import re
    def words(s):
        return set(re.findall(r'[a-z]{3,}', s.lower()))
    orig_words = words(original.replace('\ufffd', ''))
    og_words   = words(og)
    if not orig_words or not og_words:
        return False
    overlap = orig_words & og_words
    return len(overlap) >= 2


def fix_text(text: str | None, fallback: str | None = None) -> str | None:
    """
    Fixes a garbled string:
    - If fallback is clean and related to original, use it
    - Otherwise strip the replacement characters
    """
    if not text or GARBLE not in text:
        return text
    if is_clean(fallback) and titles_are_related(text, fallback):
        return fallback
    # Strip replacement characters and clean up whitespace
    fixed = text.replace(GARBLE, "").strip()
    # Collapse multiple spaces
    import re
    fixed = re.sub(r" {2,}", " ", fixed)
    return fixed if fixed else None


def main():
    parser = argparse.ArgumentParser(description="Fix garbled encoding in bookmark JSON files.")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    args = parser.parse_args()

    files = sorted(DATA_DIR.rglob("*.json"))
    fixed_count = 0
    skipped     = 0

    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            print(f"  ✗ Could not read {path.name}: {e}", file=sys.stderr)
            continue

        title      = data.get("title")
        annotation = data.get("annotation")
        og         = data.get("og") or {}
        og_title   = og.get("title")
        og_desc    = og.get("description")

        title_needs_fix      = title and GARBLE in title
        annotation_needs_fix = annotation and GARBLE in annotation

        if not title_needs_fix and not annotation_needs_fix:
            skipped += 1
            continue

        new_title      = fix_text(title, fallback=og_title) if title_needs_fix else title
        new_annotation = fix_text(annotation, fallback=None) if annotation_needs_fix else annotation

        if args.dry_run:
            print(f"── {path.name}")
            if title_needs_fix:
                print(f"  title (before): {title}")
                print(f"  title (after):  {new_title}")
                source = "og_title" if is_clean(og_title) else "stripped"
                print(f"  source: {source}")
            if annotation_needs_fix:
                print(f"  annotation (before): {annotation}")
                print(f"  annotation (after):  {new_annotation}")
            print()
        else:
            data["title"]      = new_title
            data["annotation"] = new_annotation
            path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

        fixed_count += 1

    print(f"{'─' * 44}")
    if args.dry_run:
        print(f"[dry-run] {fixed_count} files would be fixed, {skipped} skipped.")
    else:
        print(f"Done. {fixed_count} files fixed, {skipped} skipped.")
        if fixed_count > 0:
            print(f"\nNext step: python3.12 scripts/build.py")


if __name__ == "__main__":
    main()
