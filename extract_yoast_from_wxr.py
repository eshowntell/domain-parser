#!/usr/bin/env python3
"""
extract_yoast_from_wxr.py  —  Extract Yoast SEO meta from a WordPress WXR export.

Reads the WXR XML file locally, pulls only the Yoast SEO fields,
and writes a clean CSV. Nothing sensitive leaves your machine.

USAGE
─────
  python extract_yoast_from_wxr.py --wxr path/to/export.xml
  python extract_yoast_from_wxr.py --wxr path/to/export.xml --out output/yoast_meta.csv

OUTPUT CSV columns
──────────────────
  url              — old site URL (for matching via slug_redirects.csv)
  post_type        — post | page
  old_wp_id        — original WP post ID
  seo_title        — _yoast_wpseo_title
  meta_description — _yoast_wpseo_metadesc
  focus_keyword    — _yoast_wpseo_focuskw
"""

import argparse
import csv
import os
import sys
import xml.etree.ElementTree as ET

# ── Yoast meta keys we care about ────────────────────────────────────────────
YOAST_KEYS = {
    "_yoast_wpseo_title":    "seo_title",
    "_yoast_wpseo_metadesc": "meta_description",
    "_yoast_wpseo_focuskw":  "focus_keyword",
}

WP_NS = "http://wordpress.org/export/1.2/"


def extract(wxr_path: str, out_path: str):
    print(f"  Reading {wxr_path} …")

    try:
        tree = ET.parse(wxr_path)
    except ET.ParseError as e:
        print(f"  ERROR: Could not parse XML — {e}")
        sys.exit(1)

    root    = tree.getroot()
    channel = root.find("channel")
    if channel is None:
        print("  ERROR: No <channel> element found — is this a valid WXR file?")
        sys.exit(1)

    rows   = []
    total  = 0
    kept   = 0

    for item in channel.findall("item"):
        total += 1

        post_type = item.findtext(f"{{{WP_NS}}}post_type", "").strip()
        if post_type not in ("post", "page"):
            continue

        status = item.findtext(f"{{{WP_NS}}}status", "").strip()
        if status not in ("publish", "draft"):
            continue

        url       = item.findtext("link", "").strip()
        old_wp_id = item.findtext(f"{{{WP_NS}}}post_id", "").strip()

        # Collect all post meta
        meta: dict[str, str] = {}
        for pm in item.findall(f"{{{WP_NS}}}postmeta"):
            key = pm.findtext(f"{{{WP_NS}}}meta_key", "").strip()
            val = pm.findtext(f"{{{WP_NS}}}meta_value", "").strip()
            if key in YOAST_KEYS:
                meta[key] = val

        # Only include rows that have at least one Yoast field
        if not any(k in meta for k in YOAST_KEYS):
            continue

        rows.append({
            "url":             url,
            "post_type":       post_type,
            "old_wp_id":       old_wp_id,
            "seo_title":       meta.get("_yoast_wpseo_title", ""),
            "meta_description": meta.get("_yoast_wpseo_metadesc", ""),
            "focus_keyword":   meta.get("_yoast_wpseo_focuskw", ""),
        })
        kept += 1

    print(f"  {total} items in WXR — {kept} with Yoast meta extracted")

    if not rows:
        print("  No Yoast meta found. Check that the export includes post meta.")
        sys.exit(0)

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    fieldnames = ["url", "post_type", "old_wp_id",
                  "seo_title", "meta_description", "focus_keyword"]
    with open(out_path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)

    print(f"  → {out_path}  ({kept} rows)")
    print("  Done.  Next: python apply_meta.py")


def main():
    ap = argparse.ArgumentParser(
        description="Extract Yoast SEO meta from a WordPress WXR export file")
    ap.add_argument("--wxr", required=True,
                    help="Path to the WordPress WXR export XML file")
    ap.add_argument("--out", default="output/yoast_meta.csv",
                    help="Output CSV path (default: output/yoast_meta.csv)")
    args = ap.parse_args()

    if not os.path.exists(args.wxr):
        print(f"  ERROR: File not found: {args.wxr}")
        sys.exit(1)

    print("=" * 65)
    print("extract_yoast_from_wxr — Extract Yoast meta from WXR")
    print("=" * 65)
    extract(args.wxr, args.out)


if __name__ == "__main__":
    main()
