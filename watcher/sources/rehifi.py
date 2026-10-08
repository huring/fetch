"""Rehifi source adapter.

Rehifi (rehifi.se) is a Swedish online store selling used/refurbished hifi
gear with warranty - not a classifieds marketplace, so there's no per-seller
"ad" and no location/shipping variation (it's one shop, ships everywhere).

Its own search endpoint is explicitly disallowed by robots.txt
(``Disallow: /search*``, confirmed live 2026-10), so this adapter doesn't use
it. Instead it uses the mechanism the site's robots.txt itself points
crawlers at: the sitemap (``Sitemap: https://www.rehifi.se/sitemap.xml``,
not disallowed), which lists every product page (current stock and
historical/sold alike) as a plain URL - the product's name is in the URL
slug, so a search phrase can be matched against slugs without fetching
anything. Only slug-matching candidates get their product page fetched, to
read price/availability/description from Rehifi's own schema.org JSON-LD
and description markup. ``/product/*``, ``/category/*`` and the sitemap
itself are all allowed by robots.txt.

The full sitemap is ~28 files / ~28k URLs (the bulk of it historical/sold
items), so it's fetched once and cached in-process for a day rather than
re-crawled every poll cycle - it doesn't change faster than that.
"""
from __future__ import annotations

import html as html_module
import json
import logging
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import requests

from watcher.models import Listing
from watcher.sources.base import SourceError, get_text

logger = logging.getLogger(__name__)

BASE_URL = "https://www.rehifi.se"
SITEMAP_INDEX_URL = f"{BASE_URL}/sitemap.xml"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Gecko/20100101 Firefox/120.0"

# Safety cap: a broad phrase shouldn't fetch an unbounded number of product
# pages in one cycle.
MAX_CANDIDATES_PER_PHRASE = 25

_CATALOG_CACHE_TTL = timedelta(hours=24)
_catalog_cache: Optional[List[Tuple[str, str]]] = None
_catalog_cache_fetched_at: Optional[datetime] = None

_SITEMAP_PRODUCTS_RE = re.compile(r"<loc>(https://www\.rehifi\.se/sitemap-products-[^<]+)</loc>")
_PRODUCT_URL_RE = re.compile(r"<loc>(https://www\.rehifi\.se/product/[^<]+)</loc>")
_JSONLD_RE = re.compile(r'<script type="application/ld\+json"[^>]*>(.*?)</script>', re.S)
_LONG_DESCRIPTION_RE = re.compile(
    r'<div class="long description product-long-description">(.*?)</div>', re.S
)


def _fetch_product_catalog() -> List[Tuple[str, str]]:
    """Returns (url, slug) for every product page in the sitemap."""
    try:
        index_xml = get_text(SITEMAP_INDEX_URL, headers={"User-Agent": USER_AGENT})
    except Exception as exc:
        raise SourceError(f"Could not fetch Rehifi sitemap index: {exc}") from exc

    catalog: List[Tuple[str, str]] = []
    for sitemap_url in _SITEMAP_PRODUCTS_RE.findall(index_xml):
        try:
            xml = get_text(sitemap_url, headers={"User-Agent": USER_AGENT})
        except Exception as exc:
            logger.warning("Could not fetch Rehifi product sitemap %s: %s", sitemap_url, exc)
            continue
        for url in _PRODUCT_URL_RE.findall(xml):
            slug = url.rstrip("/").rsplit("/", 1)[-1]
            catalog.append((url, slug))
    return catalog


def _get_product_catalog() -> List[Tuple[str, str]]:
    global _catalog_cache, _catalog_cache_fetched_at
    now = datetime.utcnow()
    if (
        _catalog_cache is not None
        and _catalog_cache_fetched_at is not None
        and (now - _catalog_cache_fetched_at) < _CATALOG_CACHE_TTL
    ):
        return _catalog_cache

    try:
        catalog = _fetch_product_catalog()
    except SourceError:
        if _catalog_cache is not None:
            logger.warning("Rehifi sitemap refresh failed - using the last cached catalog")
            return _catalog_cache
        raise

    _catalog_cache = catalog
    _catalog_cache_fetched_at = now
    return catalog


def _extract_product_jsonld(html: str) -> Optional[Dict[str, Any]]:
    for match in _JSONLD_RE.finditer(html):
        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if isinstance(item, dict) and item.get("@type") == "Product":
                return item
    return None


def _extract_long_description(html: str) -> str:
    match = _LONG_DESCRIPTION_RE.search(html)
    if not match:
        return ""
    text = re.sub(r"<br\s*/?>", "\n", match.group(1), flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return html_module.unescape(text).replace("\xa0", " ").strip()


def _parse_product_page(html: str, url: str) -> Optional[Listing]:
    product = _extract_product_jsonld(html)
    if product is None:
        return None

    offers = product.get("offers") or {}
    if "InStock" not in (offers.get("availability") or ""):
        return None  # sold / archived - not a current listing

    price = None
    if offers.get("price") is not None:
        try:
            price = int(round(float(offers["price"])))
        except (TypeError, ValueError):
            price = None

    external_id = str(product.get("sku") or url.rstrip("/").rsplit("/", 1)[-1])

    return Listing(
        source="rehifi",
        external_id=external_id,
        title=product.get("name") or "",
        description=_extract_long_description(html),
        price=price,
        url=url,
        location=None,  # one shop, ships nationally - no per-listing location
        ships=True,
        published_at=None,
        raw=product,
    )


def fetch(phrase: str) -> List[Listing]:
    # Slugs separate model numbers with hyphens (e.g. "accuphase-a-46"), so a
    # phrase word is compared against the slug with hyphens normalized to
    # spaces on BOTH sides - otherwise a hyphenated phrase word like "a-46"
    # can never match, since the slug's own hyphen was replaced with a space
    # but the phrase word's wasn't. This previously made every search phrase
    # with a hyphenated model number (most hifi gear) match nothing at all.
    words = [w for w in phrase.lower().replace("-", " ").split() if w]
    if not words:
        return []

    catalog = _get_product_catalog()
    matches = [url for url, slug in catalog if all(w in slug.replace("-", " ") for w in words)]
    if len(matches) > MAX_CANDIDATES_PER_PHRASE:
        logger.warning(
            "Rehifi phrase %r matched %d products - checking only the first %d",
            phrase, len(matches), MAX_CANDIDATES_PER_PHRASE,
        )
        matches = matches[:MAX_CANDIDATES_PER_PHRASE]

    listings: List[Listing] = []
    for url in matches:
        try:
            html = get_text(url, headers={"User-Agent": USER_AGENT})
        except Exception as exc:
            logger.warning("Could not fetch Rehifi product page %s: %s", url, exc)
            continue
        listing = _parse_product_page(html, url)
        if listing is not None:
            listings.append(listing)
    return listings


def check_active(url: str) -> bool:
    """True if the product is still in stock. Unlike Blocket/Vinted, a sold
    Rehifi product's page stays up (moved to an internal "archive" category)
    rather than 404ing, so liveness is read from the JSON-LD offer's
    availability field rather than from the page's mere existence."""
    if not url:
        return False
    try:
        html = get_text(url, headers={"User-Agent": USER_AGENT})
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            return False
        raise SourceError(f"Could not check Rehifi product liveness for {url}: {exc}") from exc
    except Exception as exc:
        raise SourceError(f"Could not check Rehifi product liveness for {url}: {exc}") from exc

    product = _extract_product_jsonld(html)
    if product is None:
        return False
    offers = product.get("offers") or {}
    return "InStock" in (offers.get("availability") or "")
