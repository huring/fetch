"""Blocket source adapter.

Hits the same unauthenticated JSON search endpoint Blocket's own web frontend
uses. No API key needed, but note: Blocket's robots.txt prohibits automated
access without written permission - this project uses it anyway for personal,
low-frequency (one request per search every ~20 min) monitoring, a deliberate
choice made when this project was scoped. See README for context.

Response shape confirmed by a live request during development (2026-10):
top-level key is "docs", price is {"amount": ...}, and the publish time is an
epoch-millisecond "timestamp" field. The search results carry no ad body/
description text at all - only title, price, location and url are available
from the search endpoint itself; ``fetch_ad_description`` below fetches a
partial description from the ad's own detail page as a second request, for
candidates that already passed the deterministic prefilter on title/price
alone (see README and pipeline.py for where that's called).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import List, Optional

import requests

from watcher.models import Listing
from watcher.sources.base import SourceError, extract_jsonld_description, get_json, get_text, page_has_jsonld_block

logger = logging.getLogger(__name__)

SEARCH_URL = "https://www.blocket.se/recommerce/forsale/search/api/search/SEARCH_ID_BAP_COMMON"
DEFAULT_CATEGORY = "93"  # Elektronik & vitvaror
DEFAULT_SUB_CATEGORY = "1.93.3906"  # Ljud & Bild
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Gecko/20100101 Firefox/120.0"


def fetch(q: str, location: str = "", max_pages: int = 2) -> List[Listing]:
    """Fetch listings for a single Blocket search phrase.

    The response field names used in ``_parse_ad`` follow the shape documented
    by the `blocket-api` project and several independent scrapers, not an
    officially published schema. If Blocket changes its response shape,
    ``_parse_ad`` is the one place to fix - run with --dry-run after any
    change to confirm parsing still works.
    """
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    listings: List[Listing] = []
    for page in range(1, max_pages + 1):
        params = {
            "q": q,
            "cg": DEFAULT_CATEGORY,
            "sc": DEFAULT_SUB_CATEGORY,
            "sort": "PUBLISHED_DESC",
            "page": page,
        }
        if location:
            params["location"] = location
        try:
            data = get_json(SEARCH_URL, params=params, headers=headers)
        except Exception as exc:
            raise SourceError(f"Blocket search failed for q={q!r}: {exc}") from exc

        ads = data.get("docs") or data.get("data") or data.get("ads") or []
        if not ads:
            break
        for ad in ads:
            listing = _parse_ad(ad)
            if listing is not None:
                listings.append(listing)
    return listings


def _parse_ad(ad: dict) -> Optional[Listing]:
    ad_id = ad.get("ad_id") or ad.get("id")
    if not ad_id:
        logger.warning("Skipping Blocket ad with no id, keys=%s", list(ad.keys()))
        return None

    title = ad.get("subject") or ad.get("heading") or ""
    description = ad.get("body") or ad.get("description") or ""

    price = None
    price_field = ad.get("price")
    if isinstance(price_field, dict):
        price = price_field.get("amount", price_field.get("value"))
    elif isinstance(price_field, (int, float)):
        price = int(price_field)

    url = ad.get("canonical_url") or ad.get("share_url") or ad.get("url") or ""

    location = None
    location_field = ad.get("location")
    if isinstance(location_field, list) and location_field:
        location = location_field[0].get("name")
    elif isinstance(location_field, dict):
        location = location_field.get("name")
    elif isinstance(location_field, str):
        location = location_field

    published_at = None
    list_time = ad.get("timestamp") or ad.get("list_time") or ad.get("published")
    if isinstance(list_time, (int, float)):
        seconds = list_time / 1000 if list_time > 1e12 else list_time
        try:
            published_at = datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (ValueError, OSError):
            published_at = None
    elif list_time:
        try:
            published_at = datetime.fromisoformat(str(list_time).replace("Z", "+00:00"))
        except ValueError:
            published_at = None

    return Listing(
        source="blocket",
        external_id=str(ad_id),
        title=title,
        description=description,
        price=price,
        url=url,
        location=location,
        ships=None,  # Blocket's shipping flag isn't confirmed; left to the scorer's judgment.
        published_at=published_at,
        raw=ad,
    )


def fetch_ad_description(url: str) -> str:
    """Best-effort fetch of a short description snippet from an ad's detail
    page, via its embedded schema.org JSON-LD block (confirmed present on a
    live ad page during development). This is a truncated meta-description
    (~150 chars), NOT the full ad body - the complete body is rendered
    client-side via an API this project could not locate without a browser.
    Still a real improvement over the search endpoint's title-only data.
    Returns "" on any failure; never raises, since a failed enrichment
    shouldn't block scoring - it just falls back to title-only for that ad.
    """
    if not url:
        return ""
    try:
        html = get_text(url, headers={"User-Agent": USER_AGENT})
    except Exception as exc:
        logger.warning("Could not fetch Blocket ad detail page %s: %s", url, exc)
        return ""

    return extract_jsonld_description(html)


def check_active(url: str) -> bool:
    """True if an ad is still live, used by the daily liveness sweep (see
    watcher/liveness.py) to decide whether to drop a listing rather than
    inferring it from search-result absence, which is unreliable once an ad
    is old enough to fall off Blocket's newest-first sorted results. A 404
    means the ad is gone outright (confirmed live, 2026-10); a 200 response
    missing the expected JSON-LD block is treated the same way."""
    if not url:
        return False
    try:
        html = get_text(url, headers={"User-Agent": USER_AGENT})
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            return False
        raise SourceError(f"Could not check Blocket ad liveness for {url}: {exc}") from exc
    except Exception as exc:
        raise SourceError(f"Could not check Blocket ad liveness for {url}: {exc}") from exc
    return page_has_jsonld_block(html)
