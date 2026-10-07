"""Tradera source adapter (REST v4, official API).

NOTE: Tradera's developer portal (api.tradera.com) is a JS-rendered SPA that
could not be fully introspected during development. The endpoint path and
parameter names below follow the shape documented by community REST v4
clients (auto-generated from Tradera's own OpenAPI spec), not a directly
confirmed schema. Verify this against your authenticated developer-portal
view the first time you run it for real (use --dry-run), and adjust
``_SEARCH_PATH`` / the params dict below if Tradera's actual schema differs.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import List, Optional

from watcher.models import Listing, TraderaQuery
from watcher.sources.base import SourceError, get_json

logger = logging.getLogger(__name__)

BASE_URL = "https://api.tradera.com/v4"
_SEARCH_PATH = "/search"


def fetch(query: TraderaQuery, app_id: str, app_key: str, max_pages: int = 2) -> List[Listing]:
    headers = {"X-App-Id": app_id, "X-App-Key": app_key, "Accept": "application/json"}
    listings: List[Listing] = []
    for page in range(1, max_pages + 1):
        params = {"query": query.query, "pageNumber": page}
        if query.category_id:
            params["categoryId"] = query.category_id
        try:
            data = get_json(f"{BASE_URL}{_SEARCH_PATH}", params=params, headers=headers)
        except Exception as exc:
            raise SourceError(f"Tradera search failed for query={query.query!r}: {exc}") from exc

        items = data.get("items") or data.get("results") or []
        if not items:
            break
        for item in items:
            listing = _parse_item(item)
            if listing is not None:
                listings.append(listing)
    return listings


def _parse_item(item: dict) -> Optional[Listing]:
    item_id = item.get("id") or item.get("itemId")
    if not item_id:
        logger.warning("Skipping Tradera item with no id, keys=%s", list(item.keys()))
        return None

    title = item.get("shortDescription") or item.get("title") or ""
    description = item.get("longDescription") or item.get("description") or ""

    price = None
    price_field = item.get("buyItNowPrice") or item.get("maxBid") or item.get("price")
    if isinstance(price_field, dict):
        price = price_field.get("value")
    elif isinstance(price_field, (int, float)):
        price = int(price_field)

    url = item.get("itemUrl") or item.get("url") or ""
    location = item.get("location") or None

    shipping_options = item.get("shippingOptions") or item.get("shipping")
    ships = bool(shipping_options) if shipping_options is not None else None

    published_at = None
    start_date = item.get("startDate") or item.get("listedAt")
    if start_date:
        try:
            published_at = datetime.fromisoformat(str(start_date).replace("Z", "+00:00"))
        except ValueError:
            published_at = None

    return Listing(
        source="tradera",
        external_id=str(item_id),
        title=title,
        description=description,
        price=price,
        url=url,
        location=location,
        ships=ships,
        published_at=published_at,
        raw=item,
    )
