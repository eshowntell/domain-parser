#!/usr/bin/env python3
"""
6c_prep_pages.py  —  Phase 2: Page decision engine.

Reads url_content_to_decide.csv and urls_to_combine_and_or_migrate.csv,
detects page type, assigns layout hints + CSS class hints from the FTJ theme,
generates clean slugs via the Anthropic API, and outputs a decisions JSON
that feeds 6_rewrite_pages.py.

PAGE TYPE DETECTION
───────────────────
Detected from URL path + title + headings. Maps to:
  hero            →  homepage-adjacent landing pages
  living-option   →  independent/assisted/memory/skilled pages
  team            →  staff bios, management, team pages
  faq             →  FAQ, frequently asked questions
  quiz            →  interactive quiz pages  (placeholder content generated)
  pricing         →  pricing, brochures, budget pages
  list            →  amenities, services, features list pages
  contact         →  contact, request info, tour pages
  community       →  neighborhood, parks, local area pages
  blog-home       →  blog index page (skip — handled by WP)
  generic         →  everything else

SKIP LIST
─────────
  bold-timeline entries
  homepage (handled by template)
  blog-home

Outputs
───────
  data/06c_decisions_pages.json   →  input for 6_rewrite_pages.py
  data/06c_skip_pages.json        →  skipped URLs + reasons

Usage
─────
  python 6c_prep_pages.py                 # full run with slug generation
  python 6c_prep_pages.py --no-slugs      # skip API slug generation
  python 6c_prep_pages.py --dry-run       # print decisions, write no files
  python 6c_prep_pages.py --min-score 40  # only include pages scoring above this
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
from rewrite_utils import llm_complete
from utils import setup_logging

log = setup_logging("6c_prep_pages")

# ── Paths ──────────────────────────────────────────────────────────────────────
PAGES_META_CSV    = "urls_to_combine_and_or_migrate.csv"
PAGES_CONTENT_CSV = "url_content_to_decide.csv"

OUT_DECISIONS = os.path.join(config.DATA_DIR, "06c_decisions_pages.json")
OUT_SKIP      = os.path.join(config.DATA_DIR, "06c_skip_pages.json")

THEME_FILES = {
    "additional_css": os.path.join(config.DATA_DIR, "additional_css.txt"),
    "theme_json":     os.path.join(config.DATA_DIR, "theme.json"),
    "style_css":      os.path.join(config.DATA_DIR, "style.css"),
    "functions_php":  os.path.join(config.DATA_DIR, "functions.php"),
}

MIN_SCORE_DEFAULT = 0   # include all by default


# ══════════════════════════════════════════════════════════════════════════════
#  COLUMN MAPS
# ══════════════════════════════════════════════════════════════════════════════

# urls_to_combine_and_or_migrate.csv  (has header row)
META_COLS = {
    "url":      "URL",
    "title":    "Title",
    "h1":       "H1 (Best)",
    "type":     "Type",
    "status":   "Status",
    "grade":    "Grade",
    "score":    "Score",
    "modified": "Date Modified",
    "words":    "Word Count",
    "images":   "Image Count",
    "sessions": None,   # detected dynamically (contains date range)
    "engaged":  "Engaged Sessions",
    "avg_time": "Avg Eng Time (s)",
    "slug":     "Slug",
    "template": "Template",
}

# url_content_to_decide.csv  (no header row)
CONTENT_COL = {
    "url":       0,
    "title":     1,
    "h1":        2,
    "status":    3,
    "words":     4,
    "headings":  5,
    "img_urls":  6,
    "video_urls":7,
    "body_text": 8,
}


# ══════════════════════════════════════════════════════════════════════════════
#  PAGE TYPE DETECTION
# ══════════════════════════════════════════════════════════════════════════════

# (pattern, page_type) — checked in order against lowercased URL path
URL_TYPE_PATTERNS = [
    (r"^/?$",                          "skip-homepage"),
    (r"bold-timeline",                 "skip-timeline"),
    (r"/blog/?$",                      "skip-blog-home"),
    (r"is-it-dementia",                "quiz"),
    (r"is-it-still-safe",              "quiz"),
    (r"is-it-time-to-get-help",        "quiz"),
    (r"is-it-time-to-downsize",        "quiz"),
    (r"is-it-the-right-time",          "quiz"),
    (r"retirement-living.*quiz",       "quiz"),
    (r"living-options/memory-care",    "living-option"),
    (r"living-options/assisted",       "living-option"),
    (r"living-options/independent",    "living-option"),
    (r"living-options/skilled",        "living-option"),
    (r"living-options",                "living-option"),
    (r"our-team|management|board|ambassadors|team-member", "team"),
    (r"join-our-team",                 "team"),
    (r"faq|frequently-asked",          "faq"),
    (r"pricing|brochures|budget",      "pricing"),
    (r"contact|request-info|tour",     "contact"),
    (r"the-neighborhood|parks|ruston|tacoma|downtown|community", "community"),
    (r"give-volunteer|philanthropy|donate|ways-to-give", "list"),
    (r"amenities|services|wellness|fitness|dining|lifestyle", "list"),
    (r"availability",                  "list"),
    (r"about-us|our-story|history|promise|timeline", "hero"),
    (r"senior-center",                 "list"),
]

URL_TYPE_RE = [(re.compile(p, re.IGNORECASE), t) for p, t in URL_TYPE_PATTERNS]


def detect_page_type(url: str, title: str = "", headings: str = "") -> str:
    path = urlparse(url).path
    for pattern, ptype in URL_TYPE_RE:
        if pattern.search(path):
            return ptype
    # Fallback: title hints
    t = title.lower()
    if any(k in t for k in ["team", "staff", "director", "president", "ceo"]):
        return "team"
    if any(k in t for k in ["faq", "question"]):
        return "faq"
    if any(k in t for k in ["contact", "tour", "schedule"]):
        return "contact"
    if any(k in t for k in ["pricing", "cost", "afford"]):
        return "pricing"
    return "generic"


# ══════════════════════════════════════════════════════════════════════════════
#  LAYOUT HINTS & CSS CLASS HINTS
# ══════════════════════════════════════════════════════════════════════════════

# Maps page_type → (layout_hint, css_classes_hint, block_hints, notes)
PAGE_TYPE_CONFIG = {
    "hero": {
        "layout_hint": "hero-page",
        "css_classes": ["ftj-cta-banner", "ftj-heading-accent", "ftj-bg-tan"],
        "extra_blocks": ["wp:cover", "wp:buttons"],
        "notes": "Opening paragraph with mission/warmth. Key highlights as short list. Closing CTA button linking to /contact.",
    },
    "living-option": {
        "layout_hint": "location-page",
        "css_classes": ["ftj-content-sidebar", "ftj-section-guide", "ftj-cta-banner", "ftj-img-text"],
        "extra_blocks": ["wp:columns", "wp:table", "wp:buttons"],
        "notes": "Intro paragraph. H2 sections for key features/amenities. If pricing is mentioned preserve it exactly. Closing CTA. Use wp:columns for any side-by-side image+text layouts.",
    },
    "team": {
        "layout_hint": "team-page",
        "css_classes": ["ftj-bio", "ftj-bio-photo", "ftj-bio-name", "ftj-bio-title", "ftj-bio-body"],
        "extra_blocks": ["wp:columns"],
        "notes": (
            "For each person: wrap in a div with class 'ftj-bio'. "
            "Inside: div.ftj-bio-photo for the image, div.ftj-bio-text containing "
            "p.ftj-bio-name, p.ftj-bio-title, and div.ftj-bio-body for the bio paragraph. "
            "Use wp:html blocks for the ftj-bio divs since these use custom HTML structure."
        ),
    },
    "faq": {
        "layout_hint": "list-page",
        "css_classes": ["ftj-section-heading", "ftj-bg-tan"],
        "extra_blocks": ["wp:details"],
        "notes": "Use wp:details blocks for each Q&A pair. Group related questions under H2 section headers.",
    },
    "quiz": {
        "layout_hint": "quiz-placeholder",
        "css_classes": ["ftj-cta-banner", "ftj-bg-tan"],
        "extra_blocks": ["wp:buttons"],
        "notes": (
            "QUIZ PAGE: The original page had an interactive quiz. "
            "Generate a static informational page covering the same topic. "
            "Include: intro paragraph explaining the topic, H2 sections with key signs/indicators as a wp:list, "
            "a closing paragraph encouraging the reader to contact FTJ, "
            "and a CTA button linking to /contact. "
            "Add an HTML comment at the top: <!-- QUIZ PLACEHOLDER: original quiz functionality not yet implemented -->"
        ),
    },
    "pricing": {
        "layout_hint": "list-page",
        "css_classes": ["ftj-section-heading", "ftj-bg-tan", "ftj-cta-banner"],
        "extra_blocks": ["wp:table", "wp:buttons"],
        "notes": "Preserve all pricing figures and ranges exactly. Use wp:table for any comparison data. CTA for pricing brochure or contact.",
    },
    "contact": {
        "layout_hint": "contact-page",
        "css_classes": ["ftj-cta-banner", "ftj-section-heading"],
        "extra_blocks": ["wp:buttons", "wp:columns"],
        "notes": "Include address (5340 N Bristol St, Tacoma, WA 98407), phone (253-752-6621). Add a prominent CTA button. Note any form as: <!-- FORM PLACEHOLDER: [form name] -->",
    },
    "community": {
        "layout_hint": "location-page",
        "css_classes": ["ftj-img-text", "ftj-section-heading", "ftj-cards-grid"],
        "extra_blocks": ["wp:columns", "wp:image"],
        "notes": "Conversational tone about the Tacoma neighborhood. H2 per location/topic. Images where provided.",
    },
    "list": {
        "layout_hint": "list-page",
        "css_classes": ["ftj-section-heading", "ftj-heading-accent", "ftj-bg-tan"],
        "extra_blocks": ["wp:list", "wp:columns", "wp:buttons"],
        "notes": "Intro paragraph. H2 per category or service. Items as wp:list. Closing CTA.",
    },
    "generic": {
        "layout_hint": "",
        "css_classes": ["ftj-section-heading"],
        "extra_blocks": ["wp:paragraph", "wp:heading", "wp:list"],
        "notes": "Use best judgment based on the source content structure.",
    },
}


# ══════════════════════════════════════════════════════════════════════════════
#  LOADERS
# ══════════════════════════════════════════════════════════════════════════════

def load_pages_meta() -> dict[str, dict]:
    if not os.path.exists(PAGES_META_CSV):
        log.error(f"Not found: {PAGES_META_CSV}")
        sys.exit(1)
    records = {}
    with open(PAGES_META_CSV, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        headers = reader.fieldnames or []
        # Find sessions column dynamically (contains date range in name)
        sessions_col = next((h for h in headers if "Sessions" in h and "Engaged" not in h and "90" not in h), None)
        for row in reader:
            url = (row.get("URL") or "").strip().rstrip("/")
            if not url.startswith("http"):
                continue
            def _int(v):
                try: return int(float(str(v or 0).replace(",","")))
                except: return 0
            def _float(v):
                try: return float(str(v or 0).replace(",",""))
                except: return 0.0
            records[url] = {
                "url":      url,
                "title":    (row.get("Title") or "").strip(),
                "h1":       (row.get("H1 (Best)") or "").strip(),
                "type":     (row.get("Type") or "").strip(),
                "status":   (row.get("Status") or "").strip(),
                "grade":    (row.get("Grade") or "").strip(),
                "score":    _int(row.get("Score")),
                "modified": (row.get("Date Modified") or "").strip(),
                "words":    _int(row.get("Word Count")),
                "images":   _int(row.get("Image Count")),
                "sessions": _int(row.get(sessions_col)) if sessions_col else 0,
                "engaged":  _int(row.get("Engaged Sessions")),
                "avg_time": _float(row.get("Avg Eng Time (s)")),
                "slug":     (row.get("Slug") or "").strip(),
                "template": (row.get("Template") or "").strip(),
            }
    log.info(f"  Loaded {len(records)} page meta records")
    return records


def load_pages_content() -> dict[str, dict]:
    if not os.path.exists(PAGES_CONTENT_CSV):
        log.error(f"Not found: {PAGES_CONTENT_CSV}")
        sys.exit(1)
    records = {}
    with open(PAGES_CONTENT_CSV, encoding="utf-8-sig", newline="") as f:
        for row in csv.reader(f):
            if not row or len(row) < 5:
                continue
            url = row[CONTENT_COL["url"]].strip().rstrip("/")
            if not url.startswith("http"):
                continue
            img_raw = row[CONTENT_COL["img_urls"]].strip() if len(row) > CONTENT_COL["img_urls"] else ""
            vid_raw = row[CONTENT_COL["video_urls"]].strip() if len(row) > CONTENT_COL["video_urls"] else ""
            records[url] = {
                "url":        url,
                "title":      row[CONTENT_COL["title"]].strip(),
                "body_text":  row[CONTENT_COL["body_text"]].strip() if len(row) > CONTENT_COL["body_text"] else "",
                "headings":   row[CONTENT_COL["headings"]].strip() if len(row) > CONTENT_COL["headings"] else "",
                "image_urls": [u.strip() for u in img_raw.splitlines() if u.strip()],
                "video_urls": [u.strip() for u in vid_raw.splitlines() if u.strip()],
            }
    log.info(f"  Loaded {len(records)} page content records")
    return records


def load_theme_context() -> str:
    """Load theme files into a condensed string for the system prompt."""
    parts = []
    for name, path in THEME_FILES.items():
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                content = f.read()
            # Condense — just the CSS class names and key values, not full files
            if name == "additional_css":
                # Extract class names
                classes = re.findall(r"\.(ftj-[\w-]+)", content)
                unique = list(dict.fromkeys(classes))
                parts.append(f"AVAILABLE FTJ CSS CLASSES:\n" + "\n".join(f"  .{c}" for c in unique))
            elif name == "theme_json":
                try:
                    tj = json.loads(content)
                    palette = tj.get("settings",{}).get("color",{}).get("palette",[])
                    colors = [f"  has-{p['slug']}-color / has-{p['slug']}-background-color  ({p['color']})" for p in palette]
                    parts.append("THEME COLOR SLUGS:\n" + "\n".join(colors))
                    sizes = tj.get("settings",{}).get("typography",{}).get("fontSizes",[])
                    fsizes = [f"  has-{s['slug']}-font-size  ({s['size']})" for s in sizes]
                    parts.append("THEME FONT SIZE SLUGS:\n" + "\n".join(fsizes))
                except Exception:
                    pass
        else:
            log.warning(f"  Theme file not found: {path}")
    return "\n\n".join(parts)


def slug_from_url(url: str) -> str:
    path = urlparse(url).path.strip("/")
    if not path:
        return "page"
    last = path.rstrip("/").split("/")[-1]
    return re.sub(r"[^a-z0-9]+", "-", last.lower()).strip("-") or "page"


# ══════════════════════════════════════════════════════════════════════════════
#  SLUG GENERATION
# ══════════════════════════════════════════════════════════════════════════════

SLUG_SYSTEM_PAGES = """You generate short, SEO-friendly WordPress URL slugs for website pages.
Rules:
- Maximum 4 words, ideally 2-3
- Lowercase, hyphens only, no stop words (a, an, the, at, in, of, for, to, with)
- Keyword-rich: capture the core topic/service
- Must be unique across the batch
- Pages with parent paths should reflect their position (e.g. assisted-living not living-options-assisted-living)
Return ONLY a JSON array of slug strings in the same order as the input.
Each item in the input array is: {"title": "...", "path": "..."}
Use both title and path to inform the slug."""


def generate_slugs_pages(items: list[dict], batch_size: int = 20) -> dict[str, str]:
    url_to_slug = {}
    batches = [items[i:i+batch_size] for i in range(0, len(items), batch_size)]
    log.info(f"  Generating page slugs: {len(items)} items in {len(batches)} batches …")

    for batch in tqdm(batches, desc="Slug gen", leave=False):
        payload = [{"title": b.get("title",""), "path": urlparse(b["url"]).path} for b in batch]
        prompt = f"Generate unique slugs for these {len(payload)} pages:\n" + json.dumps(payload, ensure_ascii=False)
        try:
            text, _ = llm_complete(
                SLUG_SYSTEM_PAGES, prompt, task="slug",
                max_tokens=800, temperature=0.0, extract_slug=False,
            )
            text = (text or "").strip()
            text = re.sub(r"^```(?:json)?\s*\n?", "", text)
            text = re.sub(r"\n?```\s*$", "", text).strip()
            slugs = json.loads(text)
            if isinstance(slugs, list) and len(slugs) == len(batch):
                for item, slug in zip(batch, slugs):
                    clean = re.sub(r"[^a-z0-9-]", "", str(slug).lower().strip())
                    url_to_slug[item["url"]] = clean or slug_from_url(item["url"])
            else:
                for item in batch:
                    url_to_slug[item["url"]] = slug_from_url(item["url"])
        except Exception as e:
            log.warning(f"  Slug batch error: {e}")
            for item in batch:
                url_to_slug[item["url"]] = slug_from_url(item["url"])
        time.sleep(0.3)

    # Deduplicate
    seen: dict[str, int] = {}
    for url in list(url_to_slug):
        s = url_to_slug[url]
        if s in seen:
            seen[s] += 1
            url_to_slug[url] = f"{s}-{seen[s]}"
        else:
            seen[s] = 1

    return url_to_slug


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    ap = argparse.ArgumentParser(description="Page decision engine for Phase 2")
    ap.add_argument("--no-slugs",   action="store_true",
                    help="Skip Anthropic API slug generation")
    ap.add_argument("--dry-run",    action="store_true",
                    help="Print decisions without writing output files")
    ap.add_argument("--min-score",  type=int, default=MIN_SCORE_DEFAULT,
                    help=f"Only include pages with score >= this (default {MIN_SCORE_DEFAULT})")
    args = ap.parse_args()

    log.info("=" * 65)
    log.info("6c_prep_pages — Page decision engine")
    log.info("=" * 65)

    # Load
    meta    = load_pages_meta()
    content = load_pages_content()
    theme   = load_theme_context()
    log.info(f"  Theme context: {len(theme)} chars loaded")

    decisions = []
    skip_list = []

    for url, m in meta.items():
        page_type = detect_page_type(url, m.get("title",""), "")

        # Skip list
        if page_type.startswith("skip"):
            skip_list.append({"url": url, "title": m.get("title",""), "reason": page_type})
            continue
        if m.get("type") == "bold-timeline":
            skip_list.append({"url": url, "title": m.get("title",""), "reason": "bold-timeline"})
            continue
        if m.get("score", 0) < args.min_score:
            skip_list.append({"url": url, "title": m.get("title",""), "reason": f"score-below-{args.min_score}"})
            continue

        # Merge content
        c = content.get(url, {})
        type_config = PAGE_TYPE_CONFIG.get(page_type, PAGE_TYPE_CONFIG["generic"])

        record = {
            "url":          url,
            "new_slug":     "",   # filled later
            "new_title":    m.get("h1") or m.get("title",""),
            "page_type":    page_type,
            "layout_hint":  type_config["layout_hint"],
            "css_classes":  type_config["css_classes"],
            "extra_blocks": type_config["extra_blocks"],
            "note":         type_config["notes"],
            "grade":        m.get("grade",""),
            "score":        m.get("score",0),
            "sessions":     m.get("sessions",0),
            "words":        m.get("words",0),
            "modified":     m.get("modified",""),
            "body_text":    c.get("body_text",""),
            "headings":     c.get("headings",""),
            "image_urls":   c.get("image_urls",[]),
            "video_urls":   c.get("video_urls",[]),
        }
        decisions.append(record)

    log.info(f"  Pages to rewrite: {len(decisions)}")
    log.info(f"  Pages skipped:    {len(skip_list)}")

    # Type breakdown
    from collections import Counter
    type_counts = Counter(d["page_type"] for d in decisions)
    for ptype, count in sorted(type_counts.items()):
        log.info(f"    {ptype:<20} {count}")

    # Slug generation
    if not args.no_slugs:
        slug_items = [{"url": d["url"], "title": d["new_title"]} for d in decisions]
        slug_map   = generate_slugs_pages(slug_items)
        for d in decisions:
            d["new_slug"] = slug_map.get(d["url"], slug_from_url(d["url"]))
    else:
        for d in decisions:
            d["new_slug"] = slug_from_url(d["url"])

    if args.dry_run:
        log.info("\n── DRY RUN — no files written ──")
        for d in decisions[:10]:
            log.info(f"  [{d['page_type']:15}] {d['new_title']} → /{d['new_slug']}")
        return

    os.makedirs(config.DATA_DIR, exist_ok=True)

    output = {
        "generated":   datetime.utcnow().isoformat() + "Z",
        "site":        config.SITE_URL,
        "theme_context": theme,
        "pages":       decisions,
    }
    with open(OUT_DECISIONS, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    log.info(f"  → {OUT_DECISIONS}  ({len(decisions)} pages)")

    with open(OUT_SKIP, "w", encoding="utf-8") as f:
        json.dump(skip_list, f, indent=2, ensure_ascii=False)
    log.info(f"  → {OUT_SKIP}  ({len(skip_list)} skipped)")

    log.info("\n" + "=" * 65)
    log.info("Done. Next step:")
    log.info("  python 6_rewrite_pages.py --post")
    log.info("=" * 65)


if __name__ == "__main__":
    main()
