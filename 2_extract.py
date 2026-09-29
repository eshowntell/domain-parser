#!/usr/bin/env python3
"""
2_extract.py  —  Pull body text, H1, image count, and links from every page.

Strategy:
  • Uses WP REST API `content.rendered` for clean body text (no theme boilerplate).
  • Falls back to live-page scraping + boilerplate stripping when REST content
    is empty (e.g. page-builder-only pages with no stored content).
  • Scrapes the live site's <nav> elements once (homepage) to capture the master
    menu structure.
  • Writes one record per page; non-public / draft pages are included but flagged.

Outputs:
  data/02_content.json    — per-page content records
  data/02_menus.json      — master navigation items
"""

import json
import os
import re
import sys
from urllib.parse import urlparse, urljoin

from bs4 import BeautifulSoup
from tqdm import tqdm

sys.path.insert(0, os.path.dirname(__file__))
import config
from utils import setup_logging, api_get, scrape_get

log = setup_logging("2_extract")

IN_FILE  = os.path.join(config.DATA_DIR, "01_pages.json")
OUT_FILE = os.path.join(config.DATA_DIR, "02_content.json")
NAV_FILE = os.path.join(config.DATA_DIR, "02_menus.json")
SITE_HOST = urlparse(config.SITE_URL).netloc


# ── Boilerplate fingerprinting (used for fallback scraping) ───────────────────

def fingerprint_boilerplate(homepage_html: str) -> set[str]:
    """
    Extract text snippets from the homepage's structural elements
    (header, footer, nav) so we can remove them from other pages.
    Returns a set of short text fragments.
    """
    soup = BeautifulSoup(homepage_html, "lxml")
    boilerplate_texts: set[str] = set()
    selectors = [
        "header", "footer", "nav",
        ".site-header", ".site-footer", ".main-navigation",
        "#masthead", "#colophon", "#site-navigation",
        ".navbar", ".nav-bar", ".navigation",
        '[role="banner"]', '[role="contentinfo"]', '[role="navigation"]',
    ]
    for sel in selectors:
        for el in soup.select(sel):
            frag = el.get_text(" ", strip=True)[:120]
            if frag:
                boilerplate_texts.add(frag)
    return boilerplate_texts


def strip_boilerplate_from_soup(soup: BeautifulSoup) -> None:
    """Remove common WP theme chrome in-place."""
    remove_selectors = [
        "header", "footer", "nav",
        ".site-header", ".site-footer", ".main-navigation",
        "#masthead", "#colophon", "#site-navigation",
        ".navbar", ".nav-bar", ".navigation",
        ".sidebar", "#sidebar", "aside",
        ".widget-area", ".widget",
        ".breadcrumbs", ".breadcrumb",
        '[role="banner"]', '[role="contentinfo"]', '[role="navigation"]',
        "script", "style", "noscript",
    ]
    for sel in remove_selectors:
        for el in soup.select(sel):
            el.decompose()


# ── Content extraction ────────────────────────────────────────────────────────

def extract_from_api_content(html: str, page_url: str) -> dict:
    """Parse WP REST API `content.rendered` HTML into our schema."""
    soup = BeautifulSoup(html, "lxml")

    # Collect image URLs then remove images
    # Check src first, then common lazy-load attributes
    image_urls = []
    for img in soup.find_all("img"):
        src = (
            img.get("src", "") or
            img.get("data-src", "") or
            img.get("data-lazy-src", "") or
            img.get("data-original", "")
        ).strip()
        if src and not src.startswith("data:"):
            image_urls.append(urljoin(page_url, src))
    image_count = len(image_urls)
    for img in soup.find_all("img"):
        img.decompose()

    # Collect video URLs (native <video> and common iframe embeds)
    video_urls = []
    for video in soup.find_all("video"):
        src = video.get("src", "").strip()
        if src:
            video_urls.append(urljoin(page_url, src))
        for source in video.find_all("source"):
            src = source.get("src", "").strip()
            if src:
                video_urls.append(urljoin(page_url, src))
    for iframe in soup.find_all("iframe"):
        src = iframe.get("src", "").strip()
        if src and any(h in src for h in ("youtube", "vimeo", "wistia", "loom")):
            video_urls.append(src)
    # Gutenberg wp-block-embed figures store the embed URL as plain text,
    # not as an <iframe src>, so the loop above misses them
    for figure in soup.find_all("figure", class_=re.compile(r"wp-block-embed")):
        wrapper = figure.find(class_=re.compile(r"wp-block-embed__wrapper"))
        if wrapper:
            candidate = wrapper.get_text(strip=True)
            if any(h in candidate for h in ("youtube.com", "youtu.be", "vimeo.com", "wistia.com", "loom.com")):
                video_urls.append(candidate)

    # Links
    internal_links, external_links = [], []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith("#") or href.startswith("mailto:") or href.startswith("tel:"):
            continue
        abs_href = urljoin(page_url, href)
        if urlparse(abs_href).netloc == SITE_HOST:
            internal_links.append(abs_href.rstrip("/"))
        else:
            external_links.append(abs_href)
    for a in soup.find_all("a"):
        a.unwrap()

    # H1 within content (uncommon in WP but possible)
    content_h1 = ""
    first_h1 = soup.find("h1")
    if first_h1:
        content_h1 = first_h1.get_text(strip=True)

    # All headings
    headings = []
    for tag in soup.find_all(["h1","h2","h3","h4","h5","h6"]):
        headings.append({"level": tag.name, "text": tag.get_text(strip=True)})

    # Clean text
    text = soup.get_text(separator=" ", strip=True)
    text = re.sub(r"\s{2,}", " ", text).strip()
    word_count = len(text.split())

    return {
        "body_text":       text,
        "word_count":      word_count,
        "image_count":     image_count,
        "image_urls":      list(dict.fromkeys(image_urls)),   # deduped, order preserved
        "video_urls":      list(dict.fromkeys(video_urls)),
        "internal_links":  list(set(internal_links)),
        "external_links":  list(set(external_links)),
        "content_h1":      content_h1,
        "headings":        headings[:30],
        "source":          "api",
    }


def find_content_element(soup):
    """Try progressively broader selectors to find the page content area.
    Covers standard WP, Elementor, Divi, WPBakery, and Beaver Builder.
    Returns a Tag or None.
    """
    candidates = [
        # Standard WordPress
        lambda s: s.find("main"),
        lambda s: s.find(id="main"),
        lambda s: s.find(id="content"),
        lambda s: s.find(class_=re.compile(
            r"(^|\s)(entry-content|post-content|page-content|site-content|content-area)(\s|$)")),
        lambda s: s.find("article"),
        # Elementor
        lambda s: s.find(attrs={"data-elementor-type": True}),
        lambda s: s.find(class_=re.compile(r"elementor-section-wrap|elementor-widget-wrap")),
        lambda s: s.find(class_=re.compile(r"^elementor$")),
        # Divi
        lambda s: s.find(id="et-main-area"),
        lambda s: s.find(class_=re.compile(r"et_builder_inner_content|et_pb_section")),
        # WPBakery / Visual Composer
        lambda s: s.find(class_=re.compile(r"wpb_content_element|vc_row")),
        # Beaver Builder
        lambda s: s.find(class_=re.compile(r"fl-builder-content")),
        # Generic fallback: biggest <div> by text length
        lambda s: max(
            (d for d in s.find_all("div") if len(d.get_text(strip=True)) > 200),
            key=lambda d: len(d.get_text(strip=True)),
            default=None
        ),
    ]
    for fn in candidates:
        try:
            result = fn(soup)
            if result:
                return result
        except Exception:
            pass
    return None


def extract_from_live_page(html: str, page_url: str) -> dict:
    """Scrape a live rendered page, stripping theme chrome."""
    soup = BeautifulSoup(html, "lxml")

    # Remove obvious chrome before any content search
    for sel in ["header", "footer", "nav", "#wpadminbar", ".admin-bar",
                "script", "style", "noscript",
                ".site-header", ".site-footer", ".main-navigation",
                "#masthead", "#colophon", "#site-navigation"]:
        for el in soup.select(sel):
            el.decompose()

    main = find_content_element(soup)
    if main:
        work_soup = BeautifulSoup(str(main), "lxml")
    else:
        # Nuclear fallback: collect all semantic text blocks from body
        work_soup = soup

    image_urls = []
    for img in work_soup.find_all("img"):
        src = (
            img.get("src", "") or
            img.get("data-src", "") or
            img.get("data-lazy-src", "") or
            img.get("data-original", "")
        ).strip()
        if src and not src.startswith("data:"):
            image_urls.append(urljoin(page_url, src))
    image_count = len(image_urls)
    for img in work_soup.find_all("img"):
        img.decompose()

    video_urls = []
    for video in work_soup.find_all("video"):
        src = video.get("src", "").strip()
        if src:
            video_urls.append(urljoin(page_url, src))
        for source in video.find_all("source"):
            src = source.get("src", "").strip()
            if src:
                video_urls.append(urljoin(page_url, src))
    for iframe in work_soup.find_all("iframe"):
        src = iframe.get("src", "").strip()
        if src and any(h in src for h in ("youtube", "vimeo", "wistia", "loom")):
            video_urls.append(src)
    # Gutenberg wp-block-embed figures store the embed URL as plain text,
    # not as an <iframe src>, so the loop above misses them
    for figure in work_soup.find_all("figure", class_=re.compile(r"wp-block-embed")):
        wrapper = figure.find(class_=re.compile(r"wp-block-embed__wrapper"))
        if wrapper:
            candidate = wrapper.get_text(strip=True)
            if any(h in candidate for h in ("youtube.com", "youtu.be", "vimeo.com", "wistia.com", "loom.com")):
                video_urls.append(candidate)

    internal_links, external_links = [], []
    for a in work_soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith("#") or href.startswith("mailto:") or href.startswith("tel:"):
            continue
        abs_href = urljoin(page_url, href)
        if urlparse(abs_href).netloc == SITE_HOST:
            internal_links.append(abs_href.rstrip("/"))
        else:
            external_links.append(abs_href)

    headings = []
    for tag in work_soup.find_all(["h1","h2","h3","h4","h5","h6"]):
        headings.append({"level": tag.name, "text": tag.get_text(strip=True)})

    content_h1 = headings[0]["text"] if headings and headings[0]["level"] == "h1" else ""

    for a in work_soup.find_all("a"):
        a.unwrap()
    text = work_soup.get_text(separator=" ", strip=True)
    text = re.sub(r"\s{2,}", " ", text).strip()

    # If we got very little text, try nuclear: collect block element text from whole page
    if len(text.split()) < 30:
        body = BeautifulSoup(html, "lxml").find("body") or BeautifulSoup(html, "lxml")
        for sel in ["header", "footer", "nav", "script", "style", "noscript",
                    "#wpadminbar", ".site-header", ".site-footer", "#masthead", "#colophon"]:
            for el in body.select(sel):
                el.decompose()
        blocks = body.find_all(["p", "h1", "h2", "h3", "h4", "h5", "h6",
                                 "li", "td", "blockquote", "figcaption"])
        nuclear_text = " ".join(
            t for b in blocks
            if len((t := b.get_text(" ", strip=True))) > 25
        )
        nuclear_text = re.sub(r"\s{2,}", " ", nuclear_text).strip()
        if len(nuclear_text.split()) > len(text.split()):
            text = nuclear_text

    return {
        "body_text":      text,
        "word_count":     len(text.split()),
        "image_count":    image_count,
        "image_urls":     list(dict.fromkeys(image_urls)),
        "video_urls":     list(dict.fromkeys(video_urls)),
        "internal_links": list(set(internal_links)),
        "external_links": list(set(external_links)),
        "content_h1":     content_h1,
        "headings":       headings[:30],
        "source":         "scrape",
    }


def get_live_h1(html: str) -> str:
    """Extract the first <h1> from the full rendered page (theme's H1)."""
    soup = BeautifulSoup(html, "lxml")
    h1 = soup.find("h1")
    return h1.get_text(strip=True) if h1 else ""


# ── Menu extraction ───────────────────────────────────────────────────────────

def extract_menus(html: str, page_url: str) -> list[dict]:
    """Pull every nav element from the homepage and return a flat menu record list."""
    soup = BeautifulSoup(html, "lxml")
    menus = []
    nav_idx = 0

    for nav in soup.find_all("nav"):
        nav_idx += 1
        aria_label = nav.get("aria-label", "").strip()
        nav_id     = nav.get("id", "").strip()
        nav_class  = " ".join(nav.get("class", []))
        nav_name   = aria_label or nav_id or nav_class or f"nav-{nav_idx}"

        # Walk the link tree
        def walk(element, parent_text="", depth=0):
            for li in element.find_all("li", recursive=False):
                a = li.find("a", recursive=False) or li.find("a")
                if not a:
                    continue
                href  = a.get("href", "").strip()
                label = a.get_text(strip=True)
                target = a.get("target", "")
                if href and label:
                    abs_href = urljoin(page_url, href) if not href.startswith("http") else href
                    menus.append({
                        "menu_name":   nav_name,
                        "depth":       depth,
                        "label":       label,
                        "url":         abs_href,
                        "parent_text": parent_text,
                        "target":      target,
                    })
                sub_ul = li.find("ul")
                if sub_ul:
                    walk(sub_ul, label, depth + 1)

        top_ul = nav.find("ul")
        if top_ul:
            walk(top_ul)

    # Also capture any WP block navigation (Gutenberg nav blocks may exist elsewhere)
    for block_nav in soup.find_all(class_=re.compile(r"wp-block-navigation")):
        if block_nav.find_parent("nav"):
            continue  # already captured
        for a in block_nav.find_all("a", href=True):
            href  = a["href"].strip()
            label = a.get_text(strip=True)
            if href and label:
                menus.append({
                    "menu_name":   "wp-block-navigation",
                    "depth":       0,
                    "label":       label,
                    "url":         urljoin(page_url, href),
                    "parent_text": "",
                    "target":      a.get("target", ""),
                })

    return menus


# ── Main ──────────────────────────────────────────────────────────────────────

# ── Menu fetch via WP REST API (fallback for JS-rendered navs) ────────────────

def fetch_menus_from_api() -> list[dict]:
    """
    Pull nav menu items from /wp-json/wp/v2/menu-items (WP 5.9+).
    Also resolves menu names from /wp-json/wp/v2/menus where available.
    Returns the same flat record format as extract_menus().
    """
    # 1. Fetch menu name map (id → name) — best-effort
    menu_names: dict[int, str] = {}
    r = api_get("menus")
    if r:
        data = r.json()
        if isinstance(data, list):
            for m in data:
                menu_names[m.get("id", 0)] = m.get("name", f"menu-{m.get('id')}")

    # 2. Paginate through all menu-items
    raw_items: list[dict] = []
    page = 1
    while True:
        r = api_get("menu-items", {"per_page": 100, "page": page})
        if not r:
            break
        data = r.json()
        if not isinstance(data, list) or not data:
            break
        raw_items.extend(data)
        total_pages = int(r.headers.get("X-WP-TotalPages", 1))
        log.info(f"  menu-items: page {page}/{total_pages} — {len(data)} items")
        if page >= total_pages:
            break
        page += 1

    if not raw_items:
        return []

    # 3. Build id → item map for parent resolution and depth calculation
    id_map: dict[int, dict] = {item["id"]: item for item in raw_items}

    def get_depth(item_id: int, seen: set = None) -> int:
        if seen is None:
            seen = set()
        if item_id in seen:
            return 0          # cycle guard
        seen.add(item_id)
        item = id_map.get(item_id, {})
        parent_id = item.get("parent", 0)
        if not parent_id or parent_id not in id_map:
            return 0
        return 1 + get_depth(parent_id, seen)

    def get_label(item: dict) -> str:
        return BeautifulSoup(
            item.get("title", {}).get("rendered", ""), "lxml"
        ).get_text(strip=True)

    # 4. Build flat records sorted by menu_order
    records: list[dict] = []
    for item in sorted(raw_items, key=lambda x: (x.get("menus", [0])[0] if x.get("menus") else 0,
                                                  x.get("menu_order", 0))):
        dest_url  = item.get("url", "")
        label     = get_label(item)
        if not label or not dest_url:
            continue

        parent_id   = item.get("parent", 0)
        parent_item = id_map.get(parent_id, {})
        parent_lbl  = get_label(parent_item) if parent_id else ""
        depth       = get_depth(item["id"])

        menu_id_list = item.get("menus", [])
        menu_id      = menu_id_list[0] if menu_id_list else 0
        menu_name    = menu_names.get(menu_id) or f"menu-{menu_id}" if menu_id else "main-menu"

        records.append({
            "menu_name":   menu_name,
            "depth":       depth,
            "label":       label,
            "url":         dest_url,
            "parent_text": parent_lbl,
            "target":      item.get("attr_title", ""),
        })

    return records


# ── Main ────────────────────────────────────────────────────────────────────
def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true",
                        help="Re-extract all pages, ignoring cached results")
    args = parser.parse_args()

    log.info("=" * 65)
    log.info("STEP 2 — Content extraction")
    log.info("=" * 65)

    if not os.path.exists(IN_FILE):
        log.error(f"{IN_FILE} not found. Run 1_crawl.py first.")
        sys.exit(1)

    with open(IN_FILE, encoding="utf-8") as f:
        pages = json.load(f)
    log.info(f"Loaded {len(pages)} pages from {IN_FILE}")

    # ── Step A: scrape homepage for boilerplate + menus ───────────────────────
    log.info(f"\nFetching homepage for boilerplate fingerprint + nav …")
    hp_resp = scrape_get(config.SITE_URL)
    menus = []
    if hp_resp:
        menus = extract_menus(hp_resp.text, config.SITE_URL)
        log.info(f"  Found {len(menus)} menu items across {len(set(m['menu_name'] for m in menus))} nav elements")

    # Fallback: JS-rendered menus won't appear in raw HTML scrape.
    # Use WP REST API menu-items endpoint instead (WP 5.9+).
    if not menus:
        log.info("  Homepage scrape got 0 nav items — trying WP REST API menu-items …")
        menus = fetch_menus_from_api()
        log.info(f"  Found {len(menus)} menu items via REST API")

    if not menus and os.path.exists(NAV_FILE):
        with open(NAV_FILE, encoding="utf-8") as _nf:
            _existing = json.load(_nf)
        if _existing:
            log.info(f"  Got 0 menu items from scrape+API — keeping existing {NAV_FILE} ({len(_existing)} items).")
            log.info(f"  To rebuild from scratch, run:  python build_menus.py")
        else:
            with open(NAV_FILE, "w", encoding="utf-8") as f:
                json.dump(menus, f, indent=2, ensure_ascii=False)
            log.info(f"  Saved menus → {NAV_FILE}")
    else:
        with open(NAV_FILE, "w", encoding="utf-8") as f:
            json.dump(menus, f, indent=2, ensure_ascii=False)
        log.info(f"  Saved menus → {NAV_FILE}")
    if not hp_resp:
        log.warning("  Could not fetch homepage.")

    # ── Step B: per-page content ──────────────────────────────────────────────
    results = []
    done_urls: set[str] = set()
    if os.path.exists(OUT_FILE) and not args.force:
        with open(OUT_FILE, encoding="utf-8") as f:
            existing = json.load(f)
        results = existing
        # Skip pages that already have real content; re-process zero-word pages
        for r in results:
            if r.get("word_count", 0) > 0 or r.get("error", "") not in ("", None):
                done_urls.add(r["url"])
        skipped    = sum(1 for r in results if r["url"] in done_urls)
        reprocess  = sum(1 for r in results if r["url"] not in done_urls)
        # Remove zero-word records so they get re-added cleanly
        results = [r for r in results if r["url"] in done_urls]
        log.info(f"  Resuming — {skipped} pages already have content, skipping.")
        if reprocess:
            log.info(f"  Re-processing {reprocess} zero-word pages (extraction likely failed before).")
    elif args.force:
        log.info("  --force: re-extracting all pages from scratch.")

    todo = [p for p in pages if p["url"] not in done_urls]
    log.info(f"  Pages to process: {len(todo)}")

    api_ok = 0
    scrape_ok = 0
    failed = 0

    for page in tqdm(todo, desc="Extracting", unit="pg"):
        url      = page["url"]
        wp_id    = page.get("wp_id")
        type_slug = page.get("type", "page")
        status   = page.get("status", "")

        record = {
            "url":           url,
            "title":         page.get("title", ""),
            "h1_title":      page.get("title", ""),
            "h1_live":       "",
            "h1_content":    "",
            "type":          type_slug,
            "status":        status,
            "date_created":  page.get("date_created", ""),
            "date_modified": page.get("date_modified", ""),
            "body_text":     "",
            "word_count":    0,
            "image_count":   0,
            "image_urls":    [],          # ← new
            "video_urls":    [],          # ← new
            "internal_links": [],
            "external_links": [],
            "headings":      [],
            "source":        "none",
            "error":         "",
            # ── Phase 2 migration fields (filled in manually or by a later step) ──
            "redirect_from": url,         # explicit old URL for redirect export
            "new_slug":      "",          # e.g. /assisted-living/lillian-pratt
            "new_title":     "",          # cleaned title, no SEO stuffing
            "layout_hint":   "",          # hero-page | location-page | list-page | blog-post | team-page
            "parent_slug":   "",          # for rebuilding URL hierarchy
            "note":          "",          # per-page migration instructions
            "priority":      "",          # high | medium | low  (or leave numeric, derived later)
        }

        # Determine the REST endpoint for this type
        type_to_rest = {
            "page":  "pages",
            "post":  "posts",
        }
        rest_base = type_to_rest.get(type_slug)

        # Try WP REST API first (if we have a wp_id)
        api_content_html = ""
        if wp_id and rest_base:
            r = api_get(f"{rest_base}/{wp_id}")
            if r:
                data = r.json()
                api_content_html = data.get("content", {}).get("rendered", "")
                # Featured image lives outside content.rendered — fetch it separately
                featured_media_id = data.get("featured_media")
                if featured_media_id:
                    img_r = api_get(f"media/{featured_media_id}")
                    if img_r:
                        img_data = img_r.json()
                        feat_src = img_data.get("source_url", "").strip()
                        if feat_src:
                            record["image_urls"] = [feat_src]  # seed; extract() appends more

        if api_content_html and len(api_content_html.strip()) > 50:
            extracted = extract_from_api_content(api_content_html, url)
            # Merge image_urls: featured image (already in record) goes first
            featured = record.get("image_urls", [])
            content_imgs = extracted.get("image_urls", [])
            merged = list(dict.fromkeys(featured + content_imgs))
            record.update(extracted)
            record["image_urls"] = merged
            record["h1_content"] = extracted["content_h1"]
            api_ok += 1
            # Also get live H1 for public pages (matches what users actually see)
            if status == "publish":
                live_resp = scrape_get(url)
                if live_resp:
                    record["h1_live"] = get_live_h1(live_resp.text)
        else:
            # Fallback: live scrape (works for page-builder pages, sitemap-only, etc.)
            if status in ("publish", "public", ""):
                live_resp = scrape_get(url)
                if live_resp:
                    extracted = extract_from_live_page(live_resp.text, url)
                    record.update(extracted)
                    record["h1_live"] = extracted["content_h1"]
                    scrape_ok += 1
                else:
                    record["error"] = "scrape_failed"
                    failed += 1
            else:
                record["error"] = "non_public_no_api_content"
                failed += 1

        # Best H1: live > content > title
        record["h1_best"] = (
            record.get("h1_live") or
            record.get("h1_content") or
            record.get("h1_title") or ""
        )

        results.append(record)

        # Save incrementally every 50 pages
        if len(results) % 50 == 0:
            with open(OUT_FILE, "w", encoding="utf-8") as f:
                json.dump(results, f, indent=2, ensure_ascii=False)

    # Final save
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    log.info(f"\n{'─'*65}")
    log.info(f"Processed {len(todo)} pages")
    log.info(f"  Via WP REST API:  {api_ok}")
    log.info(f"  Via live scrape:  {scrape_ok}")
    log.info(f"  Errors / skipped: {failed}")
    log.info(f"Saved → {OUT_FILE}")
    log.info("Step 2 complete. Run:  python 3_ga4_merge.py  (or skip to step 4)")


if __name__ == "__main__":
    main()
