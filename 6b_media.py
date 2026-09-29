#!/usr/bin/env python3
"""
6b_media.py  —  Media migration: download from live site, upload to ftj3.local.

Reads:
  ftj_keeper_list.csv             — 35 keeper page URLs (col A)
  ftj_site_audit_image_urls.csv   — full audit export; col A = page URL,
                                    col G = image URLs (newline-separated),
                                    col H = video URLs (newline-separated)

For every media URL found on keeper pages:
  1. Cross-references keeper list against the image URL CSV
  2. Downloads each file from the live site to ~/Downloads/ftj-media/
  3. Uploads it to ftj3.local via WP REST API (/wp-json/wp/v2/media)
  4. Records old URL → new URL mapping

Outputs:
  data/06_media_map.json    — {old_url: new_url, ...}  used by 6_rewrite.py
  data/06_media_404.json    — [{media_url, page_url, page_title}, ...] for manual review
  ~/Downloads/ftj-media/    — downloaded files (safe to delete after a successful run)

Usage:
  python 6b_media.py                  # full run
  python 6b_media.py --limit 5        # process first 10 files (good for testing)
  python 6b_media.py --force          # re-download + re-upload everything
  python 6b_media.py --skip-download  # files already on disk, just upload + map
"""

import argparse
import csv
import json
import mimetypes
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import requests
from tqdm import tqdm

sys.path.insert(0, os.path.dirname(__file__))
import config
from utils import setup_logging

log = setup_logging("6b_media")

# ── Paths ─────────────────────────────────────────────────────
KEEPER_CSV     = config.PHASE2_KEEPER_CSV
IMAGE_CSV      = config.PHASE2_IMAGE_CSV
MEDIA_MAP_FILE = os.path.join(config.DATA_DIR, "06_media_map.json")
MEDIA_404_FILE = os.path.join(config.DATA_DIR, "06_media_404.json")
DOWNLOAD_DIR   = Path.home() / "Downloads" / "ftj-media"

MEDIA_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico", ".avif",
    ".mp4", ".mov", ".avi", ".webm", ".ogv", ".m4v",
    ".mp3", ".ogg", ".wav", ".m4a",
    ".pdf",
}

SITE_HOST = urlparse(config.SITE_URL).netloc.lstrip("www.")


# ╔══════════════════════════════════════════════════════════════╗
# ║  COLLECTION                                                 ║
# ╚══════════════════════════════════════════════════════════════╝

def load_keeper_urls() -> dict[str, str]:
    """
    Load keeper list CSV.
    Returns {normalised_url: title} for all 35 keeper pages.
    """
    if not os.path.exists(KEEPER_CSV):
        log.error(f"Keeper CSV not found: {KEEPER_CSV}")
        sys.exit(1)
    keepers = {}
    with open(KEEPER_CSV, encoding="cp1252", newline="") as f:
        reader = csv.reader(f)
        for i, row in enumerate(reader):
            if i == 0 or not row:
                continue
            url   = row[0].strip().rstrip("/")
            title = row[1].strip() if len(row) > 1 else url
            if url:
                keepers[url] = title
    log.info(f"  Loaded {len(keepers)} keeper URLs from {KEEPER_CSV}")
    return keepers


def collect_media_from_csv(keepers: dict[str, str]) -> list[dict]:
    """
    Read ftj_site_audit_image_urls.csv and collect all media URLs
    for pages that appear in the keeper list.

    CSV layout (from 5_report.py Page Content tab):
      col A (0) — URL
      col B (1) — Title
      col C (2) — H1 (Best)
      col D (3) — Status
      col E (4) — Word Count
      col F (5) — Headings (H2-H4)
      col G (6) — Image URLs  (newline-separated)
      col H (7) — Video URLs  (newline-separated)
      col I (8) — Full Body Text

    Returns deduplicated list of:
      {"media_url": ..., "page_url": ..., "page_title": ...}
    Same media file appearing on multiple pages is included only once.
    """
    if not os.path.exists(IMAGE_CSV):
        log.error(f"Image URL CSV not found: {IMAGE_CSV}")
        log.error(f"  Expected at: {IMAGE_CSV}")
        log.error(f"  Set PHASE2_IMAGE_CSV in config.py to the correct path.")
        sys.exit(1)

    seen: dict[str, dict] = {}   # media_url → first-seen record
    matched_pages  = 0
    skipped_pages  = 0

    with open(IMAGE_CSV, encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        for i, row in enumerate(reader):
            if i == 0 or not row:
                continue

            page_url = row[0].strip().rstrip("/") if len(row) > 0 else ""
            if not page_url or page_url not in keepers:
                skipped_pages += 1
                continue

            matched_pages += 1
            page_title = keepers[page_url]

            # Col G — image URLs, col H — video URLs; both newline-separated
            raw_images = row[6].strip() if len(row) > 6 else ""
            raw_videos = row[7].strip() if len(row) > 7 else ""

            all_media = []
            if raw_images:
                all_media += [u.strip() for u in raw_images.splitlines() if u.strip()]
            if raw_videos:
                all_media += [u.strip() for u in raw_videos.splitlines() if u.strip()]

            for media_url in all_media:
                if not media_url:
                    continue
                parsed = urlparse(media_url)
                ext    = Path(parsed.path).suffix.lower()

                # Skip unrecognised extensions
                if ext not in MEDIA_EXTENSIONS:
                    continue
                # Skip external URLs (YouTube embeds etc.)
                host = parsed.netloc.lstrip("www.")
                if host and SITE_HOST not in host and host not in SITE_HOST:
                    continue
                # Skip data URIs
                if media_url.startswith("data:"):
                    continue

                if media_url not in seen:
                    seen[media_url] = {
                        "media_url":  media_url,
                        "page_url":   page_url,
                        "page_title": page_title,
                    }

    log.info(f"  CSV rows matched to keepers: {matched_pages}")
    log.info(f"  Unique media files to migrate: {len(seen)}")
    if matched_pages < len(keepers):
        log.warning(
            f"  Only {matched_pages}/{len(keepers)} keeper pages found in the CSV. "
            f"Check that URLs match exactly between the two files."
        )
    return list(seen.values())


# ╔══════════════════════════════════════════════════════════════╗
# ║  DOWNLOAD / UPLOAD                                          ║
# ╚══════════════════════════════════════════════════════════════╝

def local_path_for(media_url: str) -> Path:
    """
    Map a remote URL to a local path under DOWNLOAD_DIR,
    preserving the wp-content/uploads/... folder structure.
      https://franketobeyjones.com/wp-content/uploads/2024/01/hero.jpg
      → ~/Downloads/ftj-media/wp-content/uploads/2024/01/hero.jpg
    """
    parsed = urlparse(media_url)
    rel    = parsed.path.lstrip("/")
    return DOWNLOAD_DIR / rel


def download_file(media_url: str, dest: Path) -> bool:
    """Download one file from the live site. Returns True on success."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        resp = requests.get(
            media_url,
            timeout=config.REQUEST_TIMEOUT,
            stream=True,
            headers={"User-Agent": "ftj-migration-bot/1.0"},
        )
        if resp.status_code == 404:
            return False
        resp.raise_for_status()
        with open(dest, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=65536):
                fh.write(chunk)
        return True
    except Exception as e:
        log.warning(f"    download error: {e}")
        return False


def upload_to_new_site(local_path: Path) -> str | None:
    mime, _ = mimetypes.guess_type(str(local_path))
    if not mime:
        mime = "application/octet-stream"

    endpoint = f"{config.NEW_SITE_URL.rstrip('/')}/wp-json/wp/v2/media"
    
    # Check if file already exists in media library
    try:
        search = requests.get(
            endpoint,
            auth=(config.NEW_WP_USERNAME, config.NEW_WP_APP_PASSWORD),
            params={"search": local_path.name, "per_page": 1},
            timeout=30,
        )
        results = search.json()
        if results and isinstance(results, list) and len(results) > 0:
            existing_url = results[0].get("source_url")
            if existing_url:
                existing_id = results[0].get("id", "")
                log.info(f"    already exists: {local_path.name}")
                return {"url": existing_url, "id": existing_id}
    except Exception:
        pass  # fall through to upload

    # Upload
    try:
        with open(local_path, "rb") as fh:
            resp = requests.post(
                endpoint,
                auth=(config.NEW_WP_USERNAME, config.NEW_WP_APP_PASSWORD),
                headers={
                    "Content-Disposition": f'attachment; filename="{local_path.name}"',
                    "Content-Type": mime,
                },
                data=fh,
                timeout=60,
            )
        resp.raise_for_status()
        data = resp.json()
        new_url = data.get("source_url", "")
        new_id  = data.get("id", "")
        return {"url": new_url, "id": new_id}
    except requests.exceptions.HTTPError as e:
        log.warning(f"    upload HTTP error: {e} — {resp.text[:200]}")
    except Exception as e:
        log.warning(f"    upload error: {e}")
    return None

# ╔══════════════════════════════════════════════════════════════╗
# ║  MAP / 404 PERSISTENCE                                      ║
# ╚══════════════════════════════════════════════════════════════╝

def load_existing_map() -> dict:
    if os.path.exists(MEDIA_MAP_FILE):
        with open(MEDIA_MAP_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_map(media_map: dict):
    os.makedirs(config.DATA_DIR, exist_ok=True)
    with open(MEDIA_MAP_FILE, "w", encoding="utf-8") as f:
        json.dump(media_map, f, indent=2, ensure_ascii=False)


def save_404s(not_found: list[dict]):
    os.makedirs(config.DATA_DIR, exist_ok=True)
    with open(MEDIA_404_FILE, "w", encoding="utf-8") as f:
        json.dump(not_found, f, indent=2, ensure_ascii=False)
    if not_found:
        log.warning(f"  {len(not_found)} 404s logged → {MEDIA_404_FILE}")


# ╔══════════════════════════════════════════════════════════════╗
# ║  MAIN                                                       ║
# ╚══════════════════════════════════════════════════════════════╝

def main():
    parser = argparse.ArgumentParser(description="Migrate media from live site to new WP build")
    parser.add_argument("--limit",         type=int, default=0,
                        help="Max files to process this run (0 = no limit)")
    parser.add_argument("--force",         action="store_true",
                        help="Re-download and re-upload files already in the map")
    parser.add_argument("--skip-download", action="store_true",
                        help="Skip downloading — use files already in ~/Downloads/ftj-media/")
    parser.add_argument("--skip-upload", action="store_true",
                        help="Download only; skip upload (the Phoso bridge uploads renamed media later)")
    args = parser.parse_args()

    log.info("=" * 65)
    log.info("STEP 6b — Media migration")
    log.info("=" * 65)
    log.info(f"  Keeper CSV:  {KEEPER_CSV}")
    log.info(f"  Image CSV:   {IMAGE_CSV}")
    log.info(f"  Source:      {config.SITE_URL}")
    log.info(f"  Download to: {DOWNLOAD_DIR}")
    log.info(f"  Upload to:   {config.NEW_SITE_URL}")

    # ── Collect ───────────────────────────────────────────────
    keepers     = load_keeper_urls()
    media_items = collect_media_from_csv(keepers)

    # ── Skip already-mapped files ─────────────────────────────
    media_map = load_existing_map()
    if media_map and not args.force:
        before      = len(media_items)
        media_items = [m for m in media_items if m["media_url"] not in media_map]
        skipped     = before - len(media_items)
        if skipped:
            log.info(f"  Skipping {skipped} already-mapped files (--force to redo)")

    if args.limit and args.limit > 0:
        media_items = media_items[:args.limit]

    if not media_items:
        log.info("Nothing to do — all media already mapped.")
        log.info(f"  Map: {MEDIA_MAP_FILE}  ({len(media_map)} entries)")
        sys.exit(0)

    log.info(f"\nProcessing {len(media_items)} files …\n")

    # ── Per-file loop ─────────────────────────────────────────
    downloaded = 0
    uploaded   = 0
    not_found  = []
    errors     = []

    for item in tqdm(media_items, desc="Media", unit="file"):
        media_url  = item["media_url"]
        page_url   = item["page_url"]
        page_title = item["page_title"]
        local_path = local_path_for(media_url)

        # 1. Download
        if not args.skip_download:
            if local_path.exists() and not args.force:
                pass  # already on disk from a previous run
            else:
                ok = download_file(media_url, local_path)
                if not ok:
                    log.warning(f"  404: {media_url}")
                    not_found.append({
                        "media_url":  media_url,
                        "page_url":   page_url,
                        "page_title": page_title,
                    })
                    continue
                downloaded += 1
                time.sleep(0.2)
        else:
            if not local_path.exists():
                log.warning(f"  Not on disk (--skip-download): {local_path}")
                errors.append(media_url)
                continue

        # Download-only mode: the Phoso bridge uploads renamed media later.
        if args.skip_upload:
            continue

        # 2. Upload
        result = upload_to_new_site(local_path)
        if not result:
            log.warning(f"  Upload failed: {local_path.name}")
            errors.append(media_url)
            continue

        if isinstance(result, dict):
            media_map[media_url] = {"url": result["url"], "id": result["id"]}
        else:
            media_map[media_url] = {"url": result, "id": ""}
        uploaded += 1

        # Incremental save every 10 uploads
        if uploaded % 10 == 0:
            save_map(media_map)

    # ── Final saves ───────────────────────────────────────────
    save_map(media_map)
    save_404s(not_found)

    # ── Summary ───────────────────────────────────────────────
    log.info(f"\n{'='*65}")
    log.info("Media migration complete")
    log.info(f"  Downloaded:  {downloaded}")
    log.info(f"  Uploaded:    {uploaded}")
    log.info(f"  404s:        {len(not_found)}"
             + (f"  → see {MEDIA_404_FILE}" if not_found else ""))
    log.info(f"  Errors:      {len(errors)}")
    log.info(f"  Map total:   {len(media_map)} entries  →  {MEDIA_MAP_FILE}")
    log.info(f"  Local files: {DOWNLOAD_DIR}")
    log.info(f"\nNext: python 6_rewrite.py")
    log.info("=" * 65)


if __name__ == "__main__":
    main()
