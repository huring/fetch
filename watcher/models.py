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


class BlocketQuery(BaseModel):
    q: str
    category: Optional[str] = None
    sub_category: Optional[str] = None
    region: Optional[str] = None


class Search(BaseModel):
    id: Optional[int] = None
    name: str
    enabled: bool = True
    scope: Scope = "local"
    location: str = ""
    require_shipping: bool = False
    max_price: Optional[int] = None
    excluded_models: List[str] = Field(default_factory=list)
    excluded_words: List[str] = Field(default_factory=list)
    required_keywords: List[str] = Field(default_factory=list)
    watched_models: List[WatchedModel] = Field(default_factory=list)
    hard_criteria: List[str] = Field(default_factory=list)
    soft_criteria: List[str] = Field(default_factory=list)
    # Keyed by marketplace key (e.g. "blocket"); each value is a list of raw
    # dicts matching that marketplace's query_model, validated on use via the
    # marketplace registry rather than at the Search model level, so adding a
    # new marketplace never requires changing this schema.
    marketplace_queries: Dict[str, List[Dict[str, Any]]] = Field(default_factory=dict)
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
