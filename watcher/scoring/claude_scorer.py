"""Listing scoring via the Claude Message Batches API (structured outputs).

Scoring runs asynchronously: watcher/pipeline.py submits one batch request
per chunk of a search's pending listings, and a later pass collects results
once Anthropic finishes processing them (usually minutes, up to 24h - see
submit_pending_scoring/collect_finished_batches in pipeline.py). There's no
synchronous scoring path any more - this is a scheduled background tool with
no one waiting on a response, and the Batch API is 50% cheaper per token for
exactly that kind of workload.

The stable part of each prompt (the search's criteria, watched models,
instructions) is sent as a cached `system` block - it's identical across
every chunk scored for the same search, so a search with more pending
listings than one chunk holds reuses it at a fraction of the cost instead of
paying full price again for each chunk.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from watcher.models import ScoreResult, Search
from watcher.scoring.schema import strict_json_schema

logger = logging.getLogger(__name__)

HAIKU_INPUT_COST_PER_MTOK = 1.00
HAIKU_OUTPUT_COST_PER_MTOK = 5.00
BATCH_DISCOUNT = 0.5  # Message Batches API: 50% off every token, including cache reads/writes

MAX_TOKENS = 4096


class _ListingScore(BaseModel):
    listing_index: int
    score: int = Field(ge=1, le=10)
    reasoning: str = Field(description="One short sentence (under ~20 words) explaining the score.")
    uncertain_specs: List[str] = Field(default_factory=list)
    price_assessment: str = Field(description="A few words, e.g. 'fair price' or 'overpriced by ~500 SEK'.")


class _BatchScoreResponse(BaseModel):
    scores: List[_ListingScore]


_RESPONSE_SCHEMA = strict_json_schema(_BatchScoreResponse.model_json_schema())


def estimate_cost_usd(model: str, input_tokens: int, output_tokens: int, batch: bool = True) -> float:
    if "haiku-4-5" not in model:
        logger.warning(
            "No pricing table entry for model %s; estimating cost using Haiku 4.5 rates", model
        )
    cost = (
        (input_tokens / 1_000_000) * HAIKU_INPUT_COST_PER_MTOK
        + (output_tokens / 1_000_000) * HAIKU_OUTPUT_COST_PER_MTOK
    )
    return cost * BATCH_DISCOUNT if batch else cost


def _build_system_text(search: Search) -> str:
    lines: List[str] = [f'You are assessing second-hand marketplace listings for the watch list "{search.name}".']
    if search.hard_criteria:
        lines.append("Hard requirements (must satisfy):")
        lines.extend(f"- {c}" for c in search.hard_criteria)
    if search.soft_criteria:
        lines.append("Nice-to-haves (boost score, not disqualifying):")
        lines.extend(f"- {c}" for c in search.soft_criteria)
    ideal_models = [wm for wm in search.watched_models if wm.is_ideal]
    other_models = [wm for wm in search.watched_models if not wm.is_ideal]
    if ideal_models:
        lines.append(
            "Buy-it-now target(s) for this search (pattern: note, good price) - if a listing is "
            "genuinely one of these (or a clear equivalent) in working condition at or below its "
            "good price, score it 10/10. Score every other candidate in this search relative to how "
            "it compares against this benchmark (closer in spec/condition/price = higher, further = "
            "lower), while still respecting the hard requirements above:"
        )
        for wm in ideal_models:
            lines.append(f"- {wm.pattern}: {wm.note} (good price: {wm.good_price or 'n/a'})")
    if other_models:
        lines.append("Specifically watched models (pattern: note, good price):")
        for wm in other_models:
            lines.append(f"- {wm.pattern}: {wm.note} (good price: {wm.good_price or 'n/a'})")
    if search.require_shipping:
        lines.append(
            "This search is national in scope: prefer/require listings where the seller ships. "
            "If shipping availability is unclear from the listing text, flag it in uncertain_specs "
            "rather than assuming either way."
        )
    lines.append(
        "Keep reasoning to one short sentence and price_assessment to a few words - these are for "
        "quick scanning, not detailed essays."
    )
    return "\n".join(lines)


def _build_user_text(candidates: List[Dict[str, Any]]) -> str:
    listings_block = []
    for idx, c in enumerate(candidates):
        block = f"[{idx}] title: {c['title']}\nprice: {c['price']}\ndescription: {c['description']}\nurl: {c['url']}"
        if c.get("source_note"):
            block += f"\nnote: {c['source_note']}"
        listings_block.append(block)

    return (
        f"There are exactly {len(candidates)} listings below, indexed [0] to [{len(candidates) - 1}]. "
        "Return exactly one result per listing, no more, no fewer. For each, give a score from 1 "
        "(irrelevant/bad match) to 10 (excellent match), brief reasoning, a price assessment, and a list "
        "of any specs you are not certain about from the listing text - never guess a spec you can't "
        "confirm, flag it instead. Set listing_index to match the bracketed index below exactly - do not "
        "invent an index outside that range.\n\n" + "\n\n".join(listings_block)
    )


def build_batch_request(
    custom_id: str, model: str, search: Search, candidates: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """One entry for the `requests` list passed to
    client.messages.batches.create(...) - scores one chunk (up to
    settings.scoring_batch_size) of a single search's pending listings."""
    return {
        "custom_id": custom_id,
        "params": {
            "model": model,
            "max_tokens": MAX_TOKENS,
            "system": [
                {"type": "text", "text": _build_system_text(search), "cache_control": {"type": "ephemeral"}}
            ],
            "messages": [{"role": "user", "content": _build_user_text(candidates)}],
            "output_config": {"format": {"type": "json_schema", "schema": _RESPONSE_SCHEMA}},
        },
    }


def parse_score_message(content: List[Any], num_candidates: int) -> List[Optional[ScoreResult]]:
    """Parses a completed batch result's message content (a list of content
    blocks) into one ScoreResult per candidate index. A candidate the
    response doesn't cover comes back as None and is retried on the next
    scoring sweep rather than silently dropped."""
    results: List[Optional[ScoreResult]] = [None] * num_candidates
    text = next((block.text for block in content if getattr(block, "type", None) == "text"), None)
    if text is None:
        logger.warning("Claude's batch response had no text content block")
        return results
    try:
        parsed = _BatchScoreResponse.model_validate_json(text)
    except Exception as exc:
        logger.warning("Could not parse Claude's batch response JSON: %s", exc)
        return results

    for item in parsed.scores:
        if 0 <= item.listing_index < num_candidates:
            results[item.listing_index] = ScoreResult(
                score=item.score,
                reasoning=item.reasoning,
                uncertain_specs=item.uncertain_specs,
                price_assessment=item.price_assessment,
            )
        else:
            logger.warning("Claude returned out-of-range listing_index %s", item.listing_index)

    return results
