#!/usr/bin/env python3
"""
6_rewrite.py  —  Phase 2: AI-assisted rewriting for SOLO BLOG POSTS.

For each blog post in the solo list:
  1. Reads data/06c_solo_blogs.json  (produced by 6c_prep_blogs.py)
     Falls back to ftj_keeper_list.csv if the JSON doesn't exist.
  2. Fetches rendered HTML from PHASE2_SOURCE_URL
  3. Sends content + context to the Anthropic API for rewriting
  4. Saves the result to data/06_rewrite/<slug>.json
  5. Optionally POSTs the rewritten post to ftj3.local as a WP *post* (not page)

Usage:
  python 6_rewrite.py                    # dry-run all solo blogs
  python 6_rewrite.py --post             # rewrite AND post to new site
  python 6_rewrite.py --post --limit 3   # smoke-test: rewrite + post 3
  python 6_rewrite.py --url /blog/slug   # single post by path
  python 6_rewrite.py --force            # reprocess already-written posts
  python 6_rewrite.py --source live      # pull from production instead of local

Outputs:
  data/06_rewrite/<slug>.json   — one file per rewritten post
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
    call_anthropic, extract_content_from_html, fetch_page_html,
    source_url_for, slug_from_url, validate_content, map_image,
    format_image_lines, core_block_reference,
)

log = setup_logging("6_rewrite")

# ── Paths ─────────────────────────────────────────────────────
SOLO_BLOGS_FILE = os.path.join(config.DATA_DIR, "06c_solo_blogs.json")  # preferred input
KEEPER_CSV      = config.PHASE2_KEEPER_CSV                               # legacy fallback
REWRITE_DIR     = config.PHASE2_REWRITE_DIR


# ╔══════════════════════════════════════════════════════════════╗
# ║  PROMPTS                                                     ║
# ╚══════════════════════════════════════════════════════════════╝

SYSTEM_PROMPT = f"""You are a content writer for Franke Tobey Jones (FTJ), a nonprofit \
senior living community at 5340 N Bristol St, Tacoma, WA 98407. You are helping migrate \
their website to a clean new WordPress build.

Your task: given source material from the old blog post, write a polished, improved version \
for the new site.

TONE
- Warm, clear, and welcoming — this audience is seniors and adult children making \
important housing decisions
- Confident but not salesy; informative but not clinical
- No keyword stuffing, no repetitive phrases, no filler

FORMAT
Begin your response with exactly one slug line, then the Gutenberg HTML.
The slug line must be the very first line, in this exact format:
  SLUG: your-short-slug-here

Then output only WordPress Gutenberg block HTML — no preamble, no explanation, no markdown fences.
Use these blocks:
{core_block_reference()}

RULES
- Do NOT include the post title as H1 — the theme renders that from the post title
- Do NOT include navigation, header, or footer content
- DO preserve all factual details exactly: addresses, phone numbers, names, dates
- Structure: opening paragraph (hook) → H2 for the first main section → H3 for all \
subsequent sections → closing paragraph. Conversational tone. No CTA button needed.
- If image_urls are provided in the user message, include them as wp:image blocks \
using the mapped new URLs. Place images where they naturally fit in the content flow.
- If a per-post note is provided, follow it precisely
- SLUG rules: 3-6 words, lowercase, hyphenated, no 'blog-' prefix, descriptive of the
  actual content topic.
  Good: spring-gardening-senior-center  Bad: plant-the-possibilities-spring-gardening-begins-at-ftj
- Output the SLUG line first, then ONLY the Gutenberg block HTML"""


# ╔══════════════════════════════════════════════════════════════╗
# ║  HELPERS                                                     ║
# ╚══════════════════════════════════════════════════════════════╝

# source_url_for, slug_from_url, fetch_page_html, extract_content_from_html
# → imported from rewrite_utils


def build_prompt(page: dict, live_content: dict | None, media_map: dict) -> str:
    title      = page.get("new_title") or page.get("title", "")
    old_url    = page.get("url", "")
    new_slug   = page.get("new_slug", "") or slug_from_url(old_url)
    note       = page.get("note", "")
    image_urls = page.get("image_urls", [])
    video_urls = page.get("video_urls", [])

    if live_content:
        body_text = live_content.get("body_text", "") or page.get("body_text", "")
        headings  = live_content.get("headings", []) or page.get("headings", [])
    else:
        body_text = page.get("body_text", "")
        headings  = page.get("headings", [])

    # headings from 6c_prep_blogs arrives as a raw string; normalise
    if isinstance(headings, str):
        headings = [{"level": "h2", "text": h.strip()} for h in headings.splitlines() if h.strip()]

    words = body_text.split()
    if len(words) > 3000:
        body_text = " ".join(words[:3000]) + " […]"

    lines = [
        f"PAGE:            {old_url}",
        f"NEW TITLE:       {title}",
        f"SUGGESTED SLUG (improve if too long): {new_slug}",
        f"CATEGORY:        {page.get('category', 'Uncategorized')}",
    ]
    if note:
        lines.append(f"\nPER-POST NOTE (follow this precisely):\n{note}")
    if headings:
        h_list = "\n".join(f"  [{h['level'].upper()}] {h['text']}" for h in headings[:20])
        lines.append(f"\nEXISTING HEADINGS (for structure reference):\n{h_list}")

    if image_urls:
        lines.append("\nIMAGE URLS — include as wp:image blocks using BOTH the URL and ID provided:")
        lines.extend(format_image_lines(image_urls, media_map or {}))

    if video_urls:
        lines.append("\nVIDEO / EMBED URLS:")
        for u in video_urls[:5]:
            lines.append(f"  {u}")

    lines.append(f"\nSOURCE CONTENT ({len(body_text.split())} words):\n{body_text}")
    lines.append("\nRemember: first line must be SLUG: your-slug-here, then the Gutenberg HTML.")
    return "\n".join(lines)


# call_anthropic → imported from rewrite_utils


def post_to_new_site(page: dict, rewritten_html: str) -> int | None:
    """POST a rewritten blog post to the new WP site via REST API.
    Always targets /wp-json/wp/v2/posts (not pages).
    """
    ok, reason = validate_content(rewritten_html)
    if not ok:
        log.error(f"    Refusing to post — {reason}")
        return None

    title    = page.get("new_title") or page.get("title", "Untitled")
    new_slug = page.get("new_slug", "").strip("/") or slug_from_url(page.get("url", ""))

    # Always strip any accidental 'blog-' prefix — WP blog posts don't need it
    new_slug = re.sub(r"^blog-", "", new_slug)

    # Category
    category_name = page.get("category", "Uncategorized")
    category_id   = config.CATEGORY_MAP.get(category_name, 1)

    # Date — WP requires ISO 8601: YYYY-MM-DDTHH:MM:SS
    # Prefer the original publish date; fall back to last-modified if unknown.
    original_date = (
        page.get("created", "") or page.get("date_created", "") or
        page.get("modified", "") or page.get("date_modified", "") or ""
    ).strip()
    # Normalise space-separated datetime to T-separated
    original_date = original_date.replace(" ", "T")
    # Bare date only → add midnight time
    if re.match(r"^\d{4}-\d{2}-\d{2}$", original_date):
        original_date = original_date + "T00:00:00"
    # Final validation — must match YYYY-MM-DDTHH:MM:SS
    if original_date and not re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", original_date):
        log.warning(f"    Ignoring unrecognised date format: '{original_date}'")
        original_date = ""

    # ── Always post as a WP post, never a page ────────────────
    endpoint = f"{config.NEW_SITE_URL.rstrip('/')}/wp-json/wp/v2/posts"

    payload = {
        "title":      title,
        "content":    rewritten_html,
        "status":     config.PHASE2_POST_STATUS,
        "slug":       new_slug,
        "categories": [category_id],
    }
    if original_date:
        payload["date"] = original_date

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
        link    = data.get("link", f"/{new_slug}/")
        log.info(f"    → Posted  '{title}'  slug=/{new_slug}  (ID {post_id})  {link}")
        return post_id
    except requests.exceptions.HTTPError as e:
        log.error(f"    WP POST failed: {e} — {resp.text[:300]}")
    except Exception as e:
        log.error(f"    WP POST error: {e}")
    return None


def save_result(result: dict) -> str:
    os.makedirs(REWRITE_DIR, exist_ok=True)
    fname = slug_from_url(result["url"]) + ".json"
    path  = os.path.join(REWRITE_DIR, fname)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    return path


# ╔══════════════════════════════════════════════════════════════╗
# ║  INPUT LOADERS                                              ║
# ╚══════════════════════════════════════════════════════════════╝

def load_solo_blogs() -> list[dict]:
    """
    Primary: read data/06c_solo_blogs.json (output of 6c_prep_blogs.py).
    These records already have: url, title, new_slug, category, body_text,
    image_urls, video_urls, headings.
    """
    with open(SOLO_BLOGS_FILE, encoding="utf-8") as f:
        records = json.load(f)
    log.info(f"  Loaded {len(records)} solo blog posts from {SOLO_BLOGS_FILE}")

    # Normalise field names to what the rest of this script expects
    pages = []
    for r in records:
        pages.append({
            "url":        r.get("url", "").rstrip("/"),
            "title":      r.get("title", ""),
            "new_title":  r.get("title", ""),
            "new_slug":   re.sub(r"^blog-", "", (r.get("new_slug") or "").strip("/")),
            "category":   r.get("category", "Uncategorized"),
            "body_text":  r.get("body_text", ""),
            "headings":   r.get("headings", ""),
            "image_urls": r.get("image_urls", []),
            "video_urls": r.get("video_urls", []),
            "created":    r.get("created", ""),
            "modified":   r.get("modified", ""),
            "note":       r.get("note", ""),
            # Always a post
            "type":       "post",
        })
    return pages


def load_keeper_csv() -> list[dict]:
    """
    Legacy fallback: read ftj_keeper_list.csv.
    Infers post vs page from URL path (/blog/ prefix).
    """
    if not os.path.exists(KEEPER_CSV):
        log.warning(f"  Keeper CSV not found: {KEEPER_CSV}")
        return []
    pages = []
    with open(KEEPER_CSV, encoding="cp1252", newline="") as f:
        reader = csv.reader(f)
        for i, row in enumerate(reader):
            if i == 0 or not row:
                continue
            url      = row[0].strip().rstrip("/")
            category = row[6].strip() if len(row) > 6 else "Uncategorized"
            if not url:
                continue
            url_path = urlparse(url).path
            is_post  = url_path.startswith("/blog/") or url_path.startswith("/news/")
            pages.append({
                "url":        url,
                "title":      "",
                "new_title":  "",
                "new_slug":   slug_from_url(url),
                "category":   category,
                "body_text":  "",
                "headings":   [],
                "image_urls": [],
                "video_urls": [],
                "created":    "",
                "modified":   "",
                "note":       "",
                "type":       "post" if is_post else "page",
            })
    log.info(f"  Loaded {len(pages)} records from keeper CSV (fallback)")
    return pages


# ╔══════════════════════════════════════════════════════════════╗
# ║  MAIN                                                        ║
# ╚══════════════════════════════════════════════════════════════╝

def main():
    parser = argparse.ArgumentParser(description="Phase 2: AI blog post rewriter")
    parser.add_argument("--post",   action="store_true",
                        help="POST rewritten posts to the new site (default: dry-run)")
    parser.add_argument("--limit",  type=int, default=config.PHASE2_LIMIT,
                        help="Max posts to process this run (0 = no limit)")
    parser.add_argument("--force",  action="store_true",
                        help="Reprocess posts that already have a rewrite JSON")
    parser.add_argument("--url",    type=str, default=None,
                        help="Process a single post by its path (e.g. /blog/my-post)")
    parser.add_argument("--source", choices=["local", "live"], default=None,
                        help="Override source: 'local' = PHASE2_SOURCE_URL, 'live' = production")
    args = parser.parse_args()

    source_base = config.SITE_URL if args.source == "live" else config.PHASE2_SOURCE_URL
    dry_run = not args.post

    log.info("=" * 65)
    log.info("STEP 6 — Phase 2: AI blog post rewriter")
    log.info("=" * 65)
    log.info(f"  Source:   {source_base}")
    log.info(f"  New site: {config.NEW_SITE_URL}")
    log.info(f"  Model:    {config.ANTHROPIC_MODEL}")
    log.info(f"  Dry run:  {dry_run}  (pass --post to write to new site)")
    log.info(f"  Limit:    {args.limit or 'none'}")

    # ── Load pages ────────────────────────────────────────────
    if os.path.exists(SOLO_BLOGS_FILE):
        todo = load_solo_blogs()
    else:
        log.warning(f"  {SOLO_BLOGS_FILE} not found — falling back to keeper CSV")
        todo = load_keeper_csv()

    if not todo:
        log.error("No blog posts to process. Run 6c_prep_blogs.py first.")
        sys.exit(1)

    # ── Load media map ────────────────────────────────────────
    media_map_path = os.path.join(config.DATA_DIR, "06_media_map.json")
    media_map = {}
    if os.path.exists(media_map_path):
        with open(media_map_path, encoding="utf-8") as f:
            media_map = json.load(f)
        log.info(f"  Media map: {len(media_map)} entries loaded")
    else:
        log.warning("  No media map found — images will use original URLs without IDs")

    # ── Skip posts consumed by 6c_combine.py ─────────────────
    consumed_path = os.path.join(config.DATA_DIR, "06c_consumed.json")
    if os.path.exists(consumed_path):
        with open(consumed_path, encoding="utf-8") as f:
            consumed = set(json.load(f))
        before = len(todo)
        todo = [p for p in todo if p.get("url", "").rstrip("/") not in consumed]
        skipped = before - len(todo)
        if skipped:
            log.info(f"  Skipping {skipped} posts already merged by 6c_combine.py")

    # ── Filter to single URL if requested ────────────────────
    if args.url:
        target = args.url.rstrip("/")
        todo = [p for p in todo if urlparse(p.get("url","")).path.rstrip("/") == target]
        if not todo:
            log.error(f"No post found matching path '{args.url}'")
            sys.exit(1)

    # ── Skip already-done posts unless --force ────────────────
    os.makedirs(REWRITE_DIR, exist_ok=True)
    if not args.force:
        before = len(todo)
        todo = [p for p in todo
                if not os.path.exists(
                    os.path.join(REWRITE_DIR, slug_from_url(p.get("url","")) + ".json")
                )]
        skipped = before - len(todo)
        if skipped:
            log.info(f"  Skipping {skipped} already-rewritten posts (use --force to redo)")

    # ── Apply limit ───────────────────────────────────────────
    if args.limit and args.limit > 0:
        todo = todo[:args.limit]

    if not todo:
        log.info("Nothing to process.")
        sys.exit(0)

    log.info(f"\nProcessing {len(todo)} blog posts …\n")

    ok = posted = failed = 0

    for page in tqdm(todo, desc="Rewriting", unit="post"):
        url   = page.get("url", "")
        title = page.get("new_title") or page.get("title", url)

        log.info(f"\n{'─'*55}")
        log.info(f"  {title}")
        log.info(f"  {url}")

        # 1. Fetch live/local content (supplements pre-extracted body_text if available)
        fetch_url    = source_url_for(url, source_base)
        live_html    = fetch_page_html(fetch_url)
        live_content = None
        if live_html:
            live_content = extract_content_from_html(live_html)
            log.info(f"  Fetched {len(live_content['body_text'].split())} words from {fetch_url}")
        else:
            log.warning(f"  Could not fetch {fetch_url} — using pre-extracted content")

        # 2. Build prompt and call API
        prompt = build_prompt(page, live_content, media_map)
        log.info(f"  Calling {config.ANTHROPIC_MODEL} …")
        rewritten, suggested_slug = call_anthropic(SYSTEM_PROMPT, prompt)

        if not rewritten:
            log.error("  ✗ API returned nothing — skipping")
            failed += 1
            continue

        # 3. Resolve final slug (AI wins, then prep slug, then URL-derived)
        fallback_slug = page.get("new_slug", "") or slug_from_url(url)
        new_slug = re.sub(r"^blog-", "", (suggested_slug or fallback_slug).strip("/"))
        page["new_slug"] = new_slug
        log.info(f"  → /{new_slug}  ({len(rewritten.split())} words)")

        # 4. Build result record
        result = {
            "url":               url,
            "new_slug":          new_slug,
            "new_title":         title,
            "category":          page.get("category", "Uncategorized"),
            "rewritten_content": rewritten,
            "image_urls":        page.get("image_urls", []),
            "video_urls":        page.get("video_urls", []),
            "source_fetched":    bool(live_html),
            "fetch_url":         fetch_url,
            "post_id":           None,
            "posted_at":         None,
            "model":             config.ANTHROPIC_MODEL,
            "timestamp":         datetime.utcnow().isoformat() + "Z",
            "word_count_in":     len((live_content or {}).get("body_text","").split()),
            "word_count_out":    len(rewritten.split()),
            "ai_slug":           suggested_slug,
        }

        # 5. Save JSON
        saved_path = save_result(result)
        log.info(f"  Saved → {saved_path}")
        ok += 1

        # 6. POST to new site as a WP post
        if not dry_run:
            post_id = post_to_new_site(page, rewritten)
            if post_id:
                result["post_id"]   = post_id
                result["posted_at"] = datetime.utcnow().isoformat() + "Z"
                save_result(result)
                posted += 1

        time.sleep(0.5)

    # ── Summary ───────────────────────────────────────────────
    log.info(f"\n{'='*65}")
    log.info("Blog rewrite complete")
    log.info(f"  Rewritten: {ok}")
    if not dry_run:
        log.info(f"  Posted:    {posted}")
    log.info(f"  Failed:    {failed}")
    log.info(f"  Output:    {REWRITE_DIR}/")
    if dry_run:
        log.info(f"\n  Dry-run mode — nothing posted to {config.NEW_SITE_URL}")
        log.info(f"  Review the JSON files, then rerun with --post when ready.")
    log.info("=" * 65)


if __name__ == "__main__":
    main()
