#!/usr/bin/env python3
"""
prep_meta.py  —  Bridge: WXR Yoast CSV  →  old_site_meta.json for apply_meta.py

WHY THIS EXISTS
───────────────
apply_meta.py reads output/old_site_meta.json, which was originally produced
by fetch_old_meta.py — but that script 403s on the live site and was replaced
by extract_yoast_from_wxr.py, which outputs a CSV (output/yoast_meta.csv)
with NO new-site slug mapping. This script closes the gap:

    yoast_meta.csv  +  slug_redirects.csv   →   output/old_site_meta.json

MATCHING
────────
Old URLs are normalized (scheme/host/www/trailing-slash insensitive) and
matched by path. post_type for the NEW site comes from the redirect map's
`type` column (page → page, blog-* → post) since blog posts changed type
during migration. For combined blogs (many old URLs → one new slug), only
the FIRST matching old URL's meta is kept per new slug — extra hits are
logged as collisions and skipped.

USAGE
─────
  python prep_meta.py
  python prep_meta.py --meta output/yoast_meta.csv --redirects output/slug_redirects.csv

OUTPUT
──────
  output/old_site_meta.json     — input for apply_meta.py
  output/prep_meta_unmatched.csv — old URLs with Yoast meta but no redirect match
"""

import argparse
import csv
import json
import os
import sys
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(__file__))
import config
from utils import setup_logging

log = setup_logging("prep_meta")

META_CSV      = os.path.join(config.OUTPUT_DIR, "yoast_meta.csv")
REDIRECTS_CSV = os.path.join(config.OUTPUT_DIR, "slug_redirects.csv")
OUT_JSON      = os.path.join(config.OUTPUT_DIR, "old_site_meta.json")
UNMATCHED_CSV = os.path.join(config.OUTPUT_DIR, "prep_meta_unmatched.csv")


def norm_path(url: str) -> str:
    """Normalize a URL to a comparable path: lowercase, no trailing slash."""
    p = urlparse(url.strip())
    path = (p.path or "/").rstrip("/").lower()
    return path or "/"


def redirect_post_type(rtype: str) -> str:
    """Redirect map 'type' → new-site post_type."""
    return "page" if rtype == "page" else "post"   # blog-solo / blog-combined → post


def main():
    ap = argparse.ArgumentParser(description="Build old_site_meta.json from WXR CSV + redirect map")
    ap.add_argument("--meta",      default=META_CSV,      help=f"Yoast CSV (default: {META_CSV})")
    ap.add_argument("--redirects", default=REDIRECTS_CSV, help=f"Redirect map CSV (default: {REDIRECTS_CSV})")
    ap.add_argument("--out",       default=OUT_JSON,      help=f"Output JSON (default: {OUT_JSON})")
    args = ap.parse_args()

    log.info("=" * 65)
    log.info("prep_meta — Build old_site_meta.json for apply_meta.py")
    log.info("=" * 65)

    for path, hint in ((args.meta, "Run: python extract_yoast_from_wxr.py --wxr <export.xml>"),
                       (args.redirects, "Run: python update_slugs.py --redirects")):
        if not os.path.exists(path):
            log.error(f"  Not found: {path}")
            log.error(f"  {hint}")
            sys.exit(1)

    # ── Load redirect map: normalized old path → {new_slug, new_url, type} ──
    redirect_index: dict[str, dict] = {}
    with open(args.redirects, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            old = row.get("old_url", "").strip()
            if not old:
                continue
            redirect_index[norm_path(old)] = {
                "new_slug": row.get("new_slug", "").strip(),
                "new_url":  row.get("new_url", "").strip(),
                "type":     row.get("type", "").strip(),
            }
    log.info(f"  Redirect map: {len(redirect_index)} old URLs")

    # ── Load Yoast meta CSV and match ──
    entries, unmatched = [], []
    claimed_slugs: dict[str, str] = {}   # new_slug → old_url that claimed it
    collisions = 0

    with open(args.meta, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    log.info(f"  Yoast meta:   {len(rows)} rows")

    for row in rows:
        old_url = row.get("url", "").strip()
        entry = {
            "old_url":          old_url,
            "old_wp_id":        row.get("old_wp_id", ""),
            "seo_title":        row.get("seo_title", "").strip(),
            "meta_description": row.get("meta_description", "").strip(),
            "focus_keyword":    row.get("focus_keyword", "").strip(),
        }

        # Nothing worth pushing? skip silently.
        if not any((entry["seo_title"], entry["meta_description"], entry["focus_keyword"])):
            continue

        # Unmatchable URL (e.g. ?page_id=N drafts) or not in redirect map
        match = redirect_index.get(norm_path(old_url)) if old_url.startswith("http") else None
        if not match or not match["new_slug"]:
            entry.update({"matched": False, "new_slug": "", "new_url": "",
                          "post_type": row.get("post_type", "page")})
            unmatched.append(entry)
            entries.append(entry)
            continue

        new_slug = match["new_slug"]

        # Combined blogs: many old URLs → one slug. First meta wins.
        if new_slug in claimed_slugs:
            collisions += 1
            log.info(f"  collision: {old_url} → '{new_slug}' already claimed by {claimed_slugs[new_slug]} — skipped")
            continue
        claimed_slugs[new_slug] = old_url

        entry.update({
            "matched":   True,
            "new_slug":  new_slug,
            "new_url":   match["new_url"],
            "post_type": redirect_post_type(match["type"]),
        })
        entries.append(entry)

    matched = sum(1 for e in entries if e["matched"])
    log.info(f"\n  Matched: {matched}   Unmatched: {len(unmatched)}   Combined-blog collisions skipped: {collisions}")

    os.makedirs(config.OUTPUT_DIR, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2, ensure_ascii=False)
    log.info(f"  → {args.out}")

    if unmatched:
        with open(UNMATCHED_CSV, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["old_url", "old_wp_id", "post_type",
                                              "seo_title", "meta_description", "focus_keyword"],
                               extrasaction="ignore")
            w.writeheader()
            w.writerows(unmatched)
        log.info(f"  Unmatched review file → {UNMATCHED_CSV}")

    log.info("\n  Next: python apply_meta.py --dry-run")


if __name__ == "__main__":
    main()
