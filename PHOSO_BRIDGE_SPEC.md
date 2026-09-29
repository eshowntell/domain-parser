# domain-parser ⇄ Phoso Bridge — Design Spec
**Status:** draft for review · **Updated:** 2026-09-01

How the media-migration phase hands images to **Phoso** (the locked, external
SigLIP + Florence-2 image sorter) for tagging/captioning/bucketing, waits for a
human correction pass, then reconciles the corrected output back into the
pipeline — renamed, mapped, and ready to upload to the new domain's media library.

The same bridge serves both site media (`6b`) and PDF-extracted images (`6d`).

---

## 1. Principles (the load-bearing decisions)

1. **The bridge treats Phoso's output as a black box.** The bridge never imports
   or drives Phoso and requires no Phoso code changes to do its job — it reads the
   output folder + embedded metadata. (One deliberate *config* change was made to
   the Phoso app itself, separate from the bridge: `FLORENCE_ALWAYS = True` in
   `florence.py`, so it captions **every** image — see §5/§7. That's a pipeline
   decision, not a bridge dependency.) Phoso is run manually by the user (sort
   **and** correct). No Qt in this repo, nothing to double-maintain.
2. **The bridge owns the maps.** Phoso doesn't know where an image came from on
   the web — *we* do, because `6b` downloaded it. The og→new linkage is
   reconstructed entirely on our side.
3. **Join on content, not filename.** Filenames collide (`image.jpg` from two
   folders) and Phoso appends `_1`/`_2` on collision. We match og↔final by
   **perceptual hash (pHash)**, which survives Phoso's copy, metadata embedding,
   *and* the user's bucket moves — because Phoso only ever rewrites metadata,
   never pixels.
4. **Media finalizes before content; content before slugs.** On a **fresh**
   library we slot Phoso *before* the content rewrite, so the correct renamed
   media URLs are baked into content on the first pass — no post-hoc page PATCH.
5. **Correction is also the cull.** Images the user deletes during the Phoso
   correction pass simply aren't in the final tree, so they never upload.
   "Don't keep what we won't publish" falls out for free.

---

## 2. Where it slots in the pipeline

```
6b download ──► phoso_bridge --og-manifest        (Map 1: og_url ↔ local_path ↔ pHash)
                        │
                run Phoso (manual; Florence captions EVERY image): sort → tag → buckets
                        │
                ⏸ PAUSE — user corrects misplaced images in Phoso
                        │
                phoso_bridge --reconcile           (walk FINAL buckets, pHash-join,
                        │                            caption → SEO name, Map 2 merge)
                        │
                phoso_bridge --upload              (upload renamed + meta → new domain,
                        │                            write 06_media_map.json)
                        ▼
                6_rewrite / content ──► update_slugs ──► apply_meta
                 (bakes correct       (slug/redirect      (Yoast meta)
                  image refs via         layer)
                  existing map_image)
```

`06_media_map.json` (`og_url → {new_wp_url, id}`) is the **unchanged hand-off**
the content rewrite already consumes via `rewrite_utils.map_image`. The bridge's
whole job is to produce that map with **Phoso-renamed** URLs. Nothing downstream
changes.

---

## 3. Modules / entry point

One flag-driven script, `phoso_bridge.py`, mirroring the multi-mode style of
`update_slugs.py`:

| Mode | When | Reads | Writes |
|---|---|---|---|
| `--og-manifest` | right after `6b` download | download tree + `6b`'s og_url↔local_path collection | `data/06_media_og_manifest.json` |
| `--reconcile` | after Phoso sort **and** human correction | og manifest + final Phoso bucket tree + embedded XMP | `data/06_media_rename_map.json` + `data/06_media_reconcile_report.csv` |
| `--upload` | after reconcile is reviewed | rename map + renamed files | updates `data/06_media_map.json`, enriches rename map with `wp_url`/`wp_id` |
| `--report` | any time | current maps | human-readable diff (culled / orphaned / ambiguous) |

`6b_media.py` is refactored so its **download + collection** half stays and gains
og-manifest writing; its **upload** half (`upload_to_new_site`) is reused by
`phoso_bridge --upload` rather than run standalone.

---

## 4. Map 1 — the "og" half (built at download)

`data/06_media_og_manifest.json` — one record per downloaded media file:

| Field | Source | Purpose |
|---|---|---|
| `og_url` | `6b` collection | the redirect-FROM URL referenced in old content |
| `og_name` | basename of `og_url` | original filename |
| `og_library_path` | `wp-content/uploads/…` portion of `og_url` | where it lived in the old library |
| `local_path` | `6b` `local_path_for()` | where Phoso will read it FROM (= Phoso's `source_file`) |
| `phash` | computed on the downloaded pixels | **primary join key** |
| `sha256` | downloaded bytes | exact-dup detection / integrity |
| `found_on` | `6b` collection | pages that reference this media (for later ref-swap) |
| `wp_old_id` | if known | reference |
| `is_video` | extension check | videos bypass Phoso (see §9) |

Because `local_path` preserves the `wp-content/uploads/...` structure, `og_url`
is also reconstructable from it — but we record it explicitly so the join never
depends on string surgery.

---

## 5. Phoso run (manual, external, unchanged)

User runs Phoso pointed at the download tree (**keeper media only** — 6b's
collection is already keeper-scoped, so nothing non-published reaches Phoso).
Phoso, as it already does:
- classifies via SigLIP into `output/<category>/` buckets (non-destructive copy),
- runs **Florence-2 on EVERY image** (`FLORENCE_ALWAYS = True`) → caption + OCR
  written to XMP, and the caption also patched into EXIF `ImageDescription` by the
  describe pass; plus category/subcategory/scores (IPTC/EXIF) and color palette (XMP),
- writes `classification_report.csv` (`source_file`, `category`, `confidence`, …),
- may create `_multi/<a>+<b>/` duplicate copies for dual-category images.

> **Florence-always (2026-09-01):** the old trigger logic skipped confident
> portrait/nature/food/sports/architecture images, and those fell back to SigLIP
> *template* descriptions that were frequently wrong on the subject (people →
> "a bird" / "a cat" / "a natural landscape"). Since wrong filenames are
> unacceptable, `should_trigger_florence()` now always returns True. Cost: every
> image pays the Florence tax (~5–15× the SigLIP-only per-image time; two
> `generate()` calls). Acceptable for a bounded one-time migration.

Then the user **corrects** misplaced images by moving them between buckets in
Phoso, and **deletes** any they don't want to publish.

> ⚠️ After correction, `classification_report.csv` is a *pre-correction snapshot*
> and its `category` column is stale for moved files. The bridge does **not**
> trust it for category — folder location is the source of truth (§6).

---

## 6. Map 2 + merge — `--reconcile` (the core algorithm)

Runs **after** correction. Produces `data/06_media_rename_map.json`.

```
1. Enumerate final files: walk output/<category>/**, SKIPPING _multi/ (those are
   duplicate copies of a primary already counted).
2. For each final file f:
     a. category   = f's immediate parent folder   ← authoritative (post-correction)
     b. phash_f    = pHash(pixels of f)
     c. match f → Map 1 by phash (Hamming distance; prefer exact 0, guard ≤ 4)
          - unique match  → carry og_url, og_name, og_library_path, found_on
          - 0 matches     → ORPHAN (user-added image, or format-converted) → flag
          - >1 matches    → AMBIGUOUS (true visual dup) → disambiguate by
                            local_path/og_name, else flag for manual review
     d. caption    = read EXIF ImageDescription (0x010E) via PIL   ← PRIMARY name source
                     (with FLORENCE_ALWAYS, this is a real Florence caption for
                      EVERY image, e.g. "the image shows two hamburgers on a white
                      plate" — accurate, descriptive)
        ocr          = read Xmp.phoso.OCRText via pyexiv2   ← OPTIONAL enrichment only
        (category/scores also in IPTC keywords: category:<id>, subcat:<id>, …)
     e. new_name   = seo_slug(caption) + original_ext   (dedup with -1/-2)
        alt_text   = caption trimmed to ≤125 chars
        description= full caption (+ OCR when has_text)
3. Cull check: any Map 1 entry with NO final-file match = culled or quarantined
   → recorded in the report, NOT uploaded.
4. Write rename map + reconcile report.
```

`data/06_media_rename_map.json` — one record per **kept** image:

| Field | Meaning |
|---|---|
| `og_url`, `og_name`, `og_library_path` | from Map 1 (via pHash join) |
| `phash` | the join key |
| `final_category` | corrected bucket (folder) |
| `caption`, `ocr`, `has_text` | from embedded XMP |
| `new_name` | SEO filename from caption |
| `alt_text`, `description` | WP media meta to attach at upload |
| `final_local_path` | the corrected on-disk file to upload |
| `wp_url`, `wp_id` | filled by `--upload` |

`06_media_reconcile_report.csv` surfaces everything needing eyes:
**orphans**, **ambiguous pHash matches**, **culled originals**, and a
**`caption_source`** flag (`florence` vs `template`) per row — detected by whether
`Xmp.phoso.Description` exists / whether the EXIF text matches the SigLIP template
shape. With `FLORENCE_ALWAYS` these should all be `florence`; a stray `template`
row now means Florence *failed* on that one image (not that it was skipped) — a
useful red flag for the override pass (§7a).

> Note: Florence is enabled by the "Embed EXIF/IPTC tags into copied files"
> checkbox (on by default) **and** now runs on every image via `FLORENCE_ALWAYS`.
> Confirmed loading reliably on the run box (`phoso_run.log`: ready every run,
> ~3.8 GB VRAM, CUDA, no errors). Keep that box checked on the real run.

---

## 7. SEO rename policy

> **Finding:** Phoso does **not** rename image files — verified across the sort
> loop, `florence.py`, and `tagger.py` (the only `.rename()` in Phoso touches a
> *config* file). It writes a caption into `Xmp.phoso.Description` but never
> changes the filename. **Decision: Option A** — the bridge renames from that
> caption, keeping Phoso locked.

**Option A — bridge renames (SELECTED). With `FLORENCE_ALWAYS`, the caption is the primary source:**

> **Why it landed here:** early runs showed the SigLIP *template* fallback was
> frequently wrong on the subject (people → "a bird" / "a cat" / "a natural
> landscape"), and Florence's old trigger logic skipped exactly FTJ's dominant
> categories. Rather than name from unreliable data, we set **`FLORENCE_ALWAYS =
> True`** so Florence captions **every** image — verified accurate on the real run
> (`two-hamburgers-white-plate`, `mannequin-wearing-orange-harness`,
> `little-girl-standing-front-mirror`). So the caption is now trustworthy across
> the board, and naming keys off it.

**Primary name source = the Florence caption** (EXIF `ImageDescription`), slugified:
- `seo_slug(caption)`: strip the "the image shows" lead, drop light stopwords,
  hyphenate, cap ~5–8 words / ~60 chars, keep the original extension, dedupe with
  `-1`/`-2`. e.g. *"The image shows two hamburgers on a white plate"* →
  `two-hamburgers-white-plate.jpg`.
- **Fallback = the human-corrected `final_category` (+ subcategory)** only for the
  rare image where a caption is missing (Florence failed → `caption_source ==
  template`/none): `photo_portrait_two_people` → `two-people-portrait.jpg`. Never
  slug from a `template` caption's subject.
- **Human override** (§7a) is the final say for anything the report flags.

Result: accurate, descriptive filenames on **every** image, with a reliable
category-based safety net for the rare caption miss — never the "a-bird.jpg for a
person" failure.

### 7a. Human name override (available, rarely needed now)
With Florence on every image, the auto-slug is accurate for the vast majority — but
`--reconcile` still writes the **proposed `new_name`, caption, `caption_source`,
and `final_category` into the reconcile report** and **pauses for review**, so the
user can edit `new_name` before `--upload` reads it. The rows worth a glance are
the `caption_source == template`/none ones (Florence failed → on the category
fallback) and any caption that reads oddly. The bulk sails through untouched.

**Option B — no rename:** `new_name = og_name`; migrate meta only. Simpler; the
`og_name` join stays trivial; no SEO filenames.

> WordPress **sanitizes and de-dupes filenames on upload** — so `new_name` is the
> *intended* name; the authoritative URL is whatever WP returns as `source_url`.
> `--upload` records that, never the intended name (§8).

---

## 8. `--upload` — push + build the hand-off map

For each kept record (reusing `6b`'s `upload_to_new_site`, extended):
1. Upload `final_local_path` under `new_name` to `/wp-json/wp/v2/media`.
2. In the same call (or a follow-up PATCH), set WP `title`, `alt_text`,
   `caption`, `description` from the rename map.
3. Read back WP's actual `source_url` + `id`.
4. Write **`06_media_map.json`**: `og_url → {url: <wp source_url>, id}` — the
   exact shape `map_image` already expects. Also mirror `wp_url`/`wp_id` back
   into the rename map for auditing.

After this, the content rewrite runs unchanged and swaps every old image URL for
the new renamed WP URL via the existing media-map mechanism.

---

## 9. PDF images (`6d`) reuse

`6d_pdf_pages` already extracts images from each PDF into `data/06d_pdfs/<slug>/`.
Scale: ~300 pages, ~140 linked PDFs. **Process one keeper PDF at a time**
(6d already supports this via `--url` / `--limit`) — a per-PDF Phoso run keeps
each correction batch small and reviewable, and isolates failures to one document.

Those extracted images feed the **same** bridge:
- per keeper PDF: `6d` extract → Phoso that PDF's image folder → correct →
  `--reconcile` → `--upload`.
- Same pHash join, same rename, same `06_media_map` (PDF image og "URL" = its
  extracted local path or a synthesized key).
- The PDF's own file still uploads as today; only its **embedded images** route
  through Phoso. One component, two callers.

---

## 10. Ordering rules (must hold)

1. **media → content → slugs → meta.** Never finalize content before media URLs
   are known.
2. On a **fresh** library, run Phoso + `--upload` **before** `6_rewrite` so refs
   bake correctly the first time (no live-page PATCH).
3. If content is *already* posted (not the fresh case), a separate
   `--swap-refs` pass would PATCH posted pages old_url→new_url by `post_id`
   — **out of scope for the fresh build**, noted only for completeness.
4. `update_slugs` and `apply_meta` run **after** media + content are settled,
   exactly as in `RUNBOOK.md`.

---

## 11. Dependencies

- `imagehash` (pHash) + `Pillow` (have) — the join **and** the caption read.
  Captions come from **EXIF ImageDescription via PIL** (verified populated on every
  image), so the critical path needs no extra metadata library.
- `pyexiv2` — **optional**, only to pull `Xmp.phoso.OCRText` (and the richer
  `Xmp.phoso.Description`) for alt-text enrichment. Finicky on Windows (needs the
  exiv2 lib); kept off the critical path deliberately.
- Phoso's own heavy stack (torch/transformers/SigLIP/Florence) lives in **Phoso**,
  not here — the bridge never imports it. This repo stays light.

---

## 12. Edge cases & handling

| Case | Handling |
|---|---|
| `_multi/` dual-category copies | skipped in reconcile (duplicates of a primary) |
| True visual duplicates (same pHash, 2+ og) | disambiguate by local_path/name; else flag |
| Phoso-quarantined / unreadable | no bucket file → recorded as culled, not uploaded |
| User-deleted during correction | absent from final tree → culled (intended) |
| Orphan in output (no og match) | flagged; user decides keep/drop |
| Caption-less image (Florence failed on it) | rare with FLORENCE_ALWAYS; rename falls back to corrected category (+subcat); flagged as `template`/none |
| Format conversion by Phoso (e.g. HEIC→JPG) | pHash still matches; use actual bytes' ext |
| Videos (.mp4/.mov) | bypass Phoso entirely; basic filename/page-context meta only |

---

## 13. Decisions

| # | Decision | Status |
|---|---|---|
| 1 | **Caption read path** | ✅ **EXIF ImageDescription via PIL** (primary, universal — verified on real output); `pyexiv2`/`Xmp.phoso.OCRText` optional enrichment |
| 1b | **Caption quality** | ✅ **Resolved by `FLORENCE_ALWAYS = True`** (florence.py). Early runs showed SigLIP-template fallback names were subject-wrong (people → "bird"/"cat"/"landscape") and Florence skipped FTJ's dominant categories. Now Florence captions **every** image (verified accurate), so the caption is the **primary** name source (§7); category is the fallback only for a rare Florence failure. Cost: ~5–15×/image runtime |
| 2 | **Feed scope** | ✅ **Pre-filter to keepers first** — only keeper media goes to Phoso; correction culls further |
| 3 | **PDF handling** | ✅ **One keeper PDF at a time** (6d `--url`), per-PDF Phoso run |
| 4 | **Who renames** | ✅ **A — bridge renames.** Primary = **Florence caption** (accurate for every image via FLORENCE_ALWAYS); category+subcat is the fallback for a rare caption miss (see §7). Bridge reads Phoso output only |
| 5 | **pHash threshold** | ✅ default: match at Hamming **0** (pixels are byte-identical post-Phoso); allow ≤4 as a safety cushion, flag anything matching only above 0. Internal knob, not a workflow choice |
| 6 | **Rename flavor** | ✅ default **caption slug** (strip "the image shows" lead, ~5–8 words, ~60 chars); a `--category-prefix` toggle can prepend `events-…`. Category fallback only when no caption |

---

## 14. Explicitly out of scope

- Further Phoso code changes for the *bridge* (it reads output only). The one
  deliberate app change already made — `FLORENCE_ALWAYS = True` in `florence.py`
  (caption everything) — stands; it's a pipeline decision, not a bridge dependency.
- Content re-PATCH / live-page ref swap (fresh build bakes refs on first pass).
- The LLM provider (Ollama/Anthropic) work — separate track.
- Video understanding — images only.
