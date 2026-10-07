"""Batch listing scoring via the Claude API (structured outputs).

Each call scores a batch of listings from one search against that search's
criteria. Listings Claude's response doesn't cover (mismatched count, parse
edge cases) come back as None and are left pending in storage so they're
retried on the next scheduled run rather than silently dropped.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from anthropic import Anthropic
from pydantic import BaseModel, Field

from watcher.models import ScoreResult, Search

logger = logging.getLogger(__name__)

HAIKU_INPUT_COST_PER_MTOK = 1.00
HAIKU_OUTPUT_COST_PER_MTOK = 5.00


class _ListingScore(BaseModel):
    listing_index: int
    score: int = Field(ge=1, le=10)
    reasoning: str
    uncertain_specs: List[str] = Field(default_factory=list)
    price_assessment: str


class _BatchScoreResponse(BaseModel):
    scores: List[_ListingScore]


def estimate_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    if "haiku-4-5" not in model:
        logger.warning(
            "No pricing table entry for model %s; estimating cost using Haiku 4.5 rates", model
        )
    return (
        (input_tokens / 1_000_000) * HAIKU_INPUT_COST_PER_MTOK
        + (output_tokens / 1_000_000) * HAIKU_OUTPUT_COST_PER_MTOK
    )


def _build_prompt(search: Search, candidates: List[Dict[str, Any]]) -> str:
    lines: List[str] = []
    if search.hard_criteria:
        lines.append("Hard requirements (must satisfy):")
        lines.extend(f"- {c}" for c in search.hard_criteria)
    if search.soft_criteria:
        lines.append("Nice-to-haves (boost score, not disqualifying):")
        lines.extend(f"- {c}" for c in search.soft_criteria)
    if search.watched_models:
        lines.append("Specifically watched models (pattern: note, good price):")
        for wm in search.watched_models:
            lines.append(f"- {wm.pattern}: {wm.note} (good price: {wm.good_price or 'n/a'})")
    if search.require_shipping:
        lines.append(
            "This search is national in scope: prefer/require listings where the seller ships. "
            "If shipping availability is unclear from the listing text, flag it in uncertain_specs "
            "rather than assuming either way."
        )

    listings_block = []
    for idx, c in enumerate(candidates):
        block = f"[{idx}] title: {c['title']}\nprice: {c['price']}\ndescription: {c['description']}\nurl: {c['url']}"
        if c.get("source_note"):
            block += f"\nnote: {c['source_note']}"
        listings_block.append(block)

    return (
        f'You are assessing second-hand marketplace listings for the watch list "{search.name}".\n\n'
        + "\n".join(lines)
        + f"\n\nThere are exactly {len(candidates)} listings below, indexed [0] to [{len(candidates) - 1}]. "
        "Return exactly one result per listing, no more, no fewer. For each, give a score from 1 "
        "(irrelevant/bad match) to 10 (excellent match), brief reasoning, a price assessment, and a list "
        "of any specs you are not certain about from the listing text - never guess a spec you can't "
        "confirm, flag it instead. Set listing_index to match the bracketed index below exactly - do not "
        "invent an index outside that range.\n\n" + "\n\n".join(listings_block)
    )


def score_batch(
    client: Anthropic, model: str, search: Search, candidates: List[Dict[str, Any]]
) -> Tuple[List[Optional[ScoreResult]], int, int]:
    """Returns (results, input_tokens, output_tokens). results[i] maps to
    candidates[i], or None if Claude's response didn't cover that index."""
    prompt = _build_prompt(search, candidates)
    response = client.messages.parse(
        model=model,
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
        output_format=_BatchScoreResponse,
    )

    results: List[Optional[ScoreResult]] = [None] * len(candidates)
    for item in response.parsed_output.scores:
        if 0 <= item.listing_index < len(candidates):
            results[item.listing_index] = ScoreResult(
                score=item.score,
                reasoning=item.reasoning,
                uncertain_specs=item.uncertain_specs,
                price_assessment=item.price_assessment,
            )
        else:
            logger.warning("Claude returned out-of-range listing_index %s", item.listing_index)

    return results, response.usage.input_tokens, response.usage.output_tokens
