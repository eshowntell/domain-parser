#!/usr/bin/env python3
"""
phoso_bridge.py — Bridge between Phoso (the external image sorter) and the
domain-parser media migration.

Phoso is a black box to us: it sorts downloaded media into category buckets and
writes a Florence caption into each image's EXIF ImageDescription. This bridge
reads that OUTPUT (folders + captions), matches each processed image back to its
original web URL by perceptual hash, renames it from its caption, and produces
the old_url → new_wp_url map the content rewrite already consumes.

See PHOSO_BRIDGE_SPEC.md for the full design.

MODES
─────
  python phoso_bridge.py --og-manifest --download-dir <dir>
        Build Map 1: for every downloaded image, {og_url, og_name,
        og_library_path, local_path, phash, sha256}. og_url is reconstructed
        from the path under --download-dir (which preserves wp-content/uploads/…)
        joined to config.SITE_URL.  → config.MEDIA_OG_MANIFEST

  python phoso_bridge.py --reconcile [--phoso-dir <dir>] [--test]
        The core. Walk the final Phoso buckets (skipping _multi/), pHash-join
        each image back to Map 1, read its Florence caption, slug it into a new
        filename, and write the rename map + a review report.
        --test skips the og-join (no Map 1 needed) so you can eyeball the
        caption→name output against any Phoso output folder.
        → config.MEDIA_RENAME_MAP , config.MEDIA_RECONCILE_CSV

  python phoso_bridge.py --report
        Read-only summary of the current rename map / reconcile report.

  python phoso_bridge.py --upload [--post]
        Upload the renamed images to the new site's media library, attach meta,
        and write config.MEDIA_MAP_FILE (og_url → {url,id}). Dry-run unless
        --post. (Requires a reachable config.NEW_SITE_URL.)

Reads Phoso output from config.PHOSO_OUTPUT_DIR by default.
"""

import argparse
import csv
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(__file__))
import config
from utils import setup_logging

log = setup_logging("phoso_bridge")

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tiff", ".tif"}

# pHash join tolerance. Phoso only rewrites metadata (not pixels), so the same
# image hashes to distance 0; the small cushion covers a format re-encode.
PHASH_EXACT      = 0
PHASH_MAX_HAMMING = 4


# ══════════════════════════════════════════════════════════════════════════════
#  METADATA / HASH HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _phash(path):
    """Perceptual hash (imagehash) of an image, or None on failure."""
    try:
        import imagehash
        from PIL import Image
        with Image.open(path) as im:
            return imagehash.phash(im)
    except Exception as e:
        log.warning(f"  pHash failed for {os.path.basename(path)}: {e}")
        return None


def _sha256_pixels(path):
    """Content hash of decoded pixels (metadata-invariant), or '' on failure."""
    try:
        from PIL import Image
        with Image.open(path) as im:
            im = im.convert("RGB")
            h = hashlib.sha256()
            h.update(im.tobytes())
            h.update(f"{im.width}x{im.height}".encode())
            return h.hexdigest()[:16]
    except Exception:
        return ""


def _read_caption(path):
    """Florence caption from EXIF ImageDescription (0x010E) via PIL, or ''."""
    try:
        from PIL import Image
        ex = Image.open(path).getexif()
        return (ex.get(0x010E, "") or "").strip()
    except Exception:
        return ""


def _read_ocr(path):
    """Optional: Xmp.phoso.OCRText via pyexiv2 (returns '' if pyexiv2 absent)."""
    try:
        import pyexiv2
        img = pyexiv2.Image(str(path))
        xmp = img.read_xmp()
        img.close()
        return (xmp.get("Xmp.phoso.OCRText", "") or "").strip()
    except Exception:
        return ""


def _caption_source(caption):
    """Classify a caption: 'florence' (free-form), 'template' (SigLIP), or 'none'."""
    low = caption.lower()
    if low.startswith(("the image", "this image")):
        return "florence"
    if "photographed" in low and "mood" in low:
        return "template"
    return "none"


# ══════════════════════════════════════════════════════════════════════════════
#  NAMING
# ══════════════════════════════════════════════════════════════════════════════

_SLUG_STOP = frozenset({
    "the", "a", "an", "of", "on", "in", "with", "and", "is", "are", "at", "to",
    "photographed", "image", "shows", "showing", "mood", "light", "that", "this",
    "depicts", "there", "some", "for", "its", "it",
})
_CAPTION_LEADS = ("the image shows ", "the image is ", "this image shows ",
                  "the image depicts ", "an image of ", "a photo of ")


def seo_slug(caption, max_words=6):
    """Slugify a caption into an SEO filename stem (no extension)."""
    text = caption.strip()
    # template shape "<subject>, photographed ..." → subject clause only
    if "photographed" in text.lower() and "," in text:
        text = text.split(",")[0]
    low = text.lower()
    for lead in _CAPTION_LEADS:
        if low.startswith(lead):
            text = text[len(lead):]
            break
    toks = [w for w in re.sub(r"[^a-z0-9 ]", " ", text.lower()).split()
            if w not in _SLUG_STOP]
    seen, uniq = set(), []              # drop repeated words so the cap fills with distinct ones
    for w in toks:
        if w not in seen:
            seen.add(w)
            uniq.append(w)
    return "-".join(uniq[:max_words])


def category_slug(category):
    """Reliable fallback name from a corrected bucket, e.g.
    photo_portrait_two_people → portrait-two-people."""
    c = re.sub(r"^(photo|photos)_", "", category)
    return re.sub(r"[^a-z0-9]+", "-", c.lower()).strip("-") or "image"


def _dedupe(stem, ext, used):
    """Ensure stem+ext is unique within `used` (a set), appending -1/-2/…"""
    name = f"{stem}{ext}"
    n = 1
    while name.lower() in used:
        name = f"{stem}-{n}{ext}"
        n += 1
    used.add(name.lower())
    return name


# ══════════════════════════════════════════════════════════════════════════════
#  FINAL-BUCKET WALK
# ══════════════════════════════════════════════════════════════════════════════

def iter_final_images(phoso_dir):
    """Yield (path, category) for every image in phoso_dir/<category>/…,
    skipping the _multi/ dual-category duplicate folder."""
    root = Path(phoso_dir)
    if not root.is_dir():
        log.error(f"Phoso output dir not found: {phoso_dir}")
        return
    for cat_dir in sorted(root.iterdir()):
        if not cat_dir.is_dir():
            continue
        if cat_dir.name == config.PHASE_MULTI_DIRNAME:
            continue
        category = cat_dir.name
        for img in sorted(cat_dir.rglob("*")):
            if img.is_file() and img.suffix.lower() in IMAGE_EXTS:
                yield img, category


# ══════════════════════════════════════════════════════════════════════════════
#  MODE: --og-manifest
# ══════════════════════════════════════════════════════════════════════════════

def cmd_og_manifest(download_dir):
    """Build Map 1 from a download tree that preserves wp-content/uploads/…"""
    root = Path(download_dir)
    if not root.is_dir():
        log.error(f"Download dir not found: {download_dir}")
        sys.exit(1)

    site = config.SITE_URL.rstrip("/")
    entries = []
    for img in sorted(root.rglob("*")):
        if not (img.is_file() and img.suffix.lower() in IMAGE_EXTS):
            continue
        rel = img.relative_to(root).as_posix()          # wp-content/uploads/2024/01/x.jpg
        og_url = f"{site}/{rel}"
        ph = _phash(img)
        entries.append({
            "og_url":           og_url,
            "og_name":          img.name,
            "og_library_path":  rel,
            "local_path":       str(img),
            "phash":            str(ph) if ph else "",
            "sha256":           _sha256_pixels(img),
            "is_video":         False,
        })

    os.makedirs(os.path.dirname(config.MEDIA_OG_MANIFEST), exist_ok=True)
    with open(config.MEDIA_OG_MANIFEST, "w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2, ensure_ascii=False)
    log.info(f"  Map 1 written: {len(entries)} images → {config.MEDIA_OG_MANIFEST}")


# ══════════════════════════════════════════════════════════════════════════════
#  MODE: --reconcile
# ══════════════════════════════════════════════════════════════════════════════

def _load_og_index():
    """Load Map 1 and return (list of entries, list of (ImageHash, entry))."""
    if not os.path.exists(config.MEDIA_OG_MANIFEST):
        return None, None
    import imagehash
    with open(config.MEDIA_OG_MANIFEST, encoding="utf-8") as f:
        entries = json.load(f)
    index = []
    for e in entries:
        if e.get("phash"):
            try:
                index.append((imagehash.hex_to_hash(e["phash"]), e))
            except Exception:
                continue
    return entries, index


def _join(final_phash, og_index):
    """Return (entry, distance, status) matching final_phash against Map 1.
    status ∈ {'matched','ambiguous','orphan'}."""
    if final_phash is None or not og_index:
        return None, None, "orphan"
    dists = [(final_phash - oh, e) for oh, e in og_index]
    dists.sort(key=lambda x: x[0])
    best = dists[0][0]
    if best > PHASH_MAX_HAMMING:
        return None, best, "orphan"
    tied = [e for d, e in dists if d == best]
    if len(tied) > 1:
        return tied[0], best, "ambiguous"
    return tied[0], best, "matched"


def cmd_reconcile(phoso_dir, test_mode):
    log.info("=" * 65)
    log.info("phoso_bridge --reconcile")
    log.info(f"  Phoso output: {phoso_dir}")
    log.info(f"  Mode:         {'TEST (no og-join)' if test_mode else 'full'}")

    og_entries, og_index = (None, None) if test_mode else _load_og_index()
    if not test_mode and not og_index:
        log.error(f"  Map 1 not found ({config.MEDIA_OG_MANIFEST}). Run --og-manifest "
                  f"first, or use --test to preview naming without the join.")
        sys.exit(1)

    rename_map = []
    report_rows = []
    used_names = set()
    counts = {"matched": 0, "ambiguous": 0, "orphan": 0,
              "florence": 0, "template": 0, "none": 0, "total": 0}

    for path, category in iter_final_images(phoso_dir):
        counts["total"] += 1
        caption = _read_caption(path)
        csource = _caption_source(caption)
        counts[csource] += 1

        # Name: caption slug when we have a real caption; else category fallback.
        if csource == "florence" and seo_slug(caption):
            stem = seo_slug(caption)
        else:
            stem = category_slug(category)          # template/none → reliable fallback
        new_name = _dedupe(stem, path.suffix.lower(), used_names)

        alt_text = caption[:125] if csource == "florence" else ""

        rec = {
            "final_local_path": str(path),
            "final_category":   category,
            "caption":          caption,
            "caption_source":   csource,
            "ocr":              "",
            "new_name":         new_name,
            "alt_text":         alt_text,
            "description":      caption if csource == "florence" else "",
            "phash":            str(_phash(path) or ""),
            # og fields (filled by join)
            "og_url": "", "og_name": "", "og_library_path": "",
            "join_status": "n/a" if test_mode else "", "join_distance": "",
            "wp_url": "", "wp_id": "",
        }

        if not test_mode:
            fph = None
            try:
                import imagehash
                fph = imagehash.hex_to_hash(rec["phash"]) if rec["phash"] else None
            except Exception:
                fph = None
            entry, dist, status = _join(fph, og_index)
            rec["join_status"] = status
            rec["join_distance"] = "" if dist is None else int(dist)
            counts[status] = counts.get(status, 0) + 1
            if entry:
                rec["og_url"] = entry.get("og_url", "")
                rec["og_name"] = entry.get("og_name", "")
                rec["og_library_path"] = entry.get("og_library_path", "")

        rename_map.append(rec)
        report_rows.append({
            "final_local_path": rec["final_local_path"],
            "final_category":   rec["final_category"],
            "new_name":         rec["new_name"],
            "caption_source":   rec["caption_source"],
            "caption":          rec["caption"][:120],
            "join_status":      rec["join_status"],
            "join_distance":    rec["join_distance"],
            "og_url":           rec["og_url"],
        })

    # ── Cull check: og entries with no final match (only in full mode) ──────
    culled = []
    if not test_mode and og_entries:
        matched_urls = {r["og_url"] for r in rename_map if r["og_url"]}
        culled = [e for e in og_entries if e.get("og_url") not in matched_urls]

    # ── Write outputs ───────────────────────────────────────────────────────
    os.makedirs(os.path.dirname(config.MEDIA_RENAME_MAP), exist_ok=True)
    with open(config.MEDIA_RENAME_MAP, "w", encoding="utf-8") as f:
        json.dump(rename_map, f, indent=2, ensure_ascii=False)

    os.makedirs(os.path.dirname(config.MEDIA_RECONCILE_CSV), exist_ok=True)
    with open(config.MEDIA_RECONCILE_CSV, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["final_local_path", "final_category",
                            "new_name", "caption_source", "caption",
                            "join_status", "join_distance", "og_url"])
        w.writeheader()
        w.writerows(report_rows)

    # ── Summary ─────────────────────────────────────────────────────────────
    log.info(f"\n  Images:        {counts['total']}")
    log.info(f"  Caption source: florence={counts['florence']}  "
             f"template={counts['template']}  none={counts['none']}")
    if not test_mode:
        log.info(f"  Join:          matched={counts.get('matched',0)}  "
                 f"ambiguous={counts.get('ambiguous',0)}  orphan={counts.get('orphan',0)}")
        log.info(f"  Culled (no final file): {len(culled)}")
    log.info(f"\n  Rename map → {config.MEDIA_RENAME_MAP}")
    log.info(f"  Report     → {config.MEDIA_RECONCILE_CSV}")
    log.info("\n  Review the report, edit new_name where needed, then: "
             "python phoso_bridge.py --upload")
    log.info("=" * 65)


# ══════════════════════════════════════════════════════════════════════════════
#  MODE: --report
# ══════════════════════════════════════════════════════════════════════════════

def cmd_report():
    if not os.path.exists(config.MEDIA_RENAME_MAP):
        log.error(f"No rename map yet ({config.MEDIA_RENAME_MAP}). Run --reconcile first.")
        sys.exit(1)
    with open(config.MEDIA_RENAME_MAP, encoding="utf-8") as f:
        rows = json.load(f)
    n = len(rows)
    flor = sum(1 for r in rows if r["caption_source"] == "florence")
    tmpl = sum(1 for r in rows if r["caption_source"] == "template")
    none = sum(1 for r in rows if r["caption_source"] == "none")
    orph = sum(1 for r in rows if r.get("join_status") == "orphan")
    ambi = sum(1 for r in rows if r.get("join_status") == "ambiguous")
    uploaded = sum(1 for r in rows if r.get("wp_url"))
    log.info("=" * 65)
    log.info(f"  Rename map: {n} images")
    log.info(f"  Caption:  florence={flor}  template={tmpl}  none={none}")
    log.info(f"  Join:     orphan={orph}  ambiguous={ambi}")
    log.info(f"  Uploaded: {uploaded}/{n}")
    log.info(f"\n  Rows needing a look (template/none caption or bad join):")
    flagged = [r for r in rows if r["caption_source"] != "florence"
               or r.get("join_status") in ("orphan", "ambiguous")]
    for r in flagged[:40]:
        log.info(f"    [{r['caption_source']:<8} {r.get('join_status','n/a'):<9}] "
                 f"{r['new_name']:<40} {r['caption'][:45]}")
    if len(flagged) > 40:
        log.info(f"    … and {len(flagged) - 40} more (see {config.MEDIA_RECONCILE_CSV})")
    log.info("=" * 65)


# ══════════════════════════════════════════════════════════════════════════════
#  MODE: --upload
# ══════════════════════════════════════════════════════════════════════════════

def cmd_upload(do_post):
    if not os.path.exists(config.MEDIA_RENAME_MAP):
        log.error(f"No rename map ({config.MEDIA_RENAME_MAP}). Run --reconcile first.")
        sys.exit(1)
    with open(config.MEDIA_RENAME_MAP, encoding="utf-8") as f:
        rows = json.load(f)

    log.info("=" * 65)
    log.info(f"phoso_bridge --upload  ({'POST' if do_post else 'dry-run'})")
    log.info(f"  Target: {config.NEW_SITE_URL}")

    import mimetypes
    import requests
    auth = (config.NEW_WP_USERNAME, config.NEW_WP_APP_PASSWORD)
    endpoint = f"{config.NEW_SITE_URL.rstrip('/')}/wp-json/wp/v2/media"

    media_map = {}
    if os.path.exists(config.MEDIA_MAP_FILE):
        with open(config.MEDIA_MAP_FILE, encoding="utf-8") as f:
            media_map = json.load(f)

    uploaded = skipped = failed = 0
    for r in rows:
        src = r["final_local_path"]
        if r.get("wp_url"):
            skipped += 1
            continue
        if not do_post:
            log.info(f"  would upload: {r['new_name']}  (alt: {r['alt_text'][:40]})")
            continue
        mime = mimetypes.guess_type(r["new_name"])[0] or "application/octet-stream"
        try:
            with open(src, "rb") as fh:
                resp = requests.post(
                    endpoint, auth=auth,
                    headers={"Content-Disposition": f'attachment; filename="{r["new_name"]}"',
                             "Content-Type": mime},
                    data=fh, timeout=60,
                )
            resp.raise_for_status()
            data = resp.json()
            new_url, new_id = data.get("source_url", ""), data.get("id", "")
            # attach meta (title/alt/caption/description)
            if new_id:
                requests.post(f"{endpoint}/{new_id}", auth=auth, timeout=30, json={
                    "title": r["new_name"], "alt_text": r["alt_text"],
                    "caption": r["description"], "description": r["description"],
                })
            r["wp_url"], r["wp_id"] = new_url, new_id
            if r.get("og_url"):
                media_map[r["og_url"]] = {"url": new_url, "id": new_id}
            uploaded += 1
        except Exception as e:
            log.warning(f"  upload failed for {r['new_name']}: {e}")
            failed += 1

    if do_post:
        with open(config.MEDIA_RENAME_MAP, "w", encoding="utf-8") as f:
            json.dump(rows, f, indent=2, ensure_ascii=False)
        os.makedirs(os.path.dirname(config.MEDIA_MAP_FILE), exist_ok=True)
        with open(config.MEDIA_MAP_FILE, "w", encoding="utf-8") as f:
            json.dump(media_map, f, indent=2, ensure_ascii=False)
        log.info(f"\n  Uploaded {uploaded}, skipped {skipped}, failed {failed}")
        log.info(f"  media_map → {config.MEDIA_MAP_FILE}  ({len(media_map)} entries)")
    else:
        log.info(f"\n  Dry-run: {len(rows)} images would upload. Re-run with --post.")
    log.info("=" * 65)


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    ap = argparse.ArgumentParser(description="Phoso ⇄ domain-parser media bridge")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--og-manifest", action="store_true", help="Build Map 1 from a download tree")
    mode.add_argument("--reconcile",   action="store_true", help="Join + rename + report from Phoso output")
    mode.add_argument("--report",      action="store_true", help="Read-only summary of the rename map")
    mode.add_argument("--upload",      action="store_true", help="Upload renamed media to the new site")
    ap.add_argument("--download-dir", default=None, help="(og-manifest) download tree root")
    ap.add_argument("--phoso-dir",    default=config.PHOSO_OUTPUT_DIR, help="Phoso output folder")
    ap.add_argument("--test",         action="store_true", help="(reconcile) skip og-join; preview naming only")
    ap.add_argument("--post",         action="store_true", help="(upload) actually POST (default dry-run)")
    args = ap.parse_args()

    if args.og_manifest:
        if not args.download_dir:
            ap.error("--og-manifest requires --download-dir")
        cmd_og_manifest(args.download_dir)
    elif args.reconcile:
        cmd_reconcile(args.phoso_dir, args.test)
    elif args.report:
        cmd_report()
    elif args.upload:
        cmd_upload(args.post)


if __name__ == "__main__":
    main()
