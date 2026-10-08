"""Core data types shared across sources, scoring, storage and the admin UI."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

Scope = Literal["local", "national"]
ScoringMode = Literal["plain", "rated"]
CheckFrequency = Literal["daily", "weekly", "monthly"]


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
    current_price: Optional[int] = None
    lowest_price_seen: Optional[int] = None
    last_alert_price: Optional[int] = None
    last_checked_at: Optional[str] = None
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
    raw: Dict[str, Any] = field(default_factory=dict)


class ScoreResult(BaseModel):
    score: int = Field(ge=1, le=10)
    reasoning: str
    uncertain_specs: List[str] = Field(default_factory=list)
    price_assessment: str
