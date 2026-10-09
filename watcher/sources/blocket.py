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
from typing import List, Optional, Tuple

import requests

from watcher.models import Listing
from watcher.sources.base import SourceError, extract_jsonld_description, get_json, get_text, page_has_jsonld_block

logger = logging.getLogger(__name__)

SEARCH_URL = "https://www.blocket.se/recommerce/forsale/search/api/search/SEARCH_ID_BAP_COMMON"
DEFAULT_CATEGORY = "93"  # Elektronik & vitvaror
DEFAULT_SUB_CATEGORY = "1.93.3906"  # Ljud & Bild
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Gecko/20100101 Firefox/120.0"


def fetch(
    q: str,
    max_pages: int = 2,
    location_code: Optional[str] = None,
    sort_by_distance_from: Optional[Tuple[float, float]] = None,
) -> List[Listing]:
    """Fetch listings for a single Blocket search phrase.

    The response field names used in ``_parse_ad`` follow the shape documented
    by the `blocket-api` project and several independent scrapers, not an
    officially published schema. If Blocket changes its response shape,
    ``_parse_ad`` is the one place to fix - run with --dry-run after any
    change to confirm parsing still works.

    ``location_code`` is Blocket's own county facet code (e.g. "0.300025" for
    Norrbotten - see blocket_geo.py), not a plain place name. backlog #26
    previously found the search endpoint 400s on a plain Swedish county name
    - true, but that was the wrong param shape: confirmed live (2026-10) that
    the facet code works and genuinely filters server-side. When a search's
    location doesn't resolve to a known county (see
    blocket_geo.resolve_county_code), the caller passes None and relies on
    the client-side location match instead (pipeline._filter_by_scope).

    ``sort_by_distance_from``, given as an (lat, lon) tuple, switches from the
    default newest-first ordering to the same "Closest" sort Blocket's own
    site offers - confirmed live (2026-10) to genuinely reorder (and
    therefore, since only the first ``max_pages`` pages are ever fetched,
    reshape *which* results get pulled in) around that point rather than
    publish date.
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
        if location_code:
            params["location"] = location_code
        if sort_by_distance_from is not None:
            lat, lon = sort_by_distance_from
            params["sort"] = "CLOSEST"
            params["lat"] = lat
            params["lon"] = lon
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
                if sort_by_distance_from is not None:
                    distance = ad.get("distance")
                    if isinstance(distance, (int, float)):
                        listing.distance_km = distance
                listings.append(listing)
    return listings


def count(q: str, location_code: Optional[str] = None) -> int:
    """Total number of matches for a search phrase, for the NLP search
    builder's result-size preview (backlog #21) - a single lightweight
    request, no pagination. Confirmed live (2026-10): Blocket's response
    metadata already carries the true total count across every page
    (``result_size.match_count``) - cheaper and more accurate than paging
    through results to count them, and unrelated to ``num_results`` (which is
    actually just this page's size, not a total, despite the name)."""
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    params = {"q": q, "cg": DEFAULT_CATEGORY, "sc": DEFAULT_SUB_CATEGORY, "sort": "PUBLISHED_DESC", "page": 1}
    if location_code:
        params["location"] = location_code
    try:
        data = get_json(SEARCH_URL, params=params, headers=headers)
    except Exception as exc:
        raise SourceError(f"Blocket count failed for q={q!r}: {exc}") from exc
    return int(data.get("metadata", {}).get("result_size", {}).get("match_count", 0))


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

    image_url = ""
    image_field = ad.get("image")
    if isinstance(image_field, dict):
        image_url = image_field.get("url", "")

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
        image_url=image_url or None,
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
