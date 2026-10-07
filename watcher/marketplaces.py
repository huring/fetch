"""Marketplace registry: a code-level catalog of the places a search can run.

Each marketplace bundles everything specific to that source - its query
schema, how to fetch listings for it, how to parse/render its query lines in
the admin UI's plain-text textareas, and (if it needs one) what auth fields
it requires. Blocket is the first and currently only entry; adding a new
marketplace means writing its source adapter plus one `register(...)` call
here - nothing in pipeline.py, the admin routes, or the templates needs to
change, since all three are driven by this registry.

Polling cadence, request delay, and any auth values are NOT part of this
code-level registry - those are per-deployment, admin-UI-editable settings
stored in the `marketplace_configs` table (see marketplace_configs.py).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple, Type

from pydantic import BaseModel

from watcher.models import BlocketQuery, Listing, MarketplaceConfig, Search
from watcher.settings import Settings
from watcher.sources import blocket as blocket_source


@dataclass(frozen=True)
class AuthField:
    key: str
    label: str
    secret: bool = True


@dataclass(frozen=True)
class Marketplace:
    key: str
    display_name: str
    query_model: Type[BaseModel]
    fetch: Callable[..., List[Listing]]  # fetch(query, *, search, config, settings) -> List[Listing]
    parse_query_line: Callable[[List[str]], Optional[BaseModel]]
    query_to_line: Callable[[BaseModel], str]
    query_line_hint: str
    auth_fields: Tuple[AuthField, ...] = ()
    default_poll_interval_minutes: int = 240
    default_request_delay_seconds: float = 2.0
    # Optional: given a listing's url, fetch additional description text not
    # present in the search results themselves (Blocket needs this; a
    # marketplace whose search results are already complete leaves it unset).
    enrich_description: Optional[Callable[[str], str]] = None


MARKETPLACES: Dict[str, "Marketplace"] = {}


def register(marketplace: Marketplace) -> None:
    MARKETPLACES[marketplace.key] = marketplace


def get(key: str) -> Optional[Marketplace]:
    return MARKETPLACES.get(key)


# --- Blocket -----------------------------------------------------------------

def _blocket_fetch(
    query: BlocketQuery, *, search: Search, config: MarketplaceConfig, settings: Settings
) -> List[Listing]:
    location = search.location if search.scope == "local" else ""
    return blocket_source.fetch(query, location=location, max_pages=settings.max_pages_per_query)


def _blocket_parse_query_line(parts: List[str]) -> Optional[BlocketQuery]:
    q = parts[0] if parts else ""
    if not q:
        return None
    category = parts[1] if len(parts) > 1 and parts[1] else None
    sub_category = parts[2] if len(parts) > 2 and parts[2] else None
    return BlocketQuery(q=q, category=category, sub_category=sub_category)


def _blocket_query_to_line(query: BlocketQuery) -> str:
    return f"{query.q} | {query.category or ''} | {query.sub_category or ''}"


def _blocket_enrich_description(url: str) -> str:
    return blocket_source.fetch_ad_description(url)


register(
    Marketplace(
        key="blocket",
        display_name="Blocket",
        query_model=BlocketQuery,
        fetch=_blocket_fetch,
        parse_query_line=_blocket_parse_query_line,
        query_to_line=_blocket_query_to_line,
        query_line_hint="one per line: q | category | sub_category - category/sub_category optional",
        auth_fields=(),
        default_poll_interval_minutes=240,
        default_request_delay_seconds=2.0,
        enrich_description=_blocket_enrich_description,
    )
)
