# domain-parser — Roadmap & Session Handoff
**Updated:** 2026-09-29 · read this first if you're a new session.

---

## TL;DR

- **domain-parser** = a WordPress **migration toolkit**: audit an old site → AI-rewrite
  its content → publish to a new WP build. Original client: **franketobeyjones.com (FTJ)**.
- **Companion app:** `E:\image_sorter` (aka **"Phoso"**) — a local SigLIP + Florence-2
  image sorter that tags/captions/buckets photos. It feeds the **media bridge**
  (`phoso_bridge.py`). *Separate project, not in this git repo.*
- **Git:** this repo is at **github.com/eshowntell/domain-parser** (private). `git push`
  works via cached credential manager (no `gh` needed).
- **Active LLM:** **DeepSeek** (`deepseek-chat`) — the Anthropic account is out of credits.
  Provider is switchable per-task; see below.
- **Secrets:** all in `.env` (git-ignored) — Anthropic/DeepSeek/Mistral keys + WP passwords.

### ▶ Immediate next step
Verify the **image fix** (see §"Where things stand"):
```bash
python run.py rewrite-pages --post --force
```
Confirm the 6 apartment thumbnails land on the ftj4.local "Living Options" page (id 2744).

---

## Where things stand

| Area | Status |
|---|---|
| Git repo + first push | ✅ done (private, clean 32-file repo) |
| Multi-provider LLM router (`LLM_TASKS` → anthropic/deepseek/mistral/ollama) | ✅ done; **DeepSeek active** |
| Limited page test (living-options-overview) | ✅ content works end-to-end: rewrite→enhance→schema→upload to ftj4.local (page 2744) |
| **Image extraction fix** | ⏳ **built, NOT yet verified** — needs the `--force` rerun above |
| Phoso **media bridge** (`phoso_bridge.py`) | ✅ built; `--reconcile` pHash-join proven on real images (14/14, dist 0); `--upload` untested (needs live WP) |
| Phoso **learning fixes** (in `E:\image_sorter`) | ✅ done + tested (see §Companion) |
| `run.py` single-front-door runner | ✅ done (all phases + manual checkpoints) |
| Full FTJ migration (real run) | ⬜ not started — only a 1-page smoke test so far |

### The image fix (why the `--force` rerun matters)
FTJ's production site is behind **Cloudflare/WP Engine** and serves a stripped 4 KB shell to
bot-like user agents (no images). Two fixes landed:
1. `utils.py` `_HEADERS` → a real Chrome User-Agent (gets the full 121 KB page + working REST).
2. `rewrite_utils.py` `extract_content_from_html` → reads lazy `data-src`, and the size filter
   was changed from OR to AND (thumbnails are 137×75 — the old `height<100` filter dropped them).

Verified: the scrape now returns all 6 images. But the last `rewrite-pages` run **skipped** the
page because its JSON already existed (`PHASE2_SKIP_DONE`), so the fix hasn't been applied to a
live rewrite yet — hence `--force`.

---

## Roadmap / TODO (rough priority order)

1. **Verify image fix** — `rewrite-pages --post --force`; confirm images on ftj4 page 2744.
2. **Full media flow** (needs a real keeper media set):
   `6b_media.py --skip-upload` (download) → run **Phoso** (sort/caption/correct) →
   `phoso_bridge.py --og-manifest → --reconcile → --upload` → writes `06_media_map.json` →
   re-rewrite content so images resolve to the new ftj4 media library.
3. **Wire `--og-manifest` into `6b`** so Map 1 is written automatically at download (optional convenience).
4. **Provider failover** (designed, not built): make `LLM_TASKS` values a *chain*
   (e.g. `["anthropic","deepseek"]`), try in order, cache "provider dead this run" on
   credit/auth failure, reset next process. Fits the fluctuating-credits situation.
5. **`resolve_model()` auto-fallback** (discussed, not built): query `/v1/models`, pick
   "latest minus one" in a pinned tier if the configured model is deprecated.
6. **Full FTJ migration** — the real end-to-end run (see RUNBOOK.md / COMMANDS.md "happy path").
7. **Genericize into a reusable template** (user deferred): parameterize org identity
   (name/description/NAP/URLs/categories) into config so the toolkit isn't FTJ-hardcoded.

---

## Key context & gotchas

- **Current config target** (`config.py` TARGET SITES block):
  `SITE_URL` + `PHASE2_SOURCE_URL` = `https://www.franketobeyjones.com` (**live scrape** —
  the local `legacyftj.local` copy was abandoned because the `.wpress` export is **9.2 GB**,
  too big for the free All-in-One WP Migration import). `NEW_SITE_URL` = `http://ftj4.local`.
  `SCHEMA_BASE_URL` stays production (baked into JSON-LD).
- **LLM providers:** `config.LLM_TASKS` routes each task; all currently → `"deepseek"`.
  Keys/models in `config.OPENAI_PROVIDERS` (deepseek-chat, mistral-large-latest) +
  `.env` (`DEEPSEEK_API_KEY`, `MISTRAL_API_KEY`). Generation knobs renamed to
  `LLM_MAX_TOKENS` / `LLM_TEMPERATURE` (were `ANTHROPIC_*`). Router lives in
  `rewrite_utils.llm_complete()` (handles anthropic / ollama / openai-compatible).
- **Anthropic key** in `.env` is the one RUNBOOK flags as previously exposed — **rotate it** eventually.
- **`gh` CLI is NOT installed.** Git pushes work via cached Git Credential Manager creds.
  Remote: `origin` → github.com/eshowntell/domain-parser.
- **Local WP sites:** `ftj4.local` (new build target). `legacyftj.local` exists but is mostly
  empty (the big import was skipped). WP app passwords are in `.env`.
- **`archive/`** (git-ignored) holds retired scripts (`fetch_old_meta`, `generate_sitemaps`,
  `build_menus`, `fix_page_hierarchy`, `fix_wp_slugs`), the `.bak` backups, the disabled
  `fix_blog_post_type` one-shot, and all prior test-run data. `data/` + `output/` are empty.
- **Skipping steps for tests:** each pipeline step just needs its input file to exist. For the
  1-page test we hand-wrote `data/06c_decisions_pages.json` (now archived) to feed
  `rewrite-pages` without running the whole audit.

## Reference docs (in-repo)
- **COMMANDS.md** — every command + how `run.py` wraps them (single front door).
- **PHOSO_BRIDGE_SPEC.md** — full media-bridge design (maps, pHash join, rename, ordering).
- **RUNBOOK.md** — go-live checklist (partly stale / FTJ-migration-state specific).
- **README.md** — original setup notes.

---

## Companion app: `E:\image_sorter` (Phoso) — changes made this session
Not in this git repo, but touched heavily. If media work resumes, know that:
- **KB learning was fixed**: pixel-hash file identity (metadata-invariant), the drag-correction
  lookup bug, a per-correction **✕ delete** in Manage KB, injection threshold → 1, and a
  **staged-commit** review flow (reassignments queue; nothing moves/learns until "Commit").
- **`FLORENCE_ALWAYS = True`** (florence.py) — captions every image (not just low-confidence),
  because SigLIP-template fallbacks produced wrong names.
- **Speed knobs**: `FLORENCE_MAX_NEW_TOKENS = 256`; OCR gated to text categories only.
- **`pyexiv2`** pinned in its `requirements.txt`. Run it via `run_desktop.bat` (venv_desktop has pyexiv2).
- All backups there are `*.pre-*.bak` next to each edited file.
