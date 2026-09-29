# ─────────────────────────────────────────────────────────────
#  config.py  —  FTJ Site Audit + Migration configuration
#  Edit these values before running any script.
# ─────────────────────────────────────────────────────────────

import os

# Project root — every path below hangs off this, so the toolkit works
# from whatever drive/folder it lives in (F:\domain-parser, a local copy,
# a laptop, etc.) with no edits. Anchored to THIS file, not the CWD.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Load secrets from a git-ignored .env file beside this config (if present).
# python-dotenv is optional — if it isn't installed, we fall back to whatever
# is already in the real environment. Secrets themselves live in .env, never here.
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(BASE_DIR, ".env"))
except ImportError:
    pass


# ═════════════════════════════════════════════════════════════
#  TARGET SITES  —  the knobs you change most. Edit here.
# ═════════════════════════════════════════════════════════════
# Live / source site being audited + migrated FROM (no trailing slash):
SITE_URL          = "https://www.franketobeyjones.com"      # WP Local copy of the old site

# Where Phase-2 rewrites FETCH source pages from. Options:
#   SITE_URL                  <- same source site
#   "http://legacyftj.local"  <- WP Local reference copy (fast, no rate limits)
PHASE2_SOURCE_URL = "https://www.franketobeyjones.com"

# New build site, migrated TO. Phase-2/3 scripts POST/PATCH here.
#   "http://ftj4.local"       <- WP Local build
#   production domain         <- at go-live
NEW_SITE_URL      = "http://ftj4.local"

# Production domain baked into JSON-LD schema (NOT the .local build):
SCHEMA_BASE_URL   = "https://www.franketobeyjones.com"
# ═════════════════════════════════════════════════════════════


# ── Credentials — source / live site ─────────────────────────
# Generate in: WP Admin → Users → Your Profile → Application Passwords
# The password displays with spaces (e.g. "AbCd EfGh IjKl ...") — paste as-is.
# NOTE: Phase 1 (crawl/extract) works without these for public content.
#       Fill them in before running Phase 2.
WP_USERNAME     = "your_admin_username"
WP_APP_PASSWORD = os.environ.get("WP_APP_PASSWORD", "")

# ── Credentials — Phase-2 source (local reference copy, if used) ──
OLD_WP_USERNAME     = "Erik"
OLD_WP_APP_PASSWORD = os.environ.get("OLD_WP_APP_PASSWORD", "")

# ── Credentials — new build site (NEW_SITE_URL, set above) ────
NEW_WP_USERNAME     = "Erik"
NEW_WP_APP_PASSWORD = os.environ.get("NEW_WP_APP_PASSWORD", "")

# ── Anthropic API (used only when a task routes to "anthropic") ─
ANTHROPIC_API_KEY     = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL       = "claude-sonnet-4-6"

# ── Shared LLM generation settings (apply to EVERY provider) ───
LLM_MAX_TOKENS      = 4096
LLM_TEMPERATURE     = 0.3   # 0.3 = focused/consistent; 0.5 = more varied phrasing
PHASE2_API_TIMEOUT  = 120   # seconds — long pages can be slow

# ── LLM provider routing ──────────────────────────────────────
# Per-task provider. Values: "anthropic" | "deepseek" | "mistral" | "ollama".
# Currently on DeepSeek (Anthropic account is out of credits). Switch any task
# by changing its value; add the matching key to .env.
LLM_TASKS = {
    "rewrite":  "deepseek",    # page/blog/PDF content generation
    "enhance":  "deepseek",    # AI page intros
    "slug":     "deepseek",    # short slug generation
    "classify": "deepseek",    # one-label cluster classification
}
# Local Ollama (only used when a task above is set to "ollama"):
OLLAMA_URL   = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.1")

# OpenAI-compatible cloud providers (Mistral, DeepSeek) — used when an LLM_TASKS
# entry routes to "mistral" or "deepseek". Keys live in .env; both speak the
# OpenAI chat-completions format, so one adapter in rewrite_utils covers them.
MISTRAL_API_KEY  = os.environ.get("MISTRAL_API_KEY", "")
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
OPENAI_PROVIDERS = {
    "mistral":  {"url": "https://api.mistral.ai/v1/chat/completions",
                 "key": MISTRAL_API_KEY,  "model": "mistral-large-latest"},
    "deepseek": {"url": "https://api.deepseek.com/chat/completions",
                 "key": DEEPSEEK_API_KEY, "model": "deepseek-chat"},
}

# ── HTTP behaviour ────────────────────────────────────────────
REQUEST_DELAY   = 0.5   # seconds between requests (be polite to the server)
REQUEST_TIMEOUT = 30    # seconds before giving up
MAX_RETRIES     = 3

# ── Paths — all anchored to BASE_DIR so they follow the drive ─
DATA_DIR   = os.path.join(BASE_DIR, "data")     # intermediate JSON/CSV files land here
OUTPUT_DIR = os.path.join(BASE_DIR, "output")   # final Excel + HTML reports land here

# ── Similarity threshold (script 4) ───────────────────────────
SIMILARITY_THRESHOLD = 0.70   # pages scoring above this are flagged as similar

# ── GA4 CSV column name hints (script 3) ──────────────────────
# If auto-detection fails, set these to the exact column names in your export.
GA4_PATH_COLUMN      = None   # e.g. "Page path"  — None = auto-detect
GA4_SESSIONS_COLUMN  = None   # e.g. "Sessions"   — None = auto-detect
GA4_PAGEVIEWS_COLUMN = None   # e.g. "Views"      — None = auto-detect


# ─────────────────────────────────────────────────────────────
#  PHASE 2 — Content rewriting + new site build
#  (TARGET SITES + credentials for these live at the top.)
# ─────────────────────────────────────────────────────────────

# ── Phase 2 paths ─────────────────────────────────────────────
PHASE2_REWRITE_DIR = os.path.join(DATA_DIR, "06_rewrite")   # one JSON per rewritten page
PHASE2_KEEPER_CSV  = os.path.join(BASE_DIR, "ftj_keeper_list.csv")

# Image URL CSV exported from the audit Excel (Page Content tab).
# Col A = page URL, col G = image URLs, col H = video URLs.
PHASE2_IMAGE_CSV   = os.path.join(BASE_DIR, "ftj_site_audit_image_urls.csv")
BLOG_CONTENT_CSV   = os.path.join(BASE_DIR, "blog_content_to_decide.csv")
RAW_PAGES_JSON     = os.path.join(DATA_DIR, "01_pages.json")

# Folder holding the 06c_decisions_*.json / 06c_solo_blogs.json source files
# (feeds update_slugs.py --redirects).
DECISIONS_DIR = DATA_DIR

# Page-JSON folders (upstream → schema’d). See RUNBOOK: 06_pages_schema is canonical.
PAGES_DIR        = os.path.join(DATA_DIR, "06_pages")
PAGES_AI_DIR     = os.path.join(DATA_DIR, "06_pages_ai")
PAGES_SCHEMA_DIR = os.path.join(DATA_DIR, "06_pages_schema")

# ── Phoso media bridge (phoso_bridge.py) ──────────────────────
# Folder Phoso writes its sorted category buckets to. Point Phoso's output here,
# run your sort + correction pass, THEN run phoso_bridge --reconcile.
PHOSO_OUTPUT_DIR    = os.path.join(BASE_DIR, "phoso_output")
# Bridge artifacts:
MEDIA_OG_MANIFEST   = os.path.join(DATA_DIR, "06_media_og_manifest.json")   # Map 1 (from 6b download)
MEDIA_RENAME_MAP    = os.path.join(DATA_DIR, "06_media_rename_map.json")     # Map 2 (merged, per kept image)
MEDIA_MAP_FILE      = os.path.join(DATA_DIR, "06_media_map.json")            # og_url → {url,id}; consumed by map_image
MEDIA_RECONCILE_CSV = os.path.join(OUTPUT_DIR, "06_media_reconcile_report.csv")
PHASE_MULTI_DIRNAME = "_multi"   # Phoso's dual-category duplicate folder — skipped in reconcile

# ── Phase 2 run behaviour ─────────────────────────────────────
PHASE2_LIMIT       = 3        # max pages per run; 0 = no limit (set to 3 while testing)
PHASE2_DRY_RUN     = False    # True = rewrite + save JSON only, do NOT post to new site
PHASE2_GRADES      = ["Migrate", "Improve", "Review"]
PHASE2_SKIP_DONE   = True     # True = skip pages that already have a rewrite JSON
PHASE2_POST_STATUS = "draft"  # status when posting to new site: "draft" or "publish"

# ── Blog Categories ───────────────────────────────────────────
CATEGORY_MAP = {
    "Apartments & Expansion":    2,
    "Health & Wellness":         3,
    "Senior Living Education":   4,
    "Events & Happenings":       5,
    "Resident & Staff Stories":  6,
    "News & Announcements":      7,
    "Philanthropy & Giving":     8,
    "Community Life":            9,
    "Uncategorized":             1,
}

# ── Organization NAP — baked into JSON-LD schema ──────────────
# (SCHEMA_BASE_URL is set in the TARGET SITES block at the top.)
ORG_PHONE   = "+12537526621"
ORG_ADDRESS = {
       "@type":           "PostalAddress",
       "streetAddress":   "5340 N Bristol St",
       "addressLocality": "Tacoma",
       "addressRegion":   "WA",
       "postalCode":      "98407",
       "addressCountry":  "US",
}
