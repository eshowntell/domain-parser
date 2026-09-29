#!/usr/bin/env python3
"""
run.py — FTJ migration runner. One front door for the whole pipeline.

Each step either shells out to a real script (extra args pass through) or is a
MANUAL checkpoint that pauses with instructions (e.g. "now run Phoso"). Steps are
grouped into phases; you can run one step or a whole phase in order.

USAGE
─────
  python run.py                       # list every step, grouped by phase
  python run.py <step> [args...]      # run one step (extra args pass through)
  python run.py <phase>               # run a phase in order, pausing at each step
                                      #   phases: phase1  decide  media  content  phase3

EXAMPLES
────────
  python run.py crawl
  python run.py media-upload-dry
  python run.py reconcile --test
  python run.py media                 # guided media sequence incl. the Phoso pause
"""

import os
import subprocess
import sys

try:                       # so box-drawing / arrows / emoji don't crash a cp1252 console
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
PY   = sys.executable

# Each step is a dict:
#   phase   : grouping label
#   desc    : one-line description
#   script  : script filename to shell out to  (omit for a MANUAL checkpoint)
#   args    : baked-in args prepended before pass-through args
#   wp      : True if it WRITES to WordPress (prompts before running)
#   manual  : instruction text for a checkpoint (omit for a script step)
STEPS = {
    # ── Phase 1 — audit (crawl the OLD site → dashboard) ──────────────
    "crawl":     dict(phase="1·audit", script="1_crawl.py", args=[], wp=False,
                      desc="Crawl old site (REST + sitemap) → data/01_pages.json"),
    "extract":   dict(phase="1·audit", script="2_extract.py", args=[], wp=False,
                      desc="Extract text/images/links/menus → data/02_content.json"),
    "ga4":       dict(phase="1·audit", script="3_ga4_merge.py", args=[], wp=False,
                      desc="Merge GA4 CSVs → data/03_merged.json (pass --lifetime/--ninety, or none to skip)"),
    "similarity":dict(phase="1·audit", script="4_similarity.py", args=[], wp=False,
                      desc="TF-IDF similarity clusters → data/04_clusters.json"),
    "report":    dict(phase="1·audit", script="5_report.py", args=[], wp=False,
                      desc="Build audit Excel + HTML dashboard → output/"),
    "review-audit": dict(phase="1·audit", desc="MANUAL: review dashboard, export keeper + decisions CSVs",
                      manual="Open output/ftj_site_audit.html. Mark Keep/Dismiss, then export "
                             "ftj_keeper_list.csv (and the 06c decision CSVs). Place them in the "
                             "project root, then continue."),

    # ── Phase 2 — decide what to keep/merge ───────────────────────────
    "group":     dict(phase="2·decide", script="6c_group.py", args=[], wp=False,
                      desc="Cluster review → 06c_groupings.json + review HTML"),
    "review-groups": dict(phase="2·decide", desc="MANUAL: review clusters, export 06c_decisions.json",
                      manual="Open output/06c_review.html, make merge/archive/skip decisions, and "
                             "export the 06c_decisions*.json files into data/. Then continue."),
    "prep-blogs":dict(phase="2·decide", script="6c_prep_blogs.py", args=[], wp=False,
                      desc="Blog decision engine → data/06c_decisions_blogs.json / solo_blogs"),
    "prep-pages":dict(phase="2·decide", script="6c_prep_pages.py", args=[], wp=False,
                      desc="Page decision engine → data/06c_decisions_pages.json"),

    # ── Phase 2 — media (bridge slots in around 6b; runs BEFORE content) ──
    "media-download": dict(phase="2·media", script="6b_media.py", args=["--skip-upload"], wp=False,
                      desc="Download keeper media to disk (upload superseded by the bridge)"),
    "manifest":  dict(phase="2·media", script="phoso_bridge.py", args=["--og-manifest",
                      "--download-dir", os.path.join(os.path.expanduser("~"), "Downloads", "ftj-media")],
                      wp=False, desc="Build Map 1 (og_url↔local_path↔pHash) from the download"),
    "run-phoso": dict(phase="2·media", desc="MANUAL: run Phoso — sort, caption, correct + Commit",
                      manual="In Phoso (E:\\image_sorter → run_desktop.bat), set OUTPUT to "
                             "config.PHOSO_OUTPUT_DIR, point INPUT at ~/Downloads/ftj-media, sort. "
                             "Let 'Generating descriptions' finish, then Review → correct + Commit. "
                             "LEAVE every image in its category folder — the bridge reads the buckets; "
                             "'correcting' means MOVING an image to the right bucket, never flattening "
                             "them to the parent. Then continue."),
    "reconcile": dict(phase="2·media", script="phoso_bridge.py", args=["--reconcile"], wp=False,
                      desc="pHash-join finals→originals, caption→name → rename map + report"),
    "reconcile-report": dict(phase="2·media", script="phoso_bridge.py", args=["--report"], wp=False,
                      desc="Read-only reconcile summary (orphans / weak names)"),
    "review-media": dict(phase="2·media", desc="MANUAL: review reconcile report, edit new_name if needed",
                      manual="Open data/06_media_reconcile_report.csv. Fix any weak/wrong new_name "
                             "(rows flagged template/none/orphan). Then continue."),
    "media-upload-dry": dict(phase="2·media", script="phoso_bridge.py", args=["--upload"], wp=False,
                      desc="Preview media upload (dry-run)"),
    "media-upload": dict(phase="2·media", script="phoso_bridge.py", args=["--upload", "--post"], wp=True,
                      desc="Upload renamed media + meta → new site; write 06_media_map.json"),

    # ── Phase 2 — content (bakes correct renamed media URLs on first pass) ──
    "combine":   dict(phase="2·content", script="6c_combine.py", args=["--post"], wp=True,
                      desc="Merge grouped blogs → new posts (drafts)"),
    "rewrite-blogs": dict(phase="2·content", script="6_rewrite.py", args=["--post"], wp=True,
                      desc="AI-rewrite solo blog posts → new posts"),
    "rewrite-pages": dict(phase="2·content", script="6_rewrite_pages.py", args=["--post"], wp=True,
                      desc="AI-rewrite + publish site pages → data/06_pages/*.json"),
    "pdf":       dict(phase="2·content", script="6d_pdf_pages.py", args=["--post"], wp=True,
                      desc="Linked PDFs → searchable WP pages (its images feed the bridge too)"),
    "enhance":   dict(phase="2·content", script="ai_enhance_pages.py", args=[], wp=False,
                      desc="Add AI intro Q&A to pages → data/06_pages_ai/*.json"),

    # ── Phase 3 — pre-go-live (all target NEW_SITE_URL) ───────────────
    "schema-dry":dict(phase="3·golive", script="inject_schema.py", args=["--dry-run"], wp=False,
                      desc="Preview schema type assignment per page"),
    "schema":    dict(phase="3·golive", script="inject_schema.py", args=[], wp=False,
                      desc="Inject JSON-LD into 06_pages_ai → 06_pages_schema"),
    "upload-dry":dict(phase="3·golive", script="upload_pages.py", args=[], wp=False,
                      desc="Preview page upload (dry-run)"),
    "upload":    dict(phase="3·golive", script="upload_pages.py", args=["--post"], wp=True,
                      desc="Upload/PATCH 06_pages_schema pages to WP"),
    "redirects": dict(phase="3·golive", script="update_slugs.py", args=["--redirects"], wp=False,
                      desc="Build old→new redirect map (slug_redirects.csv)"),
    "audit":     dict(phase="3·golive", script="update_slugs.py", args=["--audit-old-site"], wp=False,
                      desc="Check every old-site URL live vs 404 (run near launch)"),
    "fallback":  dict(phase="3·golive", script="update_slugs.py", args=["--fallback-redirects"], wp=False,
                      desc="Catch-all redirects for orphaned old URLs"),
    "redirection":dict(phase="3·golive", script="update_slugs.py", args=["--export-redirection"], wp=False,
                      desc="Export combined Redirection-plugin import CSV"),
    "sitemaps":  dict(phase="3·golive", script="update_slugs.py", args=["--sitemaps"], wp=False,
                      desc="Regenerate XML sitemaps from live WP state"),
    "meta-extract":dict(phase="3·golive", script="extract_yoast_from_wxr.py", args=[], wp=False,
                      desc="Extract Yoast meta from a WXR export (pass --wxr <path>) → yoast_meta.csv"),
    "meta-prep": dict(phase="3·golive", script="prep_meta.py", args=[], wp=False,
                      desc="yoast_meta.csv + redirects → old_site_meta.json"),
    "meta-dry":  dict(phase="3·golive", script="apply_meta.py", args=["--dry-run"], wp=False,
                      desc="Preview Yoast meta patches"),
    "meta":      dict(phase="3·golive", script="apply_meta.py", args=[], wp=True,
                      desc="Push Yoast meta to WP (AFTER go-live, Yoast active)"),
}

# Guided phase sequences (run in order, pausing at each step).
SEQUENCES = {
    "phase1": ["crawl", "extract", "ga4", "similarity", "report", "review-audit"],
    "decide": ["group", "review-groups", "prep-blogs", "prep-pages"],
    "media":  ["media-download", "manifest", "run-phoso", "reconcile",
               "reconcile-report", "review-media", "media-upload-dry", "media-upload"],
    "content":["combine", "rewrite-blogs", "rewrite-pages", "pdf", "enhance"],
    "phase3": ["schema-dry", "schema", "upload-dry", "upload", "redirects",
               "audit", "fallback", "redirection", "meta-prep", "meta-dry"],
}
# Legacy alias
SEQUENCES["pre-golive"] = SEQUENCES["phase3"]

PHASE_ORDER = ["1·audit", "2·decide", "2·media", "2·content", "3·golive"]


def site_url() -> str:
    try:
        sys.path.insert(0, HERE)
        import config
        return getattr(config, "NEW_SITE_URL", "?")
    except Exception:
        return "?"


def run_step(name: str, extra: list, force_yes: bool = False) -> int:
    step = STEPS[name]

    # ── Manual checkpoint ─────────────────────────────────────────────
    if "manual" in step:
        print(f"\n{'='*68}\n  ⏸  MANUAL — {name}: {step['desc']}\n{'-'*68}")
        print("  " + step["manual"].replace(". ", ".\n  "))
        print("=" * 68)
        if not force_yes:
            input("  Press Enter when done (Ctrl+C to stop) … ")
        return 0

    # ── Script step ───────────────────────────────────────────────────
    script, baked, wp = step["script"], step.get("args", []), step.get("wp", False)
    cmd = [PY, os.path.join(HERE, script), *baked, *extra]
    pretty = " ".join([os.path.basename(PY), script, *baked, *extra])

    print(f"\n{'='*68}\n  STEP: {name} — {step['desc']}\n  CMD : {pretty}")
    if wp:
        print(f"  ⚠️  WRITES TO WORDPRESS at {site_url()}")
        if not force_yes:
            if input("  Proceed? [y/N] ").strip().lower() != "y":
                print("  Skipped.")
                return 0
    print("=" * 68)
    return subprocess.run(cmd, cwd=HERE).returncode


def list_steps():
    print(f"\nFTJ runner — new site: {site_url()}\n")
    for phase in PHASE_ORDER:
        print(f"  ── {phase} " + "─" * (58 - len(phase)))
        for name, s in STEPS.items():
            if s["phase"] != phase:
                continue
            tag = "⏸ " if "manual" in s else ("⚠️ " if s.get("wp") else "  ")
            print(f"    {name:<17} {tag} {s['desc']}")
        print()
    print("  Guided phases (run in order, pausing each step):")
    for seq in ["phase1", "decide", "media", "content", "phase3"]:
        print(f"    python run.py {seq:<9} → {' → '.join(SEQUENCES[seq])}")
    print("\n  Extra args pass through, e.g.:  python run.py upload --limit 3\n")


def run_sequence(seq_name: str):
    for step in SEQUENCES[seq_name]:
        rc = run_step(step, [])
        if rc != 0:
            print(f"\n  ✗ '{step}' exited with code {rc} — stopping.")
            sys.exit(rc)
        if input(f"\n  '{step}' done. Continue to next step? [Y/n] ").strip().lower() == "n":
            print("  Stopped by user.")
            return
    print(f"\n  ✓ '{seq_name}' sequence complete.")


def main():
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help", "list"):
        list_steps()
        return

    name, extra = args[0], args[1:]

    if name in SEQUENCES:
        run_sequence(name)
        return

    if name not in STEPS:
        print(f"Unknown step: {name}")
        list_steps()
        sys.exit(1)

    sys.exit(run_step(name, extra))


if __name__ == "__main__":
    main()
