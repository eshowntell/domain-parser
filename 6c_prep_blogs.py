#!/usr/bin/env python3
"""
6c_prep_blogs.py  —  Phase 2: Blog decision engine.

Reads blog_content_to_decide.csv and blog_urls_to_combine.csv, groups similar
posts by TF-IDF cosine similarity + title pattern matching, applies certainty
gates to auto-decide each group, then generates clean slugs via the Anthropic
API in batches.

CERTAINTY GATES (in order of priority)
───────────────────────────────────────
1. AUTO-ARCHIVE   : grade ❌ Archive  →  skip entirely, no rewrite
2. AUTO-SOLO      : grade ✅ Migrate OR sessions ≥ 50  →  rewrite individually
3. AUTO-COMBINE   : similarity ≥ COMBINE_THRESHOLD AND all members score ≤ COMBINE_MAX_SCORE
4. TEMPLATE-BATCH : ≥ TEMPLATE_MIN_SIZE titles match a template pattern  →  archive-keep-best
5. UNDECIDED      : everything else  →  written to undecided CSV for manual review

Outputs
───────
  data/06c_decisions_blogs.json    →  combine groups  (input for 6c_combine.py)
  data/06c_solo_blogs.json         →  individual rewrite list  (input for 6_rewrite.py)
  data/06c_undecided_blogs.csv     →  needs human review
  data/06c_archive_blogs.json      →  URLs to archive/skip

Usage
─────
  python 6c_prep_blogs.py                    # full run
  python 6c_prep_blogs.py --no-slugs         # skip API slug generation (faster, uses derived slugs)
  python 6c_prep_blogs.py --threshold 0.80   # override similarity threshold
  python 6c_prep_blogs.py --dry-run          # print decisions, write no files
"""

import argparse
import csv
import json
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime
from itertools import combinations
from urllib.parse import urlparse

import numpy as np
import requests
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
from tqdm import tqdm

sys.path.insert(0, os.path.dirname(__file__))
import config
from rewrite_utils import llm_complete
from utils import setup_logging

log = setup_logging("6c_prep_blogs")

# ── Paths ──────────────────────────────────────────────────────────────────────
BLOG_URLS_CSV    = "blog_urls_to_combine.csv"
BLOG_CONTENT_CSV = "blog_content_to_decide.csv"

OUT_DECISIONS    = os.path.join(config.DATA_DIR, "06c_decisions_blogs.json")
OUT_SOLO         = os.path.join(config.DATA_DIR, "06c_solo_blogs.json")
OUT_UNDECIDED    = os.path.join(config.DATA_DIR, "06c_undecided_blogs.csv")
OUT_ARCHIVE      = os.path.join(config.DATA_DIR, "06c_archive_blogs.json")

# ── Thresholds ──────────────────────────────────────────────────────────────────
COMBINE_THRESHOLD  = 0.82   # cosine similarity → auto-combine
COMBINE_MAX_SCORE  = 55     # both posts must score at or below this to auto-combine
AUTO_SOLO_SESSIONS = 50     # sessions ≥ this → always solo rewrite
TEMPLATE_MIN_SIZE  = 3      # ≥ this many template-matched titles → archive-keep-best
MIN_WORDS_VECTORIZE = 40    # skip posts with fewer words for TF-IDF

# ── Template patterns (monthly/recurring junk) ─────────────────────────────────
TEMPLATE_PATTERNS = [
    "featured apartment of the month",
    "apartment of the month",
    "featured apt of the month",
    "save the date",
    "save the dates",
    "senior university",
    "class sneak preview",
    "class highlights",
    "monthly highlights",
    "calendar highlights",
    "this month at",
    "senior center.*highlights",
    r"\w+ highlights$",
    "informational meetings",
    "parkside view.*update",
    "project update",
]

TEMPLATE_RE = [re.compile(p, re.IGNORECASE) for p in TEMPLATE_PATTERNS]


# ══════════════════════════════════════════════════════════════════════════════
#  COLUMN MAPS
# ══════════════════════════════════════════════════════════════════════════════

# blog_urls_to_combine.csv  (no header row)
URL_COL   = {
    "url":      0,
    "title":    1,
    "h1":       2,
    "type":     3,
    "status":   4,
    "grade":    5,
    "score":    6,
    "created":  7,
    "modified": 8,
    "words":    9,
    "images":   10,
    "int_links":11,
    "ext_links":12,
    "sessions": 13,
    "engaged":  14,
    "avg_time": 15,
    "pageviews":16,
    "sess_day": 17,
    "pv_90":    18,
    "slug":     19,
    "template": 20,
}

# blog_content_to_decide.csv  (no header row)
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
#  LOADERS
# ══════════════════════════════════════════════════════════════════════════════

def load_blog_urls() -> dict[str, dict]:
    """Returns {url: record} from blog_urls_to_combine.csv."""
    if not os.path.exists(BLOG_URLS_CSV):
        log.error(f"Not found: {BLOG_URLS_CSV}")
        sys.exit(1)
    records = {}
    with open(BLOG_URLS_CSV, encoding="utf-8-sig", newline="") as f:
        for row in csv.reader(f):
            if not row or len(row) < 10:
                continue
            url = row[URL_COL["url"]].strip().rstrip("/")
            if not url.startswith("http"):
                continue
            def _int(v):
                try: return int(float(str(v).replace(",", "") or 0))
                except: return 0
            def _float(v):
                try: return float(str(v).replace(",", "") or 0)
                except: return 0.0
            records[url] = {
                "url":      url,
                "title":    row[URL_COL["title"]].strip(),
                "grade":    row[URL_COL["grade"]].strip(),
                "score":    _int(row[URL_COL["score"]]),
                "modified": row[URL_COL["modified"]].strip(),
                "words":    _int(row[URL_COL["words"]]),
                "sessions": _int(row[URL_COL["sessions"]]),
                "engaged":  _int(row[URL_COL["engaged"]]),
                "avg_time": _float(row[URL_COL["avg_time"]]),
                "slug":     row[URL_COL["slug"]].strip() if len(row) > URL_COL["slug"] else "",
            }
    log.info(f"  Loaded {len(records)} blog URL records")
    return records


def load_blog_content() -> dict[str, dict]:
    """Returns {url: record} from blog_content_to_decide.csv."""
    if not os.path.exists(BLOG_CONTENT_CSV):
        log.error(f"Not found: {BLOG_CONTENT_CSV}")
        sys.exit(1)
    records = {}
    with open(BLOG_CONTENT_CSV, encoding="utf-8-sig", newline="") as f:
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
    log.info(f"  Loaded {len(records)} blog content records")
    return records


def merge_records(urls: dict, content: dict) -> list[dict]:
    """Merge URL metadata + content into unified records."""
    merged = []
    for url, u in urls.items():
        c = content.get(url, {})
        rec = {**u}
        rec["body_text"]  = c.get("body_text", "")
        rec["image_urls"] = c.get("image_urls", [])
        rec["video_urls"] = c.get("video_urls", [])
        rec["headings"]   = c.get("headings", "")
        merged.append(rec)
    # Also add any content-only records not in URL list
    for url, c in content.items():
        if url not in urls:
            merged.append({
                "url": url, "title": c.get("title", ""), "grade": "🟡 Review",
                "score": 0, "sessions": 0, "engaged": 0, "avg_time": 0.0,
                "words": len(c.get("body_text","").split()), "modified": "",
                "body_text": c.get("body_text",""), "image_urls": c.get("image_urls",[]),
                "video_urls": c.get("video_urls",[]), "headings": c.get("headings",""), "slug": "",
            })
    log.info(f"  Merged: {len(merged)} total blog records")
    return merged


# ══════════════════════════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def slug_from_url(url: str) -> str:
    path = urlparse(url).path.strip("/")
    if not path:
        return "post"
    last = path.rstrip("/").split("/")[-1]
    return re.sub(r"[^a-z0-9]+", "-", last.lower()).strip("-") or "post"


def is_template(title: str) -> bool:
    t = title.lower()
    return any(p.search(t) for p in TEMPLATE_RE)


def normalize_title(title: str) -> str:
    """Strip date-like suffixes and numbering for title similarity."""
    t = re.sub(r"\s*[–—:~|]\s*.*$", "", title).strip()
    t = re.sub(r"\s*#?\d+\s*$", "", t).strip()
    t = re.sub(r"\s*(january|february|march|april|may|june|july|august|september|october|november|december)\s*\d*", "", t, flags=re.IGNORECASE).strip()
    t = re.sub(r"\s*20\d\d\s*$", "", t).strip()
    return t.lower()


def title_similarity(a: str, b: str) -> float:
    """Simple word-overlap Jaccard similarity between two titles."""
    wa = set(normalize_title(a).split())
    wb = set(normalize_title(b).split())
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


def connected_components(pairs: list[tuple[int, int]]) -> list[list[int]]:
    parent: dict[int, int] = {}
    def find(x):
        if parent.setdefault(x, x) != x:
            parent[x] = find(parent[x])
        return parent[x]
    def union(a, b):
        parent[find(a)] = find(b)
    for a, b in pairs:
        union(a, b)
    groups: dict[int, list[int]] = {}
    for node in parent:
        root = find(node)
        groups.setdefault(root, []).append(node)
    return list(groups.values())


# ══════════════════════════════════════════════════════════════════════════════
#  SIMILARITY
# ══════════════════════════════════════════════════════════════════════════════

def compute_similarity_pairs(records: list[dict], threshold: float) -> list[dict]:
    """TF-IDF cosine similarity across all records with enough text."""
    eligible = [r for r in records if len(r.get("body_text","").split()) >= MIN_WORDS_VECTORIZE]
    log.info(f"  Vectorizing {len(eligible)} posts (≥{MIN_WORDS_VECTORIZE} words) …")
    if len(eligible) < 2:
        return []

    texts = [r["body_text"] for r in eligible]
    vec = TfidfVectorizer(
        min_df=2, max_df=0.95, ngram_range=(1,2),
        max_features=15_000, sublinear_tf=True, stop_words="english"
    )
    try:
        matrix = vec.fit_transform(texts)
    except ValueError:
        log.warning("  TF-IDF failed — too few unique terms")
        return []

    log.info(f"  Matrix: {matrix.shape}  threshold={threshold}")
    n = len(eligible)
    CHUNK = 100
    pairs = []

    for start in tqdm(range(0, n, CHUNK), desc="Comparing", leave=False):
        end = min(start + CHUNK, n)
        sims = cosine_similarity(matrix[start:end], matrix)
        for li, row in enumerate(sims):
            gi = start + li
            for gj in range(gi + 1, n):
                score = float(row[gj])
                if score >= threshold:
                    pairs.append({
                        "idx_a": gi, "idx_b": gj,
                        "url_a": eligible[gi]["url"],
                        "url_b": eligible[gj]["url"],
                        "score": round(score, 4),
                    })

    # Also catch near-identical titles that may have thin body text
    all_urls = {r["url"] for r in eligible}
    for r in records:
        if r["url"] in all_urls:
            continue
        for other in records:
            if other["url"] == r["url"] or other["url"] in all_urls:
                continue
            ts = title_similarity(r.get("title",""), other.get("title",""))
            if ts >= 0.80:
                pairs.append({
                    "idx_a": -1, "idx_b": -1,
                    "url_a": r["url"], "url_b": other["url"],
                    "score": round(ts, 4),
                })

    log.info(f"  Similar pairs: {len(pairs)}")
    return pairs


# ══════════════════════════════════════════════════════════════════════════════
#  SLUG GENERATION
# ══════════════════════════════════════════════════════════════════════════════

SLUG_SYSTEM = """You generate short, SEO-friendly WordPress URL slugs for blog posts.
Rules:
- Maximum 5 words, ideally 3-4
- Lowercase, hyphens only, no stop words (a, an, the, at, in, of, for, to, with)
- Keyword-rich: capture the core topic
- No dates, no numbers unless essential to meaning
- No duplicate slugs — each must be unique across the batch
Return ONLY a JSON array of slug strings in the same order as the input titles.
Example input: ["How Franke Tobey Jones Supports Memory Care Residents", "Annual Wellness Walk 2022"]
Example output: ["memory-care-resident-support", "annual-wellness-walk"]"""


def generate_slugs_batch(items: list[dict], batch_size: int = 25) -> dict[str, str]:
    """
    Send titles to Anthropic API in batches, get back short slugs.
    Returns {url: slug}.
    """
    url_to_slug = {}
    batches = [items[i:i+batch_size] for i in range(0, len(items), batch_size)]
    log.info(f"  Generating slugs: {len(items)} items in {len(batches)} batches …")

    for batch in tqdm(batches, desc="Slug gen", leave=False):
        titles = [b.get("title", "") or slug_from_url(b["url"]) for b in batch]
        prompt = (
            f"Generate unique slugs for these {len(titles)} blog post titles:\n"
            + json.dumps(titles, ensure_ascii=False)
        )
        try:
            text, _ = llm_complete(
                SLUG_SYSTEM, prompt, task="slug",
                max_tokens=1000, temperature=0.0, extract_slug=False,
            )
            text = (text or "").strip()
            # Strip fences
            text = re.sub(r"^```(?:json)?\s*\n?", "", text)
            text = re.sub(r"\n?```\s*$", "", text).strip()
            slugs = json.loads(text)
            if isinstance(slugs, list) and len(slugs) == len(batch):
                for item, slug in zip(batch, slugs):
                    clean = re.sub(r"[^a-z0-9-]", "", str(slug).lower().strip())
                    url_to_slug[item["url"]] = clean or slug_from_url(item["url"])
            else:
                log.warning(f"  Slug batch returned {len(slugs)} items for {len(batch)} titles — using derived")
                for item in batch:
                    url_to_slug[item["url"]] = slug_from_url(item["url"])
        except Exception as e:
            log.warning(f"  Slug batch error: {e} — using derived slugs")
            for item in batch:
                url_to_slug[item["url"]] = slug_from_url(item["url"])

        time.sleep(0.3)

    # Deduplicate: if two URLs got same slug, append suffix
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
#  DECISION ENGINE
# ══════════════════════════════════════════════════════════════════════════════

def apply_gates(
    records: list[dict],
    pairs: list[dict],
    threshold: float,
) -> tuple[list, list, list, list]:
    """
    Returns (combine_groups, solo_list, archive_list, undecided_list).
    """
    by_url = {r["url"]: r for r in records}

    # Build adjacency from pairs
    adj: dict[str, set[str]] = defaultdict(set)
    for p in pairs:
        if p["score"] >= threshold:
            adj[p["url_a"]].add(p["url_b"])
            adj[p["url_b"]].add(p["url_a"])

    # Connected components from pairs
    all_pair_urls = set(adj.keys())
    visited = set()
    raw_groups = []
    for seed in all_pair_urls:
        if seed in visited:
            continue
        group = set()
        stack = [seed]
        while stack:
            u = stack.pop()
            if u in visited:
                continue
            visited.add(u)
            group.add(u)
            stack.extend(adj[u] - visited)
        raw_groups.append(list(group))

    # Singles — not in any pair
    paired_urls = {u for g in raw_groups for u in g}
    singles = [r for r in records if r["url"] not in paired_urls]

    combine_groups = []
    solo_list      = []
    archive_list   = []
    undecided_list = []
    group_id       = 0

    # ── Process groups ──────────────────────────────────────────────────────
    for group_urls in raw_groups:
        members = [by_url[u] for u in group_urls if u in by_url]
        if not members:
            continue

        titles = [m["title"] for m in members]

        # Gate 1: any member is ❌ Archive → archive all
        archive_members = [m for m in members if "Archive" in m.get("grade","")]
        if len(archive_members) == len(members):
            for m in members:
                archive_list.append({"url": m["url"], "title": m["title"], "reason": "grade=Archive"})
            continue

        # Gate 2: template batch
        template_hits = [m for m in members if is_template(m["title"])]
        if len(template_hits) >= TEMPLATE_MIN_SIZE:
            best = max(members, key=lambda m: (m.get("sessions",0), m.get("score",0)))
            for m in members:
                if m["url"] != best["url"]:
                    archive_list.append({"url": m["url"], "title": m["title"],
                                         "reason": "template-batch", "survivor_url": best["url"]})
            solo_list.append({**best, "reason": "template-survivor"})
            continue

        # Gate 3: any member is ✅ Migrate or high-traffic → pull out as solo
        high_value = [m for m in members if "Migrate" in m.get("grade","") or m.get("sessions",0) >= AUTO_SOLO_SESSIONS]
        combine_candidates = [m for m in members if m not in high_value]
        for m in high_value:
            solo_list.append({**m, "reason": "high-value-from-group"})

        if not combine_candidates:
            continue

        # Gate 4: auto-combine — all candidates score ≤ threshold
        max_score = max(m.get("score",0) for m in combine_candidates)
        if max_score <= COMBINE_MAX_SCORE and len(combine_candidates) >= 2:
            group_id += 1
            primary = max(combine_candidates, key=lambda m: (m.get("sessions",0), m.get("score",0), m.get("words",0)))
            combine_groups.append({
                "group_id":       group_id,
                "combined_title": normalize_title(primary["title"]).title(),
                "combined_slug":  "",   # filled by slug generation pass
                "type":           "narrative" if any(
                    kw in " ".join(titles).lower()
                    for kw in ["resident", "staff", "story", "meet", "celebrat", "remember"]
                ) else "factual",
                "category":       "Uncategorized",
                "primary_url":    primary["url"],
                "members":        [m["url"] for m in combine_candidates],
                "member_scores":  {m["url"]: m.get("score",0) for m in combine_candidates},
                "member_sessions":{m["url"]: m.get("sessions",0) for m in combine_candidates},
            })
        else:
            # Undecided: group exists but auto-combine didn't fire
            for m in combine_candidates:
                undecided_list.append({**m,
                    "reason": f"group-no-gate  max_score={max_score}  size={len(combine_candidates)}"})

    # ── Process singles ──────────────────────────────────────────────────────
    for r in singles:
        grade = r.get("grade","")
        if "Archive" in grade:
            archive_list.append({"url": r["url"], "title": r["title"], "reason": "grade=Archive"})
        elif "Migrate" in grade or r.get("sessions",0) >= AUTO_SOLO_SESSIONS:
            solo_list.append({**r, "reason": "solo-high-value"})
        elif "Improve" in grade or "Review" in grade:
            # Low traffic singles: still worth rewriting
            solo_list.append({**r, "reason": "solo-rewrite"})
        else:
            undecided_list.append({**r, "reason": "single-no-gate"})

    log.info(f"  Combine groups:  {len(combine_groups)}")
    log.info(f"  Solo rewrites:   {len(solo_list)}")
    log.info(f"  Archive:         {len(archive_list)}")
    log.info(f"  Undecided:       {len(undecided_list)}")

    return combine_groups, solo_list, archive_list, undecided_list


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    ap = argparse.ArgumentParser(description="Blog decision engine for Phase 2")
    ap.add_argument("--threshold",  type=float, default=COMBINE_THRESHOLD,
                    help=f"Cosine similarity threshold for auto-combine (default {COMBINE_THRESHOLD})")
    ap.add_argument("--no-slugs",   action="store_true",
                    help="Skip Anthropic API slug generation (use URL-derived slugs)")
    ap.add_argument("--dry-run",    action="store_true",
                    help="Print decisions without writing output files")
    args = ap.parse_args()

    log.info("=" * 65)
    log.info("6c_prep_blogs — Blog decision engine")
    log.info("=" * 65)
    log.info(f"  combine threshold : {args.threshold}")
    log.info(f"  auto-solo sessions: ≥{AUTO_SOLO_SESSIONS}")
    log.info(f"  combine max score : ≤{COMBINE_MAX_SCORE}")
    log.info(f"  slug generation   : {'disabled' if args.no_slugs else 'enabled'}")

    # Load
    urls    = load_blog_urls()
    content = load_blog_content()
    records = merge_records(urls, content)

    # Similarity
    pairs = compute_similarity_pairs(records, args.threshold)

    # Decisions
    combine_groups, solo_list, archive_list, undecided_list = apply_gates(
        records, pairs, args.threshold
    )

    # Slug generation
    if not args.no_slugs:
        # Slugs for combine groups (use combined_title)
        slug_items_groups = [{"url": g["primary_url"], "title": g["combined_title"]}
                              for g in combine_groups]
        # Slugs for solo rewrites
        slug_items_solo   = [{"url": r["url"], "title": r.get("title","")} for r in solo_list]

        all_slug_items = slug_items_groups + slug_items_solo
        if all_slug_items:
            slug_map = generate_slugs_batch(all_slug_items)

            for g in combine_groups:
                g["combined_slug"] = slug_map.get(g["primary_url"], slug_from_url(g["primary_url"]))

            for r in solo_list:
                r["new_slug"] = slug_map.get(r["url"], slug_from_url(r["url"]))
        else:
            log.info("  Nothing to slugify")
    else:
        for g in combine_groups:
            if not g["combined_slug"]:
                g["combined_slug"] = slug_from_url(g["primary_url"])
        for r in solo_list:
            r["new_slug"] = slug_from_url(r["url"])

    if args.dry_run:
        log.info("\n── DRY RUN — no files written ──")
        log.info(f"Combine groups ({len(combine_groups)}):")
        for g in combine_groups[:5]:
            log.info(f"  [{g['group_id']}] {g['combined_title']} → /{g['combined_slug']}  ({len(g['members'])} members)")
        log.info(f"Solo rewrites ({len(solo_list)}):")
        for r in solo_list[:5]:
            log.info(f"  {r['title']} → /{r.get('new_slug','')}  ({r.get('reason','')})")
        log.info(f"Archive ({len(archive_list)}), Undecided ({len(undecided_list)})")
        return

    os.makedirs(config.DATA_DIR, exist_ok=True)
    os.makedirs(config.OUTPUT_DIR, exist_ok=True)

    # Write decisions JSON (input for 6c_combine.py)
    decisions = {
        "generated": datetime.utcnow().isoformat() + "Z",
        "site":      config.SITE_URL,
        "threshold": args.threshold,
        "groups":    combine_groups,
    }
    with open(OUT_DECISIONS, "w", encoding="utf-8") as f:
        json.dump(decisions, f, indent=2, ensure_ascii=False)
    log.info(f"  → {OUT_DECISIONS}  ({len(combine_groups)} groups)")

    # Write solo list JSON (input for 6_rewrite.py)
    with open(OUT_SOLO, "w", encoding="utf-8") as f:
        json.dump(solo_list, f, indent=2, ensure_ascii=False)
    log.info(f"  → {OUT_SOLO}  ({len(solo_list)} pages)")

    # Write archive JSON
    with open(OUT_ARCHIVE, "w", encoding="utf-8") as f:
        json.dump(archive_list, f, indent=2, ensure_ascii=False)
    log.info(f"  → {OUT_ARCHIVE}  ({len(archive_list)} pages)")

    # Write undecided CSV
    with open(OUT_UNDECIDED, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["url","title","grade","score","sessions","words","reason"])
        for r in undecided_list:
            w.writerow([r.get("url",""), r.get("title",""), r.get("grade",""),
                        r.get("score",""), r.get("sessions",""), r.get("words",""),
                        r.get("reason","")])
    log.info(f"  → {OUT_UNDECIDED}  ({len(undecided_list)} pages)")

    log.info("\n" + "=" * 65)
    log.info("Done. Next steps:")
    log.info("  1. Review data/06c_undecided_blogs.csv — add to solo or skip")
    log.info("  2. python 6c_combine.py --decisions data/06c_decisions_blogs.json --post")
    log.info("  3. python 6_rewrite.py  (reads 06c_solo_blogs.json automatically)")
    log.info("=" * 65)


if __name__ == "__main__":
    main()
