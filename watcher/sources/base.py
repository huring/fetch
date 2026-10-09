"""Shared HTTP helpers for source adapters: retry/backoff and a common error type."""
from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional

import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential


class SourceError(Exception):
    """Raised when a source fails to return usable results after retries."""


@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=0.5, min=0.5, max=4),
    retry=retry_if_exception_type(requests.RequestException),
)
def get_json(
    url: str,
    params: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: int = 15,
) -> Any:
    response = requests.get(url, params=params, headers=headers, timeout=timeout)
    response.raise_for_status()
    return response.json()


@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=0.5, min=0.5, max=4),
    retry=retry_if_exception_type(requests.RequestException),
)
def get_text(
    url: str,
    params: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: int = 15,
) -> str:
    response = requests.get(url, params=params, headers=headers, timeout=timeout)
    response.raise_for_status()
    return response.text


_JSONLD_RE = re.compile(r'<script type="application/ld\+json"[^>]*>(.*?)</script>', re.S)


def page_has_jsonld_block(html: str) -> bool:
    """True if the page has a parses-successfully schema.org JSON-LD block -
    the mechanism both Blocket's and Vinted's item detail pages use for a
    live listing. Used as a liveness signal: a sold/removed listing's page
    commonly drops this block even when the URL itself still returns 200."""
    match = _JSONLD_RE.search(html)
    if not match:
        return False
    try:
        json.loads(match.group(1))
    except json.JSONDecodeError:
        return False
    return True


def extract_jsonld_description(html: str) -> str:
    """Best-effort extraction of the "description" field from an embedded
    schema.org JSON-LD block (``<script type="application/ld+json">``), the
    mechanism both Blocket's and Vinted's item detail pages use to carry a
    textual description. Returns "" if the block is missing or malformed -
    never raises, since a failed enrichment shouldn't block scoring, it just
    falls back to title-only for that listing."""
    match = _JSONLD_RE.search(html)
    if not match:
        return ""
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return ""
    return data.get("description") or ""


_JSONLD_ALL_RE = re.compile(r'<script type="application/ld\+json"[^>]*>(.*?)</script>', re.S)


def _iter_jsonld_blocks(html: str):
    """Unlike _JSONLD_RE's callers above (which only ever look at a single,
    known block on a page they control the shape of), an arbitrary retailer
    page commonly embeds several JSON-LD blocks (breadcrumbs, organization,
    product...), sometimes several entities wrapped together under one
    "@graph" key - this walks all of them."""
    for match in _JSONLD_ALL_RE.finditer(html):
        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(data, list):
            yield from (b for b in data if isinstance(b, dict))
        elif isinstance(data, dict) and isinstance(data.get("@graph"), list):
            yield from (b for b in data["@graph"] if isinstance(b, dict))
        elif isinstance(data, dict):
            yield data


def extract_jsonld_product(html: str) -> Optional[Dict[str, Any]]:
    """Best-effort extraction of a schema.org Product block from a page's
    JSON-LD - used by watched-item price checks (watcher/price_watch.py) as
    a far more reliable source of price/availability/image than scraping
    rendered text, since most e-commerce platforms emit this for Google's
    rich-snippet eligibility regardless of how the visible page itself is
    rendered (including JS-rendered pages whose plain-text content never
    shows a price at all). Returns None if no Product block is found."""
    for block in _iter_jsonld_blocks(html):
        type_ = block.get("@type")
        types = type_ if isinstance(type_, list) else [type_]
        if any(str(t).lower() == "product" for t in types if t):
            return block
    return None


def image_url_from_jsonld_product(product: Dict[str, Any]) -> str:
    """Pulls the image URL out of a schema.org Product block's "image"
    field, which is inconsistently a single URL string, a list of them, or
    a single ImageObject dict - shared by watcher/price_watch.py (a page's
    own JSON-LD) and sources/rehifi.py (the same JSON-LD shape, already
    parsed as that source's `raw`). Returns "" if nothing usable is found."""
    image = product.get("image")
    if isinstance(image, str):
        return image
    if isinstance(image, list) and image:
        first = image[0]
        return first if isinstance(first, str) else (first.get("url", "") if isinstance(first, dict) else "")
    if isinstance(image, dict):
        return image.get("url", "")
    return ""


_OG_IMAGE_RE = re.compile(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', re.I)
_OG_IMAGE_RE_ALT = re.compile(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', re.I)


def extract_og_image(html: str) -> str:
    """Best-effort fallback image URL for a page with no (or no parseable)
    JSON-LD Product block - the OpenGraph og:image meta tag, present on
    virtually any retailer page regardless of platform. Checks both
    attribute orders since meta tags aren't consistently authored one way.
    Returns "" if missing - never raises."""
    match = _OG_IMAGE_RE.search(html) or _OG_IMAGE_RE_ALT.search(html)
    return match.group(1) if match else ""
