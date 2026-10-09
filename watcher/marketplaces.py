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
from watcher.sources import auctionet as auctionet_source
from watcher.sources import blocket as blocket_source
from watcher.sources import rehifi as rehifi_source
from watcher.sources import vinted as vinted_source
from watcher.sources.blocket_geo import resolve_county_code


@dataclass(frozen=True)
class AuthField:
    key: str
    label: str
    secret: bool = True


@dataclass(frozen=True)
class CountEstimate:
    """How many of a marketplace's listings currently match one search
    phrase - for the NLP search builder's result-size preview (backlog #21),
    never for the real fetch/prefilter pipeline. ``exact`` is False when a
    marketplace doesn't expose a true total (Vinted's pagination nav
    truncates past 3 pages - see sources/vinted.py's count()), in which case
    ``total`` is a confirmed lower bound, not a guess."""
    total: int
    exact: bool


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
    # True for a live-auction marketplace (current bid rises toward a
    # deadline) rather than a fixed-price classifieds marketplace. Changes
    # three things: `price` means "current bid requirement" rather than an
    # asking price (see models.Listing.auction_ends_at), a listing's removal
    # is detected from auction_ends_at passing rather than check_active
    # re-fetching the page (see liveness.run_auction_end_sweep), and its
    # candidates carry auction_ends_at into Claude's scoring prompt.
    is_auction: bool = False
    # Optional: given a search phrase (plus the full search, for anything
    # location-specific like Blocket's county filter), a cheap estimate of
    # how many of this marketplace's listings currently match it - used only
    # by the NLP search builder's result-size preview (backlog #21), never
    # by the real fetch/prefilter pipeline. A marketplace that can't offer
    # one cheaply leaves this unset rather than approximating by paging
    # through everything (none currently do, but nothing requires it).
    estimate_count: Optional[Callable[[str, Search, MarketplaceConfig, Settings], CountEstimate]] = None
    # A one-line description of what this marketplace actually carries, for
    # the search builder's system prompt - it needs this to judge which
    # registered marketplaces plausibly fit a given search (e.g. a vintage
    # synth probably isn't on Rehifi), not to describe listings once fetched
    # (that's scoring_note's job).
    category_note: str = ""
    # Whether a brand-new search - built via the prompt wizard, or a
    # watched-item's linked "find used" search (see price_watch.py) - starts
    # with this marketplace already included. True for everything except one
    # that's currently broken (see Vinted below); still fully wired up and
    # selectable by hand via Advanced edit either way, this only changes
    # what's pre-selected for something new.
    enabled_by_default: bool = True


MARKETPLACES: Dict[str, "Marketplace"] = {}


def register(marketplace: Marketplace) -> None:
    MARKETPLACES[marketplace.key] = marketplace


def get(key: str) -> Optional[Marketplace]:
    return MARKETPLACES.get(key)


def default_marketplace_keys() -> List[str]:
    """Every marketplace a brand-new search should start out with, before
    any user customization - see Marketplace.enabled_by_default."""
    return [key for key, m in MARKETPLACES.items() if m.enabled_by_default]


# --- Blocket -----------------------------------------------------------------

def _blocket_fetch(
    phrase: str, *, search: Search, config: MarketplaceConfig, settings: Settings
) -> List[Listing]:
    # A "local" scope search whose location names a Swedish county (e.g.
    # "Norrbotten") gets filtered server-side by Blocket itself (see
    # blocket_source.fetch's docstring) - confirmed live (2026-10) to
    # actually narrow results, unlike the plain-name attempt backlog #26
    # tried. Anything that doesn't resolve to a known county (a city name, a
    # typo, "national" scope) falls back to no server-side filter, same as
    # before - pipeline._filter_by_scope still applies client-side.
    location_code = resolve_county_code(search.location) if search.scope == "local" and search.location else None
    # A "plain" search has no AI ranking of its own, so its results are
    # ordered the same way Blocket's own "Closest" sort would - confirmed
    # live (2026-10) as what Lars actually wants to see first for a
    # no-Claude search. A "rated" search keeps the default newest-first
    # fetch order, since Claude's score is what orders those for display.
    sort_by_distance_from = (settings.home_lat, settings.home_lon) if search.scoring_mode == "plain" else None
    return blocket_source.fetch(
        phrase,
        max_pages=settings.max_pages_per_query,
        location_code=location_code,
        sort_by_distance_from=sort_by_distance_from,
    )


def _blocket_enrich_description(url: str) -> str:
    return blocket_source.fetch_ad_description(url)


def _blocket_check_active(url: str) -> bool:
    return blocket_source.check_active(url)


def _blocket_estimate_count(
    phrase: str, search: Search, config: MarketplaceConfig, settings: Settings
) -> CountEstimate:
    location_code = resolve_county_code(search.location) if search.scope == "local" and search.location else None
    return CountEstimate(total=blocket_source.count(phrase, location_code=location_code), exact=True)


register(
    Marketplace(
        key="blocket",
        display_name="Blocket",
        fetch=_blocket_fetch,
        auth_fields=(),
        category_note=(
            "Sweden's largest general classifieds site, every category (electronics, vehicles, furniture, "
            "instruments, etc) - a safe default for almost anything secondhand."
        ),
        estimate_count=_blocket_estimate_count,
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


def _vinted_estimate_count(phrase: str, search: Search, config: MarketplaceConfig, settings: Settings) -> CountEstimate:
    total, exact = vinted_source.count(phrase)
    return CountEstimate(total=total, exact=exact)


register(
    Marketplace(
        key="vinted",
        display_name="Vinted",
        fetch=_vinted_fetch,
        auth_fields=(),
        category_note=(
            "A fashion/lifestyle secondhand marketplace (clothing, accessories) that's shipping-first and "
            "cross-border by default - some electronics/hifi gear shows up, but it's not its main category. "
            "No location filter at all, so not useful for a tightly local-scope search."
        ),
        estimate_count=_vinted_estimate_count,
        default_poll_interval_minutes=240,
        default_request_delay_seconds=2.0,
        enrich_description=_vinted_enrich_description,
        check_active=_vinted_check_active,
        # Off by default for new searches (2026-10): Vinted now serves a
        # Cloudflare "managed challenge" for every /catalog request
        # (confirmed live - a plain HTTP fetch can never pass it, it needs
        # real JS execution), so every fetch currently 403s. Still fully
        # wired up and selectable via Advanced edit for whenever backlog
        # #29 (FlareSolverr) or similar lands.
        enabled_by_default=False,
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


def _rehifi_estimate_count(phrase: str, search: Search, config: MarketplaceConfig, settings: Settings) -> CountEstimate:
    return CountEstimate(total=rehifi_source.count(phrase), exact=True)


register(
    Marketplace(
        key="rehifi",
        display_name="Rehifi",
        fetch=_rehifi_fetch,
        auth_fields=(),
        category_note=(
            "A Swedish specialist retailer (not classifieds) selling used/refurbished hifi/audio gear only, "
            "with warranty - good for a search that's specifically hifi equipment, useless for anything else "
            "(vehicles, furniture, non-audio electronics, etc)."
        ),
        estimate_count=_rehifi_estimate_count,
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


# --- Auctionet (auction) ------------------------------------------------------

def _auctionet_fetch(
    phrase: str, *, search: Search, config: MarketplaceConfig, settings: Settings
) -> List[Listing]:
    # Auctionet has no location/city filter in its search API - like Vinted,
    # search.location/scope aren't used.
    return auctionet_source.fetch(phrase, max_pages=settings.max_pages_per_query)


def _auctionet_estimate_count(phrase: str, search: Search, config: MarketplaceConfig, settings: Settings) -> CountEstimate:
    return CountEstimate(total=auctionet_source.count(phrase), exact=True)


register(
    Marketplace(
        key="auctionet",
        display_name="Auctionet",
        fetch=_auctionet_fetch,
        auth_fields=(),
        category_note=(
            "A live-auction aggregator across many independent Swedish auction houses, every category "
            "including vintage/high-end hifi, antiques, art, vehicles - prices are current bid requirements, "
            "not fixed asking prices, and every result has a bidding deadline."
        ),
        estimate_count=_auctionet_estimate_count,
        default_poll_interval_minutes=240,
        default_request_delay_seconds=2.0,
        is_auction=True,
        scoring_note=(
            "This listing is a LIVE AUCTION on Auctionet (a Swedish auction-house aggregator), not a "
            "fixed-price classified ad - 'price' is the current bid requirement (what a new bidder would "
            "need to bid right now to lead), not a seller's asking price, and it will typically rise "
            "further before the auction ends. Judge the price against what a fair final price would "
            "likely be, not just the current bid, and mention the auction's remaining time (given below) "
            "in your reasoning since it's decision-relevant - a 'good price' today may not hold once "
            "bidding continues."
        ),
    )
)
