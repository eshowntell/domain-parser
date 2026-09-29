#!/usr/bin/env python3
"""
6d_pdf_pages.py  —  Phase 2: Convert linked PDFs into searchable WordPress pages.

For each PDF linked from the old site:
  1. Collects PDF URLs from the content inventory (02_content.json)
  2. Downloads PDFs to data/06d_pdfs/
  3. Extracts text via pdfplumber (falls back to OCR via pytesseract for scans)
  4. Extracts embedded images via PyMuPDF (fitz)
  5. Uploads the PDF + extracted images to the new WP media library
  6. Calls Claude to generate a Gutenberg page from the extracted content
  7. Appends a wp:file download block linking to the uploaded PDF
  8. Optionally POSTs the page to the new site as a draft

Produces a searchable page that AI bots can parse, with the original PDF
still available as a download.

Dependencies (pip install):
  pdfplumber PyMuPDF pytesseract pdf2image Pillow requests tqdm

System deps for OCR fallback:
  tesseract-ocr  (apt install / choco install / brew install)
  poppler-utils   (for pdf2image)

Usage
─────
  python 6d_pdf_pages.py                     # dry-run: extract + generate only
  python 6d_pdf_pages.py --post              # extract, generate, upload, post
  python 6d_pdf_pages.py --post --limit 5    # smoke test
  python 6d_pdf_pages.py --force             # redo already-processed PDFs
  python 6d_pdf_pages.py --url /path/to.pdf  # single PDF by URL path
  python 6d_pdf_pages.py --skip-upload       # generate pages but don't upload media
  python 6d_pdf_pages.py --collect-only      # just build the PDF list, download nothing

Outputs
───────
  data/06d_pdfs/<slug>.pdf              — downloaded PDF files
  data/06d_pdfs/<slug>/                 — extracted images per PDF
  data/06d_pdf_pages/<slug>.json        — one record per generated page
  data/06d_pdf_inventory.json           — master list of discovered PDF URLs
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, unquote

import requests
from tqdm import tqdm

sys.path.insert(0, os.path.dirname(__file__))
import config
from utils import setup_logging, scrape_get
from rewrite_utils import (
    call_anthropic, validate_content, slug_from_url,
    core_block_reference, BLOCK_TEMPLATES,
)

log = setup_logging("6d_pdf_pages")

# ── Paths ─────────────────────────────────────────────────────
PDF_DIR       = os.path.join(config.DATA_DIR, "06d_pdfs")
PAGES_DIR     = os.path.join(config.DATA_DIR, "06d_pdf_pages")
INVENTORY     = os.path.join(config.DATA_DIR, "06d_pdf_inventory.json")
PAGE_COUNTS   = os.path.join(config.DATA_DIR, "06d_pdf_page_counts.json")
MEDIA_MAP     = os.path.join(config.DATA_DIR, "06_media_map.json")

CONTENT_FILE  = os.path.join(config.DATA_DIR, "02_content.json")
MERGED_FILE   = os.path.join(config.DATA_DIR, "03_merged.json")
REWRITE_DIR   = config.PHASE2_REWRITE_DIR

# ── Limits ────────────────────────────────────────────────────
MAX_PDF_PAGES     = 50      # skip PDFs longer than this (likely not a brochure)
MAX_TEXT_WORDS     = 8000    # cap text sent to Claude
MIN_TEXT_WORDS     = 20      # below this → treat as scanned, try OCR
MAX_IMAGES_PER_PDF = 20      # cap extracted images
MIN_IMAGE_BYTES    = 5000    # skip tiny images (icons, spacers, masks)
OCR_DPI            = 200     # resolution for OCR rasterization


# ══════════════════════════════════════════════════════════════════════════════
#  SYSTEM PROMPT
# ══════════════════════════════════════════════════════════════════════════════

SYSTEM_PROMPT = f"""You are a content writer for Franke Tobey Jones (FTJ), a nonprofit \
senior living community at 5340 N Bristol St, Tacoma, WA 98407. You are helping migrate \
their website to a clean new WordPress build.

Your task: given text extracted from a PDF document, create a well-structured WordPress \
page that makes this content searchable, accessible, and readable on the web.

TONE
- Warm, clear, welcoming — audience is seniors and adult children making housing decisions
- Confident but not salesy; informative but not clinical
- Clean up OCR artifacts, fix spacing issues, but preserve all factual content exactly

FORMAT
Begin your response with exactly one slug line, then the Gutenberg HTML.
The slug line must be the very first line, in this exact format:
  SLUG: your-short-slug-here

Then output ONLY WordPress Gutenberg block HTML — no preamble, no explanation, no markdown fences.
Use these blocks:
{core_block_reference()}

RULES
- Do NOT include navigation, header, or footer content
- DO preserve all factual details exactly: addresses, phone numbers, names, dates, pricing
- Clean up OCR noise (broken words, stray characters, misread punctuation) but NEVER \
  change factual content — if you're unsure about a number or name, keep the original
- Structure: organize the content logically with H2 for the first section, H3 for \
  subsequent sections. If the PDF had clear sections, mirror that structure.
- If the source text is very short or mostly boilerplate, produce a concise page — \
  don't pad it with filler
- If image URLs are provided, include them as wp:image blocks where they fit the content flow
- A download link for the original PDF will be added automatically — you don't need to \
  include one
- SLUG rules: 3-6 words, lowercase, hyphenated, descriptive of the content topic
- Output the SLUG line first, then ONLY the Gutenberg block HTML"""


# ══════════════════════════════════════════════════════════════════════════════
#  COLLECT PDF URLS
# ══════════════════════════════════════════════════════════════════════════════

def collect_pdf_urls() -> list[dict]:
    """Scan the content inventory for internal links ending in .pdf"""
    # Load page inventory
    src = MERGED_FILE if os.path.exists(MERGED_FILE) else CONTENT_FILE
    if not os.path.exists(src):
        log.error(f"No content inventory found. Run 2_extract.py first.")
        sys.exit(1)

    with open(src, encoding="utf-8") as f:
        pages = json.load(f)

    pdf_urls: dict[str, dict] = {}   # url → {url, found_on, title_hint}

    for page in pages:
        page_url = page.get("url", "")
        for link in page.get("internal_links", []):
            if link.lower().endswith(".pdf"):
                norm = link.rstrip("/")
                if norm not in pdf_urls:
                    # Derive a title hint from the filename
                    filename = unquote(urlparse(norm).path.split("/")[-1])
                    title_hint = re.sub(r"\.pdf$", "", filename, flags=re.IGNORECASE)
                    title_hint = re.sub(r"[-_]+", " ", title_hint).strip().title()
                    pdf_urls[norm] = {
                        "url": norm,
                        "found_on": [page_url],
                        "title_hint": title_hint,
                        "filename": filename,
                    }
                else:
                    pdf_urls[norm]["found_on"].append(page_url)

        # Also check external_links in case PDFs are on a CDN or subdomain
        for link in page.get("external_links", []):
            if link.lower().endswith(".pdf") and "franketobeyjones" in link.lower():
                norm = link.rstrip("/")
                if norm not in pdf_urls:
                    filename = unquote(urlparse(norm).path.split("/")[-1])
                    title_hint = re.sub(r"\.pdf$", "", filename, flags=re.IGNORECASE)
                    title_hint = re.sub(r"[-_]+", " ", title_hint).strip().title()
                    pdf_urls[norm] = {
                        "url": norm,
                        "found_on": [page_url],
                        "title_hint": title_hint,
                        "filename": filename,
                    }

    # Also scan rewrite JSONs for PDF links in image_urls/video_urls that got miscategorized
    if os.path.isdir(REWRITE_DIR):
        for fn in os.listdir(REWRITE_DIR):
            if not fn.endswith(".json"):
                continue
            try:
                with open(os.path.join(REWRITE_DIR, fn), encoding="utf-8") as f:
                    rec = json.load(f)
                for field in ("image_urls", "video_urls"):
                    for link in rec.get(field, []):
                        if link.lower().endswith(".pdf"):
                            norm = link.rstrip("/")
                            if norm not in pdf_urls:
                                filename = unquote(urlparse(norm).path.split("/")[-1])
                                title_hint = re.sub(r"\.pdf$", "", filename, flags=re.IGNORECASE)
                                title_hint = re.sub(r"[-_]+", " ", title_hint).strip().title()
                                pdf_urls[norm] = {
                                    "url": norm,
                                    "found_on": [rec.get("url", fn)],
                                    "title_hint": title_hint,
                                    "filename": filename,
                                }
            except Exception:
                continue

    results = sorted(pdf_urls.values(), key=lambda x: x["url"])
    log.info(f"  Found {len(results)} unique PDF URLs across {len(pages)} pages")
    return results


# ══════════════════════════════════════════════════════════════════════════════
#  DOWNLOAD
# ══════════════════════════════════════════════════════════════════════════════

def pdf_slug(url: str) -> str:
    """Derive a filesystem-safe slug from a PDF URL."""
    filename = unquote(urlparse(url).path.split("/")[-1])
    name = re.sub(r"\.pdf$", "", filename, flags=re.IGNORECASE)
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "document"


def download_pdf(url: str, dest_path: str) -> bool:
    """Download a PDF file. Returns True on success."""
    try:
        resp = scrape_get(url)
        if resp and resp.status_code == 200:
            ct = resp.headers.get("content-type", "").lower()
            if "pdf" in ct or url.lower().endswith(".pdf"):
                with open(dest_path, "wb") as f:
                    f.write(resp.content)
                log.info(f"    ↓ {os.path.basename(dest_path)}  "
                         f"({len(resp.content) / 1024:.0f} KB)")
                return True
            else:
                log.warning(f"    Not a PDF (content-type: {ct}): {url}")
        else:
            status = resp.status_code if resp else "no response"
            log.warning(f"    Download failed ({status}): {url}")
    except Exception as e:
        log.warning(f"    Download error: {url} — {e}")
    return False


# ══════════════════════════════════════════════════════════════════════════════
#  EXTRACT TEXT + IMAGES
# ══════════════════════════════════════════════════════════════════════════════

def extract_pdf(pdf_path: str, images_dir: str) -> dict:
    """Extract text and images from a PDF file.

    Returns dict with:
        text:          extracted body text
        page_count:    number of pages
        image_paths:   list of extracted image file paths
        method:        'pdfplumber' | 'ocr' | 'empty'
        ocr_used:      bool
    """
    result = {
        "text": "",
        "page_count": 0,
        "image_paths": [],
        "method": "empty",
        "ocr_used": False,
    }

    # ── Text extraction via pdfplumber ────────────────────────
    try:
        import pdfplumber
    except ImportError:
        log.error("  pdfplumber not installed. Run: pip install pdfplumber")
        return result

    try:
        with pdfplumber.open(pdf_path) as pdf:
            result["page_count"] = len(pdf.pages)

            if len(pdf.pages) > MAX_PDF_PAGES:
                log.warning(f"    {len(pdf.pages)} pages — exceeds limit of {MAX_PDF_PAGES}, skipping")
                result["method"] = "too_long"
                return result

            text_parts = []
            for i, page in enumerate(pdf.pages):
                try:
                    page_text = page.extract_text() or ""
                    text_parts.append(page_text)
                except Exception as e:
                    log.warning(f"    Page {i+1} text extraction failed: {e}")
                    text_parts.append("")

            result["text"] = "\n\n".join(text_parts).strip()
    except Exception as e:
        log.warning(f"    pdfplumber failed: {e}")

    # ── OCR fallback for scanned PDFs ─────────────────────────
    word_count = len(result["text"].split())
    if word_count < MIN_TEXT_WORDS:
        log.info(f"    Only {word_count} words extracted — trying OCR fallback")
        try:
            from pdf2image import convert_from_path
            import pytesseract

            images = convert_from_path(pdf_path, dpi=OCR_DPI,
                                       first_page=1,
                                       last_page=min(MAX_PDF_PAGES, result["page_count"] or 99))
            ocr_parts = []
            for i, img in enumerate(images):
                try:
                    ocr_text = pytesseract.image_to_string(img)
                    ocr_parts.append(ocr_text)
                except Exception as e:
                    log.warning(f"    OCR page {i+1} failed: {e}")

            ocr_text = "\n\n".join(ocr_parts).strip()
            if len(ocr_text.split()) > word_count:
                result["text"] = ocr_text
                result["ocr_used"] = True
                result["method"] = "ocr"
                log.info(f"    OCR recovered {len(ocr_text.split())} words")
            else:
                log.info(f"    OCR produced {len(ocr_text.split())} words (not better)")

        except ImportError:
            log.warning("    pytesseract/pdf2image not installed — OCR skipped")
        except Exception as e:
            log.warning(f"    OCR failed: {e}")

    if result["method"] == "empty" and len(result["text"].split()) >= MIN_TEXT_WORDS:
        result["method"] = "pdfplumber"

    # ── Image extraction via PyMuPDF ──────────────────────────
    try:
        import fitz  # PyMuPDF
    except ImportError:
        log.warning("    PyMuPDF (fitz) not installed — image extraction skipped")
        return result

    try:
        os.makedirs(images_dir, exist_ok=True)
        doc = fitz.open(pdf_path)
        image_count = 0

        for page_idx in range(min(len(doc), MAX_PDF_PAGES)):
            page = doc[page_idx]
            for img_info in page.get_images():
                if image_count >= MAX_IMAGES_PER_PDF:
                    break

                xref = img_info[0]
                try:
                    pix = fitz.Pixmap(doc, xref)

                    # Skip tiny images (icons, masks, spacers)
                    if pix.width < 80 or pix.height < 80:
                        continue

                    # Convert CMYK → RGB
                    if pix.n - pix.alpha > 3:
                        pix = fitz.Pixmap(fitz.csRGB, pix)

                    img_path = os.path.join(images_dir, f"img_{page_idx+1}_{xref}.png")
                    pix.save(img_path)

                    # Skip if file is too small (likely decorative)
                    if os.path.getsize(img_path) < MIN_IMAGE_BYTES:
                        os.remove(img_path)
                        continue

                    result["image_paths"].append(img_path)
                    image_count += 1
                except Exception as e:
                    log.warning(f"    Image xref={xref} extraction failed: {e}")

        doc.close()
        if result["image_paths"]:
            log.info(f"    Extracted {len(result['image_paths'])} images")
    except Exception as e:
        log.warning(f"    Image extraction failed: {e}")

    return result


# ══════════════════════════════════════════════════════════════════════════════
#  UPLOAD TO WP MEDIA LIBRARY
# ══════════════════════════════════════════════════════════════════════════════

AUTH     = (config.NEW_WP_USERNAME, config.NEW_WP_APP_PASSWORD)
MEDIA_EP = f"{config.NEW_SITE_URL.rstrip('/')}/wp-json/wp/v2/media"


def upload_file(filepath: str, title: str = "") -> dict | None:
    """Upload a file to the WP media library. Returns {id, url} or None."""
    import mimetypes
    filename = os.path.basename(filepath)
    mime, _ = mimetypes.guess_type(filepath)
    mime = mime or "application/octet-stream"

    try:
        with open(filepath, "rb") as f:
            resp = requests.post(
                MEDIA_EP,
                files={"file": (filename, f, mime)},
                data={"title": title or filename, "status": "inherit"},
                auth=AUTH,
                timeout=60,
            )
        resp.raise_for_status()
        data = resp.json()
        result = {
            "id":  data.get("id"),
            "url": data.get("source_url", ""),
        }
        log.info(f"    ↑ {filename}  →  ID {result['id']}")
        return result
    except requests.exceptions.HTTPError as e:
        log.error(f"    Upload failed: {e} — {getattr(resp, 'text', '')[:200]}")
    except Exception as e:
        log.error(f"    Upload error for {filename}: {e}")
    return None


# ══════════════════════════════════════════════════════════════════════════════
#  BUILD PROMPT + GENERATE PAGE
# ══════════════════════════════════════════════════════════════════════════════

def build_prompt(entry: dict, extracted: dict, image_entries: list[dict]) -> str:
    """Build the user prompt for Claude from extracted PDF content."""
    title = entry.get("title_hint", "Document")
    url   = entry.get("url", "")

    text = extracted["text"]
    words = text.split()
    if len(words) > MAX_TEXT_WORDS:
        text = " ".join(words[:MAX_TEXT_WORDS]) + " […]"

    lines = [
        f"ORIGINAL PDF URL:    {url}",
        f"SUGGESTED TITLE:     {title}",
        f"PAGES IN PDF:        {extracted['page_count']}",
        f"EXTRACTION METHOD:   {extracted['method']}",
        f"WORD COUNT:          {len(words)}",
    ]

    if extracted["ocr_used"]:
        lines.append("\nNOTE: This text was extracted via OCR and may contain artifacts — "
                      "clean up broken words and stray characters but preserve all facts.")

    if image_entries:
        lines.append(f"\nIMAGE URLS — include the most relevant as wp:image blocks:")
        for img in image_entries[:10]:
            img_id = img.get("id", "")
            img_url = img.get("url", "")
            if img_id:
                lines.append(f"  URL: {img_url}  ID: {img_id}")
            else:
                lines.append(f"  URL: {img_url}  (no ID — omit id attribute)")

    lines.append(f"\nEXTRACTED TEXT ({len(text.split())} words):\n{text}")
    lines.append("\nRemember: first line must be SLUG: your-slug-here, then the Gutenberg HTML.")
    return "\n".join(lines)


def append_download_block(content: str, pdf_entry: dict) -> str:
    """Append a wp:file download block at the end of the generated content."""
    pdf_id  = pdf_entry.get("id", 0)
    pdf_url = pdf_entry.get("url", "")
    title   = pdf_entry.get("title", "this document")

    if pdf_id and pdf_url:
        block = (
            f'\n\n<!-- wp:file {{"id":{pdf_id},"href":"{pdf_url}","displayPreview":false}} -->\n'
            f'<div class="wp-block-file">'
            f'<a href="{pdf_url}">Download the original PDF: {title}</a>'
            f'</div>\n'
            f'<!-- /wp:file -->'
        )
    elif pdf_url:
        block = (
            f'\n\n<!-- wp:file {{"href":"{pdf_url}","displayPreview":false}} -->\n'
            f'<div class="wp-block-file">'
            f'<a href="{pdf_url}">Download the original PDF: {title}</a>'
            f'</div>\n'
            f'<!-- /wp:file -->'
        )
    else:
        return content   # no PDF uploaded, skip the block

    return content + block


# ══════════════════════════════════════════════════════════════════════════════
#  POST TO WP
# ══════════════════════════════════════════════════════════════════════════════

def post_page(title: str, slug: str, content: str, parent_id: int = 0) -> int | None:
    """POST a page to WordPress. Returns post_id or None."""
    ok, reason = validate_content(content)
    if not ok:
        log.error(f"    Refusing to post — {reason}")
        return None

    endpoint = f"{config.NEW_SITE_URL.rstrip('/')}/wp-json/wp/v2/pages"
    payload = {
        "title":   title,
        "content": content,
        "status":  config.PHASE2_POST_STATUS,
        "slug":    slug,
    }
    if parent_id:
        payload["parent"] = parent_id

    try:
        resp = requests.post(endpoint, json=payload, auth=AUTH, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        post_id = data.get("id")
        link = data.get("link", f"/{slug}")
        log.info(f"    → Posted /{slug}  (ID {post_id})  {link}")
        return post_id
    except requests.exceptions.HTTPError as e:
        log.error(f"    WP POST failed: {e} — {getattr(resp, 'text', '')[:300]}")
    except Exception as e:
        log.error(f"    WP POST error: {e}")
    return None


# ══════════════════════════════════════════════════════════════════════════════
#  SAVE / LOAD RESULTS
# ══════════════════════════════════════════════════════════════════════════════

def result_path(slug: str) -> str:
    return os.path.join(PAGES_DIR, slug + ".json")


def save_result(result: dict) -> str:
    os.makedirs(PAGES_DIR, exist_ok=True)
    path = result_path(result["slug"])
    with open(path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    return path


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    ap = argparse.ArgumentParser(description="Phase 2: Convert PDFs to searchable WP pages")
    ap.add_argument("--post",          action="store_true",
                     help="Upload media + POST pages to WP (default: dry-run)")
    ap.add_argument("--limit",         type=int, default=0,
                     help="Max PDFs to process this run (0 = no limit)")
    ap.add_argument("--force",         action="store_true",
                     help="Reprocess PDFs that already have a result JSON")
    ap.add_argument("--url",           type=str, default=None,
                     help="Process a single PDF by URL path")
    ap.add_argument("--skip-upload",   action="store_true",
                     help="Generate pages but don't upload media to WP")
    ap.add_argument("--collect-only",  action="store_true",
                     help="Just discover PDF URLs and write inventory — process nothing")
    args = ap.parse_args()

    dry_run = not args.post

    log.info("=" * 65)
    log.info("6d_pdf_pages — Convert linked PDFs to searchable WordPress pages")
    log.info("=" * 65)
    log.info(f"  New site:  {config.NEW_SITE_URL}")
    log.info(f"  Dry run:   {dry_run}")
    log.info(f"  Limit:     {args.limit or 'none'}")

    # ── Collect PDF URLs ──────────────────────────────────────
    log.info("\nScanning content inventory for PDF links …")
    pdf_entries = collect_pdf_urls()

    # Save inventory
    os.makedirs(config.DATA_DIR, exist_ok=True)
    with open(INVENTORY, "w", encoding="utf-8") as f:
        json.dump(pdf_entries, f, indent=2, ensure_ascii=False)
    log.info(f"  Inventory → {INVENTORY}")

    if args.collect_only:
        log.info("\n  --collect-only: stopping after inventory.")
        log.info(f"  {len(pdf_entries)} PDFs found. Review {INVENTORY} then rerun without --collect-only.")
        sys.exit(0)

    if not pdf_entries:
        log.info("  No PDF links found in content inventory.")
        sys.exit(0)

    # ── Filter ────────────────────────────────────────────────
    todo = pdf_entries

    if args.url:
        target = args.url.rstrip("/")
        todo = [e for e in todo if urlparse(e["url"]).path.rstrip("/") == target
                or e["url"].rstrip("/") == target]
        if not todo:
            log.error(f"No PDF found matching '{args.url}'")
            sys.exit(1)

    # Skip already-done unless --force
    os.makedirs(PAGES_DIR, exist_ok=True)
    if not args.force:
        before = len(todo)
        todo = [e for e in todo
                if not os.path.exists(result_path(pdf_slug(e["url"])))]
        skipped = before - len(todo)
        if skipped:
            log.info(f"  Skipping {skipped} already-processed PDFs (--force to redo)")

    if args.limit and args.limit > 0:
        todo = todo[:args.limit]

    if not todo:
        log.info("Nothing to process.")
        sys.exit(0)

    log.info(f"\nProcessing {len(todo)} PDFs …\n")
    os.makedirs(PDF_DIR, exist_ok=True)

    # ── Load media map (for updating with new uploads) ────────
    media_map = {}
    if os.path.exists(MEDIA_MAP):
        with open(MEDIA_MAP, encoding="utf-8") as f:
            media_map = json.load(f)

    # ── Load page-count log (accumulates across runs) ──────────
    page_counts = {}
    if os.path.exists(PAGE_COUNTS):
        with open(PAGE_COUNTS, encoding="utf-8") as f:
            page_counts = json.load(f)

    ok = posted = failed = skipped_extract = 0

    for entry in tqdm(todo, desc="PDFs", unit="pdf"):
        url   = entry["url"]
        slug  = pdf_slug(url)
        title = entry.get("title_hint", slug.replace("-", " ").title())

        log.info(f"\n{'─'*55}")
        log.info(f"  {title}")
        log.info(f"  {url}")

        # 1. Download
        local_pdf = os.path.join(PDF_DIR, slug + ".pdf")
        if not os.path.exists(local_pdf):
            if not download_pdf(url, local_pdf):
                failed += 1
                continue
        else:
            log.info(f"    Already downloaded: {local_pdf}")

        # 2. Extract text + images
        images_dir = os.path.join(PDF_DIR, slug)
        extracted = extract_pdf(local_pdf, images_dir)

        if extracted["page_count"]:
            log.info(f"    {extracted['page_count']} pages")
        page_counts[slug] = {
            "url":         url,
            "title":       title,
            "page_count":  extracted["page_count"],
            "method":      extracted["method"],
        }

        if extracted["method"] in ("empty", "too_long"):
            log.warning(f"    No usable text extracted — skipping")
            skipped_extract += 1
            continue

        log.info(f"    {len(extracted['text'].split())} words via {extracted['method']}, "
                 f"{len(extracted['image_paths'])} images")

        # 3. Upload PDF + images to WP media library
        pdf_media = {}     # {id, url} for the PDF itself
        image_entries = [] # [{id, url}, ...] for extracted images

        if not dry_run and not args.skip_upload:
            # Upload the PDF
            pdf_media = upload_file(local_pdf, title) or {}
            if pdf_media:
                media_map[url] = pdf_media
                time.sleep(0.2)

            # Upload extracted images
            for img_path in extracted["image_paths"]:
                img_name = Path(img_path).stem.replace("_", " ").title()
                img_result = upload_file(img_path, f"{title} — {img_name}")
                if img_result:
                    image_entries.append(img_result)
                    time.sleep(0.2)
        elif extracted["image_paths"]:
            # Dry run: use local paths as placeholders
            for img_path in extracted["image_paths"]:
                image_entries.append({"id": "", "url": img_path})

        # 4. Generate page via Claude
        prompt = build_prompt(entry, extracted, image_entries)
        log.info(f"  Calling {config.ANTHROPIC_MODEL} …")
        rewritten, suggested_slug = call_anthropic(SYSTEM_PROMPT, prompt)

        if not rewritten:
            log.error("  ✗ API returned nothing — skipping")
            failed += 1
            continue

        # Resolve slug
        final_slug = suggested_slug or slug
        final_slug = re.sub(r"^blog-", "", final_slug.strip("/"))

        # Append download block
        rewritten = append_download_block(rewritten, {
            "id":    pdf_media.get("id", 0),
            "url":   pdf_media.get("url", url),
            "title": title,
        })

        log.info(f"  → /{final_slug}  ({len(rewritten.split())} words)")

        # 5. Build result record
        result = {
            "slug":              final_slug,
            "title":             title,
            "source_pdf_url":    url,
            "found_on":          entry.get("found_on", []),
            "rewritten_content": rewritten,
            "page_count":        extracted["page_count"],
            "extraction_method": extracted["method"],
            "ocr_used":          extracted["ocr_used"],
            "word_count_in":     len(extracted["text"].split()),
            "word_count_out":    len(rewritten.split()),
            "images_extracted":  len(extracted["image_paths"]),
            "images_uploaded":   len([e for e in image_entries if e.get("id")]),
            "pdf_wp_id":         pdf_media.get("id"),
            "pdf_wp_url":        pdf_media.get("url", ""),
            "post_id":           None,
            "posted_at":         None,
            "model":             config.ANTHROPIC_MODEL,
            "timestamp":         datetime.utcnow().isoformat() + "Z",
        }

        saved = save_result(result)
        log.info(f"  Saved → {saved}")
        ok += 1

        # 6. POST to WP
        if not dry_run:
            post_id = post_page(title, final_slug, rewritten)
            if post_id:
                result["post_id"]   = post_id
                result["posted_at"] = datetime.utcnow().isoformat() + "Z"
                save_result(result)
                posted += 1

        time.sleep(0.5)   # rate limit

    # ── Save updated media map ────────────────────────────────
    if media_map and not dry_run and not args.skip_upload:
        with open(MEDIA_MAP, "w", encoding="utf-8") as f:
            json.dump(media_map, f, indent=2, ensure_ascii=False)
        log.info(f"\n  Media map updated → {MEDIA_MAP}")

    # ── Save page-count log ─────────────────────────────────────
    with open(PAGE_COUNTS, "w", encoding="utf-8") as f:
        json.dump(page_counts, f, indent=2, ensure_ascii=False)

    total_pdfs_counted = len(page_counts)
    total_pages = sum(v["page_count"] for v in page_counts.values())

    # ── Summary ───────────────────────────────────────────────
    log.info(f"\n{'='*65}")
    log.info("PDF page generation complete")
    log.info(f"  Generated:  {ok}")
    log.info(f"  Skipped:    {skipped_extract}  (no extractable text)")
    log.info(f"  Failed:     {failed}")
    if not dry_run:
        log.info(f"  Posted:     {posted}")
    log.info(f"\n  PDFs with page counts:  {total_pdfs_counted}")
    log.info(f"  Total pages:            {total_pages}")
    log.info(f"  Page counts → {PAGE_COUNTS}")
    if dry_run:
        log.info(f"\n  Dry-run — nothing posted or uploaded.")
        log.info(f"  Review {PAGES_DIR}/ then rerun with --post.")
    log.info("=" * 65)


if __name__ == "__main__":
    main()
