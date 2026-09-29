#!/usr/bin/env python3
"""
4_similarity.py  —  Find pages with similar or duplicate content.

Method:
  • TF-IDF vectorisation of all page body text.
  • Cosine similarity matrix (computed in sparse chunks for memory efficiency).
  • Pairs scoring above SIMILARITY_THRESHOLD (default 0.70) are flagged.
  • Connected-component clustering groups related pages together.

Output:  data/04_clusters.json

Each cluster record:
  {
    "cluster_id": 1,
    "urls": ["...", "..."],
    "titles": ["...", "..."],
    "max_similarity": 0.87,
    "pair_count": 3,
    "pairs": [{"url_a": "...", "url_b": "...", "score": 0.87}, ...]
  }
"""

import json
import os
import sys
from itertools import combinations

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
from tqdm import tqdm

sys.path.insert(0, os.path.dirname(__file__))
import config
from utils import setup_logging

log = setup_logging("4_similarity")

IN_FILE  = os.path.join(config.DATA_DIR, "03_merged.json")
FALLBACK = os.path.join(config.DATA_DIR, "02_content.json")   # if step 3 was skipped
OUT_FILE = os.path.join(config.DATA_DIR, "04_clusters.json")

MIN_WORDS = 50   # skip pages with fewer than this many words


def connected_components(pairs: list[tuple[int, int]]) -> list[list[int]]:
    """Union-find to group nodes into connected components."""
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


def main():
    log.info("=" * 65)
    log.info("STEP 4 — Content similarity analysis")
    log.info("=" * 65)

    # Load data
    in_path = IN_FILE if os.path.exists(IN_FILE) else FALLBACK
    if not os.path.exists(in_path):
        log.error("No input file found. Run 2_extract.py (and optionally 3_ga4_merge.py) first.")
        sys.exit(1)
    log.info(f"Loading from: {in_path}")

    with open(in_path, encoding="utf-8") as f:
        pages = json.load(f)

    # Filter to pages with enough text
    eligible = [
        p for p in pages
        if p.get("word_count", 0) >= MIN_WORDS and p.get("body_text", "").strip()
    ]
    skipped = len(pages) - len(eligible)
    log.info(f"Pages with ≥{MIN_WORDS} words: {len(eligible)}  (skipped {skipped} thin/empty pages)")

    if len(eligible) < 2:
        log.warning("Not enough pages to compare. Exiting.")
        json.dump([], open(OUT_FILE, "w"))
        return

    urls   = [p["url"]   for p in eligible]
    titles = [p.get("title", "") for p in eligible]
    texts  = [p["body_text"]  for p in eligible]

    # TF-IDF
    log.info("Fitting TF-IDF vectoriser …")
    vectoriser = TfidfVectorizer(
        min_df=2,
        max_df=0.95,
        ngram_range=(1, 2),
        max_features=20_000,
        sublinear_tf=True,
        stop_words="english",
    )
    tfidf_matrix = vectoriser.fit_transform(texts)
    log.info(f"Matrix shape: {tfidf_matrix.shape}")

    # Compute similarities in row chunks (avoids loading full NxN into memory)
    CHUNK = 100
    n = len(eligible)
    threshold = config.SIMILARITY_THRESHOLD

    similar_pairs: list[dict] = []
    log.info(f"Scanning pairs (threshold={threshold}) …")

    for start in tqdm(range(0, n, CHUNK), desc="Comparing"):
        end = min(start + CHUNK, n)
        chunk = tfidf_matrix[start:end]
        sims = cosine_similarity(chunk, tfidf_matrix)  # shape: (chunk_size, n)
        for local_i, row in enumerate(sims):
            global_i = start + local_i
            for j in range(global_i + 1, n):
                score = float(row[j])
                if score >= threshold:
                    similar_pairs.append({
                        "idx_a": global_i,
                        "idx_b": j,
                        "url_a": urls[global_i],
                        "url_b": urls[j],
                        "title_a": titles[global_i],
                        "title_b": titles[j],
                        "score":   round(score, 4),
                    })

    log.info(f"Similar pairs found: {len(similar_pairs)}")

    # Cluster by connected components
    pair_indices = [(p["idx_a"], p["idx_b"]) for p in similar_pairs]
    components   = connected_components(pair_indices)

    clusters = []
    for cluster_id, indices in enumerate(sorted(components, key=len, reverse=True), start=1):
        cluster_urls   = [urls[i]   for i in indices]
        cluster_titles = [titles[i] for i in indices]

        # Pairs within this cluster
        cluster_pairs = [
            p for p in similar_pairs
            if p["idx_a"] in indices and p["idx_b"] in indices
        ]
        max_sim = max((p["score"] for p in cluster_pairs), default=0.0)

        clusters.append({
            "cluster_id":      cluster_id,
            "page_count":      len(indices),
            "max_similarity":  round(max_sim, 4),
            "urls":            cluster_urls,
            "titles":          cluster_titles,
            "pairs":           sorted(cluster_pairs, key=lambda x: -x["score"])[:20],
        })

    log.info(f"Clusters: {len(clusters)}")
    log.info(f"  Near-duplicates (score ≥ 0.90): {sum(1 for p in similar_pairs if p['score'] >= 0.90)}")
    log.info(f"  High similarity  (0.80–0.89):   {sum(1 for p in similar_pairs if 0.80 <= p['score'] < 0.90)}")
    log.info(f"  Moderate         (0.70–0.79):   {sum(1 for p in similar_pairs if 0.70 <= p['score'] < 0.80)}")

    # Also produce a flat list of all pairs for the Excel report
    output = {
        "clusters":    clusters,
        "all_pairs":   sorted(similar_pairs, key=lambda x: -x["score"]),
        "total_pairs": len(similar_pairs),
        "threshold":   threshold,
    }

    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    log.info(f"\nSaved → {OUT_FILE}")
    log.info("Step 4 complete. Run:  python 5_report.py")


if __name__ == "__main__":
    main()
