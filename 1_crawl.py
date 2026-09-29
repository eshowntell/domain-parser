#!/usr/bin/env python3
"""
1_crawl.py  —  Build a complete URL inventory via WP REST API + sitemaps.

Outputs:  data/01_pages.json

For each URL it captures:
  wp_id, url, title, type, status, date_created, date_modified,
  parent_id, slug, template, menu_order, excerpt
"""

import json
import sys
import os
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from tqdm import tqdm

sys.path.insert(0, os.path.dirname(__file__))
import config
from utils import setup_logging, api_get, scrape_get

log = setup_logging("1_crawl")

OUT = os.path.join(config.DATA_DIR, "01_pages.json")


# ── WP REST API helpers ───────────────────────────────────────────────────────

def get_all_of_type(rest_base: str, status: str = "any") -> list:
    """Paginate through every item of a WP REST API endpoint.

    WP Engine's WAF often rejects status=any. We fall back to requesting each
    known status individually so nothing is silently dropped.
    """
    # Statuses to try — 'any' first; if it fails or returns an error dict we
    # fall back to fetching each status explicitly.
    ALL_STATUSES = ["publish", "draft", "private", "pending", "future", "trash"]

    def _fetch_status(s):
        results = []
        page = 1
        while True:
            params = {"per_page": 100, "page": page}
            if s:
                params["status"] = s
            r = api_get(rest_base, params)
            if r is None:
                break
            data = r.json()
            # Detect WP error response (dict instead of list)
            if isinstance(data, dict):
                code = data.get("code", "")
                if code:
                    log.debug(f"  {rest_base} status={s}: {code} — {data.get('message','')}")
                break
            if not data:
                break
            results.extend(data)
            total_pages = int(r.headers.get("X-WP-TotalPages", 1))
            log.info(f"  {rest_base} (status={s}): page {page}/{total_pages} — {len(data)} items")
            if page >= total_pages:
                break
            page += 1
        return results

    # Try status=any first (works on non-WPE sites with admin creds)
    items = _fetch_status("any")
    if items:
        return items

    # Fall back: fetch each status individually
    log.info(f"  status=any returned nothing for '{rest_base}' — trying per-status fallback …")
    seen_ids: set[int] = set()
    all_items = []
    for s in ALL_STATUSES:
        batch = _fetch_status(s)
        for item in batch:
            iid = item.get("id")
            if iid not in seen_ids:
                seen_ids.add(iid)
                all_items.append(item)
    return all_items


def discover_post_types() -> list[tuple[str, str]]:
    """Return list of (type_slug, rest_base) for all public, REST-enabled post types."""
    r = api_get(f"{config.SITE_URL}/wp-json/wp/v2/types")
    if r is None:
        log.warning("Cannot read post types — falling back to pages + posts")
        return [("page", "pages"), ("post", "posts")]
    types = r.json()
    result = []
    for slug, info in types.items():
        rest_base = info.get("rest_base")
        if rest_base and slug != "attachment":
            result.append((slug, rest_base))
    return result


def normalize(item: dict, type_slug: str) -> dict:
    """Flatten a WP REST API item to our audit schema."""
    excerpt_html = item.get("excerpt", {}).get("rendered", "")
    excerpt_text = BeautifulSoup(excerpt_html, "lxml").get_text(strip=True)[:500]
    return {
        "wp_id":         item.get("id"),
        "url":           item.get("link", "").rstrip("/"),
        "title":         BeautifulSoup(
                             item.get("title", {}).get("rendered", ""), "lxml"
                         ).get_text(strip=True),
        "type":          type_slug,
        "status":        item.get("status", ""),
        "date_created":  item.get("date", "")[:10],      # YYYY-MM-DD
        "date_modified": item.get("modified", "")[:10],
        "parent_id":     item.get("parent", 0),
        "slug":          item.get("slug", ""),
        "template":      item.get("template", ""),
        "menu_order":    item.get("menu_order", 0),
        "excerpt":       excerpt_text,
    }


# ── Sitemap crawl ─────────────────────────────────────────────────────────────

def parse_sitemap(xml: str) -> tuple[list[str], list[str]]:
    """Return (page_urls, sub_sitemap_urls) from a sitemap/sitemap-index XML."""
    soup = BeautifulSoup(xml, "lxml-xml")
    subs  = [t.find("loc").text.strip() for t in soup.find_all("sitemap") if t.find("loc")]
    pages = [u.find("loc").text.strip() for u in soup.find_all("url")    if u.find("loc")]
    return pages, subs


def crawl_sitemaps() -> set[str]:
    candidates = [
        f"{config.SITE_URL}/wp-sitemap.xml",
        f"{config.SITE_URL}/sitemap.xml",
        f"{config.SITE_URL}/sitemap_index.xml",
    ]
    all_page_urls: set[str] = set()
    queue: list[str] = []

    for url in candidates:
        r = scrape_get(url)
        if r and r.status_code == 200:
            ct = r.headers.get("content-type", "")
            body = r.text.strip()
            if "xml" in ct or body.startswith("<?xml") or "<urlset" in body or "<sitemapindex" in body:
                log.info(f"  Primary sitemap: {url}  (content-type: {ct})")
                queue.append(r.text)
                break
            else:
                log.warning(f"  {url} returned content-type {ct!r} — skipping")

    visited_subs: set[str] = set()
    for xml in queue:
        pages, subs = parse_sitemap(xml)
        for p in pages:
            if config.SITE_URL in p:
                all_page_urls.add(p.rstrip("/"))
        for s in subs:
            if s not in visited_subs:
                visited_subs.add(s)
                r = scrape_get(s)
                if r and r.status_code == 200:
                    p2, _ = parse_sitemap(r.text)
                    for p in p2:
                        if config.SITE_URL in p:
                            all_page_urls.add(p.rstrip("/"))

    return all_page_urls


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    log.info("=" * 65)
    log.info("STEP 1 — WP REST API crawl + sitemap scan")
    log.info("=" * 65)

    # Verify connection
    r = api_get(f"{config.SITE_URL}/wp-json/")
    if r is None:
        log.error("Cannot reach WP REST API. Check config.py credentials.")
        sys.exit(1)
    site_info = r.json()
    log.info(f"Connected to: {site_info.get('name', config.SITE_URL)}")
    log.info(f"WP namespace: {list(site_info.get('namespaces', []))}")

    # Discover post types
    post_types = discover_post_types()
    log.info(f"Post types: {[pt[0] for pt in post_types]}")

    all_items: list[dict] = []
    seen: set[tuple] = set()

    for type_slug, rest_base in tqdm(post_types, desc="Post types"):
        log.info(f"\nFetching '{type_slug}' (rest_base={rest_base}) …")
        raw = get_all_of_type(rest_base)
        for item in raw:
            key = (item.get("id"), type_slug)
            if key not in seen:
                seen.add(key)
                all_items.append(normalize(item, type_slug))
        log.info(f"  → {len(raw)} items")

    # Sitemap supplement
    log.info("\nScanning sitemaps for any missed URLs …")
    sitemap_urls = crawl_sitemaps()
    log.info(f"  Sitemap total: {len(sitemap_urls)} URLs")

    existing_urls = {item["url"] for item in all_items}
    extras = 0
    for url in sitemap_urls:
        norm = url.rstrip("/")
        if norm not in existing_urls:
            all_items.append({
                "wp_id":         None,
                "url":           norm,
                "title":         "",
                "type":          "sitemap-only",
                "status":        "public",
                "date_created":  "",
                "date_modified": "",
                "parent_id":     0,
                "slug":          urlparse(url).path.strip("/").split("/")[-1],
                "template":      "",
                "menu_order":    0,
                "excerpt":       "",
            })
            extras += 1
    log.info(f"  Added {extras} URLs found only in sitemap")

    # Summary
    log.info(f"\n{'─'*65}")
    log.info(f"Total URLs: {len(all_items)}")
    by_type   = {}
    by_status = {}
    for item in all_items:
        by_type[item["type"]]     = by_type.get(item["type"], 0) + 1
        by_status[item["status"]] = by_status.get(item["status"], 0) + 1
    log.info("By type:   " + "  |  ".join(f"{t}: {c}" for t, c in sorted(by_type.items())))
    log.info("By status: " + "  |  ".join(f"{s}: {c}" for s, c in sorted(by_status.items())))

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(all_items, f, indent=2, ensure_ascii=False)
    log.info(f"\nSaved → {OUT}")
    log.info("Step 1 complete. Run:  python 2_extract.py")


if __name__ == "__main__":
    main()
