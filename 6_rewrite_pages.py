#!/usr/bin/env python3
"""
6_rewrite_pages.py  —  Phase 2: AI rewrite + publish for site pages.

Reads page_keepers.csv (or data/06c_decisions_pages.json if it exists) and for
each page:
  1. Sorts pages shallowest-first so parents exist before children
  2. Auto-creates stub parent pages for any missing intermediate paths
  3. Fetches rendered HTML from the live site (extracts body + images)
  4. Calls the Anthropic API for Gutenberg block HTML + a short clean slug
  5. Validates output
  6. Optionally POSTs to ftj3.local as a draft page with correct slug + parent

Usage
─────
  python 6_rewrite_pages.py                     # dry-run all pages
  python 6_rewrite_pages.py --post              # rewrite AND post to new site
  python 6_rewrite_pages.py --post --limit 1    # single page smoke test
  python 6_rewrite_pages.py --url /about-us     # single page by path
  python 6_rewrite_pages.py --force             # redo pages already written

Outputs
───────
  data/06_pages/<slug>.json   — one file per rewritten page
"""

import argparse
import csv
import json
import os
import re
import sys
import time
from datetime import datetime
from urllib.parse import urlparse

import requests
from tqdm import tqdm

sys.path.insert(0, os.path.dirname(__file__))
import config
from utils import setup_logging
from rewrite_utils import (
    call_anthropic, fetch_page_html, validate_content,
    format_image_lines, core_block_reference, extended_block_reference,
    extract_content_from_html as _extract_content,
)

log = setup_logging("6_rewrite_pages")

# ── Paths ──────────────────────────────────────────────────────
KEEPER_CSV     = os.path.join(os.path.dirname(__file__), "page_keepers.csv")
DECISIONS_FILE = os.path.join(config.DATA_DIR, "06c_decisions_pages.json")
PAGES_DIR      = os.path.join(config.DATA_DIR, "06_pages")
MEDIA_MAP_FILE = os.path.join(config.DATA_DIR, "06_media_map.json")

# Slug → WP page ID cache (populated as pages are posted, so children find parents)
_slug_cache: dict[str, int] = {}


# ══════════════════════════════════════════════════════════════════════════════
#  SYSTEM PROMPT
# ══════════════════════════════════════════════════════════════════════════════

SYSTEM_PROMPT_BASE = f"""You are a content writer for Franke Tobey Jones (FTJ), a nonprofit \
senior living community at 5340 N Bristol St, Tacoma, WA 98407. You are helping migrate \
their website to a clean new WordPress build.

Your task: given source material and layout instructions, write polished Gutenberg block HTML \
for a site page.

TONE
- Warm, clear, welcoming — audience is seniors and adult children making housing decisions
- Confident but not salesy; informative but not clinical
- No keyword stuffing, no repetitive phrases, no filler

OUTPUT FORMAT
Begin your response with exactly one slug line, then the Gutenberg HTML.
The slug line must be the very first line, in this exact format:
  SLUG: your-short-slug-here

Then output ONLY WordPress Gutenberg block HTML. No preamble, no explanation, no markdown fences.

BLOCK REFERENCE
───────────────
{core_block_reference()}
{extended_block_reference()}

FTJ THEME COLORS
  has-green-dark-color         (#367c2b)
  has-green-darker-color       (#2d5a3d)
  has-teal-button-color        (#1a5c4e)
  has-orange-button-color      (#D48103)
  has-tan-light-background-color (#f9f5e9)
  has-grey-light-background-color (#d8d5cc)

FTJ CUSTOM CSS CLASSES
  ftj-cta-banner      — teal background CTA section with centered text + button
  ftj-bg-tan          — tan background section
  ftj-section-heading — grey subheading style (24px, font-weight 400)
  ftj-heading-accent  — green underline accent bar below a heading
  ftj-img-text        — two-column image+text flex layout
  ftj-img-text reverse — reversed (text left, image right)
  ftj-bio             — person bio card (photo + text side by side)
  ftj-bio-photo / ftj-bio-text / ftj-bio-name / ftj-bio-title / ftj-bio-body
  ftj-cards-grid      — 4-column card grid
  ftj-card            — individual card in grid
  ftj-two-col-img     — full-width two-panel image with overlay text
  ftj-video-grid      — 3-column video thumbnail grid
  ftj-tab-nav         — tab navigation row

RULES
- Do NOT include the page title as H1 — the theme renders that from the post title
- Do NOT include navigation, header, or footer content
- DO preserve all factual details exactly: addresses, phone numbers, room counts, pricing, staff names
- DO use H2 for the FIRST section heading only; use H3 for all subsequent section headings
- DO follow the LAYOUT HINT and NOTES provided in the user message
- DO add appropriate ftj- CSS classes to blocks where they improve layout
- If image URLs are provided, include them using the IMAGE block format above
- SLUG rules: 3-6 words, lowercase, hyphenated, descriptive of actual content.
  Use only the last meaningful path segment.
  Good: duplex-cottages  Bad: duplex-cottages-independent-senior-living
- Output the SLUG line first, then ONLY the Gutenberg block HTML"""


def build_system_prompt(theme_context: str = "") -> str:
    if theme_context:
        return SYSTEM_PROMPT_BASE + f"\n\nTHEME CONTEXT (reference only):\n{theme_context[:3000]}"
    return SYSTEM_PROMPT_BASE


# ══════════════════════════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def slug_from_path_segment(url: str) -> str:
    """Last path segment only, lowercased and hyphenated."""
    from rewrite_utils import slug_from_url
    return slug_from_url(url, fallback="page")


def path_depth(url: str) -> int:
    return len(urlparse(url).path.strip("/").split("/"))


# fetch_page_html → imported from rewrite_utils


def extract_content_from_html(html: str) -> dict:
    """Strip chrome, return body text, headings (as string), and content image URLs.
    Pages need headings as a string and image extraction — delegates to shared util."""
    return _extract_content(html, headings_as_str=True)


def get_or_create_parent(slug: str, title: str, grandparent_id: int = 0) -> int:
    """
    Return the WP page ID for a slug. If it doesn't exist, create a stub page.
    Results are cached in _slug_cache.
    """
    if slug in _slug_cache:
        return _slug_cache[slug]

    # Check WP
    try:
        resp = requests.get(
            f"{config.NEW_SITE_URL.rstrip('/')}/wp-json/wp/v2/pages",
            params={"slug": slug, "per_page": 1, "status": "any"},
            auth=(config.NEW_WP_USERNAME, config.NEW_WP_APP_PASSWORD),
            timeout=15,
        )
        results = resp.json()
        if results and isinstance(results, list):
            pid = results[0].get("id", 0)
            if pid:
                _slug_cache[slug] = pid
                log.info(f"    Parent exists: /{slug}  (ID {pid})")
                return pid
    except Exception as e:
        log.warning(f"    Parent lookup failed for '{slug}': {e}")

    # Create stub
    payload = {
        "title":   title,
        "slug":    slug,
        "content": f"<!-- wp:paragraph --><p>{title}</p><!-- /wp:paragraph -->",
        "status":  "draft",
    }
    if grandparent_id:
        payload["parent"] = grandparent_id
    try:
        resp = requests.post(
            f"{config.NEW_SITE_URL.rstrip('/')}/wp-json/wp/v2/pages",
            json=payload,
            auth=(config.NEW_WP_USERNAME, config.NEW_WP_APP_PASSWORD),
            timeout=30,
        )
        resp.raise_for_status()
        pid = resp.json().get("id", 0)
        _slug_cache[slug] = pid
        log.info(f"    Created stub: /{slug}  (ID {pid})")
        return pid
    except Exception as e:
        log.error(f"    Could not create stub for '{slug}': {e}")
        return 0


def ensure_parent_chain(url: str, dry_run: bool) -> int:
    """
    Walk the full URL path and ensure every intermediate page exists.
    Returns the direct parent ID for the given URL.
    Returns 0 if root-level or dry-run.
    """
    parts = urlparse(url).path.strip("/").split("/")
    if len(parts) <= 1:
        return 0  # root-level page, no parent needed
    if dry_run:
        return 0  # don't create stubs in dry-run

    parent_id = 0
    for i in range(len(parts) - 1):
        slug  = parts[i]
        title = slug.replace("-", " ").title()
        parent_id = get_or_create_parent(slug, title, parent_id)

    return parent_id


def load_pages_from_csv(csv_path: str) -> list[dict]:
    pages = []
    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            url = row.get("URL", "").strip().rstrip("/")
            if not url:
                continue
            title = row.get("Title", "").strip() or row.get("H1 (Best)", "").strip()
            pages.append({
                "url":          url,
                "title":        title,
                "new_title":    title,
                "new_slug":     slug_from_path_segment(url),
                "page_type":    "generic",
                "layout_hint":  "",
                "note":         "",
                "css_classes":  [],
                "extra_blocks": [],
                "image_urls":   [],
                "video_urls":   [],
                "body_text":    "",
                "headings":     "",
            })
    log.info(f"  Loaded {len(pages)} pages from {csv_path}")
    return pages


def load_pages_from_decisions(json_path: str) -> tuple[list[dict], str]:
    with open(json_path, encoding="utf-8") as f:
        data = json.load(f)
    pages = data.get("pages", [])
    theme_context = data.get("theme_context", "")
    log.info(f"  Loaded {len(pages)} pages from {json_path}")
    return pages, theme_context


def build_prompt(page: dict, media_map: dict) -> str:
    lines = [
        f"PAGE URL:     {page['url']}",
        f"PAGE TITLE:   {page.get('new_title', '')}",
        f"SUGGESTED SLUG (improve if too long): {page.get('new_slug', '')}",
        f"PAGE TYPE:    {page.get('page_type', 'generic')}",
        f"LAYOUT HINT:  {page.get('layout_hint', '')}",
    ]

    css = page.get("css_classes", [])
    blocks = page.get("extra_blocks", [])
    if css:
        lines.append(f"SUGGESTED CSS CLASSES: {', '.join(css)}")
    if blocks:
        lines.append(f"AVAILABLE EXTRA BLOCKS: {', '.join(blocks)}")

    note = page.get("note", "")
    if note:
        lines.append(f"\nLAYOUT NOTES (follow precisely):\n{note}")

    headings = page.get("headings", "")
    if headings:
        lines.append(f"\nEXISTING HEADINGS (structure reference):\n{headings[:500]}")

    image_urls = page.get("image_urls", [])
    if image_urls:
        lines.append("\nIMAGE URLS — include as wp:image blocks:")
        lines.extend(format_image_lines(image_urls, media_map, limit=10))

    video_urls = page.get("video_urls", [])
    if video_urls:
        lines.append("\nVIDEO / EMBED URLS:")
        for u in video_urls[:3]:
            lines.append(f"  {u}")

    body = page.get("body_text", "")
    words = body.split()
    if len(words) > 3000:
        body = " ".join(words[:3000]) + " […]"

    lines.append(f"\nSOURCE CONTENT ({len(body.split())} words):\n{body}")
    lines.append("\nRemember: first line must be SLUG: your-slug-here, then the Gutenberg HTML.")
    return "\n".join(lines)


# call_anthropic → imported from rewrite_utils


def post_to_new_site(page: dict, content: str, parent_id: int) -> int | None:
    ok, reason = validate_content(content)
    if not ok:
        log.error(f"    Refusing to post — {reason}")
        return None

    title    = page.get("new_title") or page.get("title", "Untitled")
    new_slug = (page.get("new_slug") or slug_from_path_segment(page["url"])).strip("/")
    endpoint = f"{config.NEW_SITE_URL.rstrip('/')}/wp-json/wp/v2/pages"

    payload = {
        "title":   title,
        "content": content,
        "status":  config.PHASE2_POST_STATUS,
        "slug":    new_slug,
    }
    if parent_id:
        payload["parent"] = parent_id

    modified = page.get("modified", "") or page.get("date_modified", "")
    if modified and len(modified) == 10:
        payload["date"] = modified + "T00:00:00"

    try:
        resp = requests.post(
            endpoint,
            json=payload,
            auth=(config.NEW_WP_USERNAME, config.NEW_WP_APP_PASSWORD),
            timeout=30,
        )
        resp.raise_for_status()
        data    = resp.json()
        post_id = data.get("id")
        link    = data.get("link", f"/{new_slug}")
        # Cache slug → ID so deeper children can find this as their parent
        _slug_cache[new_slug] = post_id
        log.info(f"    → Posted {link}  (ID {post_id})")
        return post_id
    except requests.exceptions.HTTPError as e:
        log.error(f"    WP POST failed: {e} — {getattr(resp, 'text', '')[:300]}")
    except Exception as e:
        log.error(f"    WP POST error: {e}")
    return None


def save_result(result: dict) -> str:
    os.makedirs(PAGES_DIR, exist_ok=True)
    path = os.path.join(PAGES_DIR, result["new_slug"] + ".json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    return path


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    ap = argparse.ArgumentParser(description="Phase 2: AI page rewriter")
    ap.add_argument("--post",   action="store_true",
                    help="POST pages to new site (default: dry-run)")
    ap.add_argument("--limit",  type=int, default=0,
                    help="Max pages to process this run (0 = no limit)")
    ap.add_argument("--type",   type=str, default=None,
                    help="Only process pages of this type (e.g. team, faq)")
    ap.add_argument("--url",    type=str, default=None,
                    help="Process a single page by its path")
    ap.add_argument("--force",  action="store_true",
                    help="Reprocess pages that already have a JSON")
    args = ap.parse_args()

    dry_run = not args.post

    log.info("=" * 65)
    log.info("6_rewrite_pages — Phase 2: Page rewriter")
    log.info("=" * 65)
    log.info(f"  New site:  {config.NEW_SITE_URL}")
    log.info(f"  Dry run:   {dry_run}")
    log.info(f"  Limit:     {args.limit or 'none'}")

    # ── Load pages ───────────────────────────────────────────────
    theme_context = ""
    if os.path.exists(DECISIONS_FILE):
        log.info(f"  Input: {DECISIONS_FILE}")
        pages, theme_context = load_pages_from_decisions(DECISIONS_FILE)
    elif os.path.exists(KEEPER_CSV):
        log.warning(f"  Input: {KEEPER_CSV} (legacy fallback — run 6c_prep_pages.py to fix this)")
        pages = load_pages_from_csv(KEEPER_CSV)
    else:
        log.error(f"  No input found. Expected {DECISIONS_FILE} — run 6c_prep_pages.py first.")
        sys.exit(1)

    system_prompt = build_system_prompt(theme_context)

    # ── Load media map ───────────────────────────────────────────
    media_map = {}
    if os.path.exists(MEDIA_MAP_FILE):
        with open(MEDIA_MAP_FILE, encoding="utf-8") as f:
            media_map = json.load(f)
        log.info(f"  Media map: {len(media_map)} entries")

    # ── Filter ───────────────────────────────────────────────────
    os.makedirs(PAGES_DIR, exist_ok=True)
    todo = pages

    if args.url:
        target = args.url.rstrip("/")
        todo = [p for p in pages if urlparse(p.get("url", "")).path.rstrip("/") == target]
        if not todo:
            log.error(f"No page found matching path '{args.url}'")
            sys.exit(1)

    if args.type:
        todo = [p for p in todo if p.get("page_type") == args.type]
        log.info(f"  Filtered to type '{args.type}': {len(todo)} pages")

    if not args.force:
        before = len(todo)
        todo = [p for p in todo
                if not os.path.exists(
                    os.path.join(PAGES_DIR,
                                 (p.get("new_slug") or slug_from_path_segment(p.get("url", ""))) + ".json")
                )]
        skipped = before - len(todo)
        if skipped:
            log.info(f"  Skipping {skipped} already-written pages (--force to redo)")

    # ── CRITICAL: sort shallowest first so parents post before children ──
    todo.sort(key=lambda p: path_depth(p.get("url", "")))

    if args.limit and args.limit > 0:
        todo = todo[:args.limit]

    if not todo:
        log.info("Nothing to process.")
        sys.exit(0)

    log.info(f"\nProcessing {len(todo)} pages (shallowest first) …\n")

    ok = posted = failed = 0

    for page in tqdm(todo, desc="Rewriting", unit="pg"):
        url       = page.get("url", "")
        title     = page.get("new_title") or page.get("title", url)
        fallback_slug = page.get("new_slug") or slug_from_path_segment(url)
        page_type = page.get("page_type", "generic")

        log.info(f"\n{'─'*55}")
        log.info(f"  [{page_type}] {title}")
        log.info(f"  {url}")

        # Fetch live content + images if not already populated
        if not page.get("body_text"):
            live_html = fetch_page_html(url)
            if live_html:
                extracted = extract_content_from_html(live_html)
                page["body_text"] = extracted["body_text"]
                page["headings"]  = extracted["headings"]
                if not page.get("image_urls"):
                    page["image_urls"] = extracted.get("image_urls", [])
                log.info(f"  Fetched {len(page['body_text'].split())} words, "
                         f"{len(page.get('image_urls', []))} images")
            else:
                log.warning(f"  Could not fetch {url} — content will be sparse")

        # Ensure parent chain exists on WP before posting this page
        parent_id = ensure_parent_chain(url, dry_run)

        prompt    = build_prompt(page, media_map)
        rewritten, suggested_slug = call_anthropic(system_prompt, prompt)

        if not rewritten:
            log.error("  ✗ API returned nothing — skipping")
            failed += 1
            continue

        new_slug = suggested_slug or fallback_slug
        page["new_slug"] = new_slug
        log.info(f"  → /{new_slug}  (parent_id={parent_id})  ({len(rewritten.split())} words)")

        result = {
            "url":               url,
            "new_slug":          new_slug,
            "new_title":         title,
            "page_type":         page_type,
            "layout_hint":       page.get("layout_hint", ""),
            "rewritten_content": rewritten,
            "image_urls":        page.get("image_urls", []),
            "parent_id":         parent_id,
            "post_id":           None,
            "posted_at":         None,
            "model":             config.ANTHROPIC_MODEL,
            "timestamp":         datetime.utcnow().isoformat() + "Z",
            "word_count_out":    len(rewritten.split()),
            "ai_slug":           suggested_slug,
        }

        saved_path = save_result(result)
        log.info(f"  Saved → {saved_path}")
        ok += 1

        if not dry_run:
            post_id = post_to_new_site(page, rewritten, parent_id)
            if post_id:
                result["post_id"]   = post_id
                result["posted_at"] = datetime.utcnow().isoformat() + "Z"
                save_result(result)
                posted += 1

        time.sleep(0.5)

    log.info(f"\n{'='*65}")
    log.info("Page rewrite complete")
    log.info(f"  Rewritten: {ok}")
    if not dry_run:
        log.info(f"  Posted:    {posted}")
    log.info(f"  Failed:    {failed}")
    if dry_run:
        log.info(f"\n  Dry-run — nothing posted. Run with --post when ready.")
    log.info("=" * 65)


if __name__ == "__main__":
    main()
