#!/usr/bin/env python3
"""
6c_group.py  —  Phase 2: review the TF-IDF clusters from script 4 and decide
                what to do with each one.

Why this replaces the old LLM-grouping pass
-------------------------------------------
Script 4 (TF-IDF cosine) already finds near-duplicate clusters very well — e.g.
the 25 "Featured Apartment of the Month" posts at 0.995 similarity. Re-asking
an LLM to rediscover those is wasted effort and (as we saw) flakey. This script
takes the existing 04_clusters.json as ground truth and lets you make one
DECISION per cluster:

    merge              → fold members into a single new article
    archive_keep_best  → keep the strongest member, 301-redirect the rest to it
    archive_all        → kill them all (no survivor; redirect target is up to you)
    skip               → leave the cluster alone

Rule-based defaults auto-flag templated junk (Apartment of the Month, Save the
Date, Senior University class previews, etc.) as `archive_keep_best`. You can
override any of it in the review tool.

Optional `--classify` adds ONE Anthropic API call per cluster to write a short
human-readable summary and a recommended action (cached on disk).

Inputs
------
  data/04_clusters.json       (required — from 4_similarity.py)
  data/03_merged.json         (required — for member metadata)
  data/06c_clusters/*.json    (optional — per-cluster LLM enrichment cache)

Outputs
-------
  data/06c_groupings.json     enriched clusters + heuristic recommendations
  output/06c_review.html      interactive decision tool

The review tool exports:
  06c_decisions.json          curated decisions → input for 6c_combine.py
  06c_archive.csv             URLs flagged for archive (with survivors when known)
"""

import argparse
import json
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime
from urllib.parse import urlparse

import requests
from tqdm import tqdm

sys.path.insert(0, os.path.dirname(__file__))
import config
from rewrite_utils import llm_complete
from utils import setup_logging

log = setup_logging("6c_group")

# ── Paths ─────────────────────────────────────────────────────
CLUSTERS_IN   = os.path.join(config.DATA_DIR, "04_clusters.json")
MERGED_IN     = os.path.join(config.DATA_DIR, "03_merged.json")
CONTENT_IN    = os.path.join(config.DATA_DIR, "02_content.json")
CLUSTER_CACHE = os.path.join(config.DATA_DIR, "06c_clusters")
GROUPINGS     = os.path.join(config.DATA_DIR, "06c_groupings.json")
REVIEW_HTML   = os.path.join(config.OUTPUT_DIR, "06c_review.html")

CLASSIFY_MAX_TOKENS  = getattr(config, "PHASE2_CLASSIFY_MAX_TOKENS", 600)
CLASSIFY_TEMPERATURE = getattr(config, "PHASE2_TAG_TEMPERATURE", 0.0)

# ── Title patterns that scream "templated monthly post — archive the batch" ──
TEMPLATE_PATTERNS = [
    "featured apartment",
    "apartment of the month",
    "save the date",
    "save the dates",
    "senior university",
    "class sneak preview",
    "class highlights",
    "monthly highlights",
    "calendar highlights",
    "this month at",
    "happenings",
]

ARCHIVE_THRESHOLD_SIZE = 6      # clusters bigger than this default to archive
MERGE_MAX_SIZE         = 5      # clusters at/below this default to merge


# ╔══════════════════════════════════════════════════════════════╗
# ║  HELPERS                                                      ║
# ╚══════════════════════════════════════════════════════════════╝

def slug_from_url(url: str) -> str:
    path = urlparse(url).path.strip("/")
    if not path:
        return "homepage"
    return re.sub(r"[^a-z0-9]+", "-", path.lower()).strip("-")


def load_scorer():
    try:
        from importlib import import_module
        return import_module("5_report").page_score
    except Exception:
        return None


def matches_template(title: str) -> str | None:
    """Return the matched template phrase if the title looks like a templated
    monthly post, else None."""
    t = (title or "").lower()
    for pat in TEMPLATE_PATTERNS:
        if pat in t:
            return pat
    return None


def cluster_template_hit_ratio(titles: list[str]) -> tuple[float, str | None]:
    hits = [matches_template(t) for t in titles]
    matched = [h for h in hits if h]
    if not titles:
        return 0.0, None
    ratio = len(matched) / len(titles)
    common = Counter(matched).most_common(1)
    return ratio, (common[0][0] if common else None)


def pick_primary(members: list[dict]) -> dict:
    """Strongest member: page_score → sessions → words → most recent."""
    return max(members, key=lambda m: (
        m.get("page_score") or 0,
        int(m.get("sessions_lifetime", 0) or 0),
        int(m.get("word_count", 0) or 0),
        m.get("date_modified", "") or "",
    ))


def majority_type(members: list[dict], default: str = "factual") -> str:
    types = [m.get("tag_type") for m in members if m.get("tag_type")]
    if not types:
        return default
    return Counter(types).most_common(1)[0][0]


# ╔══════════════════════════════════════════════════════════════╗
# ║  LOAD + ENRICH CLUSTERS                                       ║
# ╚══════════════════════════════════════════════════════════════╝

def load_inventory() -> dict[str, dict]:
    src = MERGED_IN if os.path.exists(MERGED_IN) else CONTENT_IN
    if not os.path.exists(src):
        log.error("No page inventory found. Run 2_extract.py first.")
        sys.exit(1)
    with open(src, encoding="utf-8") as f:
        pages = json.load(f)
    log.info(f"  Inventory: {len(pages)} pages from {src}")
    return {p.get("url", "").rstrip("/"): p for p in pages}


def load_cached_tags() -> dict[str, dict]:
    """Pick up any per-post tags from a previous run — optional bonus context."""
    tag_dir = os.path.join(config.DATA_DIR, "06c_tags")
    out = {}
    if not os.path.isdir(tag_dir):
        return out
    for fn in os.listdir(tag_dir):
        if fn.endswith(".json"):
            try:
                with open(os.path.join(tag_dir, fn), encoding="utf-8") as f:
                    rec = json.load(f)
                if rec.get("url"):
                    out[rec["url"].rstrip("/")] = rec
            except Exception:
                continue
    if out:
        log.info(f"  Re-using {len(out)} cached per-post tag records")
    return out


def enrich_member(url: str, inv: dict[str, dict], tags: dict[str, dict],
                  scorer) -> dict | None:
    key = url.rstrip("/")
    page = inv.get(key)
    if not page:
        return None
    rec = {
        "url":           page.get("url", url),
        "title":         page.get("title", "") or page.get("h1_best", ""),
        "type":          page.get("type", ""),                # WP type (post/page)
        "wp_status":     page.get("status", ""),
        "date_modified": page.get("date_modified", ""),
        "date_created":  page.get("date_created", ""),
        "word_count":    int(page.get("word_count", 0) or 0),
        "sessions_lifetime":  int(page.get("sessions_lifetime", 0) or 0),
        "pageviews_lifetime": int(page.get("pageviews_lifetime", 0) or 0),
        "category":      page.get("category", ""),
        "slug":          page.get("slug", "") or slug_from_url(url),
        "h1_best":       page.get("h1_best", ""),
        "excerpt":       (page.get("excerpt") or "")[:200],
    }
    # tag enrichment (topic / narrative-vs-factual / keywords) — optional
    tag = tags.get(key)
    if tag:
        rec.update({
            "topic":    tag.get("topic", ""),
            "subject":  tag.get("subject", ""),
            "keywords": tag.get("keywords", []),
            "tag_type": tag.get("type", ""),   # narrative|factual
            "summary":  tag.get("summary", ""),
        })
    # page_score from 5_report
    if scorer:
        try:
            rec["page_score"] = scorer(page)[0]
        except Exception:
            rec["page_score"] = None
    return rec


def recommend_action(cluster_size: int, max_sim: float,
                     template_ratio: float) -> tuple[str, str]:
    """Heuristic recommendation. Returns (action, rationale)."""
    if template_ratio >= 0.5 and cluster_size >= 3:
        return "archive_keep_best", (
            f"{int(template_ratio*100)}% of titles match a templated pattern — "
            "looks like monthly/recurring content. Keep the strongest as a sample, "
            "archive the rest.")
    if cluster_size >= ARCHIVE_THRESHOLD_SIZE:
        return "archive_keep_best", (
            f"Large cluster ({cluster_size} pages) — usually means recurring "
            "content. Keep the best, archive the rest.")
    if cluster_size <= MERGE_MAX_SIZE and max_sim >= 0.90:
        return "merge", (
            f"Small near-duplicate group ({cluster_size} pages, max similarity "
            f"{max_sim:.2f}) — strong merge candidate.")
    if cluster_size <= MERGE_MAX_SIZE:
        return "merge", (
            f"Small cluster ({cluster_size} pages, max similarity {max_sim:.2f}) "
            "— likely complementary content worth merging.")
    return "merge", f"Medium cluster ({cluster_size} pages)."


def enrich_clusters(clusters: list[dict], inv: dict[str, dict],
                    tags: dict[str, dict], scorer) -> list[dict]:
    enriched = []
    unresolved = 0
    for c in clusters:
        members = []
        for u in c.get("urls", []):
            m = enrich_member(u, inv, tags, scorer)
            if m:
                members.append(m)
            else:
                unresolved += 1
        if not members:
            continue

        type_counts = Counter(m["type"] for m in members if m["type"])
        dominant_wp_type = type_counts.most_common(1)[0][0] if type_counts else ""
        primary = pick_primary(members)
        ratio, template = cluster_template_hit_ratio([m["title"] for m in members])
        action, rationale = recommend_action(len(members),
                                             c.get("max_similarity", 0), ratio)
        total_sessions = sum(m["sessions_lifetime"] for m in members)

        # propose a combined title/slug from the strongest member
        combined_title = re.sub(r"\s*[–—:~].*$", "", primary["title"]).strip() \
                         or primary["title"]
        combined_slug = primary["slug"] or slug_from_url(primary["url"])

        enriched.append({
            "cluster_id":      c.get("cluster_id"),
            "page_count":      len(members),
            "max_similarity":  c.get("max_similarity", 0),
            "dominant_type":   dominant_wp_type,
            "template_pattern": template,
            "template_ratio":  round(ratio, 2),
            "total_sessions":  total_sessions,
            "primary_url":     primary["url"],
            "recommended_action":   action,
            "recommended_rationale": rationale,
            "combined_title":  combined_title,
            "combined_slug":   combined_slug,
            "merge_type":      majority_type(members, "factual"),
            "members":         members,
            # filled in by --classify
            "ai_summary":      None,
            "ai_recommendation": None,
            "ai_rationale":    None,
            "ai_is_templated": None,
        })
    if unresolved:
        log.warning(f"  {unresolved} cluster member URL(s) had no inventory match.")
    return enriched


# ╔══════════════════════════════════════════════════════════════╗
# ║  OPTIONAL — LLM per-cluster summary                           ║
# ╚══════════════════════════════════════════════════════════════╝

CLASSIFY_SYSTEM = """You are auditing the blog of Franke Tobey Jones (FTJ), a nonprofit \
senior living community. You are given several blog posts that have been flagged as \
near-duplicates of each other. Decide what to do with the cluster.

Return ONLY a JSON object of this exact shape:
{
  "theme": "what these posts are about (3-6 words)",
  "is_templated": true | false,
  "recommended_action": "merge" | "archive_keep_best" | "archive_all" | "skip",
  "rationale": "one sentence explaining the recommendation"
}

Guidance:
- "archive_keep_best" = obvious monthly/recurring template content (e.g. Apartment
  of the Month, monthly highlights). Keep the strongest as a sample, archive rest.
- "archive_all" = recurring AND none worth keeping (e.g. very old event reminders).
- "merge" = small group of substantive posts on the same topic that could become
  one stronger article.
- "skip" = the posts are similar by accident; leave each as its own page."""


def anthropic_message(system: str, prompt: str, max_tokens: int,
                      temperature: float) -> str | None:
    text, _ = llm_complete(
        system, prompt, task="classify",
        max_tokens=max_tokens, temperature=temperature, extract_slug=False,
    )
    return text


def parse_json(text: str):
    if not text:
        return None
    t = re.sub(r"^```(?:json)?", "", text.strip()).strip()
    t = re.sub(r"```$", "", t).strip()
    try:
        return json.loads(t)
    except Exception:
        i, j = t.find("{"), t.rfind("}")
        if i != -1 and j != -1:
            try: return json.loads(t[i:j+1])
            except Exception: return None
    return None


def classify_cluster(cluster: dict, force: bool) -> dict | None:
    cid = cluster["cluster_id"]
    cache = os.path.join(CLUSTER_CACHE, f"cluster_{cid:03d}.json")
    if os.path.exists(cache) and not force:
        with open(cache, encoding="utf-8") as f:
            return json.load(f)

    members = cluster["members"]
    lines = [f"Cluster of {len(members)} similar posts (max similarity "
             f"{cluster['max_similarity']:.2f}):\n"]
    for i, m in enumerate(members, 1):
        snippet = (m.get("excerpt") or "")[:160]
        lines.append(f"\n[{i}] TITLE: {m['title']}")
        lines.append(f"    URL: {m['url']}")
        lines.append(f"    DATE: {m.get('date_modified','?')}  WORDS: {m['word_count']}  "
                     f"SESSIONS: {m['sessions_lifetime']}")
        if snippet:
            lines.append(f"    EXCERPT: {snippet}")
    lines.append("\nClassify the cluster now. Return ONLY the JSON object.")
    raw = anthropic_message(CLASSIFY_SYSTEM, "\n".join(lines),
                            CLASSIFY_MAX_TOKENS, CLASSIFY_TEMPERATURE)
    result = parse_json(raw)
    if not isinstance(result, dict):
        log.warning(f"  cluster {cid}: classify response not parseable")
        return None
    os.makedirs(CLUSTER_CACHE, exist_ok=True)
    with open(cache, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    return result


def run_classification(clusters: list[dict], force: bool, limit: int):
    os.makedirs(CLUSTER_CACHE, exist_ok=True)
    todo = clusters[:limit] if limit else clusters
    log.info(f"  Classifying {len(todo)} cluster(s) via {config.ANTHROPIC_MODEL} …")
    for c in tqdm(todo, desc="Classify", unit="clust"):
        rec = classify_cluster(c, force)
        if rec:
            c["ai_summary"]        = rec.get("theme")
            c["ai_recommendation"] = rec.get("recommended_action")
            c["ai_rationale"]      = rec.get("rationale")
            c["ai_is_templated"]   = bool(rec.get("is_templated"))
            if rec.get("recommended_action") in {"merge", "archive_keep_best",
                                                 "archive_all", "skip"}:
                c["recommended_action"]    = rec["recommended_action"]
                c["recommended_rationale"] = rec.get("rationale",
                                                     c["recommended_rationale"])
        time.sleep(0.2)


# ╔══════════════════════════════════════════════════════════════╗
# ║  REVIEW TOOL (standalone HTML)                                ║
# ╚══════════════════════════════════════════════════════════════╝

def build_review_html(groupings: dict) -> str:
    categories = list(config.CATEGORY_MAP.keys())
    data_json = json.dumps(groupings, ensure_ascii=False).replace("</", "<\\/")
    cats_json = json.dumps(categories, ensure_ascii=False).replace("</", "<\\/")
    html = _REVIEW_TEMPLATE
    html = html.replace("__DATA__", data_json)
    html = html.replace("__CATEGORIES__", cats_json)
    html = html.replace("__GENERATED__", groupings.get("generated", ""))
    html = html.replace("__SITE__", groupings.get("site", ""))
    return html


_REVIEW_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FTJ Cluster Decisions</title>
<style>
  :root{
    --bg:#f4f6f8; --card:#fff; --ink:#1f2933; --muted:#7b8794;
    --line:#e4e7eb; --accent:#2f6f9f; --accent-d:#23527c;
    --merge:#2e7d32; --archive:#c0392b; --skip:#7b8794; --warn:#b7791f;
    --narr:#8e44ad; --fact:#1f8a70;
  }
  *{box-sizing:border-box}
  body{margin:0;font:14px/1.5 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
       background:var(--bg);color:var(--ink)}
  header{position:sticky;top:0;z-index:30;background:var(--card);
         border-bottom:1px solid var(--line);padding:10px 18px;
         display:flex;flex-wrap:wrap;gap:8px;align-items:center}
  header h1{font-size:15px;margin:0 10px 0 0}
  header .meta{color:var(--muted);font-size:12px;margin-right:auto}
  button{font:inherit;border:1px solid var(--line);background:var(--card);
         color:var(--ink);padding:6px 10px;border-radius:6px;cursor:pointer}
  button:hover{background:#eef2f5}
  button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
  button.primary:hover{background:var(--accent-d)}
  input[type=search], input[type=text], select{
    font:inherit;padding:5px 8px;border:1px solid var(--line);border-radius:6px}
  .wrap{max-width:1200px;margin:14px auto;padding:0 16px}
  .filters{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:14px;
           background:var(--card);padding:10px 12px;border:1px solid var(--line);border-radius:8px}
  .filters label{font-size:12px;color:var(--muted);display:flex;align-items:center;gap:5px}
  .cluster{background:var(--card);border:1px solid var(--line);border-radius:10px;
           margin-bottom:14px;overflow:hidden}
  .cluster.hidden{display:none}
  .chead{display:flex;flex-wrap:wrap;gap:8px;align-items:center;
         padding:10px 14px;background:#fafbfc;border-bottom:1px solid var(--line)}
  .chead .cid{font-weight:700;color:var(--accent)}
  .pill{font-size:10.5px;padding:2px 8px;border-radius:999px;color:#fff;
        text-transform:uppercase;letter-spacing:.03em;font-weight:700}
  .pill.merge{background:var(--merge)} .pill.archive_keep_best{background:var(--archive)}
  .pill.archive_all{background:#7a1a12} .pill.skip{background:var(--skip)}
  .pill.template{background:var(--warn)}
  .pill.size{background:#374151} .pill.sim{background:#1e3a8a}
  .pill.post{background:#2563eb} .pill.page{background:#0891b2}
  .pill.narrative{background:var(--narr)} .pill.factual{background:var(--fact)}
  .rationale{padding:6px 14px;color:var(--muted);font-size:12px;font-style:italic;
             border-bottom:1px solid var(--line);background:#fcfcfd}
  .actions{display:flex;flex-wrap:wrap;gap:14px;padding:8px 14px;
           border-bottom:1px solid var(--line);background:#f8fafc;font-size:13px}
  .actions label{display:flex;align-items:center;gap:4px;cursor:pointer}
  .body{display:none}
  .cluster.open .body{display:block}
  .toggle{margin-left:auto;background:transparent;border:none;cursor:pointer;
          font-size:18px;color:var(--muted);padding:0 6px}
  .merge-fields{display:none;padding:8px 14px;gap:10px;flex-wrap:wrap;
                background:#f0f7f1;border-bottom:1px solid #d4e6d6;font-size:12.5px}
  .cluster.action-merge .merge-fields{display:flex;align-items:center}
  .merge-fields input[type=text]{font-size:12.5px}
  .merge-fields .title-in{min-width:240px;flex:1}
  .merge-fields .slug-in{min-width:160px}
  table{width:100%;border-collapse:collapse}
  td,th{padding:7px 10px;border-top:1px solid var(--line);vertical-align:top;font-size:12.5px}
  th{font-size:10.5px;text-transform:uppercase;color:var(--muted);text-align:left;
     font-weight:600;background:#fafbfc;position:sticky;top:0}
  td.c{text-align:center;white-space:nowrap}
  .ptitle{font-weight:600;color:var(--ink)}
  .ptitle a{color:var(--ink);text-decoration:none}
  .ptitle a:hover{text-decoration:underline}
  .purl{color:var(--muted);font-size:11px;word-break:break-all}
  tr.excluded td{opacity:.4;text-decoration:line-through}
  tr.survivor td{background:#f0f7f1}
  .note{background:#fff8e1;border:1px solid #ffe082;border-radius:8px;
        padding:10px 12px;margin-bottom:12px;font-size:13px}
  .summary{display:flex;gap:14px;flex-wrap:wrap;font-size:13px;color:var(--muted)}
  .summary b{color:var(--ink)}
  .ai-tag{display:inline-block;font-size:11px;padding:2px 6px;border-radius:4px;
          background:#eef2ff;color:#3730a3;margin-left:6px}
  footer{max-width:1200px;margin:24px auto;padding:0 16px;color:var(--muted);font-size:12px}
</style>
</head>
<body>
<header>
  <h1>FTJ Cluster Decisions</h1>
  <span class="meta">site: __SITE__ · generated: __GENERATED__ · <span id="summary"></span></span>
  <button onclick="resetAll()">Reset to recommended</button>
  <button onclick="expandAll(true)">Expand all</button>
  <button onclick="expandAll(false)">Collapse all</button>
  <button onclick="importDecisions()">Import…</button>
  <button onclick="exportArchive()">Export archive CSV</button>
  <button class="primary" onclick="exportJSON()">Export decisions JSON</button>
  <input type="file" id="importer" accept=".json" style="display:none">
</header>

<div class="wrap">
  <div class="note">
    Each cluster came from script 4's TF-IDF similarity pass. Decide what to do with each:
    <b>merge</b> (one new article), <b>archive (keep best)</b> (keep the survivor, 301 the rest to it),
    <b>archive all</b> (kill them all), or <b>skip</b>. Defaults are heuristic — anything templated
    (Apartment of the Month, Save the Date, etc.) is pre-flagged for archive. Override freely,
    then export the decisions JSON for <code>6c_combine.py</code>, and the archive CSV for your
    WordPress redirect / trash workflow.
  </div>

  <div class="filters">
    <label>Search <input type="search" id="search" placeholder="title or URL"
        oninput="render()"></label>
    <label>Action
      <select id="filterAction" onchange="render()">
        <option value="">All</option>
        <option value="merge">Merge</option>
        <option value="archive_keep_best">Archive (keep best)</option>
        <option value="archive_all">Archive all</option>
        <option value="skip">Skip</option>
      </select>
    </label>
    <label>Type
      <select id="filterType" onchange="render()">
        <option value="">All</option>
        <option value="post">Posts only</option>
        <option value="page">Pages only</option>
      </select>
    </label>
    <label>Min size
      <select id="filterSize" onchange="render()">
        <option value="0">Any</option>
        <option value="3">≥3</option>
        <option value="5">≥5</option>
        <option value="8">≥8</option>
      </select>
    </label>
    <label>Templated only
      <input type="checkbox" id="filterTemplate" onchange="render()">
    </label>
  </div>

  <div id="clusters"></div>
</div>

<footer>Standalone file — nothing leaves your machine. Use Import to resume from a saved decisions JSON.</footer>

<script id="payload" type="application/json">__DATA__</script>
<script id="cats" type="application/json">__CATEGORIES__</script>
<script>
const DATA = JSON.parse(document.getElementById('payload').textContent);
const CATS = JSON.parse(document.getElementById('cats').textContent);

// state per cluster: { action, primary_url, combined_title, combined_slug,
//                      merge_type, category, kept:Set<url>, open:bool }
let state = DATA.clusters.map(c => ({
  cluster_id: c.cluster_id,
  action: c.recommended_action,
  primary_url: c.primary_url,
  combined_title: c.combined_title,
  combined_slug: c.combined_slug,
  merge_type: c.merge_type,
  category: c.members.find(m=>m.category)?.category || 'Uncategorized',
  kept: new Set(c.members.map(m=>m.url)),
  open: false,
}));

function esc(s){return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function findS(cid){return state.find(s=>s.cluster_id===cid);}
function findC(cid){return DATA.clusters.find(c=>c.cluster_id===cid);}

function passFilter(c, s){
  const q  = document.getElementById('search').value.toLowerCase().trim();
  const fa = document.getElementById('filterAction').value;
  const ft = document.getElementById('filterType').value;
  const fs = parseInt(document.getElementById('filterSize').value,10);
  const ftpl = document.getElementById('filterTemplate').checked;
  if(fa && s.action!==fa) return false;
  if(ft && c.dominant_type!==ft) return false;
  if(fs && c.page_count<fs) return false;
  if(ftpl && !c.template_pattern) return false;
  if(q){
    const blob = (c.members.map(m=>m.title+' '+m.url).join(' ')).toLowerCase();
    if(!blob.includes(q)) return false;
  }
  return true;
}

function summary(){
  const v = state.map((s,i)=>({s, c:DATA.clusters[i]})).filter(({c,s})=>passFilter(c,s));
  const counts = {merge:0, archive_keep_best:0, archive_all:0, skip:0};
  let mergedPosts=0, archivedPosts=0;
  state.forEach((s,i)=>{
    counts[s.action] = (counts[s.action]||0)+1;
    const kept = [...s.kept];
    if(s.action==='merge') mergedPosts += kept.length;
    if(s.action==='archive_keep_best') archivedPosts += Math.max(kept.length-1, 0);
    if(s.action==='archive_all') archivedPosts += kept.length;
  });
  document.getElementById('summary').innerHTML =
    `<b>${state.length}</b> clusters · merge:<b>${counts.merge}</b> ` +
    `archive-keep-best:<b>${counts.archive_keep_best}</b> ` +
    `archive-all:<b>${counts.archive_all}</b> skip:<b>${counts.skip}</b> · ` +
    `→ <b>${mergedPosts}</b> posts to merge, <b>${archivedPosts}</b> to archive · ` +
    `showing <b>${v.length}</b>`;
}

function catOptions(sel){
  return CATS.map(c=>`<option ${c===sel?'selected':''}>${esc(c)}</option>`).join('');
}

function memberRow(c, s, m){
  const kept = s.kept.has(m.url);
  const isPrim = s.primary_url === m.url;
  const trCls = (!kept) ? 'excluded' : (isPrim ? 'survivor' : '');
  const score = m.page_score==null ? '–' : m.page_score;
  return `<tr class="${trCls}">
    <td class="c"><input type="checkbox" ${kept?'checked':''}
        onchange="toggleKeep(${c.cluster_id},'${encodeURIComponent(m.url)}',this.checked)"></td>
    <td class="c"><input type="radio" name="prim${c.cluster_id}" ${isPrim?'checked':''}
        onchange="setPrimary(${c.cluster_id},'${encodeURIComponent(m.url)}')"></td>
    <td>
      <div class="ptitle"><a href="${esc(m.url)}" target="_blank">${esc(m.title||'(untitled)')}</a></div>
      <div class="purl">${esc(m.url)}</div>
    </td>
    <td class="c"><span class="pill ${m.type}">${esc(m.type||'?')}</span></td>
    <td class="c">${esc(m.date_modified||'')}</td>
    <td class="c">${m.word_count||0}</td>
    <td class="c">${m.sessions_lifetime||0}</td>
    <td class="c">${score}</td>
  </tr>`;
}

function clusterCard(c, s){
  const open = s.open ? 'open' : '';
  const actCls = `action-${s.action}`;
  const sizeP = `<span class="pill size">${c.page_count} pages</span>`;
  const simP  = `<span class="pill sim">sim ${c.max_similarity.toFixed(2)}</span>`;
  const typeP = c.dominant_type ? `<span class="pill ${c.dominant_type}">${c.dominant_type}</span>` : '';
  const tplP  = c.template_pattern
        ? `<span class="pill template">tpl: ${esc(c.template_pattern)}</span>` : '';
  const actP  = `<span class="pill ${s.action}">${s.action.replace(/_/g,' ')}</span>`;
  const ai    = c.ai_summary ? `<span class="ai-tag">AI: ${esc(c.ai_summary)}</span>` : '';
  const rows  = c.members.map(m=>memberRow(c,s,m)).join('');
  const acts  = ['merge','archive_keep_best','archive_all','skip'].map(a=>`
    <label><input type="radio" name="act${c.cluster_id}" ${s.action===a?'checked':''}
       onchange="setAction(${c.cluster_id},'${a}')"> ${a.replace(/_/g,' ')}</label>`).join('');
  return `<div class="cluster ${open} ${actCls}" id="cl${c.cluster_id}">
    <div class="chead">
      <span class="cid">#${c.cluster_id}</span>
      ${sizeP} ${simP} ${typeP} ${tplP} ${actP} ${ai}
      <span class="summary" style="margin-left:8px">
        <span><b>${c.total_sessions}</b> total sess.</span>
        <span title="${esc(c.recommended_rationale)}">→ default: <b>${c.recommended_action.replace(/_/g,' ')}</b></span>
      </span>
      <button class="toggle" onclick="toggleOpen(${c.cluster_id})">${s.open?'▾':'▸'}</button>
    </div>
    <div class="actions">${acts}</div>
    <div class="merge-fields">
      <span>Combined:</span>
      <input class="title-in" type="text" value="${esc(s.combined_title)}"
        oninput="setField(${c.cluster_id},'combined_title',this.value)" placeholder="Combined title">
      <input class="slug-in" type="text" value="${esc(s.combined_slug)}"
        oninput="setField(${c.cluster_id},'combined_slug',this.value)" placeholder="combined-slug">
      <select onchange="setField(${c.cluster_id},'merge_type',this.value)">
        <option value="factual" ${s.merge_type==='factual'?'selected':''}>factual</option>
        <option value="narrative" ${s.merge_type==='narrative'?'selected':''}>narrative</option>
      </select>
      <select onchange="setField(${c.cluster_id},'category',this.value)">${catOptions(s.category)}</select>
    </div>
    <div class="rationale">${esc(c.recommended_rationale)}${c.ai_rationale?' · AI: '+esc(c.ai_rationale):''}</div>
    <div class="body">
      <table>
        <tr><th>Keep</th><th>Primary</th><th>Page</th><th>WP</th><th>Modified</th><th>Words</th><th>Sess.</th><th>Score</th></tr>
        ${rows}
      </table>
    </div>
  </div>`;
}

function render(){
  const html = DATA.clusters.map((c,i)=>{
    const s = state[i];
    return passFilter(c,s) ? clusterCard(c,s) : '';
  }).join('');
  document.getElementById('clusters').innerHTML = html || '<p class="rationale">No clusters match the filters.</p>';
  summary();
}

// ── actions ──
function toggleOpen(cid){const s=findS(cid); s.open=!s.open; render();}
function expandAll(v){state.forEach(s=>s.open=v); render();}
function setAction(cid, act){findS(cid).action=act; render();}
function setField(cid,f,v){findS(cid)[f]=v;}      // no re-render for text fields
function setPrimary(cid,url){findS(cid).primary_url=decodeURIComponent(url); render();}
function toggleKeep(cid,url,v){url=decodeURIComponent(url);
  const s=findS(cid); if(v) s.kept.add(url); else s.kept.delete(url);
  if(!v && s.primary_url===url){
    // reassign primary to the highest-scored remaining kept member
    const c=findC(cid); const cand=c.members.filter(m=>s.kept.has(m.url));
    s.primary_url = cand.length ? cand[0].url : null;
  }
  render();}
function resetAll(){
  state.forEach((s,i)=>{
    const c=DATA.clusters[i];
    s.action=c.recommended_action; s.primary_url=c.primary_url;
    s.combined_title=c.combined_title; s.combined_slug=c.combined_slug;
    s.merge_type=c.merge_type;
    s.category=c.members.find(m=>m.category)?.category||'Uncategorized';
    s.kept=new Set(c.members.map(m=>m.url));
  });
  render();
}

// ── export ──
function decisionsObj(){
  const groups=[]; const archive=[]; let gid=0;
  state.forEach((s,i)=>{
    const c=DATA.clusters[i];
    const kept=c.members.filter(m=>s.kept.has(m.url)).map(m=>m.url);
    if(s.action==='skip' || kept.length===0) return;
    if(s.action==='merge' && kept.length>=2){
      groups.push({
        group_id: ++gid,
        cluster_id: c.cluster_id,
        combined_title: s.combined_title,
        combined_slug: s.combined_slug,
        type: s.merge_type,
        category: s.category,
        primary_url: s.primary_url,
        members: kept,
      });
    } else if(s.action==='archive_keep_best'){
      const survivor = kept.includes(s.primary_url) ? s.primary_url : kept[0];
      const archived = kept.filter(u=>u!==survivor);
      if(archived.length){
        archive.push({cluster_id: c.cluster_id, survivor_url: survivor, archived_urls: archived});
      }
    } else if(s.action==='archive_all'){
      archive.push({cluster_id: c.cluster_id, survivor_url: null, archived_urls: kept});
    }
  });
  return {generated:new Date().toISOString(), site:DATA.site||'',
          groups, archive};
}
function download(name,text,mime){
  const b=new Blob([text],{type:mime}); const a=document.createElement('a');
  a.href=URL.createObjectURL(b); a.download=name; a.click(); URL.revokeObjectURL(a.href);
}
function exportJSON(){download('06c_decisions.json',
  JSON.stringify(decisionsObj(),null,2),'application/json');}
function exportArchive(){
  const d=decisionsObj();
  const rows=[['old_url','cluster_id','survivor_url','redirect_type','notes']];
  d.archive.forEach(a=>a.archived_urls.forEach(u=>
    rows.push([u, a.cluster_id, a.survivor_url||'', '301', a.survivor_url?'':'no survivor'])));
  const csv=rows.map(r=>r.map(c=>`"${String(c).replace(/"/g,'""')}"`).join(',')).join('\n');
  download('06c_archive.csv', csv, 'text/csv');
}
function importDecisions(){document.getElementById('importer').click();}
document.getElementById('importer').addEventListener('change', function(e){
  const f=e.target.files[0]; if(!f) return; const r=new FileReader();
  r.onload=()=>{try{applyDecisions(JSON.parse(r.result));}
                catch(err){alert('Bad JSON: '+err);}};
  r.readAsText(f);
});
function applyDecisions(dec){
  // groups → merge state; archive → archive_keep_best / archive_all state
  state.forEach(s=>{s.action='skip'; s.kept=new Set();});
  (dec.groups||[]).forEach(g=>{
    const s=findS(g.cluster_id); if(!s) return;
    s.action='merge'; s.primary_url=g.primary_url;
    s.combined_title=g.combined_title; s.combined_slug=g.combined_slug;
    s.merge_type=g.type||s.merge_type; s.category=g.category||s.category;
    s.kept=new Set(g.members);
  });
  (dec.archive||[]).forEach(a=>{
    const s=findS(a.cluster_id); if(!s) return;
    s.action = a.survivor_url ? 'archive_keep_best' : 'archive_all';
    s.primary_url = a.survivor_url || s.primary_url;
    const set = new Set(a.archived_urls);
    if(a.survivor_url) set.add(a.survivor_url);
    s.kept = set;
  });
  render();
}

render();
</script>
</body>
</html>
"""


# ╔══════════════════════════════════════════════════════════════╗
# ║  MAIN                                                         ║
# ╚══════════════════════════════════════════════════════════════╝

def main():
    ap = argparse.ArgumentParser(
        description="Turn TF-IDF clusters into curatable merge / archive decisions")
    ap.add_argument("--classify", action="store_true",
                    help="Add one Anthropic API call per cluster for a human-readable "
                         "summary and recommendation (cached)")
    ap.add_argument("--classify-limit", type=int, default=0,
                    help="Limit cluster classification to first N (testing)")
    ap.add_argument("--force",  action="store_true",
                    help="Re-classify clusters that already have a cache file")
    ap.add_argument("--min-size", type=int, default=2,
                    help="Skip clusters smaller than this (default 2)")
    args = ap.parse_args()

    log.info("=" * 65)
    log.info("STEP 6c — Cluster decisions (merge / archive / keep)")
    log.info("=" * 65)

    if not os.path.exists(CLUSTERS_IN):
        log.error(f"{CLUSTERS_IN} not found. Run 4_similarity.py first.")
        sys.exit(1)
    with open(CLUSTERS_IN, encoding="utf-8") as f:
        cdata = json.load(f)
    raw_clusters = cdata.get("clusters", [])
    log.info(f"  Loaded {len(raw_clusters)} clusters "
             f"(threshold={cdata.get('threshold')}, total_pairs={cdata.get('total_pairs')})")

    inv    = load_inventory()
    tags   = load_cached_tags()
    scorer = load_scorer()

    clusters = enrich_clusters(raw_clusters, inv, tags, scorer)
    clusters = [c for c in clusters if c["page_count"] >= args.min_size]
    log.info(f"  Enriched: {len(clusters)} clusters with ≥{args.min_size} resolvable members")

    rec_counts = Counter(c["recommended_action"] for c in clusters)
    log.info(f"  Heuristic recommendations: {dict(rec_counts)}")
    tpl_hits = sum(1 for c in clusters if c["template_pattern"])
    log.info(f"  Clusters flagged as templated content: {tpl_hits}")

    if args.classify:
        run_classification(clusters, args.force, args.classify_limit)
        post_rec = Counter(c["recommended_action"] for c in clusters)
        log.info(f"  After AI classification: {dict(post_rec)}")

    groupings = {
        "site":      config.SITE_URL,
        "generated": datetime.utcnow().isoformat() + "Z",
        "threshold": cdata.get("threshold"),
        "clusters":  clusters,
    }
    with open(GROUPINGS, "w", encoding="utf-8") as f:
        json.dump(groupings, f, indent=2, ensure_ascii=False)
    log.info(f"  Groupings → {GROUPINGS}")

    os.makedirs(config.OUTPUT_DIR, exist_ok=True)
    with open(REVIEW_HTML, "w", encoding="utf-8") as f:
        f.write(build_review_html(groupings))
    log.info(f"\nReview tool → {REVIEW_HTML}")
    log.info("Open it, set per-cluster decisions, Export decisions JSON + archive CSV.")
    log.info("Next:  python 6c_combine.py            (dry run on merge groups)")
    log.info("       python 6c_combine.py --post     (write drafts to new site)")


if __name__ == "__main__":
    main()
