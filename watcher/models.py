"""Core data types shared across sources, scoring, storage and the admin UI."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

Scope = Literal["local", "national"]


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
    raw: Dict[str, Any] = field(default_factory=dict)


class ScoreResult(BaseModel):
    score: int = Field(ge=1, le=10)
    reasoning: str
    uncertain_specs: List[str] = Field(default_factory=list)
    price_assessment: str
