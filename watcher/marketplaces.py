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
from watcher.sources import rehifi as rehifi_source
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
    # Optional: standing context attached to every listing from this
    # marketplace when it's sent to Claude for scoring (e.g. a known quirk in
    # its results, or that every item includes a warranty). Attached per
    # listing rather than once per prompt, since a single scoring batch can
    # rarely mix listings from more than one marketplace (a straggler still
    # pending from a previous cycle).
    scoring_note: Optional[str] = None


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
        scoring_note=(
            "Some Blocket results are 'wanted' posts from buyers (e.g. Swedish \"Sökes\"/\"Köpes\"), not "
            "items actually for sale - Blocket's search results don't reliably flag these separately, so "
            "score them low/irrelevant unless this watch list is specifically about buy requests."
        ),
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


# --- Rehifi ------------------------------------------------------------------

def _rehifi_fetch(
    phrase: str, *, search: Search, config: MarketplaceConfig, settings: Settings
) -> List[Listing]:
    # One online store, ships nationally - search.location/scope aren't used.
    return rehifi_source.fetch(phrase)


def _rehifi_check_active(url: str) -> bool:
    return rehifi_source.check_active(url)


register(
    Marketplace(
        key="rehifi",
        display_name="Rehifi",
        fetch=_rehifi_fetch,
        auth_fields=(),
        default_poll_interval_minutes=240,
        default_request_delay_seconds=2.0,
        check_active=_rehifi_check_active,
        scoring_note=(
            "This listing is from Rehifi, a Swedish online store selling used/refurbished hifi gear - "
            "every purchase includes 3 months warranty, a 30-day exchange right, and a 10-day right of "
            "return. Treat that warranty coverage as a genuine advantage over private-seller marketplaces "
            "with no such protection, and factor it into your price assessment and score."
        ),
    )
)
