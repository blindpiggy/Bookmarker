"""
config.py
---------
Site configuration for Bookmarker.
Edit this file to customise the site without touching build.py.
"""

# ── Site identity ──────────────────────────────────────────────────────────────

SITE_NAME = "Bookmarker"
SITE_URL  = "https://bookmarks.aaronawad.com"
AUTHOR    = "Aaron Awad"

# ── Link behaviour ─────────────────────────────────────────────────────────────

# Add rel="nofollow" to all bookmark links.
# Prevents search engines from treating your bookmarks as endorsements.
# Has no practical effect if ROBOTS_DISALLOW_ALL is True.
LINKS_NOFOLLOW = True

# Open bookmark links in a new tab (target="_blank").
LINKS_NEW_TAB = True

# ── Feed behaviour ─────────────────────────────────────────────────────────────

# Number of bookmark cards loaded per page (lazy loading).
PAGE_SIZE = 20

# ── Crawlers ───────────────────────────────────────────────────────────────────

# Generate a robots.txt that blocks all crawlers (search engines, AI, etc.).
ROBOTS_DISALLOW_ALL = True

# Also embed <meta name="robots" content="noindex, nofollow"> in the HTML head.
# Belt-and-suspenders: works even if robots.txt is ignored.
ROBOTS_META = True
