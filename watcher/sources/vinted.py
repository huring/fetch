"""Vinted source adapter.

Vinted's own JSON API (api.vinted.<tld>/svc-catalogue/items) sits behind
Cloudflare and needs a bootstrapped access_token_web bearer token - but the
plain catalog search *page* (the one a browser loads) embeds the exact same
item data as server-rendered JSON, no token or cookie required at all
(confirmed live, 2026-10: a bare GET to vinted.se/catalog?search_text=...
returns 96 items per page as Next.js "flight" data embedded in a
<script>self.__next_f.push(...)</script> tag). This adapter parses that
embedded JSON rather than calling the token-gated API, since it's simpler and
doesn't depend on Cloudflare-fragile token bootstrapping.

Like Blocket, this is personal, low-frequency, read-only use of a public page
Vinted itself serves to every visitor - not a sanctioned API, a deliberate
choice made when this marketplace was added. See README for context.

Search results carry no description text (only title, price, a condition/
brand summary, and the url) - ``fetch_item_description`` below fetches the
full description from the item's own detail page via its embedded schema.org
JSON-LD block (same mechanism Blocket uses), for candidates that already
passed the deterministic prefilter on title/price alone.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

import requests

from watcher.models import Listing
from watcher.sources.base import SourceError, extract_jsonld_description, get_text, page_has_jsonld_block

logger = logging.getLogger(__name__)

SEARCH_URL = "https://www.vinted.se/catalog"
ITEMS_PER_PAGE = 96
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Gecko/20100101 Firefox/120.0"

_PUSH_RE = re.compile(r"self\.__next_f\.push\((\[.*?\])\)</script>", re.S)
_ITEMS_MARKER = '"items":{"items":['


def fetch(q: str, max_pages: int = 2) -> List[Listing]:
    """Fetch listings for a single Vinted search phrase.

    Vinted has no location/city filter in its search (it's shipping-first,
    cross-border by default) - unlike Blocket, there's no location param
    here at all.
    """
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html"}
    listings: List[Listing] = []
    for page in range(1, max_pages + 1):
        params = {"search_text": q, "order": "newest_first", "page": page}
        try:
            html = get_text(SEARCH_URL, params=params, headers=headers)
        except Exception as exc:
            raise SourceError(f"Vinted search failed for q={q!r}: {exc}") from exc

        items = _extract_catalog_items(html)
        if not items:
            break
        for entry in items:
            listing = _parse_item(entry)
            if listing is not None:
                listings.append(listing)
        if len(items) < ITEMS_PER_PAGE:
            break  # last page
    return listings


def _extract_catalog_items(html: str) -> List[Dict[str, Any]]:
    """Extracts the catalog items array from the page's embedded Next.js
    "flight" data. If Vinted changes its page structure, this is the one
    place to fix - the marker string and bracket-matching below are the only
    things that assume anything about that structure.

    The page can embed its flight data across more than one push() call, so
    every one is checked for the items marker rather than assuming it's in
    the first."""
    for match in _PUSH_RE.finditer(html):
        try:
            parsed = json.loads(match.group(1))
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(parsed, list) or len(parsed) < 2 or not isinstance(parsed[1], str):
            continue
        payload = parsed[1]

        idx = payload.find(_ITEMS_MARKER)
        if idx == -1:
            continue
        array_text = _extract_balanced_array(payload, idx + len(_ITEMS_MARKER) - 1)
        if array_text is None:
            logger.warning("Could not isolate Vinted's items array (unbalanced brackets)")
            return []
        try:
            return json.loads(array_text)
        except json.JSONDecodeError:
            logger.warning("Could not parse Vinted's items array as JSON")
            return []

    logger.warning("Could not find Vinted's embedded catalog items in the page")
    return []


def _extract_balanced_array(text: str, start: int) -> Optional[str]:
    """Returns the substring of ``text`` starting at ``start`` (which must be
    a '[') up to and including its matching ']', respecting nested brackets
    and quoted strings."""
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _parse_item(entry: Dict[str, Any]) -> Optional[Listing]:
    item = entry.get("productItem") or {}
    item_id = item.get("id")
    if not item_id:
        logger.warning("Skipping Vinted item with no id, keys=%s", list(item.keys()))
        return None

    title = item.get("title") or ""

    price = None
    price_field = item.get("price")
    if isinstance(price_field, dict) and price_field.get("amount") is not None:
        try:
            price = int(round(float(price_field["amount"])))
        except (TypeError, ValueError):
            price = None

    relative_url = item.get("url") or ""
    url = f"https://www.vinted.se{relative_url}" if relative_url else ""

    return Listing(
        source="vinted",
        external_id=str(item_id),
        title=title,
        description="",  # not present in search results; see fetch_item_description
        price=price,
        url=url,
        location=None,  # Vinted has no city/region data in search results
        ships=True,  # Vinted is shipping-only by design
        published_at=None,  # not present in search results
        image_url=item.get("thumbnailUrl") or None,
        raw=item,
    )


def fetch_item_description(url: str) -> str:
    """Best-effort fetch of the full description from an item's detail page,
    via its embedded schema.org JSON-LD block. Unlike Blocket's truncated
    snippet, this is the complete description text (confirmed live). Note:
    the price embedded in that same JSON-LD block is in the seller's own
    listing currency, not SEK - only use this for the description, never for
    price. Returns "" on any failure; never raises."""
    if not url:
        return ""
    try:
        html = get_text(url, headers={"User-Agent": USER_AGENT})
    except Exception as exc:
        logger.warning("Could not fetch Vinted item detail page %s: %s", url, exc)
        return ""

    return extract_jsonld_description(html)


def check_active(url: str) -> bool:
    """True if an item is still live, used by the daily liveness sweep (see
    watcher/liveness.py) to decide whether to drop a listing rather than
    inferring it from search-result absence - unreliable here in particular,
    since Vinted sorts newest-first and an old-but-still-active item will
    fall off fetched pages as newer matches push it down, long before it's
    actually sold. A 404 means the item is gone outright (confirmed live,
    2026-10); a 200 response missing the expected JSON-LD block is treated
    the same way."""
    if not url:
        return False
    try:
        html = get_text(url, headers={"User-Agent": USER_AGENT})
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            return False
        raise SourceError(f"Could not check Vinted item liveness for {url}: {exc}") from exc
    except Exception as exc:
        raise SourceError(f"Could not check Vinted item liveness for {url}: {exc}") from exc
    return page_has_jsonld_block(html)
