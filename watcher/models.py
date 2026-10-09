"""Core data types shared across sources, scoring, storage and the admin UI."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

Scope = Literal["local", "national"]
ScoringMode = Literal["plain", "rated"]
CheckFrequency = Literal["daily", "weekly", "monthly"]
DigestStyle = Literal["itemized", "summary_link"]


class WatchedModel(BaseModel):
    pattern: str
    note: str = ""
    good_price: str = ""
    # The buy-it-now / grail target for this search: if a listing is
    # genuinely this (or equivalent) in working condition at or below
    # good_price, Claude is instructed to score it 10/10 and use it as the
    # benchmark every other candidate in the search is judged against.
    is_ideal: bool = False


class Search(BaseModel):
    id: Optional[int] = None
    name: str
    enabled: bool = True
    scope: Scope = "local"
    location: str = ""
    require_shipping: bool = False
    max_price: Optional[int] = None
    min_price: Optional[int] = None
    # "plain": deterministic prefilter only, no Claude call - just a browsable
    # list plus an optional instant_alert_price threshold. "rated": today's
    # full pipeline (hard/soft criteria, watched_models, Claude scoring).
    # New searches default to "plain" (the admin UI's new-search form starts
    # with the AI-rating toggle off); existing rows are backfilled to "rated"
    # by the db migration so already-configured searches keep scoring exactly
    # as before.
    scoring_mode: ScoringMode = "plain"
    # Plain-mode only: a listing at or below this price triggers an instant
    # Slack alert instead of waiting for the daily digest. None means every
    # match just waits for the digest.
    instant_alert_price: Optional[int] = None
    # How this search's non-instant matches appear in the daily digest:
    # "itemized" (today's behavior, one line per listing) or "summary_link"
    # (one line - "N new items in <search name>" linking into the admin UI -
    # for a high-volume search where itemizing every match would spam
    # Slack). Default is "itemized" here (existing rows keep behaving
    # exactly as before); the admin UI's new-search form defaults the
    # *toggle* to summary_link instead, since that's the better default for
    # a freshly created search per Lars's own framing of this feature.
    digest_style: DigestStyle = "itemized"
    excluded_models: List[str] = Field(default_factory=list)
    excluded_words: List[str] = Field(default_factory=list)
    required_keywords: List[str] = Field(default_factory=list)
    watched_models: List[WatchedModel] = Field(default_factory=list)
    hard_criteria: List[str] = Field(default_factory=list)
    soft_criteria: List[str] = Field(default_factory=list)
    # The actual search terms to run, shared across every marketplace this
    # search is attached to - each marketplace decides how to use them (e.g.
    # as a Blocket "q" param), so this stays plain text rather than a
    # marketplace-specific query shape.
    search_phrases: List[str] = Field(default_factory=list)
    # Registry keys (see marketplaces.py) of the marketplaces this search
    # runs on, e.g. ["blocket"].
    marketplaces: List[str] = Field(default_factory=list)
    # The free-text prompt (see watcher/scoring/search_builder.py) this
    # search was generated from, if it was - None for a search built with
    # the manual/advanced form instead. "Editing" a prompt-built search means
    # re-running the builder against an edited version of this text, which
    # regenerates every field above from scratch; a manual edit afterward is
    # not reflected back into this text and will be lost on the next
    # regeneration (the admin UI warns about this - see search_form.html).
    creation_prompt: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class WatchedItem(BaseModel):
    """A single arbitrary product URL (any retailer - Amazon, a brand's own
    store, anything) tracked for a price drop, independent of the
    search/marketplace machinery - there's no search phrase or marketplace
    fetch involved, just one URL re-checked on its own schedule."""

    id: Optional[int] = None
    name: str
    url: str
    enabled: bool = True
    target_price: Optional[int] = None
    check_frequency: CheckFrequency = "daily"
    # Also look for a used version of this exact item on every registered
    # marketplace, alerting if one turns up at or below target_price - see
    # watched_items.py/price_watch.py for how the linked plain search this
    # spins up is created and kept in sync.
    find_used: bool = False
    linked_search_id: Optional[int] = None
    # Everything below is check-run state, not admin-edited - see
    # watched_items.record_check_result/record_alert.
    extracted_title: Optional[str] = None
    # A short description and a link to the product's own image (never
    # downloaded/stored - just the URL), captured alongside the title/price
    # so the admin UI can show a "is this the right item?" confirmation card
    # (backlog #23). None until the first successful check.
    extracted_description: Optional[str] = None
    extracted_image_url: Optional[str] = None
    # e.g. "SEK", "USD" - whatever Claude read off the page. None until the
    # first successful check; Slack alerts fall back to "SEK" until then
    # (every watched item so far has been SEK, but that's just a fallback,
    # not an assumption baked into the check logic itself).
    currency: Optional[str] = None
    current_price: Optional[int] = None
    # Distinguishes "genuinely out of stock as of the last successful check"
    # (current_price is None *because* of this) from "never successfully
    # checked yet" (current_price is also None, but this stays None too) -
    # without it the admin UI can't tell the two apart, and a confirmed
    # out-of-stock item just looks identical to a broken one. Always the
    # latest check's finding (unlike extracted_title/description/image_url/
    # currency, stock status isn't something to keep stale on purpose).
    in_stock: Optional[bool] = None
    lowest_price_seen: Optional[int] = None
    last_alert_price: Optional[int] = None
    last_checked_at: Optional[str] = None
    # Liveness/sold-tracking parity with marketplace listings (backlog #6) -
    # mirrors source_health's consecutive_failures/alert_sent pattern. Any
    # check that can't read a price (fetch failed, or Claude couldn't parse
    # the page) counts as a failure; a successful check resets both to zero.
    consecutive_check_failures: int = 0
    dead_alert_sent: bool = False
    # The most recent failed check's HTTP status code, if it failed with
    # one (None for a non-HTTP failure like a timeout/DNS error, and reset
    # to None the moment a check succeeds again). Surfaced as a distinct
    # "blocked" badge for 403/429 specifically - many sites (confirmed live
    # on a Shopify storefront, 2026-10) actively rate-limit/block automated
    # requests rather than just being slow or down, and that's worth a
    # different signal than the generic "unreachable" one, which only shows
    # after HEALTH_ALERT_AFTER_N_FAILURES anyway - this shows immediately.
    last_error_status: Optional[int] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class MarketplaceConfig(BaseModel):
    key: str
    poll_interval_minutes: int
    request_delay_seconds: float
    auth: Dict[str, str] = Field(default_factory=dict)
    last_fetch_at: Optional[str] = None
    updated_at: Optional[str] = None


@dataclass
class Listing:
    source: str
    external_id: str
    title: str
    description: str
    price: Optional[int]
    url: str
    location: Optional[str]
    ships: Optional[bool]
    published_at: Optional[datetime]
    # Set only by an auction marketplace (Marketplace.is_auction) - the
    # auction's hard deadline. Everything else about an auction listing
    # reuses the fields above as-is: `price` means "current bid requirement"
    # rather than a fixed asking price for these, not a separate concept.
    auction_ends_at: Optional[datetime] = None
    # A thumbnail/photo URL straight from the marketplace's own response -
    # every registered source already carries one (confirmed live, 2026-10),
    # just never extracted before. Never downloaded/stored, same as the
    # watched-item enrichment image - just linked to by its original URL,
    # for the "Top ads" cards (backlog item: richer top-ads panel).
    image_url: Optional[str] = None
    # Distance in km from Settings.home_lat/home_lon, only set when the
    # marketplace was actually asked to sort by distance (currently Blocket,
    # only for a "plain" search - see marketplaces._blocket_fetch). None for
    # everything else, including a Blocket listing fetched newest-first,
    # rather than storing a meaningless distance nobody asked to sort by.
    distance_km: Optional[float] = None
    raw: Dict[str, Any] = field(default_factory=dict)


class ScoreResult(BaseModel):
    score: int = Field(ge=1, le=10)
    reasoning: str
    uncertain_specs: List[str] = Field(default_factory=list)
    price_assessment: str
