#!/usr/bin/env python3
"""
update_slugs.py  —  Review and update WordPress slugs without touching content.

WORKFLOW
────────
  Step 1 — export current state to a CSV you can edit:
    python update_slugs.py --export

  Step 2 — open output/slug_review.csv, edit the new_slug column, save.

  Step 3 — apply your changes to WordPress + regenerate sitemaps:
    python update_slugs.py --apply

  (Optional) Build the full old-site → new-site redirect map for dev:
    python update_slugs.py --redirects

  (Optional) Regenerate sitemaps only, no slug changes:
    python update_slugs.py --sitemaps

  (Optional) Check which old-site URLs are live vs 404:
    python update_slugs.py --audit-old-site

  (Optional) Write catch-all redirects for orphaned old URLs:
    python update_slugs.py --fallback-redirects

EXPORT CSV columns
──────────────────
  post_id      — WP post/page ID (do not edit)
  post_type    — 'post' or 'page' (do not edit)
  current_slug — what WP has right now (do not edit)
  new_slug     — EDIT THIS column to set the new slug
  title        — for reference (do not edit)
  status       — publish/draft (do not edit)
  link         — current WP link (do not edit)

APPLY behaviour
───────────────
  - Rows where new_slug == current_slug are skipped
  - Rows where new_slug is blank are skipped
  - Each successful PATCH is logged with old → new slug
  - Slug changes on the new site are written to output/slug_changes.csv
  - The full old-site redirect map is regenerated automatically
  - Sitemaps are regenerated from the live WP state after all patches

REDIRECTS behaviour  (output/slug_redirects.csv)
─────────────────────────────────────────────────
  Built from three source JSON files in config.DECISIONS_DIR:
    06c_decisions_pages.json  — pages: one old URL → one new slug
    06c_decisions_blogs.json  — combined blogs: many old URLs → one new slug
    06c_solo_blogs.json       — solo blogs: one old URL → one new slug

  Also requires config.RAW_PAGES_JSON pointing to 01_pages.json
  for --audit-old-site and --fallback-redirects.

  CSV columns handed to dev:
    old_url   — original franketobeyjones.com URL (redirect FROM)
    new_url   — new site URL built from new_slug  (redirect TO)
    new_slug  — slug only, for reference
    type      — page | blog-combined | blog-solo
    note      — for blog-combined: lists all member URLs merged into new_url

  NOTE: Posts use BASE/blog/<slug>/ and pages use BASE/<slug>/ matching
  the /blog/%postname%/ permalink structure on ftj3.
"""

import argparse
import csv
import json
import os
import re
import sys
from datetime import datetime

import requests

sys.path.insert(0, os.path.dirname(__file__))
import config
from utils import setup_logging

log = setup_logging("update_slugs")

AUTH     = (config.NEW_WP_USERNAME, config.NEW_WP_APP_PASSWORD)
BASE     = config.NEW_SITE_URL.rstrip("/")
API      = f"{BASE}/wp-json/wp/v2"

# Directory that holds the 06c_decisions_*.json source files.
DECISIONS_DIR = getattr(config, "DECISIONS_DIR", os.path.dirname(__file__))

# Raw crawl of all old-site pages — used for fallback redirects and audit.
RAW_PAGES_JSON = getattr(config, "RAW_PAGES_JSON",
                          os.path.join(DECISIONS_DIR, "data", "01_pages.json"))

EXPORT_CSV       = os.path.join(config.OUTPUT_DIR, "slug_review.csv")
REDIRECT_CSV     = os.path.join(config.OUTPUT_DIR, "slug_redirects.csv")
SLUG_CHANGES_CSV = os.path.join(config.OUTPUT_DIR, "slug_changes.csv")
AUDIT_CSV        = os.path.join(config.OUTPUT_DIR, "audit_old_site.csv")
AUDIT_404_CSV    = os.path.join(config.OUTPUT_DIR, "audit_404s.csv")
FALLBACK_CSV     = os.path.join(config.OUTPUT_DIR, "fallback_redirects.csv")
OUT_PAGES        = os.path.join(config.OUTPUT_DIR, "sitemap_pages.xml")
OUT_BLOGS        = os.path.join(config.OUTPUT_DIR, "sitemap_blogs.xml")
OUT_INDEX        = os.path.join(config.OUTPUT_DIR, "sitemap_index.xml")

# Source JSON files (old-site decisions)
SRC_PAGES = os.path.join(DECISIONS_DIR, "06c_decisions_pages.json")
SRC_BLOGS = os.path.join(DECISIONS_DIR, "06c_decisions_blogs.json")
SRC_SOLOS = os.path.join(DECISIONS_DIR, "06c_solo_blogs.json")


# ══════════════════════════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _load_json(path: str) -> object:
    if not os.path.exists(path):
        log.error(f"  Source file not found: {path}")
        log.error("  Set DECISIONS_DIR in config.py to the folder containing the source files.")
        sys.exit(1)
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _new_url(new_slug: str, post_type: str = "page") -> str:
    """Build the full new-site URL from a slug.
    Pages: BASE/<slug>/   Posts: BASE/blog/<slug>/
    """
    if post_type == "page":
        return f"{BASE}/{new_slug}/"
    return f"{BASE}/blog/{new_slug}/"


def _old_url(url: str) -> str:
    """Normalise an old-site URL — just strips whitespace."""
    return url.strip()


# ══════════════════════════════════════════════════════════════════════════════
#  WP FETCH
# ══════════════════════════════════════════════════════════════════════════════

def fetch_all(endpoint: str) -> list[dict]:
    results = []
    for status in ("publish", "draft"):
        page = 1
        while True:
            try:
                resp = requests.get(
                    endpoint,
                    auth=AUTH,
                    params={"per_page": 100, "page": page, "status": status},
                    timeout=30,
                )
                if resp.status_code == 400:
                    break
                resp.raise_for_status()
                data = resp.json()
                if not isinstance(data, list) or not data:
                    break
                results.extend(data)
                if page >= int(resp.headers.get("X-WP-TotalPages", 1)):
                    break
                page += 1
            except Exception as e:
                log.error(f"  Fetch error {endpoint} status={status}: {e}")
                break
    return results


def fetch_wp_content() -> list[dict]:
    log.info("  Fetching pages from WP …")
    pages = fetch_all(f"{API}/pages")
    log.info(f"    {len(pages)} pages")

    log.info("  Fetching posts from WP …")
    posts = fetch_all(f"{API}/posts")
    log.info(f"    {len(posts)} posts")

    rows = []
    for item in pages:
        rows.append(_normalise(item, "page"))
    for item in posts:
        rows.append(_normalise(item, "post"))

    rows.sort(key=lambda r: (r["post_type"], r["title"].lower()))
    return rows


def _normalise(item: dict, post_type: str) -> dict:
    slug = item.get("slug", "")
    # Build link ourselves — don't trust WP's returned link after permalink changes
    if slug:
        link = _new_url(slug, post_type)
    else:
        link = item.get("link", "")
    return {
        "post_id":      item.get("id", ""),
        "post_type":    post_type,
        "current_slug": slug,
        "new_slug":     slug,
        "title":        (item.get("title") or {}).get("rendered", "").strip(),
        "status":       item.get("status", ""),
        "link":         link,
    }


# ══════════════════════════════════════════════════════════════════════════════
#  EXPORT
# ══════════════════════════════════════════════════════════════════════════════

def cmd_export():
    log.info("=" * 65)
    log.info("update_slugs — EXPORT")
    log.info("=" * 65)

    os.makedirs(config.OUTPUT_DIR, exist_ok=True)
    rows = fetch_wp_content()

    fieldnames = ["post_id", "post_type", "current_slug", "new_slug",
                  "title", "status", "link"]
    with open(EXPORT_CSV, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)

    log.info(f"\n  → {EXPORT_CSV}  ({len(rows)} rows)")
    log.info("\n  Edit the 'new_slug' column, then run:")
    log.info("    python update_slugs.py --apply")


# ══════════════════════════════════════════════════════════════════════════════
#  APPLY
# ══════════════════════════════════════════════════════════════════════════════

def patch_slug(post_id: int, post_type: str, new_slug: str) -> bool:
    endpoint = f"{API}/{'pages' if post_type == 'page' else 'posts'}/{post_id}"
    try:
        resp = requests.post(
            endpoint,
            json={"slug": new_slug},
            auth=AUTH,
            timeout=30,
        )
        resp.raise_for_status()
        actual = resp.json().get("slug", new_slug)
        if actual != new_slug:
            log.warning(f"    WP adjusted slug: '{new_slug}' → '{actual}' (collision?)")
        return True
    except requests.exceptions.HTTPError as e:
        log.error(f"    PATCH failed (ID {post_id}): {e} — {resp.text[:200]}")
    except Exception as e:
        log.error(f"    PATCH error (ID {post_id}): {e}")
    return False


def cmd_apply():
    log.info("=" * 65)
    log.info("update_slugs — APPLY")
    log.info("=" * 65)

    if not os.path.exists(EXPORT_CSV):
        log.error(f"  CSV not found: {EXPORT_CSV}")
        log.error("  Run --export first, edit the file, then --apply.")
        sys.exit(1)

    with open(EXPORT_CSV, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))

    to_update = []
    for row in rows:
        current = row.get("current_slug", "").strip()
        new     = row.get("new_slug", "").strip()
        new = re.sub(r"^blog-", "", new)
        if not new or new == current:
            continue
        to_update.append(row | {"new_slug": new})

    log.info(f"  {len(rows)} total rows — {len(to_update)} slugs to update")

    if not to_update:
        log.info("  Nothing to change. Edit new_slug column and try again.")
        sys.exit(0)

    slug_changes = []
    ok = failed = 0

    for row in to_update:
        pid     = int(row["post_id"])
        ptype   = row["post_type"].strip()
        current = row["current_slug"].strip()
        new     = row["new_slug"].strip()
        title   = row.get("title", "")

        log.info(f"\n  [{ptype}] {title}")
        log.info(f"    {current}  →  {new}")

        if patch_slug(pid, ptype, new):
            slug_changes.append({
                "post_id":   pid,
                "post_type": ptype,
                "old_slug":  current,
                "new_slug":  new,
                "old_url":   _new_url(current, ptype),
                "new_url":   _new_url(new, ptype),
                "title":     title,
            })
            ok += 1
        else:
            failed += 1

    log.info(f"\n  Updated: {ok}  Failed: {failed}")

    if slug_changes:
        os.makedirs(config.OUTPUT_DIR, exist_ok=True)
        fieldnames = ["post_id", "post_type", "old_slug", "new_slug",
                      "old_url", "new_url", "title"]
        with open(SLUG_CHANGES_CSV, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            w.writerows(slug_changes)
        log.info(f"  New-site slug changes → {SLUG_CHANGES_CSV}  ({len(slug_changes)} rows)")

    log.info("\n  Building old-site redirect map …")
    cmd_redirects()

    log.info("\n  Regenerating sitemaps …")
    cmd_sitemaps()


# ══════════════════════════════════════════════════════════════════════════════
#  REDIRECT MAP  (old franketobeyjones.com URLs → new site URLs)
# ══════════════════════════════════════════════════════════════════════════════

def cmd_redirects():
    log.info("=" * 65)
    log.info("update_slugs — REDIRECTS")
    log.info("=" * 65)

    rows: list[dict] = []

    # ── 1. Pages ──────────────────────────────────────────────────────────────
    pages_data = _load_json(SRC_PAGES)
    pages_list = pages_data.get("pages", pages_data) if isinstance(pages_data, dict) else pages_data
    page_count = 0
    for entry in pages_list:
        old  = _old_url(entry.get("url") or "")
        slug = (entry.get("new_slug") or "").strip()
        if not old or not slug:
            continue
        rows.append({"old_url": old, "new_url": _new_url(slug, "page"),
                     "new_slug": slug, "type": "page", "note": ""})
        page_count += 1
    log.info(f"  Pages: {page_count} redirects")

    # ── 2. Combined blogs ─────────────────────────────────────────────────────
    blogs_data = _load_json(SRC_BLOGS)
    groups = blogs_data.get("groups", blogs_data) if isinstance(blogs_data, dict) else blogs_data
    blog_combined_count = 0
    for group in groups:
        slug    = (group.get("combined_slug") or "").strip()
        members = group.get("members", [])
        if not slug or not members:
            continue
        new_url = _new_url(slug, "post")
        note    = "merged from: " + " | ".join(_old_url(m) for m in members)
        for member in members:
            old = _old_url(member)
            if not old:
                continue
            rows.append({"old_url": old, "new_url": new_url,
                         "new_slug": slug, "type": "blog-combined", "note": note})
            blog_combined_count += 1
    log.info(f"  Combined blog members: {blog_combined_count} redirects")

    # ── 3. Solo blogs ─────────────────────────────────────────────────────────
    solos_data = _load_json(SRC_SOLOS)
    solos_list = solos_data if isinstance(solos_data, list) else solos_data.get("posts", [])
    solo_count = 0
    for entry in solos_list:
        old  = _old_url(entry.get("url") or "")
        slug = (entry.get("new_slug") or "").strip()
        if not old or not slug:
            continue
        rows.append({"old_url": old, "new_url": _new_url(slug, "post"),
                     "new_slug": slug, "type": "blog-solo", "note": ""})
        solo_count += 1
    log.info(f"  Solo blogs: {solo_count} redirects")

    # ── Write CSV ──────────────────────────────────────────────────────────────
    os.makedirs(config.OUTPUT_DIR, exist_ok=True)
    fieldnames = ["old_url", "new_url", "new_slug", "type", "note"]
    with open(REDIRECT_CSV, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)

    total = page_count + blog_combined_count + solo_count
    log.info(f"\n  → {REDIRECT_CSV}  ({total} total rows)")
    log.info("  Done.")


# ══════════════════════════════════════════════════════════════════════════════
#  SITEMAPS
# ══════════════════════════════════════════════════════════════════════════════

def build_sitemap(items: list[dict], base_url: str) -> str:
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    for item in items:
        slug = item.get("slug", "")
        link = item.get("link", "")
        if slug and ("?p=" in link or "?page_id=" in link):
            link = f"{base_url}/{slug}/"
        if not link:
            continue
        modified = (item.get("modified") or item.get("date") or "")[:10]
        priority = "0.8" if item.get("status") == "publish" else "0.5"
        lines.append("  <url>")
        lines.append(f"    <loc>{link}</loc>")
        if modified:
            lines.append(f"    <lastmod>{modified}</lastmod>")
        lines.append(f"    <priority>{priority}</priority>")
        lines.append("  </url>")
    lines.append("</urlset>")
    return "\n".join(lines)


def build_index(page_url: str, blog_url: str) -> str:
    now = datetime.utcnow().strftime("%Y-%m-%d")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap>
    <loc>{page_url}</loc>
    <lastmod>{now}</lastmod>
  </sitemap>
  <sitemap>
    <loc>{blog_url}</loc>
    <lastmod>{now}</lastmod>
  </sitemap>
</sitemapindex>"""


def cmd_sitemaps():
    log.info("=" * 65)
    log.info("update_slugs — SITEMAPS")
    log.info("=" * 65)

    pages = fetch_all(f"{API}/pages")
    posts = fetch_all(f"{API}/posts")
    log.info(f"  Pages: {len(pages)}  Posts: {len(posts)}")

    os.makedirs(config.OUTPUT_DIR, exist_ok=True)

    with open(OUT_PAGES, "w", encoding="utf-8") as f:
        f.write(build_sitemap(pages, BASE))
    log.info(f"  → {OUT_PAGES}")

    with open(OUT_BLOGS, "w", encoding="utf-8") as f:
        f.write(build_sitemap(posts, BASE))
    log.info(f"  → {OUT_BLOGS}")

    page_url = f"{BASE}/sitemap_pages.xml"
    blog_url = f"{BASE}/sitemap_blogs.xml"
    with open(OUT_INDEX, "w", encoding="utf-8") as f:
        f.write(build_index(page_url, blog_url))
    log.info(f"  → {OUT_INDEX}")

    log.info("  Done.")


# ══════════════════════════════════════════════════════════════════════════════
#  AUDIT OLD SITE
# ══════════════════════════════════════════════════════════════════════════════

def _collect_all_old_urls() -> list[dict]:
    """Gather every old-site URL from the three JSON files + 01_pages.json."""
    entries: dict[str, dict] = {}

    data = _load_json(SRC_PAGES)
    for p in (data.get("pages", data) if isinstance(data, dict) else data):
        url  = (p.get("url") or "").strip()
        slug = (p.get("new_slug") or "").strip()
        if url:
            entries[url] = {"url": url, "new_slug": slug, "type": "page",
                            "source": "decisions_pages"}

    data = _load_json(SRC_BLOGS)
    for g in (data.get("groups", data) if isinstance(data, dict) else data):
        slug = (g.get("combined_slug") or "").strip()
        for m in g.get("members", []):
            url = (m or "").strip()
            if url:
                entries[url] = {"url": url, "new_slug": slug,
                                "type": "blog-combined", "source": "decisions_blogs"}

    data = _load_json(SRC_SOLOS)
    for p in (data if isinstance(data, list) else data.get("posts", [])):
        url  = (p.get("url") or "").strip()
        slug = (p.get("new_slug") or "").strip()
        if url:
            entries[url] = {"url": url, "new_slug": slug, "type": "blog-solo",
                            "source": "solo_blogs"}

    if os.path.exists(RAW_PAGES_JSON):
        with open(RAW_PAGES_JSON, encoding="utf-8") as f:
            raw = json.load(f)
        for r in raw:
            url = (r.get("url") or "").strip()
            if url and url not in entries:
                is_post = r.get("type") == "post"
                entries[url] = {"url": url, "new_slug": "",
                                "type": "blog-orphan" if is_post else "page-orphan",
                                "source": "01_pages"}

    return list(entries.values())


def _check_url(session, url: str) -> tuple[int, str]:
    try:
        resp = session.get(url, timeout=15, allow_redirects=True)
        return resp.status_code, resp.url
    except Exception as e:
        return 0, str(e)


def cmd_audit_old_site():
    log.info("=" * 65)
    log.info("update_slugs — AUDIT OLD SITE")
    log.info("=" * 65)

    entries = _collect_all_old_urls()
    log.info(f"  {len(entries)} unique old-site URLs to check …")
    log.info("  This may take a few minutes.\n")

    import time
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 (redirect-audit)"})

    results = []
    live    = []
    dead    = []

    for i, entry in enumerate(entries, 1):
        url = entry["url"]
        status, final_url = _check_url(session, url)
        row = entry | {"status": status, "final_url": final_url}
        results.append(row)

        if i % 25 == 0 or status not in (200, 301, 302):
            marker = "✓" if status == 200 else f"✗ {status}"
            log.info(f"  [{i}/{len(entries)}] {marker}  {url}")

        if status == 200:
            live.append(row)
        else:
            dead.append(row)

        time.sleep(0.1)

    log.info(f"\n  Live (200): {len(live)}   Dead/other: {len(dead)}")

    os.makedirs(config.OUTPUT_DIR, exist_ok=True)
    fieldnames = ["status", "type", "url", "new_slug", "final_url", "source"]

    with open(AUDIT_CSV, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(sorted(results, key=lambda r: r["url"]))
    log.info(f"  Full audit → {AUDIT_CSV}")

    with open(AUDIT_404_CSV, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(sorted(dead, key=lambda r: r["url"]))
    log.info(f"  Dead URLs  → {AUDIT_404_CSV}  ({len(dead)} rows)")
    log.info("  Done.")


# ══════════════════════════════════════════════════════════════════════════════
#  FALLBACK REDIRECTS
# ══════════════════════════════════════════════════════════════════════════════

def cmd_fallback_redirects():
    log.info("=" * 65)
    log.info("update_slugs — FALLBACK REDIRECTS")
    log.info("=" * 65)

    decided_urls: set[str] = set()

    data = _load_json(SRC_PAGES)
    for p in (data.get("pages", data) if isinstance(data, dict) else data):
        url = (p.get("url") or "").strip()
        if url:
            decided_urls.add(url)

    data = _load_json(SRC_BLOGS)
    for g in (data.get("groups", data) if isinstance(data, dict) else data):
        for m in g.get("members", []):
            if m.strip():
                decided_urls.add(m.strip())

    data = _load_json(SRC_SOLOS)
    for p in (data if isinstance(data, list) else data.get("posts", [])):
        url = (p.get("url") or "").strip()
        if url:
            decided_urls.add(url)

    log.info(f"  {len(decided_urls)} URLs already have explicit redirects")

    orphans: list[dict] = []
    if not os.path.exists(RAW_PAGES_JSON):
        log.warning(f"  Raw pages JSON not found: {RAW_PAGES_JSON}")
        log.warning("  Set RAW_PAGES_JSON in config.py")
    else:
        with open(RAW_PAGES_JSON, encoding="utf-8") as f:
            raw = json.load(f)
        for r in raw:
            url = (r.get("url") or "").strip()
            if not url or url in decided_urls:
                continue
            is_blog  = r.get("type") == "post"
            fallback = f"{BASE}/blog/" if is_blog else f"{BASE}/"
            orphans.append({
                "old_url":     url,
                "redirect_to": fallback,
                "type":        "blog-orphan" if is_blog else "page-orphan",
                "note":        "no decision found — redirects to "
                               + ("blog home" if is_blog else "site home"),
            })

    log.info(f"  {len(orphans)} orphaned URLs need fallback redirects")

    # Skip URLs already confirmed 404 on old site
    if os.path.exists(AUDIT_404_CSV):
        dead_urls: set[str] = set()
        with open(AUDIT_404_CSV, encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                dead_urls.add(row["url"].strip())
        before = len(orphans)
        orphans = [o for o in orphans if o["old_url"] not in dead_urls]
        log.info(f"  Skipped {before - len(orphans)} already-404 URLs (from audit)")

    os.makedirs(config.OUTPUT_DIR, exist_ok=True)
    fieldnames = ["old_url", "redirect_to", "type", "note"]
    with open(FALLBACK_CSV, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(sorted(orphans, key=lambda r: r["old_url"]))

    log.info(f"\n  → {FALLBACK_CSV}  ({len(orphans)} rows)")
    log.info("  Done.")


REDIRECTION_CSV = os.path.join(config.OUTPUT_DIR, "redirection_import.csv")


# ══════════════════════════════════════════════════════════════════════════════
#  EXPORT REDIRECTION PLUGIN CSV
# ══════════════════════════════════════════════════════════════════════════════

def cmd_export_redirection():
    log.info("=" * 65)
    log.info("update_slugs — EXPORT REDIRECTION")
    log.info("=" * 65)

    rows: list[dict] = []
    seen: set[str] = set()

    # ── 1. slug_redirects.csv (explicit old → new decisions) ──────────────────
    if not os.path.exists(REDIRECT_CSV):
        log.warning(f"  {REDIRECT_CSV} not found — run --redirects first")
    else:
        with open(REDIRECT_CSV, encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                source = "/" + row["old_url"].split("/", 3)[-1].rstrip("/") + "/"
                target = row["new_url"].rstrip("/") + "/"
                if source not in seen:
                    rows.append({"source": source, "target": target})
                    seen.add(source)
        log.info(f"  slug_redirects.csv: {len(rows)} rows")

    # ── 2. fallback_redirects.csv (orphans → blog home / site home) ───────────
    fallback_count = 0
    if not os.path.exists(FALLBACK_CSV):
        log.warning(f"  {FALLBACK_CSV} not found — run --fallback-redirects first")
    else:
        with open(FALLBACK_CSV, encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                source = "/" + row["old_url"].split("/", 3)[-1].rstrip("/") + "/"
                target = row["redirect_to"]
                if source not in seen:
                    rows.append({"source": source, "target": target})
                    seen.add(source)
                    fallback_count += 1
        log.info(f"  fallback_redirects.csv: {fallback_count} rows")

    if not rows:
        log.error("  No rows to export — run --redirects and --fallback-redirects first.")
        sys.exit(1)

    os.makedirs(config.OUTPUT_DIR, exist_ok=True)
    with open(REDIRECTION_CSV, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["source", "target"])
        w.writeheader()
        w.writerows(rows)

    log.info(f"\n  → {REDIRECTION_CSV}  ({len(rows)} total rows)")
    log.info("  Import this file into the Redirection plugin:")
    log.info("  WordPress Admin → Tools → Redirection → Import/Export → Import")
    log.info("  Done.")


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    ap = argparse.ArgumentParser(description="Review and update WordPress slugs")
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--export",             action="store_true",
                       help="Fetch all slugs from WP and write to output/slug_review.csv")
    group.add_argument("--apply",              action="store_true",
                       help="Patch WP slugs from edited CSV, rebuild redirect map, regenerate sitemaps")
    group.add_argument("--redirects",          action="store_true",
                       help="Build old-site → new-site redirect map from decision JSON files")
    group.add_argument("--sitemaps",           action="store_true",
                       help="Regenerate sitemaps only (no slug changes)")
    group.add_argument("--audit-old-site",     action="store_true",
                       help="Check every old-site URL — flags 404s vs live")
    group.add_argument("--fallback-redirects",  action="store_true",
                       help="Find orphaned old URLs and write catch-all redirects")
    group.add_argument("--export-redirection",  action="store_true",
                       help="Combine redirect map + fallbacks into Redirection plugin CSV")
    args = ap.parse_args()

    if args.export:
        cmd_export()
    elif args.apply:
        cmd_apply()
    elif args.redirects:
        cmd_redirects()
    elif args.sitemaps:
        cmd_sitemaps()
    elif args.audit_old_site:
        cmd_audit_old_site()
    elif args.fallback_redirects:
        cmd_fallback_redirects()
    elif args.export_redirection:
        cmd_export_redirection()


if __name__ == "__main__":
    main()
