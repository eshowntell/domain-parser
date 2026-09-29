#!/usr/bin/env python3
"""
ai_enhance_pages.py  —  Add AI-friendly H1 question + answer summary to page JSONs.

Reads every JSON from a source folder (06_pages/), sends the content to Claude
with a prompt to prepend a natural-language H1 question and a 2-3 sentence direct
answer, then saves the enhanced JSON to a new folder (06_pages_ai/) with the same
filename for easy drop-and-replace.

Pages that don't benefit (thank-you, payment, form-only, RSVP) are copied as-is
with a note, so the output folder is always complete.

USAGE
─────
  python ai_enhance_pages.py
  python ai_enhance_pages.py --src data/06_pages --out data/06_pages_ai
  python ai_enhance_pages.py --slug memory-care   # single page by slug
  python ai_enhance_pages.py --dry-run            # preview without API calls

SKIP TYPES  (copied unchanged)
────────────────────────────────
  Pages with these layout_hints or page_types are skipped:
    layout_hint: contact-page, form-page, payment-page, thank-you
    new_slug ending in: thank-you, payment, rsvp, sign-up, registration
    word_count_out < 80  (basically empty pages)
"""

import argparse
import glob
import json
import os
import sys
import time

import requests

sys.path.insert(0, os.path.dirname(__file__))
import config
from rewrite_utils import llm_complete
from utils import setup_logging

log = setup_logging("ai_enhance_pages")

# ── Config ────────────────────────────────────────────────────────────────────
SRC_DIR  = getattr(config, "PAGES_DIR",
                   os.path.join(os.path.dirname(__file__), "data", "06_pages"))
OUT_DIR  = getattr(config, "PAGES_AI_DIR",
                   os.path.join(os.path.dirname(__file__), "data", "06_pages_ai"))

ANTHROPIC_API_KEY = getattr(config, "ANTHROPIC_API_KEY", os.environ.get("ANTHROPIC_API_KEY", ""))
MODEL             = "claude-sonnet-4-20250514"
MAX_TOKENS        = 1500

# Slugs/hints that indicate form/utility pages — skip AI enhancement
SKIP_HINTS = {"contact-page", "form-page", "payment-page", "thank-you-page"}
SKIP_SLUG_SUFFIXES = (
    "thank-you", "payment", "rsvp", "sign-up",
    "registration", "sitemap", "test-"
)
SKIP_MIN_WORDS = 80


# ── Helpers ───────────────────────────────────────────────────────────────────

def should_skip(page: dict) -> tuple[bool, str]:
    hint  = (page.get("layout_hint") or "").lower()
    slug  = (page.get("new_slug") or "").lower()
    words = page.get("word_count_out", 0)
    ptype = (page.get("page_type") or "").lower()

    if hint in SKIP_HINTS:
        return True, f"layout_hint={hint}"
    if any(slug.endswith(s) for s in SKIP_SLUG_SUFFIXES):
        return True, f"slug ends with skip suffix"
    if words < SKIP_MIN_WORDS:
        return True, f"word_count={words} (too short)"
    return False, ""


def strip_wp_blocks(content: str) -> str:
    """Strip WordPress block comments for cleaner AI input."""
    import re
    content = re.sub(r"<!-- /?wp:[^>]+ -->", "", content)
    content = re.sub(r"<[^>]+>", " ", content)
    content = re.sub(r"\s+", " ", content).strip()
    return content[:3000]  # cap input length


def build_prompt(page: dict) -> str:
    title    = page.get("new_title", "")
    slug     = page.get("new_slug", "")
    ptype    = page.get("page_type", "")
    hint     = page.get("layout_hint", "")
    content  = strip_wp_blocks(page.get("rewritten_content", ""))

    return f"""You are helping optimize a WordPress page for AI answer engines (ChatGPT, Perplexity, Google AI Overviews).

PAGE DETAILS
  Title: {title}
  Slug: {slug}
  Page type: {ptype}
  Layout hint: {hint}

CURRENT CONTENT (stripped):
{content}

TASK
Add an AI-friendly introduction to this page. Return ONLY the two WordPress blocks below — nothing else, no explanation:

1. A <!-- wp:heading {{"level":1}} --> block with a natural-language question or clear statement as the H1. 
   - Should reflect what a user would actually search to find this page
   - For living option pages use "What is [option] at Franke Tobey Jones?"
   - For location pages use "What is [place] near Franke Tobey Jones?"
   - For FAQ pages use the most common question answered on the page
   - Keep it under 12 words

2. A <!-- wp:paragraph {{"className":"ai-summary"}} --> block with a 2-3 sentence direct answer.
   - Answer the H1 question directly and completely
   - Include the most important facts from the page content
   - Written for both humans and AI engines to extract as a snippet
   - Do NOT start with "Franke Tobey Jones" — vary the opening
   - Keep it under 60 words

FORMAT your response as exactly these two blocks and nothing else:
<!-- wp:heading {{"level":1}} -->
<h1 class="wp-block-heading">YOUR H1 HERE</h1>
<!-- /wp:heading -->

<!-- wp:paragraph {{"className":"ai-summary"}} -->
<p class="ai-summary">YOUR SUMMARY HERE</p>
<!-- /wp:paragraph -->"""


def call_claude(prompt: str) -> str | None:
    if not ANTHROPIC_API_KEY:
        log.error("  ANTHROPIC_API_KEY not set in config.py or environment")
        sys.exit(1)
    text, _ = llm_complete(
        "", prompt, task="enhance",
        max_tokens=MAX_TOKENS, extract_slug=False,
    )
    return text


def prepend_to_content(original_content: str, ai_intro: str) -> str:
    """Prepend the AI intro blocks before the existing content."""
    return ai_intro + "\n\n" + original_content


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Add AI-friendly intro to page JSONs")
    ap.add_argument("--src",     default=SRC_DIR,
                    help=f"Source folder of page JSONs (default: {SRC_DIR})")
    ap.add_argument("--out",     default=OUT_DIR,
                    help=f"Output folder (default: {OUT_DIR})")
    ap.add_argument("--slug",    default="",
                    help="Process a single page by slug")
    ap.add_argument("--dry-run", action="store_true",
                    help="Preview prompts without making API calls")
    args = ap.parse_args()

    log.info("=" * 65)
    log.info("ai_enhance_pages — Add AI-friendly intros to page JSONs")
    if args.dry_run:
        log.info("  *** DRY RUN — no API calls or files written ***")
    log.info("=" * 65)
    log.info(f"  Source: {args.src}")
    log.info(f"  Output: {args.out}")

    if not os.path.exists(args.src):
        log.error(f"  Source folder not found: {args.src}")
        sys.exit(1)

    files = sorted(glob.glob(os.path.join(args.src, "*.json")))
    if args.slug:
        files = [f for f in files if os.path.basename(f) == f"{args.slug}.json"]
        if not files:
            log.error(f"  No file found for slug: {args.slug}")
            sys.exit(1)

    log.info(f"  {len(files)} JSON files found\n")
    os.makedirs(args.out, exist_ok=True)

    enhanced = skipped = failed = 0

    for i, filepath in enumerate(files, 1):
        filename = os.path.basename(filepath)
        slug     = filename.replace(".json", "")

        with open(filepath, encoding="utf-8") as f:
            page = json.load(f)

        out_path = os.path.join(args.out, filename)

        # Check if should skip
        skip, reason = should_skip(page)
        if skip:
            log.info(f"  [{i}/{len(files)}] SKIP  {slug}  ({reason})")
            # Copy as-is with a note
            page["ai_enhanced"] = False
            page["ai_skip_reason"] = reason
            if not args.dry_run:
                with open(out_path, "w", encoding="utf-8") as f:
                    json.dump(page, f, indent=2, ensure_ascii=False)
            skipped += 1
            continue

        log.info(f"  [{i}/{len(files)}] ENHANCE  {slug}")

        if args.dry_run:
            prompt = build_prompt(page)
            log.info(f"    H1 would be generated for: {page.get('new_title','')}")
            log.info(f"    Prompt length: {len(prompt)} chars")
            enhanced += 1
            continue

        # Call Claude
        prompt   = build_prompt(page)
        ai_intro = call_claude(prompt)

        if not ai_intro:
            log.warning(f"    API returned nothing — copying original")
            page["ai_enhanced"] = False
            page["ai_skip_reason"] = "api_error"
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(page, f, indent=2, ensure_ascii=False)
            failed += 1
            continue

        # Validate we got the expected blocks
        if "wp:heading" not in ai_intro or "wp:paragraph" not in ai_intro:
            log.warning(f"    Unexpected API response format — copying original")
            log.warning(f"    Got: {ai_intro[:100]}")
            page["ai_enhanced"] = False
            page["ai_skip_reason"] = "bad_response_format"
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(page, f, indent=2, ensure_ascii=False)
            failed += 1
            continue

        # Prepend intro to content
        page["rewritten_content"] = prepend_to_content(
            page.get("rewritten_content", ""), ai_intro
        )
        page["ai_enhanced"]   = True
        page["ai_intro"]      = ai_intro
        page["word_count_out"] = page.get("word_count_out", 0) + len(ai_intro.split())

        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(page, f, indent=2, ensure_ascii=False)

        log.info(f"    ✓ intro added")
        enhanced += 1
        time.sleep(0.3)  # rate limit buffer

    log.info(f"\n  Enhanced: {enhanced}  Skipped: {skipped}  Failed: {failed}")
    log.info(f"  Output → {args.out}")
    log.info("  Done.")


if __name__ == "__main__":
    main()
