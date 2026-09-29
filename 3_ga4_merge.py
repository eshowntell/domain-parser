#!/usr/bin/env python3
"""
3_ga4_merge.py  —  Merge GA4 / analytics traffic data into the page inventory.

Usage:
  python 3_ga4_merge.py
  python 3_ga4_merge.py --csv "C:/path/to/Landing_page_stats.csv"
  python 3_ga4_merge.py --lifetime ga4_all_time.csv --ninety ga4_90days.csv

CSV format (Landing_page_stats.csv):
  Column A  — page URL or path  (used for matching)
  Columns B+ — ALL remaining columns kept with their original header names

Output:  data/03_merged.json
"""

import argparse
import json
import os
import sys
from datetime import datetime as _dt
from urllib.parse import urlparse

import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))
import config
from utils import setup_logging

log = setup_logging("3_ga4_merge")

IN_CONTENT  = os.path.join(config.DATA_DIR, "02_content.json")
OUT_FILE    = os.path.join(config.DATA_DIR, "03_merged.json")
DEFAULT_CSV = r"C:\Users\ErikS\Documents\coding_project\domain_parser\Landing_page_stats.csv"

# Map GA4 column names → our standard internal field names.
# 'sessions_lifetime' is the key one — drives the summary stats.
GA4_COLUMN_MAP = {
    "sessions":                              "sessions_lifetime",
    "engaged sessions":                      "engaged_sessions",
    "active users":                          "active_users",
    "new users":                             "new_users",
    "average engagement time per session":   "avg_engagement_time",
    "key events":                            "key_events",
    "total revenue":                         "total_revenue",
    "session key event rate":                "session_key_event_rate",
    # add more mappings here if needed
}


def to_path(url_or_path: str) -> str:
    s = str(url_or_path).strip()
    if s.startswith("http"):
        s = urlparse(s).path
    s = s.rstrip("/")
    return s if s else "/"


def col_key(name: str) -> str:
    """Map a CSV column name to a storage key.
    Uses GA4_COLUMN_MAP for known columns, otherwise generates a clean snake_case key."""
    mapped = GA4_COLUMN_MAP.get(name.lower().strip())
    if mapped:
        return mapped
    return "stat_" + name.lower().strip().replace(" ", "_").replace("/", "_")[:50]


def _compact_date_str(raw: str) -> str:
    """Convert YYYYMMDD (or YYYY-MM-DD) to M/D/YY display format."""
    raw = raw.strip().replace("-", "")
    try:
        d = _dt.strptime(raw, "%Y%m%d")
        return f"{d.month}/{d.day}/{str(d.year)[2:]}"
    except Exception:
        return raw


def load_landing_csv(path: str):
    if not os.path.exists(path):
        log.error(f"CSV not found: {path}")
        log.error('Pass the correct path with:  python 3_ga4_merge.py --csv "path/to/file.csv"')
        return None

    header_row = 0
    ga_start = None
    ga_end   = None
    try:
        with open(path, encoding="utf-8-sig") as f:
            for i, line in enumerate(f):
                stripped = line.strip()
                if stripped == "" or stripped.startswith("#"):
                    header_row = i + 1
                    # Capture GA date range from comment lines
                    low = stripped.lower()
                    if "start date" in low:
                        raw = stripped.split(":")[-1].strip()
                        ga_start = _compact_date_str(raw)
                    elif "end date" in low:
                        raw = stripped.split(":")[-1].strip()
                        ga_end = _compact_date_str(raw)
                else:
                    break
    except UnicodeDecodeError:
        pass

    df = None
    for enc in ["utf-8-sig", "utf-8", "latin-1", "cp1252"]:
        try:
            df = pd.read_csv(path, skiprows=header_row, encoding=enc)
            if not df.empty:
                break
        except Exception:
            continue

    if df is None or df.empty:
        log.error(f"Could not parse {path}")
        return None

    log.info(f"  Loaded: {len(df)} rows")
    log.info(f"  Columns: {list(df.columns)}")

    url_col     = df.columns[0]
    metric_cols = list(df.columns[1:])          # ALL columns after A

    log.info(f"  URL column (A)  : '{url_col}'")
    log.info(f"  Metric columns  : {metric_cols}")
    log.info(f"  Column → key mapping:")
    for col in metric_cols:
        log.info(f"    '{col}'  →  '{col_key(col)}'")

    df = df[[url_col] + metric_cols].copy()
    df = df.dropna(subset=[url_col])
    df["_path"] = df[url_col].apply(to_path)

    # Coerce numeric columns
    for col in metric_cols:
        cleaned = df[col].astype(str).str.replace(",", "", regex=False).str.strip()
        numeric = pd.to_numeric(cleaned, errors="coerce")
        if numeric.notna().mean() > 0.8:
            df[col] = numeric.fillna(0)
        else:
            df[col] = df[col].astype(str).str.strip()

    df = df.drop_duplicates(subset=["_path"])
    log.info(f"  Unique paths: {len(df)}")
    log.info(f"\n  Sample data:\n{df[['_path'] + metric_cols[:4]].head(5).to_string(index=False)}\n")
    if ga_start or ga_end:
        log.info(f"  GA date range: {ga_start or '?'} → {ga_end or '?'}")
    return df, metric_cols, {"ga_start": ga_start, "ga_end": ga_end}


def detect_col(df_cols, candidates):
    cols_lower = {c.lower(): c for c in df_cols}
    for cand in candidates:
        for low, orig in cols_lower.items():
            if cand.lower() in low or low in cand.lower():
                return orig
    return None


def load_ga4_csv(path: str, label: str):
    if not os.path.exists(path):
        log.error(f"GA4 file not found: {path}")
        return None
    header_row = 0
    with open(path, encoding="utf-8-sig") as f:
        for i, line in enumerate(f):
            if line.startswith("#") or line.strip() == "":
                header_row = i + 1
            else:
                break
    try:
        df = pd.read_csv(path, skiprows=header_row, encoding="utf-8-sig")
    except Exception as e:
        log.error(f"Cannot parse {path}: {e}")
        return None
    log.info(f"  {label}: {len(df)} rows, columns: {list(df.columns)}")
    path_col = detect_col(list(df.columns), [
        "Page path", "page_path", "Page path and screen class", "Page path + query string"])
    if not path_col:
        log.error("Cannot find page-path column. Set GA4_PATH_COLUMN in config.py.")
        return None
    sessions_col = detect_col(list(df.columns), ["Sessions", "sessions"])
    views_col    = detect_col(list(df.columns), ["Views", "Pageviews", "pageviews", "Page views"])
    rename = {path_col: "ga4_path"}
    if sessions_col: rename[sessions_col] = "sessions"
    if views_col:    rename[views_col]    = "pageviews"
    df = df.rename(columns=rename)
    keep = ["ga4_path"] + [c for c in ("sessions", "pageviews") if c in df.columns]
    df = df[keep].copy()
    for col in ("sessions", "pageviews"):
        if col in df.columns:
            df[col] = pd.to_numeric(
                df[col].astype(str).str.replace(",","",regex=False),
                errors="coerce").fillna(0).astype(int)
    df["ga4_path"] = df["ga4_path"].astype(str).apply(to_path)
    agg = {c: "sum" for c in ("sessions","pageviews") if c in df.columns}
    if agg:
        df = df.groupby("ga4_path", as_index=False).agg(agg)
    return df


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv",      metavar="FILE", default=DEFAULT_CSV)
    parser.add_argument("--lifetime", metavar="FILE")
    parser.add_argument("--ninety",   metavar="FILE")
    parser.add_argument("--skip-csv", action="store_true")
    args = parser.parse_args()

    log.info("=" * 65)
    log.info("STEP 3 — Merge analytics traffic data")
    log.info("=" * 65)

    if not os.path.exists(IN_CONTENT):
        log.error(f"{IN_CONTENT} not found. Run 2_extract.py first.")
        sys.exit(1)

    with open(IN_CONTENT, encoding="utf-8") as f:
        pages = json.load(f)
    log.info(f"Loaded {len(pages)} pages")

    path_index: dict = {}
    for i, page in enumerate(pages):
        p = to_path(page.get("url", ""))
        path_index.setdefault(p, []).append(i)

    for page in pages:
        page.setdefault("sessions_lifetime", 0)
        page.setdefault("pageviews_lifetime", 0)
        page.setdefault("sessions_90d", 0)
        page.setdefault("pageviews_90d", 0)

    # ── Landing page CSV ──────────────────────────────────────────────────────
    if not args.skip_csv and not (args.lifetime or args.ninety):
        log.info(f"\nLoading: {args.csv}")
        result = load_landing_csv(args.csv)
        if result is not None:
            df, metric_cols, ga_dates = result
            # Save date range for use by 5_report.py
            meta_path = os.path.join(config.DATA_DIR, "03_ga_meta.json")
            with open(meta_path, "w", encoding="utf-8") as _f:
                json.dump(ga_dates, _f)
            log.info(f"  Saved GA meta → {meta_path}")
            matched, unmatched = 0, []
            for _, row in df.iterrows():
                path = row["_path"]
                idxs = (path_index.get(path) or
                        path_index.get(path + "/") or
                        path_index.get(path.rstrip("/")))
                if not idxs:
                    unmatched.append(path)
                    continue
                for i in idxs:
                    for col in metric_cols:
                        pages[i][col_key(col)] = row[col]
                matched += 1
            log.info(f"  Matched {matched} / {len(df)} rows to pages")
            if unmatched:
                log.info(f"  Unmatched paths (first 10): {unmatched[:10]}")
                log.info(f"  ({len(unmatched)} total unmatched — likely posts/news pages not in WP inventory)")

    # ── GA4 export CSVs ───────────────────────────────────────────────────────
    def merge_ga4(df_ga4, suffix):
        matched, unmatched = 0, []
        for _, row in df_ga4.iterrows():
            path = row["ga4_path"]
            idxs = (path_index.get(path) or
                    path_index.get(path + "/") or
                    path_index.get(path.rstrip("/")))
            if not idxs:
                unmatched.append(path)
                continue
            for i in idxs:
                if "sessions"  in row: pages[i][f"sessions_{suffix}"]  = int(row["sessions"])
                if "pageviews" in row: pages[i][f"pageviews_{suffix}"] = int(row["pageviews"])
            matched += 1
        log.info(f"  Matched {matched} / {len(df_ga4)} rows")
        if unmatched:
            log.info(f"  Unmatched (first 10): {unmatched[:10]}")

    if args.lifetime:
        df_lt = load_ga4_csv(args.lifetime, "lifetime")
        if df_lt is not None:
            merge_ga4(df_lt, "lifetime")

    if args.ninety:
        df_90 = load_ga4_csv(args.ninety, "90d")
        if df_90 is not None:
            merge_ga4(df_90, "90d")

    # Summary — dynamically report every metric column that was merged
    log.info(f"\n{'─'*65}")
    # Collect all stat_ and known mapped keys that appear across pages
    all_keys = set()
    for p in pages:
        for k in p:
            if k.startswith("stat_") or k in (
                "sessions_lifetime","active_users","new_users",
                "avg_engagement_time","key_events","total_revenue",
                "session_key_event_rate","sessions_90d","pageviews_lifetime","pageviews_90d"
            ):
                all_keys.add(k)
    for key in sorted(all_keys):
        vals = [p[key] for p in pages if key in p and isinstance(p[key], (int, float)) and p[key] > 0]
        if not vals:
            continue
        label = key.replace("_", " ").replace("stat ", "").title()
        total = sum(vals)
        mean  = total / len(vals)
        # Show total for counts, mean for rates/times
        if "rate" in key or "time" in key or "avg" in key:
            log.info(f"  {label:<40}: {mean:.2f}  (avg across {len(vals)} pages)")
        else:
            log.info(f"  {label:<40}: {int(total):,}  (across {len(vals)} pages)")
    pages_matched = sum(1 for p in pages if any(k in p for k in all_keys))
    log.info(f"  {'Pages with any matched data':<40}: {pages_matched} / {len(pages)}")
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(pages, f, indent=2, ensure_ascii=False)
    log.info(f"\nSaved → {OUT_FILE}")
    log.info("Step 3 complete. Run:  python 4_similarity.py")


if __name__ == "__main__":
    main()
