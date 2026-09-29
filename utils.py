# utils.py  —  shared HTTP helpers
import time
import logging
import os
import requests

import config

log = logging.getLogger(__name__)
AUTH = (config.WP_USERNAME, config.WP_APP_PASSWORD)
API_BASE = f"{config.SITE_URL}/wp-json/wp/v2"
# A real browser UA — Cloudflare/WP Engine serve a stripped, image-less page to
# bot-like agents (a "compatible; …" UA got a 4 KB shell with no images), so we
# present as Chrome to receive the full page + REST content.
_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/120.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def setup_logging(name: str):
    """Call once at the top of each script."""
    os.makedirs(config.DATA_DIR, exist_ok=True)
    os.makedirs(config.OUTPUT_DIR, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(os.path.join(config.DATA_DIR, f"{name}.log"), encoding="utf-8"),
        ],
    )
    return logging.getLogger(name)


def api_get(endpoint: str, params: dict = None):
    """Authenticated WP REST API GET with retry logic. Returns response or None."""
    url = endpoint if endpoint.startswith("http") else f"{API_BASE}/{endpoint}"
    for attempt in range(config.MAX_RETRIES):
        try:
            time.sleep(config.REQUEST_DELAY)
            r = requests.get(url, auth=AUTH, params=params,
                             timeout=config.REQUEST_TIMEOUT, headers=_HEADERS)
            r.raise_for_status()
            return r
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code in (400, 401, 403, 404):
                return None  # don't retry on definitive failures
            _retry_or_raise(e, attempt, url)
        except requests.RequestException as e:
            _retry_or_raise(e, attempt, url)
    return None


def fix_encoding(text: str) -> str:
    """Fix common UTF-8 mojibake from Windows-1252 misreads."""
    replacements = {
        'â€"':  "\u2014",   # em dash
        'â€™':  "\u2019",   # right single quote / apostrophe
        'â€˜':  "\u2018",   # left single quote
        'â€œ':  "\u201c",   # left double quote
        'â€':   "\u201d",   # right double quote
        'â€¦':  "\u2026",   # ellipsis
        'â€¢':  "\u2022",   # bullet
        'Ã©':   "\u00e9",   # é
        'Ã ':   "\u00e0",   # à
        'Ã¨':   "\u00e8",   # è
        "\u00a0": " ",      # non-breaking space
    }
    for bad, good in replacements.items():
        text = text.replace(bad, good)
    return text


def scrape_get(url: str):
    """Unauthenticated GET of a live page. Returns response or None."""
    for attempt in range(config.MAX_RETRIES):
        try:
            time.sleep(config.REQUEST_DELAY)
            r = requests.get(url, timeout=config.REQUEST_TIMEOUT, headers=_HEADERS)
            r.raise_for_status()
            r.encoding = "utf-8"
            return r
        except requests.RequestException as e:
            _retry_or_raise(e, attempt, url)
    return None


def _retry_or_raise(exc, attempt, url):
    if attempt == config.MAX_RETRIES - 1:
        log.error(f"FAIL after {config.MAX_RETRIES} attempts: {url} — {exc}")
    else:
        log.warning(f"Retry {attempt + 1}/{config.MAX_RETRIES}: {url} — {exc}")
        time.sleep(2 ** attempt)
