#!/usr/bin/env python3
"""
6c_combine.py  —  Phase 2: merge grouped blog posts into single articles.

Reads the curated decisions file produced by the 6c_review.html tool and, for
each group of 2+ posts, asks the Anthropic API to synthesise ONE cohesive,
de-duplicated article. Behaves like 6_rewrite.py: dry-run by default, --post to
publish drafts to the new site.

For every combined group it records a redirect map so the old (merged) URLs can
301 to the new combined URL later.

Posts that were NOT combined are left alone — run 6_rewrite.py for those. This
script writes data/06c_consumed.json (every URL that got merged) so 6_rewrite.py
can skip them (see the 3-line patch in the project notes).

Usage
-----
  python 6c_combine.py                 # dry-run: synthesise + save JSON only
  python 6c_combine.py --limit 1       # just the first group (good for testing)
  python 6c_combine.py --group 3       # only group_id 3 from the decisions file
  python 6c_combine.py --post          # synthesise AND post drafts to the new site
  python 6c_combine.py --force         # redo groups that already have a combine JSON
  python 6c_combine.py --source live   # pull source from production instead of local
  python 6c_combine.py --decisions path/to/06c_decisions.json

Outputs
-------
  data/06c_combine/<slug>.json   — one record per combined article
  output/06c_redirects.csv       — old_url → new combined URL (new_url editable)
  data/06c_consumed.json         — every merged source URL (skip-list for 6_rewrite)
"""

import argparse
import csv
import html
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
from utils import setup_logging
from rewrite_utils import (
    call_anthropic, extract_content_from_html, fetch_page_html,
    source_url_for, slug_from_url, validate_content, map_image,
    format_image_lines, core_block_reference,
)

log = setup_logging("6c_combine")

# ── Paths ─────────────────────────────────────────────────────
DATA_FILE     = os.path.join(config.DATA_DIR, "03_merged.json")
CONTENT_FILE  = os.path.join(config.DATA_DIR, "02_content.json")
DECISIONS     = os.path.join(config.DATA_DIR, "06c_decisions_blogs.json")   # default location
COMBINE_DIR   = os.path.join(config.DATA_DIR, "06c_combine")
CONSUMED_FILE = os.path.join(config.DATA_DIR, "06c_consumed.json")
REDIRECTS_CSV = os.path.join(config.OUTPUT_DIR, "06c_redirects.csv")
MEDIA_MAP     = os.path.join(config.DATA_DIR, "06_media_map.json")

COMBINE_MAX_TOKENS  = getattr(config, "PHASE2_COMBINE_MAX_TOKENS", 8000)
COMBINE_TEMPERATURE = getattr(config, "LLM_TEMPERATURE", 0.3)


# ╔══════════════════════════════════════════════════════════════╗
# ║  PROMPTS                                                      ║
# ╚══════════════════════════════════════════════════════════════╝

COMBINE_SYSTEM = f"""You are a content writer for Franke Tobey Jones (FTJ), a nonprofit \
senior living community at 5340 N Bristol St, Tacoma, WA 98407, helping migrate their \
website to a clean new WordPress build.

Your task: MERGE several related old blog posts into ONE polished article for the new \
site. Combine their material — do not just concatenate. De-duplicate overlapping points, \
keep every unique fact and story, and produce a single coherent piece organised under \
clear headings.

TONE
- Warm, clear, welcoming; the audience is seniors and adult children making housing decisions.
- Confident but not salesy; informative but not clinical.
- No keyword stuffing, no repetition, no filler.

FORMAT
Output ONLY WordPress Gutenberg block HTML — no preamble, no explanation, no markdown fences.
Use these blocks:
{core_block_reference()}

RULES
- Do NOT include the article title as an H1 — the theme renders that from the post title.
- Use H2 for the FIRST section heading only. Use H3 for all subsequent section headings.
- Do NOT include navigation, header, or footer content.
- DO preserve all factual details exactly: addresses, phone numbers, names, dates, amenities.
- If the article TYPE is "narrative", keep the human voice and the best stories/quotes intact.
- If the TYPE is "factual", organise as a clear, scannable explainer/guide.
- If image URLs are provided, place a few of the most relevant ones naturally in the flow \
using the wp:image block (use the URLs exactly as given).
- Output ONLY the Gutenberg block HTML."""

# ╔══════════════════════════════════════════════════════════════╗
# ║  HELPERS                                                      ║
# ╚══════════════════════════════════════════════════════════════╝

def load_media_map() -> dict:
    if os.path.exists(MEDIA_MAP):
        with open(MEDIA_MAP, encoding="utf-8") as f:
            return json.load(f)
    return {}

# slug_from_url, source_url_for, fetch_page_html, extract_content_from_html,
# map_image, call_anthropic → imported from rewrite_utils

# ╔══════════════════════════════════════════════════════════════╗
# ║  COMBINE ONE GROUP                                            ║
# ╚══════════════════════════════════════════════════════════════╝

def pick_primary(group: dict, members: list[dict], scorer) -> dict:
    """Resolve the primary member dict (decisions wins; else best score/traffic)."""
    want = (group.get("primary_url") or "").rstrip("/")
    for m in members:
        if m.get("url", "").rstrip("/") == want:
            return m

    def key(m):
        s = 0
        if scorer:
            try:
                s = scorer(m)[0]
            except Exception:
                s = 0
        return (s, int(m.get("sessions_lifetime", 0) or 0), int(m.get("word_count", 0) or 0))
    return max(members, key=key)


def build_combine_prompt(group: dict, members: list[dict],
                         live: dict[str, dict], media_map: dict) -> tuple[str, list[str]]:
    gtype = group.get("type", "factual")
    title = group.get("combined_title", "") or members[0].get("title", "")
    slug  = group.get("combined_slug", "") or slug_from_url(group.get("primary_url", ""))

    lines = [
        f"COMBINED ARTICLE TITLE: {title}",
        f"COMBINED SLUG: {slug}",
        f"ARTICLE TYPE: {gtype}",
        f"NUMBER OF SOURCE POSTS TO MERGE: {len(members)}",
        "",
        "Merge the following source posts into one article:",
    ]

    image_pool: list[tuple[str, str]] = []   # (new_url, id_str)

    for i, m in enumerate(members, start=1):
        url = m.get("url", "")
        content = live.get(url) or {}
        body = content.get("body_text") or m.get("body_text", "")
        words = body.split()
        if len(words) > 1800:
            body = " ".join(words[:1800]) + " […]"
        headings = content.get("headings") or m.get("headings", [])
        lines.append(f"\n----- SOURCE {i} -----")
        lines.append(f"OLD URL: {url}")
        lines.append(f"OLD TITLE: {m.get('title','')}")
        if headings:
            hs = "; ".join(h.get("text", "") for h in headings[:10] if h.get("text"))
            if hs:
                lines.append(f"HEADINGS: {hs}")
        lines.append(f"BODY ({len(body.split())} words): {body}")
        for u in m.get("image_urls", []):
            new_url, new_id = map_image(u, media_map)
            if new_url and (new_url, new_id) not in image_pool:
                image_pool.append((new_url, new_id))

    if image_pool:
        lines.append("\nAVAILABLE IMAGE URLS — include as wp:image blocks using BOTH the URL and ID provided:")
        for new_url, new_id in image_pool[:12]:
            if new_id:
                lines.append(f"  URL: {new_url}  ID: {new_id}")
            else:
                lines.append(f"  URL: {new_url}  (no ID — omit id attribute from block)")
        # NOTE: image_pool is already resolved (new_url, id) tuples — format_image_lines
        # is for raw old URLs. We keep the inline format here for backward compat.

    lines.append("\nWrite the single merged Gutenberg block HTML article now:")

    # Return just the URLs for the result record (backward compat)
    return "\n".join(lines), [u for u, _ in image_pool[:12]]

def combine_path(slug: str) -> str:
    return os.path.join(COMBINE_DIR, slug + ".json")


def post_combined(result: dict) -> tuple[int | None, str | None]:
    """POST a combined article to the new site. Returns (post_id, new_url)."""
    content = result.get("rewritten_content", "")
    ok, reason = validate_content(content)
    if not ok:
        log.error(f"    Refusing to post — {reason}")
        return None, None

    category_id = config.CATEGORY_MAP.get(result.get("category", "Uncategorized"), 1)
    endpoint = f"{config.NEW_SITE_URL.rstrip('/')}/wp-json/wp/v2/posts"
    clean_slug = re.sub(r"^blog-", "", (result.get("combined_slug") or "").strip("/"))
    payload = {
        "title":      result["combined_title"],
        "content":    result["rewritten_content"],
        "status":     config.PHASE2_POST_STATUS,
        "slug":       clean_slug,
        "categories": [category_id],
    }
    if result.get("date"):
        payload["date"] = result["date"]
    try:
        resp = requests.post(endpoint, json=payload,
                             auth=(config.NEW_WP_USERNAME, config.NEW_WP_APP_PASSWORD),
                             timeout=30)
        resp.raise_for_status()
        j = resp.json()
        pid = j.get("id")
        link = j.get("link")
        log.info(f"    → Posted '{result['combined_title']}'  (ID {pid})  {link}")
        return pid, link
    except requests.exceptions.HTTPError as e:
        log.error(f"    WP POST failed: {e} — {getattr(resp,'text','')[:300]}")
    except Exception as e:
        log.error(f"    WP POST error: {e}")
    return None, None

def write_preview_html(result: dict) -> str | None:
    """Write a browser-viewable HTML preview alongside the combine JSON.
    Open the .html in any browser to see exactly how the merged article reads —
    no copy-paste, no WP fiddling, no JSON wrapper noise."""
    slug = (result.get("combined_slug") or "preview").strip("/")
    slug = re.sub(r"^blog-", "", slug) or "preview"
    preview_path = os.path.join(COMBINE_DIR, slug + ".html")

    title   = html.escape(result.get("combined_title", "Untitled"))
    body    = result.get("rewritten_content", "")
    # Strip Gutenberg block comments so the preview renders as clean HTML
    body    = re.sub(r"<!-- /?wp:[^>]* -->", "", body).strip()
    members = result.get("member_urls", [])
    n       = len(members)
    words   = result.get("word_count_out", "")
    sources = "".join(
        f'<li><a href="{html.escape(u)}">{html.escape(u)}</a></li>' for u in members)
    posted_banner = ""
    if result.get("new_url"):
        posted_banner = (f'<div class="posted">✓ Posted as draft: '
                         f'<a href="{html.escape(result["new_url"])}">{html.escape(result["new_url"])}</a> '
                         f'(ID {result.get("post_id")})</div>')

    page = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>Preview · {title}</title>
<style>
  body {{ font: 16px/1.55 -apple-system, Segoe UI, Roboto, Arial, sans-serif;
         max-width: 760px; margin: 2rem auto; padding: 0 1.25rem; color: #1f2933; }}
  h1 {{ color: #0d4f3c; font-size: 2rem; line-height: 1.15; margin: 0 0 .25rem; }}
  h2 {{ color: #0d4f3c; font-size: 1.35rem; margin-top: 2rem;
        border-bottom: 2px solid rgba(13,79,60,.2); padding-bottom: 6px; }}
  p {{ margin: 0 0 1em; }}
  img {{ max-width: 100%; height: auto; margin: 1rem 0; border-radius: 4px;
         border: 1px solid #e4e7eb; background: #fafbfc; }}
  ul {{ padding-left: 1.5rem; }} li {{ margin: .25em 0; }}
  a {{ color: #1e6f55; }}
  figure {{ margin: 1.25rem 0; }}
  .meta {{ color: #555; font-size: 12.5px; background: #f4f6f8; padding: 10px 14px;
          border-radius: 6px; border: 1px solid #e4e7eb; margin: .25rem 0 1.5rem; }}
  .meta ul {{ margin: 6px 0 0; padding-left: 1.25rem; }}
  .meta code {{ background: #fff; padding: 1px 5px; border: 1px solid #e4e7eb; border-radius: 3px; }}
  .posted {{ background: #e8f5e9; border: 1px solid #c8e6c9; padding: 8px 12px;
            border-radius: 6px; color: #1b5e20; font-size: 13px; margin: 0 0 1.5rem; }}
</style></head>
<body>
<h1>{title}</h1>
<div class="meta">
  Combined from <b>{n}</b> source{('' if n == 1 else 's')} · slug <code>/{html.escape(slug)}</code> · <b>{words}</b> words<br>
  Sources:<ul>{sources}</ul>
</div>
{posted_banner}
{body}
</body></html>"""
    try:
        os.makedirs(COMBINE_DIR, exist_ok=True)
        with open(preview_path, "w", encoding="utf-8") as f:
            f.write(page)
        return preview_path
    except Exception as e:
        log.warning(f"  Could not write preview HTML: {e}")
        return None


def combine_group(group: dict, page_by_url: dict, media_map: dict,
                  scorer, source_base: str, dry_run: bool, force: bool) -> dict | None:
    gid     = group.get("group_id")
    member_urls = [u.rstrip("/") for u in group.get("members", [])]
    members = [page_by_url[u] for u in member_urls if u in page_by_url]
    missing = [u for u in member_urls if u not in page_by_url]
    if missing:
        log.warning(f"  Group {gid}: {len(missing)} member URL(s) not found in inventory: {missing[:3]}")
    if len(members) < 2:
        log.warning(f"  Group {gid}: fewer than 2 resolvable members — skipping.")
        return None

    primary = pick_primary(group, members, scorer)
    # Prefer the post's actual WP slug; only fall back to deriving from the URL.
    # Strip a stale 'blog-' prefix baked in by an older grouping pass.
    slug = (group.get("combined_slug")
            or primary.get("slug")
            or slug_from_url(primary.get("url", ""))).strip("/")
    slug = re.sub(r"^blog-", "", slug)
    title = group.get("combined_title") or primary.get("title", "Untitled")
    gtype = group.get("type", "factual")
    category = group.get("category") or primary.get("category", "Uncategorized")

    out_path = combine_path(slug)
    # Also accept an older 'blog-' prefixed filename, in case the JSON was saved
    # by a previous run before the slug cleanup landed.
    if not os.path.exists(out_path):
        legacy = combine_path("blog-" + slug)
        if os.path.exists(legacy):
            out_path = legacy

    if os.path.exists(out_path) and not force:
        with open(out_path, encoding="utf-8") as f:
            existing = json.load(f)
        # Defensive: clean a stale 'blog-' prefix in slugs from older runs
        old_slug = existing.get("combined_slug", "")
        new_slug = re.sub(r"^blog-", "", old_slug)
        if new_slug != old_slug:
            existing["combined_slug"] = new_slug
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(existing, f, indent=2, ensure_ascii=False)
            log.info(f"  Cleaned slug: '{old_slug}' → '{new_slug}'")
        preview = write_preview_html(existing)
        if preview:
            log.info(f"  Preview → {preview}")

        # If --post is on and this article was synthesized but never posted,
        # ship it now. No Anthropic call — content is already on disk.
        if not dry_run and not existing.get("post_id"):
            log.info(f"  Group {gid} '{title}': cached merge exists but unposted — posting now …")
            pid, link = post_combined(existing)
            if pid:
                existing["post_id"]   = pid
                existing["new_url"]   = link
                existing["posted_at"] = datetime.utcnow().isoformat() + "Z"
                with open(out_path, "w", encoding="utf-8") as f:
                    json.dump(existing, f, indent=2, ensure_ascii=False)
                write_preview_html(existing)
        elif existing.get("post_id"):
            log.info(f"  Group {gid} '{title}': already combined and posted "
                     f"(ID {existing['post_id']}) — skipping (use --force to redo).")
        else:
            log.info(f"  Group {gid} '{title}': already combined ({out_path}) — "
                     "skipping (dry-run; use --post to ship it).")
        return existing

    log.info(f"\n{'─'*55}")
    log.info(f"  Group {gid}: {title}  [{gtype}]  ({len(members)} posts → /{slug})")
    log.info(f"  Primary: {primary.get('url')}")

    # Fetch fresh source per member (best-effort), else use cached body_text
    live: dict[str, dict] = {}
    for m in members:
        url = m.get("url", "")
        page_html = fetch_page_html(source_url_for(url, source_base))
        if page_html:
            live[url] = extract_content_from_html(page_html)
    if live:
        log.info(f"  Fetched fresh content for {len(live)}/{len(members)} members")

    prompt, images_used = build_combine_prompt(group, members, live, media_map)
    log.info(f"  Calling {config.ANTHROPIC_MODEL} (max_tokens={COMBINE_MAX_TOKENS}) …")
    rewritten, _ = call_anthropic(
        COMBINE_SYSTEM, prompt,
        max_tokens=COMBINE_MAX_TOKENS,
        temperature=COMBINE_TEMPERATURE,
        extract_slug=False,
    )
    if not rewritten:
        log.error(f"  ✗ API returned nothing for group {gid} — skipping")
        return None
    log.info(f"  ✓ Received {len(rewritten.split())} words of Gutenberg HTML")

    # Publish date = primary post's original publish date (fallback: earliest
    # member). Newest modified date is kept separately for reference.
    created = primary.get("date_created", "") or min(
        [m.get("date_created", "") for m in members if m.get("date_created")] or [""])
    modified = max(
        [m.get("date_modified", "") for m in members if m.get("date_modified")] or [""])
    newest = created or modified
    if newest and len(newest) == 10:
        newest = newest + "T00:00:00"

    result = {
        "group_id":         gid,
        "combined_title":   title,
        "combined_slug":    slug,
        "type":             gtype,
        "category":         category,
        "primary_url":      primary.get("url", ""),
        "member_urls":      [m.get("url", "") for m in members],
        "date":             newest,
        "date_created":     created,
        "date_modified":    modified,
        "images_used":     images_used,
        "rewritten_content": rewritten,
        "post_id":          None,
        "new_url":          None,
        "posted_at":        None,
        "model":            config.ANTHROPIC_MODEL,
        "timestamp":        datetime.utcnow().isoformat() + "Z",
        "word_count_out":   len(rewritten.split()),
    }

    os.makedirs(COMBINE_DIR, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    log.info(f"  Saved → {out_path}")
    preview = write_preview_html(result)
    if preview:
        log.info(f"  Preview → {preview}")

    if not dry_run:
        pid, link = post_combined(result)
        if pid:
            result["post_id"]   = pid
            result["new_url"]   = link
            result["posted_at"] = datetime.utcnow().isoformat() + "Z"
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(result, f, indent=2, ensure_ascii=False)
            write_preview_html(result)   # refresh preview with the posted-banner

    time.sleep(0.5)
    return result


# ╔══════════════════════════════════════════════════════════════╗
# ║  REDIRECTS + CONSUMED                                         ║
# ╚══════════════════════════════════════════════════════════════╝

def write_consumed(actionable: list[dict]):
    consumed = sorted({u.rstrip("/") for g in actionable for u in g.get("members", [])})
    with open(CONSUMED_FILE, "w", encoding="utf-8") as f:
        json.dump(consumed, f, indent=2, ensure_ascii=False)
    log.info(f"  Consumed URLs (skip-list for 6_rewrite): {len(consumed)} → {CONSUMED_FILE}")


def write_redirects(actionable: list[dict]):
    """Build the redirect CSV from decisions + any saved combine results.

    Looks up each combine JSON by `group_id` (not by slug-based filename) so it
    keeps working when slugs get renamed (e.g. 'blog-foo' → 'foo') and when
    combine JSONs were saved under either spelling. The CSV emits the *clean*
    slug — whatever WP actually has — not whatever stale spelling the decisions
    file may carry.

    new_url is filled if the group was posted; otherwise blank for manual entry.
    """
    # Index every saved combine JSON by group_id
    results_by_gid: dict = {}
    if os.path.isdir(COMBINE_DIR):
        for fn in os.listdir(COMBINE_DIR):
            if not fn.endswith(".json"):
                continue
            try:
                with open(os.path.join(COMBINE_DIR, fn), encoding="utf-8") as f:
                    r = json.load(f)
                gid = r.get("group_id")
                if gid is not None:
                    results_by_gid[gid] = r
            except Exception:
                continue

    rows = [["old_url", "group_id", "role", "combined_title",
             "proposed_new_slug", "new_url", "redirect_type", "post_id", "notes"]]
    site = config.NEW_SITE_URL.rstrip("/")
    for g in actionable:
        gid = g.get("group_id")
        res = results_by_gid.get(gid) or {}
        # Prefer the combine JSON's slug (which fix_wp_slugs.py / the patched
        # combine code keeps clean). Fall back to the decision slug. Then strip
        # any 'blog-' that managed to survive either path.
        slug = (res.get("combined_slug") or g.get("combined_slug") or "").strip("/")
        slug = re.sub(r"^blog-", "", slug)
        post_id = res.get("post_id") or ""
        # WP returns '?p=N' for drafts, which is useless as a 301 target.
        # Predict the final pretty URL from the slug (assumes /%postname%/
        # permalink structure — change this line if yours differs).
        if post_id and slug:
            new_url = f"{site}/{slug}/"
            note    = "predicted from slug (post is currently a draft)"
        else:
            new_url = res.get("new_url") or ""
            note    = "" if new_url else "not yet posted"
        title   = g.get("combined_title", "")
        primary = (g.get("primary_url") or "").rstrip("/")
        for u in g.get("members", []):
            u = u.rstrip("/")
            role = "primary" if u == primary else "merged"
            rows.append([u, gid, role, title,
                         slug, new_url, "301", post_id, note])
    os.makedirs(config.OUTPUT_DIR, exist_ok=True)
    with open(REDIRECTS_CSV, "w", encoding="utf-8", newline="") as f:
        csv.writer(f).writerows(rows)
    log.info(f"  Redirect map: {len(rows)-1} rows → {REDIRECTS_CSV}")


# ╔══════════════════════════════════════════════════════════════╗
# ║  MAIN                                                         ║
# ╚══════════════════════════════════════════════════════════════╝

def load_scorer():
    try:
        from importlib import import_module
        return import_module("5_report").page_score
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser(description="Phase 2: combine grouped blog posts")
    ap.add_argument("--post",   action="store_true", help="POST combined drafts to the new site")
    ap.add_argument("--limit",  type=int, default=config.PHASE2_LIMIT, help="Max groups this run (0=all)")
    ap.add_argument("--group",  type=int, default=None, help="Only process this group_id")
    ap.add_argument("--force",  action="store_true", help="Redo groups that already have a combine JSON")
    ap.add_argument("--source", choices=["local", "live"], default=None, help="Source for fetching content")
    ap.add_argument("--decisions", default=DECISIONS, help="Path to the curated decisions JSON")
    args = ap.parse_args()

    source_base = config.SITE_URL if args.source == "live" else config.PHASE2_SOURCE_URL
    dry_run = not args.post
    if config.PHASE2_DRY_RUN and not args.post:
        dry_run = True

    log.info("=" * 65)
    log.info("STEP 6c — Combine grouped blog posts")
    log.info("=" * 65)
    log.info(f"  Decisions: {args.decisions}")
    log.info(f"  Source:    {source_base}")
    log.info(f"  New site:  {config.NEW_SITE_URL}")
    log.info(f"  Dry run:   {dry_run}  (pass --post to write drafts)")

    if not os.path.exists(args.decisions):
        log.error(f"Decisions file not found: {args.decisions}")
        log.error("Run 6c_group.py, curate in output/06c_review.html, then Export decisions JSON here.")
        sys.exit(1)
    with open(args.decisions, encoding="utf-8") as f:
        decisions = json.load(f)

    actionable = [g for g in decisions.get("groups", [])
                  if len([u for u in g.get("members", [])]) >= 2]
    log.info(f"  Groups in decisions: {len(decisions.get('groups', []))}  "
             f"(actionable, 2+ members: {len(actionable)})")

    # Always (re)write the skip-list + redirect scaffold so the rest of the
    # pipeline is consistent even before/while combining runs.
    write_consumed(actionable)

    # Load inventory
    src = DATA_FILE if os.path.exists(DATA_FILE) else CONTENT_FILE
    if not os.path.exists(src):
        log.error("No page inventory found. Run 2_extract.py first.")
        sys.exit(1)
    with open(src, encoding="utf-8") as f:
        pages = json.load(f)
    page_by_url = {p.get("url", "").rstrip("/"): p for p in pages}

    media_map = load_media_map()
    if media_map:
        log.info(f"  Media map: {len(media_map)} entries")
    scorer = load_scorer()

    # Select groups
    todo = actionable
    if args.group is not None:
        todo = [g for g in todo if g.get("group_id") == args.group]
        if not todo:
            log.error(f"group_id {args.group} not found among actionable groups.")
            sys.exit(1)
    if not args.force:
        # A group can be skipped only if its combine JSON exists AND either
        #   (a) post_id is already set (truly done), OR
        #   (b) we're in dry-run (no posting will happen anyway).
        # Otherwise, even with a cached JSON, keep it in todo so --post can ship it.
        kept = []
        skipped_done = 0
        skipped_dry  = 0
        for g in todo:
            decision_slug = (g.get("combined_slug") or "").strip("/")
            clean = re.sub(r"^blog-", "", decision_slug)
            existing_path = None
            for cand in (combine_path(clean), combine_path(decision_slug)):
                if cand and os.path.exists(cand):
                    existing_path = cand
                    break
            if not existing_path:
                kept.append(g); continue
            try:
                with open(existing_path, encoding="utf-8") as f:
                    rec = json.load(f)
            except Exception:
                kept.append(g); continue
            if rec.get("post_id"):
                skipped_done += 1   # already posted
                continue
            if dry_run:
                skipped_dry += 1    # cached, won't post anyway
                continue
            kept.append(g)          # cached but unposted, and --post is on
        todo = kept
        if (skipped_done or skipped_dry) and args.group is None:
            log.info(f"  Skipping {skipped_done} already-posted group(s).")
            if skipped_dry:
                log.info(f"  Skipping {skipped_dry} cached-but-unposted group(s) "
                         "(dry-run — use --post to ship them).")
    if args.limit and args.limit > 0:
        todo = todo[:args.limit]

    log.info(f"\nProcessing {len(todo)} group(s) …")
    ok = posted = failed = 0
    for g in tqdm(todo, desc="Combining", unit="grp"):
        res = combine_group(g, page_by_url, media_map, scorer,
                            source_base, dry_run, args.force)
        if res:
            ok += 1
            if res.get("post_id"):
                posted += 1
        else:
            failed += 1

    # Rebuild the redirect CSV from decisions + whatever results now exist
    write_redirects(actionable)

    log.info(f"\n{'='*65}")
    log.info("Combine complete")
    log.info(f"  Combined: {ok}")
    if not dry_run:
        log.info(f"  Posted:   {posted}")
    log.info(f"  Failed:   {failed}")
    if dry_run:
        log.info(f"\n  Dry-run — nothing posted. Review {COMBINE_DIR}/ then rerun with --post.")
    log.info(f"  Redirects: {REDIRECTS_CSV}")
    log.info("=" * 65)


if __name__ == "__main__":
    main()
