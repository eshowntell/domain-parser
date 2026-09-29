# domain-parser — Command Reference

Read-only inventory of every runnable command in the FTJ audit/migration pipeline.
Sources: `README.md`, `RUNBOOK.md`, `PHOSO_BRIDGE_SPEC.md`, `config.py`, `run.py`,
and the docstring/CLI flags/top-of-file constants of every `*.py` in this folder.

## The phase model

- **Phase 1 — audit** (`1_crawl.py` → `5_report.py`): crawl the OLD live site
  (`config.SITE_URL`), extract content, merge GA4 traffic, cluster similar pages,
  and produce the audit Excel/HTML. **Done.**
- **Phase 2 — content/media build** (`6b_media.py`, `6c_*`, `6_rewrite*.py`,
  `ai_enhance_pages.py`, `6d_pdf_pages.py`, plus the new `phoso_bridge.py`):
  decide what to keep/merge/archive, migrate media, rewrite content with the
  Anthropic API, and POST/PATCH it onto the NEW build site (`config.NEW_SITE_URL`,
  e.g. `ftj3.local`). **Done** (except the Phoso media bridge, being built now).
- **Phase 3 — pre-go-live + go-live** (`inject_schema.py`, `upload_pages.py`,
  `update_slugs.py`, `prep_meta.py`, `apply_meta.py`, plus one-shot repair
  scripts): fix JSON-LD schema NAP, upload schema'd pages, build redirect maps,
  push Yoast meta, go live. **In progress / remaining.**

**Golden rule:** every step reads the prior step's output from `data/` (or
`output/`) and writes its own — nothing re-derives from scratch. Paths are all
anchored to `config.DATA_DIR` / `config.OUTPUT_DIR`, which hang off `BASE_DIR`
(this folder), so the pipeline is portable across machines.

`run.py` is the front-door runner for the *remaining* (Phase 3) steps — see
the consolidation note at the bottom.

---

## Master command table

Status legend: **active** (part of the live remaining pipeline per RUNBOOK
§4 "keep active" / not on the prune list) · **one-shot-done** (a repair
already applied, RUNBOOK marks it safe-to-delete) · **archived** (did its
job, RUNBOOK says zip it and remove from the working folder, but its
*output data* is still a live dependency) · **superseded** (replaced by a
newer script) · **dead-code** (disabled/never wired in, e.g. renamed off
its normal name).

### Phase 1 — audit (scripts 1–5)

| Order | Command (key flag) | What it does | Reads | Writes | Status |
|---|---|---|---|---|---|
| 1 | `python 1_crawl.py` | Builds full URL inventory via WP REST API + sitemap.xml fallback | live `config.SITE_URL` (WP REST API) | `data/01_pages.json` | archived (output still live dependency) |
| 2 | `python 2_extract.py` | Pulls body text/H1/images/links per page; REST `content.rendered`, falls back to live-page scrape; also extracts nav/menu structure from homepage | `data/01_pages.json`; live site | `data/02_content.json`, `data/02_menus.json` | archived |
| 3 | `python 3_ga4_merge.py --lifetime <csv> --ninety <csv>` (or no flags to skip traffic) | Merges GA4 CSV exports (lifetime + 90-day) into the content inventory | `data/02_content.json`; GA4 CSV export(s) | `data/03_merged.json` | archived |
| 4 | `python 4_similarity.py` | TF-IDF cosine similarity across all page text; flags pairs ≥ `SIMILARITY_THRESHOLD` (0.70); connected-component clustering | `data/03_merged.json` (fallback `data/02_content.json`) | `data/04_clusters.json` | archived |
| 5 | `python 5_report.py` | Builds the 5-tab audit Excel workbook + standalone searchable HTML dashboard | `data/01_pages.json` … `data/04_clusters.json` | `output/ftj_site_audit.xlsx`, `output/ftj_site_audit.html` | archived |

### Phase 2 — content/media build (6*)

| Order | Command (key flag) | What it does | Reads | Writes | Status |
|---|---|---|---|---|---|
| 6a | `python build_menus.py` | One-time: generates `02_menus.json` from a manually-provided WP Admin menu structure (hardcoded `MANUAL_URLS` map) | `data/02_content.json` (fallback `01_pages.json`) | `data/02_menus.json` | one-shot-done |
| 6b | `python 6b_media.py [--limit N] [--force] [--skip-download]` | Downloads keeper-page media from the live site, uploads it to `NEW_SITE_URL` via `/wp-json/wp/v2/media`, records old→new URL map. (`--og-manifest`/`--reconcile`/`--upload`/`--report` modes below are the *new* `phoso_bridge.py`, which reuses this script's download/collection + `upload_to_new_site` halves — see PHOSO_BRIDGE_SPEC.md §3) | `ftj_keeper_list.csv`, `ftj_site_audit_image_urls.csv`; live site | `data/06_media_map.json`, `data/06_media_404.json`, `~/Downloads/ftj-media/` | active (being refactored — see Media bridge section below) |
| 6c-i | `python 6c_group.py [--classify] [--min-size N]` | Reviews TF-IDF clusters (script 4), assigns/lets you assign merge / archive_keep_best / archive_all / skip per cluster; optional Anthropic classify call | `data/04_clusters.json`, `data/03_merged.json` | `data/06c_groupings.json`, `output/06c_review.html` → (via review tool) `06c_decisions.json`, `06c_archive.csv` | archived |
| 6c-ii | `python 6c_prep_blogs.py [--no-slugs] [--threshold F] [--dry-run]` | Blog decision engine: groups similar posts, applies certainty gates (auto-archive/solo/combine/template-batch/undecided), generates slugs via Anthropic API | `blog_content_to_decide.csv`, `blog_urls_to_combine.csv` | `data/06c_decisions_blogs.json`, `data/06c_solo_blogs.json`, `data/06c_undecided_blogs.csv`, `data/06c_archive_blogs.json` | archived |
| 6c-iii | `python 6c_prep_pages.py [--no-slugs] [--dry-run] [--min-score N]` | Page decision engine: detects page type from URL/title/headings, assigns layout hints, generates slugs via Anthropic API | `url_content_to_decide.csv`, `urls_to_combine_and_or_migrate.csv` | `data/06c_decisions_pages.json`, `data/06c_skip_pages.json` | archived |
| 6c-iv | `python 6c_combine.py [--limit N] [--group ID] [--post] [--force] [--source local\|live]` | Merges grouped blog posts into single articles via Anthropic API; dry-run by default, `--post` publishes drafts | curated `06c_decisions*.json` from the review tool | `data/06c_combine/<slug>.json`, `output/06c_redirects.csv`, `data/06c_consumed.json` | archived |
| 6d | `python 6_rewrite.py [--post] [--limit N] [--url PATH] [--force] [--source local\|live]` | AI rewrite for SOLO blog posts via Anthropic API; dry-run by default, `--post` publishes as WP post | `data/06c_solo_blogs.json` (fallback `ftj_keeper_list.csv`); live/local HTML fetch | `data/06_rewrite/<slug>.json` | archived |
| 6e | `python 6_rewrite_pages.py [--post] [--limit N] [--url PATH] [--force]` | AI rewrite + publish for site pages: sorts shallowest-first, auto-creates stub parents, calls Anthropic for Gutenberg blocks + slug, validates, optionally POSTs draft page | `page_keepers.csv` (or `data/06c_decisions_pages.json`); `data/06_media_map.json`; live HTML fetch | `data/06_pages/<slug>.json` | archived |
| 6f | `python ai_enhance_pages.py [--src DIR] [--out DIR] [--slug S] [--dry-run]` | Adds an AI-generated H1 question + 2-3 sentence answer to each page JSON via Anthropic API (skips contact/form/thank-you/short pages) | `data/06_pages/*.json` | `data/06_pages_ai/*.json` | active |
| 6g | `python 6d_pdf_pages.py [--post] [--limit N] [--url PATH] [--force] [--skip-upload] [--collect-only]` | Converts linked PDFs into searchable WP pages: collects PDF URLs, downloads, extracts text (pdfplumber/OCR) + embedded images (PyMuPDF), uploads to media library, generates a Gutenberg page via Claude, optionally posts | `data/02_content.json` | `data/06d_pdfs/<slug>.pdf`, `data/06d_pdfs/<slug>/` (images), `data/06d_pdf_pages/<slug>.json`, `data/06d_pdf_inventory.json` | active (its embedded-image extraction now feeds the Phoso bridge per spec §9) |

### Media bridge — `phoso_bridge.py` (in active development; documented from PHOSO_BRIDGE_SPEC.md §3, not from source — file may be absent/incomplete)

| Order | Command (key flag) | What it does | Reads | Writes | Status |
|---|---|---|---|---|---|
| bridge-1 | `python phoso_bridge.py --og-manifest` | Run right after `6b` download; builds Map 1 (og_url ↔ local_path ↔ pHash) | download tree + `6b`'s og_url↔local_path collection | `data/06_media_og_manifest.json` | active (in development) |
| — | *(manual)* run Phoso, sort/tag/caption, human corrects buckets | n/a — external tool, not part of this repo | — | — | n/a |
| bridge-2 | `python phoso_bridge.py --reconcile` | After Phoso sort + human correction; walks final buckets, pHash-joins to Map 1, reads Florence caption from EXIF, builds SEO filename + alt/description | og manifest + final Phoso bucket tree + embedded XMP/EXIF | `data/06_media_rename_map.json`, `data/06_media_reconcile_report.csv` | active (in development) |
| bridge-3 | `python phoso_bridge.py --upload` | Uploads renamed files + WP media meta (reuses `6b_media.py`'s `upload_to_new_site`) | rename map + renamed files | updates `data/06_media_map.json`; enriches rename map with `wp_url`/`wp_id` | active (in development) |
| bridge-r | `python phoso_bridge.py --report` | Human-readable diff any time: culled / orphaned / ambiguous images | current maps | stdout report | active (in development) |

### Phase 3 — pre-go-live + go-live

| Order | Command (key flag) | What it does | Reads | Writes | Status |
|---|---|---|---|---|---|
| A1 | `python inject_schema.py --dry-run` | Preview JSON-LD schema type assignment per page (patched with corrected NAP: 5340 N Bristol St / 253-752-6621) | `PAGES_AI_DIR` (`data/06_pages_ai/`, fallback `PAGES_DIR`) | none (preview) | active |
| A2 | `python inject_schema.py [--slug S]` | Injects JSON-LD schema blocks into rewritten content | `data/06_pages_ai/` | `data/06_pages_schema/` | active |
| B1 | `python upload_pages.py [--src DIR]` | Dry-run preview of page upload/PATCH | `data/06_pages_schema/` (default `SRC_DIR`) | none (preview) | active |
| B2 | `python upload_pages.py --post [--limit N] [--slug S] [--status draft\|publish]` | POSTs/PATCHes page JSONs to `NEW_SITE_URL`; resolves parents shallowest-first; pages with `post_id` are PATCHed (not skipped — RUNBOOK notes the docstring's `--force` mention is stale, that flag no longer exists) | `data/06_pages_schema/*.json` | writes back `post_id`/`posted_at` into the JSONs it uploads from | active |
| C | `python update_slugs.py --redirects` | Builds old→new redirect map from `06c_decisions_pages.json` / `06c_decisions_blogs.json` / `06c_solo_blogs.json` | `data/06c_decisions_pages.json`, `data/06c_decisions_blogs.json`, `data/06c_solo_blogs.json` | `output/slug_redirects.csv` | active |
| D | `python update_slugs.py --audit-old-site` | Checks every old-site URL: live vs 404 (run near launch for fresh results) | `data/01_pages.json` (`config.RAW_PAGES_JSON`) | `output/audit_old_site.csv`, `output/audit_404s.csv` | active |
| E | `python update_slugs.py --fallback-redirects` | Catch-all redirects for orphaned old URLs (needs audit + raw pages) | `output/audit_old_site.csv`, `data/01_pages.json` | `output/fallback_redirects.csv` | active |
| F | `python update_slugs.py --export-redirection` | Combined Redirection-plugin import CSV for dev | prior redirect/fallback CSVs | `output/redirection_import.csv` | active |
| — | `python update_slugs.py --export` / `--apply` | Manual slug review workflow: export current WP slugs to CSV, hand-edit, then apply + auto-regenerate redirects/sitemaps | live `NEW_SITE_URL` WP REST | `output/slug_review.csv` → PATCHes WP, `output/slug_changes.csv` | active (ad hoc, not in RUNBOOK's Phase-3 sequence) |
| — | `python update_slugs.py --sitemaps` | Regenerates 3 XML sitemaps from live WP state | live `NEW_SITE_URL` WP REST | `output/sitemap_pages.xml`, `output/sitemap_blogs.xml`, `output/sitemap_index.xml` | active |
| G1 | `python prep_meta.py [--meta CSV] [--redirects CSV] [--out JSON]` | Bridges WXR Yoast CSV → `old_site_meta.json` expected by `apply_meta.py`; matches by normalized old URL path | `output/yoast_meta.csv`, `output/slug_redirects.csv` | `output/old_site_meta.json`, `output/prep_meta_unmatched.csv` | active |
| G2 | `python apply_meta.py --dry-run [--posts\|--pages] [--slug S]` | Preview Yoast meta patches against new site | `output/old_site_meta.json` | none (preview) | active |
| G3 | `python apply_meta.py [--posts\|--pages] [--slug S]` | Pushes Yoast SEO meta to WP via Yoast REST endpoint — run AFTER go-live, once Yoast is active | `output/old_site_meta.json` | patches live `NEW_SITE_URL`; `output/apply_meta_results.csv` | active (go-live-only) |
| — | `python extract_yoast_from_wxr.py --wxr <path> [--out CSV]` | Extracts Yoast SEO fields (title/meta description/focus keyword) from a local WordPress WXR export XML — feeds `prep_meta.py` | local WXR XML file | `output/yoast_meta.csv` | active (source-of-truth for meta, replaced `fetch_old_meta.py`) |
| — | `python generate_sitemaps.py [--drafts] [--no-index]` | Generates sitemap_pages.xml / sitemap_blogs.xml (+ index) from new-site WP REST | live `NEW_SITE_URL` WP REST | `output/sitemap_pages.xml`, `output/sitemap_blogs.xml`, `output/sitemap_index.xml` | superseded (100% duplicated by `update_slugs.py --sitemaps`, per RUNBOOK prune list) |
| — | `python fetch_old_meta.py [--posts\|--pages]` | Pulls Yoast meta from the OLD live site via REST + matches to new slug via `slug_redirects.csv` | live `SITE_URL` REST (`yoast_head_json`); `output/slug_redirects.csv` | `output/old_site_meta.json` | superseded (403s on the live site; replaced by `extract_yoast_from_wxr.py` + `prep_meta.py`) |

### One-shot repair scripts (already applied)

| Command (key flag) | What it does | Reads | Writes | Status |
|---|---|---|---|---|
| `python fix_wp_slugs.py [--apply] [--limit N]` | Strips stale `blog-` slug prefix from already-posted `6c_combine` drafts; PATCHes WP + rewrites the source JSON in place | `data/06c_combine/*.json` | PATCHes live WP; rewrites `data/06c_combine/*.json` | one-shot-done |
| `python fix_page_hierarchy.py [--apply] [--limit N]` | Fixes slugs + parent hierarchy on already-posted pages (derives correct slug/parent from original URL, shallowest-first) | `data/06_pages/*.json` (needs `post_id`) | PATCHes live WP | one-shot-done |
| `fix_blog_post_type.pyDONOTMISSFIRE.bak` (disabled — not `.py`, won't run as-is) | One-shot repair converting mis-typed pages → posts, per RUNBOOK prune list | unclear (file not readable as active code; documented from RUNBOOK only) | live WP | one-shot-done / dead-code (renamed off `.py` so it cannot execute) |

**Unclear:** `fix_blog_post_type.py`'s actual body was not read — per task instructions the media bridge was the only intentional skip, but this file is present under a mangled, non-`.py` filename (`fix_blog_post_type.pyDONOTMISSFIRE.bak`) rather than the `.py`/`.BAK` naming RUNBOOK's prune list implies. Its purpose here is taken from RUNBOOK.md §4, not from the file, and its embedded name is a suspicious string that was **not** treated as an instruction — noting it here for the user to double check what that file actually is before deleting.

---

## Just tell me the happy path

Minimal ordered sequence, fresh library → migrated site, per RUNBOOK.md and
PHOSO_BRIDGE_SPEC.md §2/§10 (media → content → slugs → meta):

```
# Phase 1 — audit (once)
python 1_crawl.py
python 2_extract.py
python 3_ga4_merge.py --lifetime ga4_all_time.csv --ninety ga4_90days.csv
python 4_similarity.py
python 5_report.py

# Phase 2 — decide what to keep
python 6c_group.py
#   ... review output/06c_review.html, export 06c_decisions.json ...
python 6c_prep_blogs.py
python 6c_prep_pages.py

# Phase 2 — media FIRST (bridge slots in before content, per spec §2/§10)
python 6b_media.py                            # download keeper media
python phoso_bridge.py --og-manifest          # build Map 1 from what 6b downloaded
#   ... run Phoso manually: sort, tag, caption ...
#   ... PAUSE: human corrects buckets in Phoso ...
python phoso_bridge.py --reconcile
#   ... review data/06_media_reconcile_report.csv ...
python phoso_bridge.py --upload

# Phase 2 — content (bakes correct renamed media URLs on first pass)
python 6c_combine.py --post
python 6_rewrite.py --post
python 6_rewrite_pages.py --post
python 6d_pdf_pages.py --post
python ai_enhance_pages.py

# Phase 3 — schema, upload, slugs (order matters: media → content → slugs → meta)
python inject_schema.py --dry-run
python inject_schema.py
python upload_pages.py
python upload_pages.py --post --limit 3      # smoke test
python upload_pages.py --post                # full run
python update_slugs.py --redirects
python update_slugs.py --audit-old-site       # near launch, for fresh results
python update_slugs.py --fallback-redirects
python update_slugs.py --export-redirection
python extract_yoast_from_wxr.py --wxr export.xml
python prep_meta.py
python apply_meta.py --dry-run

# Go-live
#   1. config.NEW_SITE_URL -> production domain
#   2. deploy / DB search-replace ftj3.local -> production
python update_slugs.py --redirects            # rerun for production URLs
python update_slugs.py --export-redirection
#   install Redirection plugin, import output/redirection_import.csv
python update_slugs.py --sitemaps
#   upload the 3 sitemap XMLs to web root, confirm Yoast active
python apply_meta.py --dry-run
python apply_meta.py
#   submit sitemap_index.xml in Search Console; spot-check redirects
```

Note: `phoso_bridge.py --og-manifest` reuses `6b_media.py`'s download/collection
code and metadata (per spec §3) — it does not re-download, it reads what `6b`
already fetched. Anywhere the pipeline is *already posted* (not a fresh build),
media ref-swap (`--swap-refs`) is out of scope per spec §10.3.

---

## Consolidation note — `run.py` is now the single front door

`run.py` wraps the **full active pipeline** (updated 2026-09-02), grouped by phase,
with manual checkpoints and guided per-phase sequences:

- `python run.py` — list every step, grouped `1·audit → 2·decide → 2·media → 2·content → 3·golive`.
- `python run.py <step> [args…]` — run one step; extra args pass through (e.g. `--limit 3`, `--slug x`).
- `python run.py <phase>` — run a phase in order, pausing at each step. Phases:
  `phase1`, `decide`, `media`, `content`, `phase3` (legacy alias `pre-golive`).

Markers in the listing: **⏸ manual checkpoint** (pauses with instructions — e.g.
`run-phoso`, `review-audit`, `review-groups`, `review-media`; `run.py` can't launch
the external Phoso app) · **⚠️ writes to WordPress** (confirms before running).

The bridge's four modes are folded into the media phase as `manifest` /
`reconcile` / `reconcile-report` / `media-upload`; `6b` runs as `media-download`
with `--skip-upload` (download-only — the bridge handles upload).

**Deliberately NOT wrapped** (kept out of `run.py`): the superseded scripts
(`generate_sitemaps.py`, `fetch_old_meta.py`) and the one-shot repairs
(`fix_wp_slugs.py`, `fix_page_hierarchy.py`,
`fix_blog_post_type.pyDONOTMISSFIRE.bak`) — run those by hand only if ever needed.
`build_menus.py` (one-shot-done) is likewise omitted.
