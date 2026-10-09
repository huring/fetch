"""Auctionet source adapter.

Auctionet (auctionet.com) aggregates live auctions run by many independent
Swedish auction houses. Unlike Blocket/Vinted/Rehifi, this is a genuine
public, unauthenticated JSON API - the same endpoint the site's own search
page calls (confirmed live, 2026-10): ``GET /api/v2/items`` takes a plain
text query, returns full title/description/condition text (no separate
detail-page fetch needed, unlike Blocket/Vinted), and is paginated via
``pagination.total_pages``. robots.txt only disallows ``/admin/`` and
``/*/my`` - nothing about search or this endpoint.

This is an auction marketplace (``Marketplace.is_auction=True`` in
marketplaces.py): every item is a live ascending-bid auction with a hard
deadline, not a fixed-price classified ad. ``Listing.price`` is populated
from ``next_bid_amount`` (what a new bidder would need to bid right now to
lead) rather than a seller's asking price, and ``auction_ends_at`` carries
the deadline. The API's own ``hammered=false`` filter already excludes
concluded auctions, so this adapter never has to reason about that itself.
"""
from __future__ import annotations

import html as html_module
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from watcher.models import Listing
from watcher.sources.base import SourceError, get_json

logger = logging.getLogger(__name__)

SEARCH_URL = "https://auctionet.com/api/v2/items"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Gecko/20100101 Firefox/120.0"

_TAG_RE = re.compile(r"<[^>]+>")
_BR_RE = re.compile(r"<br\s*/?>", re.I)


def _strip_html(text: Optional[str]) -> str:
    if not text:
        return ""
    text = _BR_RE.sub("\n", text)
    text = _TAG_RE.sub("", text)
    return html_module.unescape(text).replace("\xa0", " ").strip()


def _parse_item(item: Dict[str, Any]) -> Optional[Listing]:
    item_id = item.get("id")
    if item_id is None:
        logger.warning("Skipping Auctionet item with no id, keys=%s", list(item.keys()))
        return None

    price = item.get("next_bid_amount") or item.get("starting_bid_amount") or item.get("estimate")

    description = _strip_html(item.get("description"))
    condition = _strip_html(item.get("condition"))
    if condition:
        description = f"{description}\n\nSkick: {condition}" if description else f"Skick: {condition}"

    image_url = ""
    images = item.get("images")
    if isinstance(images, list) and images and isinstance(images[0], dict):
        image_url = images[0].get("w640") or images[0].get("thumb") or ""

    ends_at = None
    if item.get("ends_at") is not None:
        ends_at = datetime.fromtimestamp(item["ends_at"], tz=timezone.utc)
    published_at = None
    if item.get("published_at") is not None:
        published_at = datetime.fromtimestamp(item["published_at"], tz=timezone.utc)

    return Listing(
        source="auctionet",
        external_id=str(item_id),
        title=item.get("title") or "",
        description=description,
        price=price,
        url=item.get("url") or "",
        location=item.get("location"),
        ships=None,  # varies per auction house/item - not reliably indicated in the API response
        published_at=published_at,
        auction_ends_at=ends_at,
        image_url=image_url or None,
        raw=item,
    )


def fetch(phrase: str, max_pages: int = 2) -> List[Listing]:
    listings: List[Listing] = []
    for page in range(1, max_pages + 1):
        params = {"q": phrase, "locale": "sv", "hammered": "false", "page": page}
        try:
            data = get_json(SEARCH_URL, params=params, headers={"User-Agent": USER_AGENT})
        except Exception as exc:
            raise SourceError(f"Auctionet search failed for q={phrase!r}: {exc}") from exc

        items = data.get("items") or []
        for item in items:
            listing = _parse_item(item)
            if listing is not None:
                listings.append(listing)

        pagination = data.get("pagination") or {}
        if page >= pagination.get("total_pages", page):
            break
    return listings
