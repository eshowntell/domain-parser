"""
inject_schema.py — FTJ Phase 3  (PATCHED 2026-06-11)
Reads page JSONs from PAGES_AI_DIR (or PAGES_DIR fallback),
appends JSON-LD schema blocks to rewritten_content,
writes to PAGES_SCHEMA_DIR for upload.

PATCH NOTES (vs original)
─────────────────────────
  • ORG_PHONE / ORG_ADDRESS corrected to FTJ's real NAP:
      (253) 752-6621 · 5340 N Bristol St, Tacoma, WA 98407
    (old values "96 Frankland Rd" / +12535525600 were wrong)
  • New SCHEMA_BASE_URL config knob — bake the PRODUCTION domain into
    schema even while the site lives on ftj3.local.
  • Removed invalid "SeniorLivingOrganization" @type (not a schema.org type).
  • FAQPage now requires real page["faq_pairs"]; otherwise falls back to
    Article (placeholder empty Q&A fails Google validation).
  • Event now requires page["event_date"]; otherwise falls back to Article
    (startDate is required; most mapped slugs are listing pages anyway).
  • TouristAttraction no longer claims FTJ's street address for off-campus
    places (Point Defiance Park is not at the client's building).
  • Person/team pages now emit AboutPage (anonymous Person had no value).
  • Per-page "url" omitted from schema — child-page permalinks include parent
    paths the script can't reliably reconstruct, and url is optional anyway.

Usage:
    python inject_schema.py --dry-run          # preview schema assignments
    python inject_schema.py                    # process all pages
    python inject_schema.py --slug memory-care # single page
"""

import os
import sys
import json
import re
import argparse
from pathlib import Path

try:
    import config
except ImportError:
    print("ERROR: config.py not found.")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ORG_NAME = "Franke Tobey Jones"

# Domain baked into schema. Set SCHEMA_BASE_URL in config.py to the PRODUCTION
# domain (e.g. "https://www.franketobeyjones.com") so schema is correct at
# go-live even while uploading to ftj3.local.
ORG_URL = getattr(config, "SCHEMA_BASE_URL",
                  getattr(config, "NEW_SITE_URL", "https://www.franketobeyjones.com")
                  ).rstrip("/")

ORG_PHONE = getattr(config, "ORG_PHONE", "+12537526621")
ORG_ADDRESS = getattr(config, "ORG_ADDRESS", {
    "@type":           "PostalAddress",
    "streetAddress":   "5340 N Bristol St",
    "addressLocality": "Tacoma",
    "addressRegion":   "WA",
    "postalCode":      "98407",
    "addressCountry":  "US",
})
ORG_IMAGE = getattr(config, "ORG_IMAGE", f"{ORG_URL}/wp-content/uploads/ftj-logo.png")

PAGES_AI_DIR = getattr(config, "PAGES_AI_DIR", None)
PAGES_DIR    = getattr(config, "PAGES_DIR",    None)
PAGES_SCHEMA_DIR = getattr(config, "PAGES_SCHEMA_DIR",
                           str(Path(PAGES_DIR or ".").parent / "06_pages_schema"))

SOURCE_DIR = PAGES_AI_DIR if (PAGES_AI_DIR and os.path.isdir(PAGES_AI_DIR)) else PAGES_DIR
if not SOURCE_DIR:
    print("ERROR: Neither PAGES_AI_DIR nor PAGES_DIR is set in config.py")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Schema type map
# slug (or slug fragment)  →  schema_type
# Evaluated top-to-bottom; first match wins.
# ---------------------------------------------------------------------------

SLUG_SCHEMA_MAP = [

    # ── SKIP (utility / transactional / no SEO value) ──────────────────────
    (["thank-you", "updates-thank-you", "request-info-thank-you",
      "schedule-tour-thank-you", "sitemap", "resource-sitemap",
      "test-anchors", "facebook", "pnsws-payment", "art-show-payment",
      "employment-form", "priority-list-form", "parkside-view-priority-list",
      "email-signup", "newsletter-signup", "university-mailing-list",
      "register-class-event", "senior-university-registration",
      "fundraising-breakfast-registration", "pnsws-registration",
      "apply-online", "food-recalls"],
     "skip"),

    # ── FAQPage (only emitted when page["faq_pairs"] exists — else Article) ──
    (["memory-care-faq", "senior-living-faq", "parkside-view-faq",
      "choose-memory-care", "choose-assisted-living",
      "choose-retirement-community", "is-it-dementia",
      "right-time-senior-living", "time-downsize", "time-get-help",
      "senior-safe-driving", "most-requested-info",
      "independent-living-benefits", "talking-loved-ones",
      "memory-care-resources", "why-choose-memory-care",
      "pricing-resources"],
     "FAQPage"),

    # ── Event (only emitted when page["event_date"] exists — else Article) ──
    (["care-area-open-house", "memory-care-open-house",
      "summer-concert-series-2026", "october-open-house",
      "pnsws", "senior-art-show", "centennial-celebration-2024",
      "ftj-day-tacoma", "events", "events-festivals",
      "events-volunteers", "senior-writers-showcase",
      "dementia-presentations", "activity-calendars-menus",
      "calendars", "newsletter-calendar", "upcoming-travel",
      "community-activities"],
     "Event"),

    # ── VideoObject ──────────────────────────────────────────────────────────
    (["virtual-tours", "team-videos", "resident-videos",
      "ftj-community-video", "team-video", "educational-videos",
      "exercise-videos", "centennial-video", "campus-photos",
      "expansion-gallery"],
     "VideoObject"),

    # ── AboutPage (team / bio pages) ─────────────────────────────────────────
    (["our-team", "management", "board", "resident-ambassadors",
      "team-highlights", "team-member-highlights",
      "community-living-team", "philanthropy-team",
      "hero-award-recipients", "tobey-award-recipients",
      "making-difference-recipients", "award-winners",
      "resident-bios", "meet-residents", "staff-contact"],
     "AboutPage"),

    # ── LodgingBusiness (living option / care level pages) ──────────────────
    (["assisted-living", "assisted-living-availability",
      "independent-living", "independent-living-2",
      "memory-care", "memory-care-community", "memory-care-facility",
      "memory-care-suites", "skilled-nursing", "skilled-nursing-facility",
      "long-term-skilled-nursing", "lillian-pratt-al",
      "bristol-view", "bristol-view-apartments",
      "garden-apartments", "garden-apartments-2",
      "duplex-cottages", "senior-cottages-duplexes",
      "tobey-jones-building", "tobey-jones-building-2",
      "parkside", "parkside-view-discovery",
      "parkside-view-depositor-benefits",
      "senior-apartments-tacoma", "living-options",
      "living-options-2", "living-options-overview",
      "retirement-living-options", "whats-available",
      "new-floor-plans", "senior-living-community"],
     "LodgingBusiness"),

    # ── LocalBusiness (contact / org-level / tour pages) ────────────────────
    (["contact", "location-map", "schedule-tour", "schedule-tour-2",
      "request-info", "about-us", "our-story", "our-promise",
      "remarkable-history", "our-timeline", "history-timeline",
      "centennial-resident-stories", "rated-best", "why-live-here",
      "philanthropy-expanding-circle", "community",
      "senior-living-calculator", "pricing-brochures",
      "professionals-referrals", "tacoma-community-tour",
      "community-tours"],
     "LocalBusiness"),

    # ── TouristAttraction (Tacoma neighborhood / destination pages) ──────────
    (["neighborhood", "neighborhood-highlights", "tacoma-culture",
      "retirement-living-tacoma", "6th-avenue-district",
      "downtown-tacoma", "proctor-district", "stadium-district",
      "point-ruston", "ruston-way-waterfront", "ruston-way-parks",
      "point-defiance-park", "point-defiance-park-zoo",
      "foss-waterway-esplanade", "wright-park", "nearby-parks",
      "seymour-botanical-conservatory", "defiance-trolley-bus",
      "brewery-district", "wineries", "theater",
      "art-galleries", "events-festivals", "food-drinks-nearby",
      "things-to-do", "services-near-ftj", "restaurants-pubs"],
     "TouristAttraction"),

    # ── Article / WebPage (informational content, catch-all) ─────────────────
    (["senior-wellness-fitness", "holistic-wellness-program",
      "wellness-wheel", "senior-dining", "senior-university",
      "learning-opportunities", "spiritual-support",
      "lifestyle", "entertainment", "residents-council",
      "community-outreach", "for-neighbors",
      "give-volunteer", "ways-to-give", "ways-to-give-2",
      "make-donation", "legacy-circle", "annual-reports",
      "power-participation", "power-participation-2",
      "employee-benefits", "culture-benefits",
      "job-opportunities", "current-opportunities",
      "hire-on-bonus", "construction-updates",
      "were-expanding", "expansion-amenities",
      "expansion-featured-stories", "history-landmarks",
      "campus-amenities", "campus-building-features",
      "project-overview", "project-overview-2",
      "centennial-media", "100-years-stories-book",
      "ftj-guest-house", "become-depositor",
      "retirement-testimonials", "moving-retirement-community",
      "stay-connected", "newsletter",
      "pt-defiance-ruston-senior-center", "pt-defiance-ruston-about",
      "center-services", "center-updates", "center-partnerships",
      "volunteers-partners", "lunch-menu", "clean-safe-guidelines",
      "covid-19"],
     "Article"),
]

# ---------------------------------------------------------------------------
# Schema builders
# ---------------------------------------------------------------------------

BASE_ORG = {
    "@type":     "Organization",
    "name":      ORG_NAME,
    "url":       ORG_URL,
    "telephone": ORG_PHONE,
    "address":   ORG_ADDRESS,
}

def _title(page):
    return page.get("new_title") or page.get("title", "")

def _desc(page):
    return (page.get("meta_description")
            or page.get("seo_description")
            or page.get("excerpt", ""))

def _date(page):
    return page.get("timestamp") or page.get("date_modified") or page.get("posted_at", "")


def schema_local_business(page: dict) -> dict:
    return {
        "@context":    "https://schema.org",
        "@type":       "LocalBusiness",
        "name":        ORG_NAME,
        "telephone":   ORG_PHONE,
        "address":     ORG_ADDRESS,
        "url":         ORG_URL,
        "image":       ORG_IMAGE,
        "description": _desc(page),
    }

def schema_lodging(page: dict) -> dict:
    return {
        "@context":    "https://schema.org",
        "@type":       "LodgingBusiness",
        "name":        f"{ORG_NAME} — {_title(page)}",
        "telephone":   ORG_PHONE,
        "address":     ORG_ADDRESS,
        "image":       ORG_IMAGE,
        "description": _desc(page),
        "containedInPlace": {
            "@type": "Organization",
            "name":  ORG_NAME,
            "url":   ORG_URL,
        },
    }

def schema_faq(page: dict) -> dict | None:
    """FAQPage requires real Q&A pairs. Returns None (→ Article fallback)
    when page["faq_pairs"] = [{"q":..,"a":..}, ...] is absent or empty."""
    faq_pairs = page.get("faq_pairs", [])
    if not faq_pairs:
        return None
    return {
        "@context": "https://schema.org",
        "@type":    "FAQPage",
        "name":     _title(page),
        "mainEntity": [
            {
                "@type": "Question",
                "name":  pair["q"],
                "acceptedAnswer": {"@type": "Answer", "text": pair["a"]},
            }
            for pair in faq_pairs
        ],
    }

def schema_event(page: dict) -> dict | None:
    """Event requires startDate. Returns None (→ Article fallback) when
    page["event_date"] is absent — most mapped slugs are listing pages."""
    start = page.get("event_date", "")
    if not start:
        return None
    return {
        "@context":    "https://schema.org",
        "@type":       "Event",
        "name":        _title(page),
        "description": _desc(page),
        "organizer":   BASE_ORG,
        "location": {
            "@type":   "Place",
            "name":    ORG_NAME,
            "address": ORG_ADDRESS,
        },
        "startDate": start,
    }

def schema_video(page: dict) -> dict:
    return {
        "@context":     "https://schema.org",
        "@type":        "VideoObject",
        "name":         _title(page),
        "description":  _desc(page),
        "thumbnailUrl": page.get("thumbnail_url", ORG_IMAGE),
        "uploadDate":   _date(page),
        "publisher":    BASE_ORG,
    }

def schema_about(page: dict) -> dict:
    return {
        "@context":    "https://schema.org",
        "@type":       "AboutPage",
        "name":        _title(page),
        "description": _desc(page),
        "about":       BASE_ORG,
    }

def schema_tourist(page: dict) -> dict:
    # NOTE: no street address — these pages describe places that are NOT at
    # FTJ's address. Locality only.
    return {
        "@context":    "https://schema.org",
        "@type":       "TouristAttraction",
        "name":        _title(page),
        "description": _desc(page),
        "address": {
            "@type":           "PostalAddress",
            "addressLocality": "Tacoma",
            "addressRegion":   "WA",
            "addressCountry":  "US",
        },
        "touristType": "Senior",
    }

def schema_article(page: dict) -> dict:
    return {
        "@context":     "https://schema.org",
        "@type":        "Article",
        "headline":     _title(page),
        "description":  _desc(page),
        "dateModified": _date(page),
        "publisher":    BASE_ORG,
    }

SCHEMA_BUILDERS = {
    "LocalBusiness":     schema_local_business,
    "LodgingBusiness":   schema_lodging,
    "FAQPage":           schema_faq,
    "Event":             schema_event,
    "VideoObject":       schema_video,
    "AboutPage":         schema_about,
    "TouristAttraction": schema_tourist,
    "Article":           schema_article,
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

LDJSON_RE = re.compile(
    r'\n?<!-- wp:html -->\s*<script type="application/ld\+json">.*?</script>\s*<!-- /wp:html -->\n?',
    re.DOTALL,
)

def resolve_schema_type(slug: str) -> str:
    for slugs, schema_type in SLUG_SCHEMA_MAP:
        if slug in slugs:
            return schema_type
    return "Article"  # default

def build_jsonld_block(schema_dict: dict) -> str:
    json_str = json.dumps(schema_dict, indent=2, ensure_ascii=False)
    return (
        f'\n<!-- wp:html -->\n'
        f'<script type="application/ld+json">\n{json_str}\n</script>\n'
        f'<!-- /wp:html -->\n'
    )

def process_page(filepath: str, dry_run: bool = False) -> dict:
    with open(filepath, "r", encoding="utf-8") as f:
        page = json.load(f)

    slug = page.get("slug") or page.get("new_slug") or Path(filepath).stem
    schema_type = resolve_schema_type(slug)

    if dry_run:
        return {"slug": slug, "schema_type": schema_type, "file": filepath}

    if schema_type == "skip":
        return {"slug": slug, "schema_type": "skip", "skipped": True}

    builder = SCHEMA_BUILDERS.get(schema_type, schema_article)
    schema_dict = builder(page)

    # Conditional builders (FAQPage/Event) fall back to Article when the
    # required data isn't present.
    if schema_dict is None:
        schema_type = "Article"
        schema_dict = schema_article(page)

    jsonld_block = build_jsonld_block(schema_dict)

    # Strip any previously injected schema block (idempotent re-runs)
    content = page.get("rewritten_content", "")
    if "application/ld+json" in content:
        content = LDJSON_RE.sub("", content)

    page["rewritten_content"] = content + jsonld_block
    page["schema_type"] = schema_type

    os.makedirs(PAGES_SCHEMA_DIR, exist_ok=True)
    out_path = os.path.join(PAGES_SCHEMA_DIR, os.path.basename(filepath))

    # Preserve post_id/posted_at already recorded in the schema-dir copy, if
    # the schema dir is "ahead" of the AI dir (e.g. a previous schema upload).
    if os.path.exists(out_path):
        try:
            with open(out_path, "r", encoding="utf-8") as f:
                prev = json.load(f)
            for key in ("post_id", "posted_at"):
                if prev.get(key) and not page.get(key):
                    page[key] = prev[key]
        except Exception:
            pass

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(page, f, indent=2, ensure_ascii=False)

    return {"slug": slug, "schema_type": schema_type, "out": out_path}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--slug", type=str, default=None)
    args = parser.parse_args()

    files = sorted(Path(SOURCE_DIR).glob("*.json"))
    if args.slug:
        files = [f for f in files if args.slug in f.stem]
        if not files:
            print(f"No file found matching slug: {args.slug}")
            sys.exit(1)

    print(f"Source:      {SOURCE_DIR}")
    print(f"Destination: {PAGES_SCHEMA_DIR}")
    print(f"Schema URL:  {ORG_URL}   "
          f"{'⚠️ LOCAL DOMAIN — set SCHEMA_BASE_URL in config.py!' if '.local' in ORG_URL else '(production — good)'}")

    results = []
    for filepath in files:
        result = process_page(str(filepath), dry_run=args.dry_run)
        results.append(result)

    # Summary
    from collections import Counter
    counts = Counter(r["schema_type"] for r in results)
    print(f"\n{'DRY RUN — ' if args.dry_run else ''}Schema injection summary ({len(results)} pages):\n")
    for schema_type, count in sorted(counts.items()):
        print(f"  {schema_type:<22} {count}")

    if args.dry_run:
        print(f"\nFull assignment list:")
        for r in results:
            print(f"  {r['slug']:<50} → {r['schema_type']}")
    else:
        skipped = sum(1 for r in results if r.get("skipped"))
        written = len(results) - skipped
        print(f"\n  Written to: {PAGES_SCHEMA_DIR}")
        print(f"  {written} processed, {skipped} skipped")

if __name__ == "__main__":
    main()
