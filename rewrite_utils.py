# rewrite_utils.py  —  shared plumbing for 6_rewrite, 6_rewrite_pages, 6c_combine
#
# Extracted so Gutenberg block templates, the Anthropic API call, content
# extraction, media-map lookups, and content validation live in ONE place.
# When the theme vendor delivers final block markup, update it here.

import logging
import re
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

import config
from utils import scrape_get

log = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
#  GUTENBERG BLOCK TEMPLATES
#
#  When the theme changes, update these strings.  Every system prompt that
#  references Gutenberg blocks pulls from here via  BLOCK_TEMPLATES["..."].
# ══════════════════════════════════════════════════════════════════════════════

BLOCK_TEMPLATES = {

    "heading_h2": """\
  <!-- wp:heading {"level":2} -->
  <h2 class="wp-block-heading">Section Title</h2>
  <!-- /wp:heading -->""",

    "heading_h3": """\
  <!-- wp:heading {"level":3} -->
  <h3 class="wp-block-heading">Subsection</h3>
  <!-- /wp:heading -->""",

    "paragraph": """\
  <!-- wp:paragraph -->
  <p>Body copy here.</p>
  <!-- /wp:paragraph -->""",

    "list": """\
  <!-- wp:list -->
  <ul class="wp-block-list">
  <!-- wp:list-item --><li>Item one</li><!-- /wp:list-item -->
  <!-- wp:list-item --><li>Item two</li><!-- /wp:list-item -->
  </ul>
  <!-- /wp:list -->""",

    "buttons": """\
  <!-- wp:buttons -->
  <div class="wp-block-buttons">
  <!-- wp:button -->
  <div class="wp-block-button"><a class="wp-block-button__link" href="/contact">Schedule a Tour</a></div>
  <!-- /wp:button -->
  </div>
  <!-- /wp:buttons -->""",

    "image_with_id": """\
  <!-- wp:image {"id":IMAGE_ID,"sizeSlug":"large","linkDestination":"none"} -->
  <figure class="wp-block-image size-large"><img src="IMAGE_URL" alt="ALT TEXT" class="wp-image-IMAGE_ID"/></figure>
  <!-- /wp:image -->""",

    "image_no_id": """\
  <!-- wp:image {"sizeSlug":"large","linkDestination":"none"} -->
  <figure class="wp-block-image size-large"><img src="IMAGE_URL" alt="ALT TEXT"/></figure>
  <!-- /wp:image -->""",

    # ── Pages-only blocks (not used by blog/combine scripts) ──────
    "columns": """\
  <!-- wp:columns -->
  <div class="wp-block-columns">
  <!-- wp:column -->
  <div class="wp-block-column">...content...</div>
  <!-- /wp:column -->
  <!-- wp:column -->
  <div class="wp-block-column">...content...</div>
  <!-- /wp:column -->
  </div>
  <!-- /wp:columns -->""",

    "details": """\
  <!-- wp:details -->
  <details class="wp-block-details"><summary>Question here?</summary>
  <!-- wp:paragraph -->
  <p>Answer here.</p>
  <!-- /wp:paragraph -->
  </details>
  <!-- /wp:details -->""",

    "table": """\
  <!-- wp:table -->
  <figure class="wp-block-table"><table><thead><tr><th>Col A</th><th>Col B</th></tr></thead>
  <tbody><tr><td>val</td><td>val</td></tr></tbody></table></figure>
  <!-- /wp:table -->""",

    "quote": """\
  <!-- wp:quote -->
  <blockquote class="wp-block-quote">
  <!-- wp:paragraph --><p>Quote text.</p><!-- /wp:paragraph -->
  <cite>Person Name, Role</cite></blockquote>
  <!-- /wp:quote -->""",

    "separator": """\
  <!-- wp:separator -->
  <hr class="wp-block-separator has-alpha-channel-opacity"/>
  <!-- /wp:separator -->""",

    "html_bio": """\
  <!-- wp:html -->
  <div class="ftj-bio">
    <div class="ftj-bio-photo"><img src="URL" alt="Name"/></div>
    <div class="ftj-bio-text">
      <p class="ftj-bio-name">Full Name</p>
      <p class="ftj-bio-title">Job Title</p>
      <div class="ftj-bio-body"><p>Bio text.</p></div>
    </div>
  </div>
  <!-- /wp:html -->""",

    "group_tan": """\
  <!-- wp:group {"style":{"color":{"background":"#f9f5e9"}},"layout":{"type":"constrained"}} -->
  <div class="wp-block-group has-background" style="background-color:#f9f5e9">
  ...blocks...
  </div>
  <!-- /wp:group -->""",

    "file_download": """\
  <!-- wp:file {"id":FILE_ID,"href":"FILE_URL","displayPreview":false} -->
  <div class="wp-block-file"><a href="FILE_URL">Download the original document (PDF)</a></div>
  <!-- /wp:file -->""",
}


def core_block_reference() -> str:
    """Block reference shared by all three rewrite prompts (blog, page, combine)."""
    return f"""
Paragraph:
{BLOCK_TEMPLATES['paragraph']}

Heading H2 (first main section only):
{BLOCK_TEMPLATES['heading_h2']}

Heading H3 (all subsequent sections):
{BLOCK_TEMPLATES['heading_h3']}

List:
{BLOCK_TEMPLATES['list']}

Buttons / CTA:
{BLOCK_TEMPLATES['buttons']}

Image (when ID is provided):
{BLOCK_TEMPLATES['image_with_id']}

Image (when no ID is provided):
{BLOCK_TEMPLATES['image_no_id']}"""


def extended_block_reference() -> str:
    """Additional blocks used only by the pages prompt."""
    return f"""
Two-column layout:
{BLOCK_TEMPLATES['columns']}

FAQ accordion:
{BLOCK_TEMPLATES['details']}

Table:
{BLOCK_TEMPLATES['table']}

Quote / testimonial:
{BLOCK_TEMPLATES['quote']}

Separator:
{BLOCK_TEMPLATES['separator']}

Custom HTML block (for ftj-bio and other custom CSS structures):
{BLOCK_TEMPLATES['html_bio']}

Group with background color (tan):
{BLOCK_TEMPLATES['group_tan']}"""


# ══════════════════════════════════════════════════════════════════════════════
#  CONTENT EXTRACTION  (strip WP chrome, return body + headings)
# ══════════════════════════════════════════════════════════════════════════════

# Selectors for theme chrome that should be removed before extraction
_CHROME_SELECTORS = [
    "header", "footer", "nav", "#wpadminbar", ".admin-bar",
    "script", "style", "noscript", ".site-header", ".site-footer",
    "#masthead", "#colophon", "#site-navigation", ".sidebar",
]


def extract_content_from_html(html: str, *, headings_as_str: bool = False) -> dict:
    """Strip WP theme chrome and return body text + headings.

    Args:
        html:  raw page HTML
        headings_as_str:  if True, return headings as a newline-joined string
                          (6_rewrite_pages format); if False, return as list of
                          dicts (6_rewrite / 6c_combine format).
    Returns:
        dict with keys: body_text, headings, and (when scraping full pages)
        image_urls if headings_as_str is True.
    """
    soup = BeautifulSoup(html, "lxml")
    for sel in _CHROME_SELECTORS:
        for el in soup.select(sel):
            el.decompose()

    # Headings
    raw_headings = []
    for tag in soup.find_all(["h1", "h2", "h3", "h4"]):
        t = tag.get_text(strip=True)
        if t:
            raw_headings.append({"level": tag.name, "text": t})

    result = {}

    if headings_as_str:
        # Pages format: "[H2] Title\n[H3] Sub"
        result["headings"] = "\n".join(
            f"[{h['level'].upper()}] {h['text']}" for h in raw_headings[:20]
        )
        # Also extract content images when in pages mode
        image_urls = []
        for img in soup.find_all("img"):
            # Read lazy-load attrs too (data-src etc.), not just src.
            src = (img.get("src") or img.get("data-src") or
                   img.get("data-lazy-src") or img.get("data-original") or "").strip()
            if not src or src.startswith("data:"):
                continue
            try:
                # Skip only true icons/spacers (BOTH dims tiny). Real content
                # thumbnails are often short (e.g. 137×75) — an OR filter wrongly
                # dropped them.
                if int(img.get("width", 999)) < 100 and int(img.get("height", 999)) < 100:
                    continue
            except (ValueError, TypeError):
                pass
            if src.startswith("/"):
                src = config.SITE_URL.rstrip("/") + src
            if src.startswith("http") and src not in image_urls:
                image_urls.append(src)
        result["image_urls"] = image_urls
    else:
        # Blog / combine format: list of dicts
        result["headings"] = raw_headings

    body = re.sub(r"\s{2,}", " ", soup.get_text(separator=" ", strip=True)).strip()
    result["body_text"] = body
    return result


# ══════════════════════════════════════════════════════════════════════════════
#  FETCH PAGE HTML
# ══════════════════════════════════════════════════════════════════════════════

def fetch_page_html(url: str) -> str | None:
    """GET a page and return raw HTML, or None on failure."""
    try:
        resp = scrape_get(url)
        if resp and resp.status_code == 200:
            return resp.text
    except Exception as e:
        log.warning(f"  fetch failed for {url}: {e}")
    return None


def source_url_for(page_url: str, source_base: str) -> str:
    """Rewrite a page URL to point at a different host (local dev vs production)."""
    parsed = urlparse(page_url)
    source = urlparse(source_base)
    return parsed._replace(scheme=source.scheme, netloc=source.netloc).geturl()


# ══════════════════════════════════════════════════════════════════════════════
#  SLUG HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def slug_from_url(url: str, fallback: str = "post") -> str:
    """Last path segment only, lowercased and hyphenated. Safe for filenames."""
    path = urlparse(url).path.strip("/")
    if not path:
        return fallback
    last = path.rstrip("/").split("/")[-1]
    return re.sub(r"[^a-z0-9]+", "-", last.lower()).strip("-") or fallback


# ══════════════════════════════════════════════════════════════════════════════
#  MEDIA MAP LOOKUP
# ══════════════════════════════════════════════════════════════════════════════

def map_image(url: str, media_map: dict) -> tuple[str, str]:
    """Look up an old image URL in the media map.
    Returns (new_url, id_str).  id_str is '' if not known.
    """
    entry = media_map.get(url, {})
    if isinstance(entry, dict):
        return entry.get("url", url), str(entry.get("id", ""))
    elif entry:
        return entry, ""
    return url, ""


def format_image_lines(image_urls: list[str], media_map: dict,
                       limit: int = 12) -> list[str]:
    """Build the IMAGE URLS prompt section lines from a list of old URLs."""
    lines = []
    for u in image_urls[:limit]:
        new_url, new_id = map_image(u, media_map)
        if new_id:
            lines.append(f"  URL: {new_url}  ID: {new_id}")
        else:
            lines.append(f"  URL: {new_url}  (no ID — omit id attribute from block)")
    return lines


# ══════════════════════════════════════════════════════════════════════════════
#  CONTENT VALIDATION
# ══════════════════════════════════════════════════════════════════════════════

def validate_content(html: str) -> tuple[bool, str]:
    """Check that rewritten Gutenberg HTML is safe to post.
    Returns (ok, reason).  Reason is '' when ok is True.
    """
    if not html or not html.strip():
        return False, "rewritten content is empty"
    if "<!-- wp:" not in html:
        return False, "content contains no Gutenberg blocks"
    if len(html.split()) < 20:
        return False, f"content suspiciously short ({len(html.split())} words)"
    return True, ""


# ══════════════════════════════════════════════════════════════════════════════
#  LLM CALL  (routes to Anthropic or a local Ollama server, per-task)
# ══════════════════════════════════════════════════════════════════════════════

def llm_complete(
    system: str,
    prompt: str,
    *,
    task: str = "rewrite",
    max_tokens: int | None = None,
    temperature: float | None = None,
    extract_slug: bool = False,
) -> tuple[str | None, str | None]:
    """Call an LLM, routed per-task to Anthropic (default) or a local Ollama server.

    Args:
        system:       system prompt string
        prompt:       user prompt string
        task:         key into config.LLM_TASKS — selects the provider
        max_tokens:   override config.LLM_MAX_TOKENS
        temperature:  override config.LLM_TEMPERATURE
        extract_slug: if True, look for a SLUG: line and strip it

    Returns:
        (text, suggested_slug) — either or both may be None.
        When extract_slug is False, suggested_slug is always None.
    """
    provider    = config.LLM_TASKS.get(task, "anthropic")
    max_tokens  = max_tokens or config.LLM_MAX_TOKENS
    temperature = temperature if temperature is not None else config.LLM_TEMPERATURE

    try:
        if provider == "ollama":
            resp = requests.post(
                f"{config.OLLAMA_URL.rstrip('/')}/api/chat",
                json={
                    "model":  config.OLLAMA_MODEL,
                    "stream": False,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user",   "content": prompt},
                    ],
                    "options": {
                        "temperature": temperature,
                        "num_predict": max_tokens,
                    },
                },
                timeout=config.PHASE2_API_TIMEOUT,
            )
            resp.raise_for_status()
            text = resp.json()["message"]["content"]
        elif provider in getattr(config, "OPENAI_PROVIDERS", {}):
            # OpenAI-compatible chat APIs (Mistral, DeepSeek, …)
            p = config.OPENAI_PROVIDERS[provider]
            resp = requests.post(
                p["url"],
                headers={"Authorization": f"Bearer {p['key']}",
                         "Content-Type": "application/json"},
                json={
                    "model":       p["model"],
                    "max_tokens":  max_tokens,
                    "temperature": temperature,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user",   "content": prompt},
                    ],
                },
                timeout=config.PHASE2_API_TIMEOUT,
            )
            resp.raise_for_status()
            text = resp.json()["choices"][0]["message"]["content"]
        else:
            headers = {
                "x-api-key":         config.ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type":      "application/json",
            }
            body = {
                "model":       config.ANTHROPIC_MODEL,
                "max_tokens":  max_tokens,
                "temperature": temperature,
                "system":      system,
                "messages":    [{"role": "user", "content": prompt}],
            }
            resp = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers=headers, json=body, timeout=config.PHASE2_API_TIMEOUT,
            )
            resp.raise_for_status()
            blocks = resp.json().get("content", [])
            text = "\n".join(b.get("text", "") for b in blocks if b.get("type") == "text")

        text = text.strip()

        # Strip markdown fences
        text = re.sub(r"^```(?:html)?\s*\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text)
        text = text.strip()

        # Extract SLUG line if requested
        suggested_slug = None
        if extract_slug:
            slug_match = re.match(
                r"^SLUG:\s*([a-z0-9][a-z0-9-]{1,80})\s*\n", text, re.IGNORECASE
            )
            if slug_match:
                suggested_slug = slug_match.group(1).strip().lower()
                text = text[slug_match.end():].strip()
                log.info(f"  AI slug: {suggested_slug}")
            else:
                log.warning("  No SLUG line in response — will use fallback slug")

        if text and "<!-- wp:" not in text:
            log.warning("  Response contains no Gutenberg block comments — may be malformed")

        return text or None, suggested_slug

    except requests.exceptions.HTTPError as e:
        log.error(f"  {provider} HTTP error: {e} — {getattr(resp, 'text', '')[:300]}")
    except Exception as e:
        log.error(f"  {provider} error: {e}")
    return None, None


def call_anthropic(
    system: str,
    prompt: str,
    *,
    max_tokens: int | None = None,
    temperature: float | None = None,
    extract_slug: bool = True,
) -> tuple[str | None, str | None]:
    """Call the Anthropic messages API.

    Args:
        system:       system prompt string
        prompt:       user prompt string
        max_tokens:   override config.LLM_MAX_TOKENS
        temperature:  override config.LLM_TEMPERATURE
        extract_slug: if True, look for a SLUG: line and strip it

    Returns:
        (text, suggested_slug) — either or both may be None.
        When extract_slug is False, suggested_slug is always None.

    Now delegates to llm_complete(task="rewrite") — see that function for the
    actual HTTP call and post-processing.
    """
    return llm_complete(
        system, prompt, task="rewrite",
        max_tokens=max_tokens, temperature=temperature, extract_slug=extract_slug,
    )
