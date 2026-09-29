#!/usr/bin/env python3
"""
apply_meta.py  —  Apply scraped Yoast SEO metadata to the new FTJ WordPress site.

Reads output/old_site_meta.json (produced by fetch_old_meta.py) and patches
each matching post/page on the new site via the Yoast REST API endpoint.

REQUIRES
────────
  - Yoast SEO plugin installed and active on the new site
  - config.NEW_WP_USERNAME + config.NEW_WP_APP_PASSWORD with editor/admin role
  - output/old_site_meta.json from fetch_old_meta.py

USAGE
─────
  python apply_meta.py                  # apply all matched entries
  python apply_meta.py --dry-run        # preview what would be patched, no changes
  python apply_meta.py --posts          # posts only
  python apply_meta.py --pages          # pages only
  python apply_meta.py --slug my-slug   # single post/page by new slug

OUTPUT
──────
  output/apply_meta_results.csv   —  log of every patch attempt (ok / failed / skipped)
"""

import argparse
import csv
import json
import os
import sys
import time

import requests

sys.path.insert(0, os.path.dirname(__file__))
import config
from utils import setup_logging

log = setup_logging("apply_meta")

AUTH     = (config.NEW_WP_USERNAME, config.NEW_WP_APP_PASSWORD)
BASE     = config.NEW_SITE_URL.rstrip("/")
API      = f"{BASE}/wp-json/wp/v2"
YOAST_API = f"{BASE}/wp-json/yoast/v1"

META_JSON    = os.path.join(config.OUTPUT_DIR, "old_site_meta.json")
RESULTS_CSV  = os.path.join(config.OUTPUT_DIR, "apply_meta_results.csv")


# ══════════════════════════════════════════════════════════════════════════════
#  FETCH NEW SITE — build slug → {id, post_type} lookup
# ══════════════════════════════════════════════════════════════════════════════

def fetch_new_site_index() -> dict[str, dict]:
    """Return {slug: {id, post_type}} for all posts + pages on the new site."""
    log.info("  Building slug index from new site …")
    index: dict[str, dict] = {}

    for post_type in ("pages", "posts"):
        page = 1
        while True:
            try:
                resp = requests.get(
                    f"{API}/{post_type}",
                    auth=AUTH,
                    params={"per_page": 100, "page": page,
                            "status": "publish,draft", "_fields": "id,slug,status"},
                    timeout=30,
                )
                if resp.status_code == 400:
                    break
                resp.raise_for_status()
                data = resp.json()
                if not isinstance(data, list) or not data:
                    break
                for item in data:
                    index[item["slug"]] = {
                        "id":        item["id"],
                        "post_type": "page" if post_type == "pages" else "post",
                        "status":    item.get("status", ""),
                    }
                if page >= int(resp.headers.get("X-WP-TotalPages", 1)):
                    break
                page += 1
            except Exception as e:
                log.error(f"  Index fetch error ({post_type} page {page}): {e}")
                break

    log.info(f"  {len(index)} posts/pages indexed on new site")
    return index


# ══════════════════════════════════════════════════════════════════════════════
#  PATCH META via Yoast REST endpoint
# ══════════════════════════════════════════════════════════════════════════════

def patch_yoast_meta(post_id: int, post_type: str, entry: dict,
                     dry_run: bool = False) -> tuple[bool, str]:
    """
    Patch Yoast SEO meta on a new site post/page.
    Returns (success, message).
    """
    endpoint = f"{API}/{'pages' if post_type == 'page' else 'posts'}/{post_id}"

    # Build the meta payload — Yoast stores these in post meta
    yoast_meta = {}
    if entry.get("seo_title"):
        yoast_meta["_yoast_wpseo_title"] = entry["seo_title"]
    if entry.get("meta_description"):
        yoast_meta["_yoast_wpseo_metadesc"] = entry["meta_description"]
    if entry.get("focus_keyword"):
        yoast_meta["_yoast_wpseo_focuskw"] = entry["focus_keyword"]

    if not yoast_meta:
        return False, "skipped — no meta to apply"

    if dry_run:
        preview = {k: v[:60] + "…" if len(v) > 60 else v
                   for k, v in yoast_meta.items()}
        return True, f"DRY RUN — would patch: {preview}"

    try:
        resp = requests.post(
            endpoint,
            json={"meta": yoast_meta},
            auth=AUTH,
            timeout=30,
        )
        resp.raise_for_status()
        return True, "ok"
    except requests.exceptions.HTTPError as e:
        return False, f"HTTP {resp.status_code}: {resp.text[:200]}"
    except Exception as e:
        return False, str(e)


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    ap = argparse.ArgumentParser(description="Apply old-site Yoast meta to new WP site")
    ap.add_argument("--dry-run",  action="store_true",
                    help="Preview patches without making any changes")
    ap.add_argument("--posts",    action="store_true", help="Posts only")
    ap.add_argument("--pages",    action="store_true", help="Pages only")
    ap.add_argument("--slug",     type=str, default="",
                    help="Apply to a single new slug only")
    args = ap.parse_args()

    do_posts = args.posts or (not args.posts and not args.pages)
    do_pages = args.pages or (not args.posts and not args.pages)

    log.info("=" * 65)
    log.info("apply_meta — Apply Yoast SEO meta to new FTJ site")
    if args.dry_run:
        log.info("  *** DRY RUN — no changes will be made ***")
    log.info("=" * 65)

    # Load meta JSON
    if not os.path.exists(META_JSON):
        log.error(f"  {META_JSON} not found.")
        log.error("  Run: python fetch_old_meta.py")
        sys.exit(1)
    with open(META_JSON, encoding="utf-8") as f:
        all_entries: list[dict] = json.load(f)
    log.info(f"  Loaded {len(all_entries)} entries from old_site_meta.json")

    # Filter to matched only
    entries = [e for e in all_entries if e.get("matched") and e.get("new_slug")]
    log.info(f"  {len(entries)} matched entries to process")

    # Apply type and slug filters
    if args.slug:
        entries = [e for e in entries if e["new_slug"] == args.slug]
        log.info(f"  Filtered to slug '{args.slug}': {len(entries)} entries")
    else:
        if not do_posts:
            entries = [e for e in entries if e["post_type"] != "post"]
        if not do_pages:
            entries = [e for e in entries if e["post_type"] != "page"]

    if not entries:
        log.info("  Nothing to process.")
        sys.exit(0)

    # Build new site slug index
    slug_index = fetch_new_site_index()

    results = []
    ok = failed = skipped = 0

    for i, entry in enumerate(entries, 1):
        new_slug  = entry["new_slug"]
        post_type = entry["post_type"]
        old_url   = entry["old_url"]

        match = slug_index.get(new_slug)
        if not match:
            log.warning(f"  [{i}/{len(entries)}] SKIP — slug not found on new site: {new_slug}")
            results.append({
                "new_slug": new_slug, "old_url": old_url,
                "post_type": post_type, "status": "skipped",
                "message": "slug not found on new site",
            })
            skipped += 1
            continue

        post_id = match["id"]
        success, message = patch_yoast_meta(post_id, post_type, entry, args.dry_run)

        marker = "✓" if success else "✗"
        log.info(f"  [{i}/{len(entries)}] {marker} [{post_type}] {new_slug}")
        if not success or args.dry_run:
            log.info(f"    {message}")

        results.append({
            "new_slug":  new_slug,
            "old_url":   old_url,
            "post_id":   post_id,
            "post_type": post_type,
            "status":    "ok" if success else "failed",
            "message":   message,
            "seo_title": entry.get("seo_title", "")[:80],
            "meta_desc": entry.get("meta_description", "")[:80],
        })

        if success:
            ok += 1
        else:
            failed += 1

        time.sleep(0.1)

    log.info(f"\n  OK: {ok}  Failed: {failed}  Skipped: {skipped}")

    # Write results CSV
    os.makedirs(config.OUTPUT_DIR, exist_ok=True)
    fieldnames = ["new_slug", "old_url", "post_id", "post_type",
                  "status", "message", "seo_title", "meta_desc"]
    with open(RESULTS_CSV, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows(results)
    log.info(f"  Results → {RESULTS_CSV}")
    log.info("  Done.")


if __name__ == "__main__":
    main()
