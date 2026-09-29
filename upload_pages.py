#!/usr/bin/env python3
"""
upload_pages.py  —  Upload AI-enhanced page JSONs to WordPress.

Reads every JSON from data/06_pages_ai/ (or --src) and POSTs each one to the
new WordPress site via REST API. No rewriting, no Anthropic calls — pure upload.

Pages already posted (have a post_id) are skipped unless --force is used.
Parent pages are resolved shallowest-first so children always find their parent.

USAGE
─────
  python upload_pages.py                  # dry-run, preview only
  python upload_pages.py --post           # upload for real
  python upload_pages.py --post --limit 3 # smoke test, 3 pages
  python upload_pages.py --post --slug memory-care  # single page
  python upload_pages.py --post --status publish    # publish instead of draft
"""

import argparse
import glob
import json
import os
import sys
import time
from datetime import datetime
from urllib.parse import urlparse

import requests

sys.path.insert(0, os.path.dirname(__file__))
import config
from utils import setup_logging

log = setup_logging("upload_pages")

BASE     = config.NEW_SITE_URL.rstrip("/")
API      = f"{BASE}/wp-json/wp/v2"
AUTH     = (config.NEW_WP_USERNAME, config.NEW_WP_APP_PASSWORD)

SRC_DIR  = getattr(config, "PAGES_SCHEMA_DIR",
                   os.path.join(config.DATA_DIR, "06_pages_schema"))

# Slug → WP page ID cache (populated as pages post, so children find parents)
_slug_cache: dict[str, int] = {}


# ══════════════════════════════════════════════════════════════════════════════
#  PARENT RESOLUTION
# ══════════════════════════════════════════════════════════════════════════════

def path_depth(url: str) -> int:
    return len([s for s in urlparse(url).path.split("/") if s])


def slug_from_path(url: str) -> str:
    return urlparse(url).path.rstrip("/").split("/")[-1]


def find_parent_id(page: dict, dry_run: bool) -> int | None:
    """Return WP ID of the parent page, or None for root pages."""
    parent_id = page.get("parent_id")

    # Already resolved as an int WP ID
    if isinstance(parent_id, int) and parent_id > 0:
        return parent_id

    # Already resolved and cached from a previous post this run
    old_url  = page.get("url", "")
    url_path = urlparse(old_url).path.rstrip("/")
    parent_path = "/".join(url_path.split("/")[:-1])
    if not parent_path or parent_path == "/":
        return None

    parent_slug = parent_path.split("/")[-1]
    if parent_slug in _slug_cache:
        return _slug_cache[parent_slug]

    # Look up on WP
    try:
        resp = requests.get(
            f"{API}/pages",
            auth=AUTH,
            params={"slug": parent_slug, "_fields": "id,slug"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        if data:
            pid = data[0]["id"]
            _slug_cache[parent_slug] = pid
            return pid
    except Exception as e:
        log.warning(f"    Parent lookup failed for '{parent_slug}': {e}")

    return None


# ══════════════════════════════════════════════════════════════════════════════
#  POST TO WORDPRESS
# ══════════════════════════════════════════════════════════════════════════════

def post_page(page: dict, parent_id: int | None,
              status: str, dry_run: bool) -> int | None:
    title    = page.get("new_title", page.get("new_slug", "Untitled"))
    content  = page.get("rewritten_content", "")
    new_slug = page.get("new_slug", "").strip("/")
    post_id  = page.get("post_id")

    if dry_run:
        action = f"PATCH ID={post_id}" if post_id else "POST new"
        log.info(f"    DRY RUN — would {action}: /{new_slug}  parent={parent_id}")
        return post_id or -1

    payload = {
        "title":   title,
        "content": content,
        "status":  status,
        "slug":    new_slug,
    }
    if parent_id:
        payload["parent"] = parent_id

    try:
        if post_id:
            # Update existing page
            resp = requests.post(
                f"{API}/pages/{post_id}",
                json=payload,
                auth=AUTH,
                timeout=30,
            )
        else:
            # Create new page
            resp = requests.post(
                f"{API}/pages",
                json=payload,
                auth=AUTH,
                timeout=30,
            )
        resp.raise_for_status()
        data     = resp.json()
        returned_id = data.get("id")
        link     = data.get("link", f"/{new_slug}/")
        _slug_cache[new_slug] = returned_id
        action = "Updated" if post_id else "Posted"
        log.info(f"    → {action} {link}  (ID {returned_id})")
        return returned_id
    except requests.exceptions.HTTPError as e:
        log.error(f"    WP request failed: {e} — {getattr(resp,'text','')[:300]}")
    except Exception as e:
        log.error(f"    WP request error: {e}")
    return None


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    ap = argparse.ArgumentParser(description="Upload AI-enhanced pages to WordPress")
    ap.add_argument("--src",    default=SRC_DIR,
                    help=f"Source folder of page JSONs (default: {SRC_DIR})")
    ap.add_argument("--post",   action="store_true",
                    help="Upload to WordPress (default: dry-run)")
    ap.add_argument("--limit",  type=int, default=0,
                    help="Max pages to upload this run (0 = no limit)")
    ap.add_argument("--slug",   type=str, default="",
                    help="Upload a single page by slug")
    ap.add_argument("--status", type=str, default="draft",
                    choices=["draft", "publish"],
                    help="WP post status (default: draft)")
    args = ap.parse_args()

    dry_run = not args.post

    log.info("=" * 65)
    log.info("upload_pages — Upload AI-enhanced pages to WordPress")
    if dry_run:
        log.info("  *** DRY RUN — nothing will be posted ***")
    log.info("=" * 65)
    log.info(f"  Source:  {args.src}")
    log.info(f"  Site:    {BASE}")
    log.info(f"  Status:  {args.status}")

    if not os.path.exists(args.src):
        log.error(f"  Source folder not found: {args.src}")
        log.error("  Run ai_enhance_pages.py first, or set PAGES_AI_DIR in config.py")
        sys.exit(1)

    # Load all JSONs
    files = sorted(glob.glob(os.path.join(args.src, "*.json")))
    if args.slug:
        files = [f for f in files if os.path.basename(f) == f"{args.slug}.json"]
        if not files:
            log.error(f"  No file found for slug: {args.slug}")
            sys.exit(1)

    pages = []
    for filepath in files:
        with open(filepath, encoding="utf-8") as f:
            page = json.load(f)
        page["_filepath"] = filepath
        pages.append(page)

    # With post_id present we PATCH (update) — only skip if --no-update is set
    # --force has no special meaning here anymore since updates are always safe

    # Sort shallowest first so parents post before children
    pages.sort(key=lambda p: path_depth(p.get("url", "")))

    if args.limit and args.limit > 0:
        pages = pages[:args.limit]

    log.info(f"  {len(pages)} pages to upload (existing post_ids will be updated)\n")

    if not pages:
        log.info("  Nothing to upload.")
        sys.exit(0)

    ok = failed = skipped = 0

    for i, page in enumerate(pages, 1):
        slug  = page.get("new_slug", "?")
        title = page.get("new_title", slug)
        ptype = page.get("page_type", "")

        log.info(f"\n  [{i}/{len(pages)}] [{ptype}] {title}")
        log.info(f"    /{slug}")

        # Skip pages with no content
        if not page.get("rewritten_content", "").strip():
            log.warning("    No content — skipping")
            skipped += 1
            continue

        parent_id = find_parent_id(page, dry_run)
        post_id   = post_page(page, parent_id, args.status, dry_run)

        if dry_run:
            ok += 1
            continue

        if post_id:
            # Update the JSON with post_id so it won't re-upload next run
            page["post_id"]   = post_id
            page["posted_at"] = datetime.utcnow().isoformat() + "Z"
            filepath = page.pop("_filepath")
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump({k: v for k, v in page.items() if not k.startswith("_")},
                          f, indent=2, ensure_ascii=False)
            ok += 1
        else:
            failed += 1

        time.sleep(0.3)

    log.info(f"\n{'='*65}")
    log.info(f"  Uploaded: {ok}  Failed: {failed}  Skipped: {skipped}")
    if dry_run:
        log.info("  Dry-run complete — run with --post when ready.")
    log.info("=" * 65)


if __name__ == "__main__":
    main()
