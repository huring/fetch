"""Marketplace registry: a code-level catalog of the places a search can run.

Each marketplace bundles everything specific to that source - how to fetch
listings for a search phrase, and (if it needs one) what auth fields it
requires. The search terms, scope/location, and criteria all live on the
Search object itself (see models.py); a marketplace just decides how to use
that shared, generic information. Blocket is the first and currently only
entry; adding a new marketplace means writing its source adapter plus one
`register(...)` call here - nothing in pipeline.py, the admin routes, or the
templates needs to change, since all three are driven by this registry.

Polling cadence, request delay, and any auth values are NOT part of this
code-level registry - those are per-deployment, admin-UI-editable settings
stored in the `marketplace_configs` table (see marketplace_configs.py).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

from watcher.models import Listing, MarketplaceConfig, Search
from watcher.settings import Settings
from watcher.sources import blocket as blocket_source
from watcher.sources import vinted as vinted_source


@dataclass(frozen=True)
class AuthField:
    key: str
    label: str
    secret: bool = True


@dataclass(frozen=True)
class Marketplace:
    key: str
    display_name: str
    # fetch(phrase, *, search, config, settings) -> List[Listing] - called
    # once per entry in search.search_phrases.
    fetch: Callable[..., List[Listing]]
    auth_fields: Tuple[AuthField, ...] = ()
    default_poll_interval_minutes: int = 240
    default_request_delay_seconds: float = 2.0
    # Optional: given a listing's url, fetch additional description text not
    # present in the search results themselves (Blocket needs this; a
    # marketplace whose search results are already complete leaves it unset).
    enrich_description: Optional[Callable[[str], str]] = None
    # Optional: given a listing's url, return whether it's still live. Used
    # by the daily liveness sweep (watcher/liveness.py) rather than inferring
    # removal from search-result absence, which is unreliable once a listing
    # is old enough to fall off a newest-first sorted search.
    check_active: Optional[Callable[[str], bool]] = None


MARKETPLACES: Dict[str, "Marketplace"] = {}


def register(marketplace: Marketplace) -> None:
    MARKETPLACES[marketplace.key] = marketplace


def get(key: str) -> Optional[Marketplace]:
    return MARKETPLACES.get(key)


# --- Blocket -----------------------------------------------------------------

def _blocket_fetch(
    phrase: str, *, search: Search, config: MarketplaceConfig, settings: Settings
) -> List[Listing]:
    location = search.location if search.scope == "local" else ""
    return blocket_source.fetch(phrase, location=location, max_pages=settings.max_pages_per_query)


def _blocket_enrich_description(url: str) -> str:
    return blocket_source.fetch_ad_description(url)


def _blocket_check_active(url: str) -> bool:
    return blocket_source.check_active(url)


register(
    Marketplace(
        key="blocket",
        display_name="Blocket",
        fetch=_blocket_fetch,
        auth_fields=(),
        default_poll_interval_minutes=240,
        default_request_delay_seconds=2.0,
        enrich_description=_blocket_enrich_description,
        check_active=_blocket_check_active,
    )
)


# --- Vinted ------------------------------------------------------------------

def _vinted_fetch(
    phrase: str, *, search: Search, config: MarketplaceConfig, settings: Settings
) -> List[Listing]:
    # Vinted has no location/city filter - search.location/scope aren't used.
    return vinted_source.fetch(phrase, max_pages=settings.max_pages_per_query)


def _vinted_enrich_description(url: str) -> str:
    return vinted_source.fetch_item_description(url)


def _vinted_check_active(url: str) -> bool:
    return vinted_source.check_active(url)


register(
    Marketplace(
        key="vinted",
        display_name="Vinted",
        fetch=_vinted_fetch,
        auth_fields=(),
        default_poll_interval_minutes=240,
        default_request_delay_seconds=2.0,
        enrich_description=_vinted_enrich_description,
        check_active=_vinted_check_active,
    )
)
