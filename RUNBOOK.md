# FTJ Migration — Canonical Runbook
**Updated:** 2026-06-11 · supersedes all previous run-order docs and the session briefing

This is the single source of truth for what runs, in what order, from here to launch.
Phases 1 and 2 are DONE. Only Phase 3 + go-live remain.

---

## 0. Current state (as of this doc)

| Thing | State |
|---|---|
| Phase 1 audit (scripts 1–5) | ✅ done |
| Media migration (6b) | ✅ done |
| Blog combine + solo rewrites (6c_*, 6_rewrite) | ✅ done, posted |
| Page rewrites (6_rewrite_pages) | ✅ done, posted |
| AI intros (ai_enhance_pages → 06_pages_ai) | ✅ done, uploaded 6/10 |
| Schema injection (inject_schema → 06_pages_schema) | ⚠️ ran with WRONG phone+address — must redo |
| Schema upload | ❌ not yet uploaded (good — bad NAP never went live) |
| Redirect map / fallbacks / Redirection CSV | ❌ pending |
| Yoast meta push | ❌ pending; needed missing bridge script (now exists: prep_meta.py) |

**Canonical page-data folder is now `data/06_pages_schema/`.**
`06_pages/` and `06_pages_ai/` are upstream snapshots — never upload from them again,
because post_id/posted_at updates only get written back to the folder you upload from.

---

## 1. ONE-TIME FIXES (do these first, today)

1. **Rotate the Anthropic API key.** It's plaintext in config.py and that config has now
   been pasted into shared/combined files. Generate a new key, update config.py,
   revoke the old one. (Consider env vars: `os.environ["ANTHROPIC_API_KEY"]`.)

2. **Drop in the patched `inject_schema.py`** (fixes: real address 5340 N Bristol St,
   real phone 253-752-6621, removes invalid `SeniorLivingOrganization` type, kills
   placeholder FAQ/Event markup, removes FTJ address from TouristAttraction pages,
   omits broken per-page URLs, adds `SCHEMA_BASE_URL`).

3. **Add to config.py:**
   ```python
   # Domain to bake into JSON-LD schema (production, not ftj3.local)
   SCHEMA_BASE_URL = "https://www.franketobeyjones.com"
   ORG_PHONE   = "+12537526621"
   ORG_ADDRESS = {
       "@type":           "PostalAddress",
       "streetAddress":   "5340 N Bristol St",
       "addressLocality": "Tacoma",
       "addressRegion":   "WA",
       "postalCode":      "98407",
       "addressCountry":  "US",
   }
   ```
   ⚠️ Confirm with the client whether the new site keeps `www.franketobeyjones.com`.
   If yes, SCHEMA_BASE_URL is set-and-forget; schema is correct on day one.

4. **Drop in `prep_meta.py`** (new) and `run.py` (optional runner).

---

## 2. PHASE 3 — run order (pre-go-live, all safe to run now)

Everything below targets ftj3.local. Use `run.py` or run manually in this order:

```
# A. Re-inject schema with corrected NAP (reads 06_pages_ai → writes 06_pages_schema)
python inject_schema.py --dry-run        # sanity-check type assignments
python inject_schema.py                  # overwrite 06_pages_schema with fixed schema

# B. Upload schema'd pages (PATCHes existing pages — post_ids preserved)
python upload_pages.py                   # dry-run preview
python upload_pages.py --post --limit 3  # smoke test 3 pages, eyeball in WP admin
python upload_pages.py --post            # full run

# C. Redirect map
python update_slugs.py --redirects             # old→new map → output/slug_redirects.csv

# D. Old-site URL health (run NEAR launch so results are fresh)
python update_slugs.py --audit-old-site        # → output/audit_old_site.csv

# E. Orphan catch-alls (needs the audit + 01_pages.json)
python update_slugs.py --fallback-redirects    # → output/fallback_redirects.csv

# F. Combined Redirection-plugin import for dev
python update_slugs.py --export-redirection    # → output/redirection_import.csv

# G. Yoast meta bridge (NEW — converts WXR CSV into what apply_meta.py expects)
python prep_meta.py                            # yoast_meta.csv + slug_redirects.csv
                                               #   → output/old_site_meta.json
python apply_meta.py --dry-run                 # verify matching looks right
# (actual apply_meta run happens AFTER go-live — see below)
```

Spot-check after B: view 2–3 pages in WP admin → code editor; confirm the
`<script type="application/ld+json">` block shows **5340 N Bristol St** and the
production domain. Paste one into https://validator.schema.org.

---

## 3. GO-LIVE checklist (in order)

1. `config.NEW_SITE_URL` → production domain.
2. Deploy/migrate the site. If your migration tool does a DB search-replace of
   `ftj3.local` → production domain, any stragglers self-heal. If not, the schema
   is already production-domained thanks to SCHEMA_BASE_URL — only embedded media
   URLs in content need the search-replace.
3. Rerun `python update_slugs.py --redirects` so the redirect CSV carries
   production URLs in the `new_url` column.
4. Rerun `python update_slugs.py --export-redirection`.
5. Install Redirection plugin on prod → import `output/redirection_import.csv`.
6. `python update_slugs.py --sitemaps` → upload the 3 XMLs to web root
   (sitemap_index.xml, sitemap_pages.xml, sitemap_blogs.xml).
7. Confirm Yoast active on prod, then:
   `python apply_meta.py --dry-run` → `python apply_meta.py`
   Check `output/apply_meta_results.csv` for failures.
8. Submit sitemap_index.xml in Google Search Console; spot-check 5 redirects
   from `slug_redirects.csv` in a browser.

---

## 4. PRUNE LIST

### Safe to delete (superseded or one-shot repairs already executed)
| File | Why |
|---|---|
| `fetch_old_meta.py` | 403'd; superseded by extract_yoast_from_wxr.py + prep_meta.py |
| `generate_sitemaps.py` | 100% duplicated by `update_slugs.py --sitemaps` (same outputs) |
| `fix_blog_post_type.py` | one-shot repair (pages→posts), already applied |
| `fix_wp_slugs.py` | one-shot repair (stale `blog-` prefixes), already applied |
| `fix_page_hierarchy.py` | one-shot repair (slugs/parents), already applied |
| `build_menus.py` | one-time menu JSON generation, already produced 02_menus.json |
| `phase_2_run_order.txt`, `phase 3 run order.txt`, `PHASE2c_NOTES.md` | superseded by this runbook |

Tiny cleanup if you delete fetch_old_meta.py: apply_meta.py's "file not found" error
message says "Run: python fetch_old_meta.py" — change to "Run: python prep_meta.py".

### Archive (done their job; keep zipped somewhere, out of the working folder)
`1_crawl.py  2_extract.py  3_ga4_merge.py  4_similarity.py  5_report.py
 6b_media.py  6c_group.py  6c_prep_blogs.py  6c_prep_pages.py
 6c_combine.py  6_rewrite.py  6_rewrite_pages.py  ai_enhance_pages.py`
⚠️ Keep the **data** they produced — `data/01_pages.json` is a live dependency of
`--audit-old-site` and `--fallback-redirects`, and the 06c decision JSONs feed
`--redirects`. Archive scripts, not data.

### Keep active (the whole remaining pipeline)
`config.py  utils.py  inject_schema.py  upload_pages.py  update_slugs.py
 extract_yoast_from_wxr.py  apply_meta.py  prep_meta.py  run.py  requirements.txt`

---

## 5. GOTCHAS (standing rules)

- **PHASE2_LIMIT = 3 is still set in config.py.** 6_rewrite / 6_rewrite_pages /
  6c_combine default to it. Irrelevant for Phase 3 scripts, but if you ever rerun
  a rewrite, pass `--limit 0` or change config.
- **Never `--force --post` together** when WP already has content (existing rule).
- **upload_pages.py docstring is stale**: real default src is `06_pages_schema/`,
  and `--force` no longer exists (post_id ⇒ PATCH automatically).
- **post_id lives in the JSONs.** If a JSON has post_id, uploads UPDATE that WP page.
  Deleting a page in WP admin without clearing post_id in the JSON ⇒ failed PATCHes.
- All Phase-3 scripts hit whatever `config.NEW_SITE_URL` points at. Double-check it
  before any `--post`/`--apply` once production credentials exist in config.
