"""Turns a free-text prompt (backlog #21) into a fully-configured Search,
instead of hand-filling the ~15-field manual form.

This is a short back-and-forth, not one call: Claude can either ask a
clarifying question (genuinely blocked - e.g. no price ceiling and no
indication it's meant to be unlimited - or offering something non-trivial
the prompt didn't address either way, like whether to also search for
comparable models) or propose a finished draft. The caller (admin/routes.py)
drives this loop turn by turn across page loads, carrying the transcript in
a hidden form field - there's no server-side session/conversation state,
consistent with the rest of this server-rendered (no JS) admin app.

Rather than Anthropic tool-use, each turn is one structured-output call
(client.messages.create with output_config.format - the same mechanism
claude_scorer.py and price_watch.py already use) returning a flat object
with an `action` discriminator and one matching nested sub-object, chosen
over a Union/anyOf-of-objects response shape to stay on the exact pattern
already proven to work elsewhere in this codebase.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Literal, Optional

from anthropic import Anthropic
from pydantic import BaseModel, Field

from watcher.models import Search, WatchedModel
from watcher.scoring.claude_scorer import estimate_cost_usd
from watcher.scoring.schema import strict_json_schema
from watcher.settings import Settings
from watcher.sources.blocket_geo import COUNTY_CODE

logger = logging.getLogger(__name__)

MAX_TOKENS = 2048
# How many "ask_user" rounds the builder allows before forcing a final
# proposal regardless - keeps a confused or contradictory prompt from
# turning into an endless back-and-forth; Claude is told to use its best
# judgement for anything still unresolved once forced.
MAX_QUESTION_ROUNDS = 4


class PhraseSuggestion(BaseModel):
    text: str = Field(description="One search phrase, exactly as it would go into a marketplace's search box.")
    source: Literal["explicit", "suggested"] = Field(
        description="'explicit' if the user's prompt asked for this phrase directly, 'suggested' if Claude "
        "added it as a comparable/similar option the user didn't explicitly name."
    )
    note: Optional[str] = Field(
        default=None,
        description="Only for a 'suggested' phrase: a short reason it's comparable (e.g. 'similar integrated "
        "amp, same price bracket') - shown to the user so they can judge whether to keep it.",
    )


class WatchedModelDraft(BaseModel):
    pattern: str = Field(description="A model name/wildcard pattern, e.g. 'Marantz PM6007' or 'TX-NR6*'.")
    note: str = Field(default="", description="A short note about why this model is watched.")
    good_price: str = Field(default="", description="A price or price range that counts as a good deal, e.g. '1500-2500 SEK'.")
    is_ideal: bool = Field(
        default=False,
        description="True only for the single buy-it-now/grail target, if the prompt describes one - a "
        "listing matching this at or below good_price gets scored 10/10 and becomes the benchmark every "
        "other candidate is judged against. Most searches have none of these.",
    )


class AskUser(BaseModel):
    question: str = Field(description="One clear question for the user, in the same language as their prompt.")


class ProposeSearch(BaseModel):
    summary: str = Field(
        description="2-4 sentences, written for the user to read on a confirmation screen, explaining how "
        "this search is configured and why - plain language, not a restatement of the raw fields."
    )
    name: str = Field(description="A short, human-readable name for this search.")
    scope: Literal["local", "national"] = Field(description="'local' only if the prompt implies a specific place.")
    location: str = Field(
        default="",
        description="Required if scope is 'local'. Use an exact Swedish county (län) name when the prompt's "
        "place maps to one - that gets real server-side filtering on Blocket - otherwise a city/area name "
        "still works, just less precisely. Empty for 'national'.",
    )
    require_shipping: bool = Field(default=False, description="True if the prompt implies shipping matters (mainly relevant for 'national' scope).")
    max_price: Optional[int] = Field(default=None, description="SEK. Null for no ceiling - don't invent one.")
    min_price: Optional[int] = Field(default=None, description="SEK. Null for no floor - only set this if the prompt implies suspiciously-cheap listings should be screened out.")
    scoring_mode: Literal["plain", "rated"] = Field(
        description="'rated': Claude scores/reasons about every match against criteria below - use this "
        "whenever the prompt expresses any quality judgement, preference, or a specific target to compare "
        "against. 'plain': a simple browsable list with only deterministic filters below, no AI judgement - "
        "use this when the prompt is just 'show me everything matching X'."
    )
    instant_alert_price: Optional[int] = Field(
        default=None, description="Plain mode only: a match at or below this price pings Slack immediately instead of waiting for the daily digest. Null if not mentioned."
    )
    digest_style: Literal["itemized", "summary_link"] = Field(
        default="summary_link",
        description="'itemized' lists every digest match; 'summary_link' collapses them into one line - use "
        "'summary_link' for a search likely to generate a lot of matches, 'itemized' otherwise.",
    )
    excluded_models: List[str] = Field(default_factory=list, description="Wildcard patterns for models to exclude entirely, e.g. 'TX-SR6*'.")
    excluded_words: List[str] = Field(default_factory=list, description="Words that disqualify a listing if present in its title/description.")
    required_keywords: List[str] = Field(default_factory=list, description="At least one of these must appear - leave empty to require none.")
    watched_models: List[WatchedModelDraft] = Field(default_factory=list)
    hard_criteria: List[str] = Field(default_factory=list, description="Rated mode only: requirements a match must satisfy, free text.")
    soft_criteria: List[str] = Field(default_factory=list, description="Rated mode only: nice-to-haves that boost score without disqualifying.")
    search_phrases: List[PhraseSuggestion] = Field(description="At least one. See PhraseSuggestion for the explicit-vs-suggested distinction.")
    marketplaces: List[str] = Field(description="Registry keys of the marketplaces this should run on - see the system prompt for which exist and what they carry.")


class BuilderTurn(BaseModel):
    action: Literal["ask_user", "propose_search"]
    ask_user: Optional[AskUser] = None
    propose_search: Optional[ProposeSearch] = None


class SearchBuilderError(Exception):
    """Claude's response couldn't be turned into a valid turn even after one
    retry - surfaced to the user as a plain error on the wizard screen rather
    than a 500, since a single bad turn shouldn't be a crash."""


_TURN_SCHEMA = strict_json_schema(BuilderTurn.model_json_schema())
_PROPOSE_ONLY_SCHEMA = strict_json_schema(ProposeSearch.model_json_schema())


def _marketplace_catalog_text() -> str:
    # Imported lazily (not at module load) to avoid a brittle import-order
    # dependency - marketplaces.py doesn't import this module, so there's no
    # real cycle, but every other call in this file is a plain function body,
    # and this keeps the one registry read in the same style.
    from watcher.marketplaces import MARKETPLACES

    lines = []
    for m in MARKETPLACES.values():
        kind = "live auction" if m.is_auction else "fixed-price classifieds"
        lines.append(f'- "{m.key}" ({m.display_name}, {kind}): {m.category_note}')
    return "\n".join(lines)


def _county_list_text() -> str:
    return ", ".join(sorted(COUNTY_CODE.keys()))


def _system_prompt() -> str:
    return (
        "You are helping configure a search for Fetch, a personal secondhand-marketplace watcher, from a "
        "free-text request (most likely Swedish). Each turn, either ask ONE clarifying question or propose "
        "a finished search - never both, never partial fields with placeholders.\n\n"
        "When to ask vs. just proceed: ask only when genuinely blocked (the prompt is contradictory, or a "
        "choice meaningfully changes the result and there's no reasonable default) or when you want to offer "
        "something non-trivial the prompt didn't address - most commonly, suggesting comparable/similar "
        "models or brands when the prompt names one specific item. If the prompt already says to include "
        "(or not include) comparable alternatives, just do what it says - don't ask to do what was already "
        "asked for. If the prompt clearly wants only that one specific item (e.g. \"I already have everything "
        "else, just need exactly this part\"), don't suggest alternatives at all. Otherwise, use a sensible "
        "default and proceed rather than asking - most prompts should resolve in one turn.\n\n"
        "Field guidance beyond what's in the schema itself:\n"
        "- scope/location: 'local' only if a place is implied. Swedish counties Blocket can filter on "
        f"precisely: {_county_list_text()}. Prefer one of these exact names when the prompt's place maps to "
        "one (even a city - use the county it's in); a city/area name works too, just less precisely.\n"
        "- scoring_mode: 'rated' whenever the prompt expresses a preference, quality bar, or a specific "
        "target to judge other listings against; 'plain' for a bare 'show me everything matching X'.\n"
        "- marketplaces - registered options and what each actually carries:\n"
        f"{_marketplace_catalog_text()}\n"
        "  Pick whichever plausibly carry this category of item; if genuinely unsure, include all of them "
        "rather than asking just to narrow the list - the deterministic filters and (for rated searches) "
        "Claude's own scoring still apply downstream, so a mismatched marketplace costs nothing but a few "
        "extra listings to filter past.\n"
        "- search_phrases: tag each 'explicit' (named in the prompt) or 'suggested' (your own comparable "
        "addition), with a short note on why for a suggested one.\n"
        "- summary: a few plain-language sentences for a human reading a confirmation screen, not a JSON dump."
    )


def _call(client: Anthropic, model: str, system: str, messages: List[Dict[str, str]], schema: Dict[str, Any]) -> Any:
    return client.messages.create(
        model=model,
        max_tokens=MAX_TOKENS,
        system=system,
        messages=messages,
        output_config={"format": {"type": "json_schema", "schema": schema}},
    )


def _log_usage(conn, settings: Settings, message: Any) -> None:
    from watcher import storage

    cost = estimate_cost_usd(settings.claude_model, message.usage.input_tokens, message.usage.output_tokens, batch=False)
    storage.log_token_usage(conn, None, settings.claude_model, message.usage.input_tokens, message.usage.output_tokens, cost)


def _text_of(message: Any) -> Optional[str]:
    return next((block.text for block in message.content if getattr(block, "type", None) == "text"), None)


def run_turn(
    conn, client: Anthropic, settings: Settings, transcript: List[Dict[str, str]], *, force_propose: bool = False
) -> BuilderTurn:
    """Runs one turn of the builder loop against the given transcript (a
    list of {"role": "user"|"assistant", "content": ...} dicts - an
    assistant turn's content is the JSON text of its own previous BuilderTurn,
    so Claude can read back what it already asked/proposed). Raises
    SearchBuilderError if Claude's response can't be turned into a valid
    BuilderTurn even after one retry with the error fed back."""
    system = _system_prompt()
    schema = _PROPOSE_ONLY_SCHEMA if force_propose else _TURN_SCHEMA
    messages = list(transcript)
    if force_propose:
        messages.append({
            "role": "user",
            "content": "Please finalize the search now - use your best judgement for anything still unresolved.",
        })

    for attempt in range(2):
        message = _call(client, settings.claude_model, system, messages, schema)
        _log_usage(conn, settings, message)
        text = _text_of(message)
        if text is not None:
            try:
                if force_propose:
                    propose = ProposeSearch.model_validate_json(text)
                    return BuilderTurn(action="propose_search", propose_search=propose)
                turn = BuilderTurn.model_validate_json(text)
                if turn.action == "ask_user" and turn.ask_user is not None:
                    return turn
                if turn.action == "propose_search" and turn.propose_search is not None:
                    return turn
                raise ValueError(f"action={turn.action!r} but its matching sub-object is missing")
            except Exception as exc:
                logger.warning("Search builder returned an invalid turn (attempt %d): %s", attempt + 1, exc)
                messages = messages + [
                    {"role": "assistant", "content": text},
                    {"role": "user", "content": f"That response was invalid: {exc}. Respond again, valid JSON only."},
                ]
                continue
        logger.warning("Search builder response had no text content block (attempt %d)", attempt + 1)

    raise SearchBuilderError("Claude could not produce a valid response for this prompt after a retry.")


def draft_to_search(draft: ProposeSearch, *, included_phrases: Optional[List[str]] = None) -> Search:
    """Flattens a ProposeSearch draft into a real Search - the provenance
    tags on search_phrases only matter for the wizard's own checkboxes, never
    beyond that. `included_phrases`, if given, is the exact phrase text list
    to keep (the admin UI lets the user uncheck suggested ones before
    creating); omitted, every phrase in the draft is kept."""
    phrases = (
        included_phrases if included_phrases is not None else [p.text for p in draft.search_phrases]
    )
    return Search(
        name=draft.name,
        scope=draft.scope,
        location=draft.location,
        require_shipping=draft.require_shipping,
        max_price=draft.max_price,
        min_price=draft.min_price,
        scoring_mode=draft.scoring_mode,
        instant_alert_price=draft.instant_alert_price,
        digest_style=draft.digest_style,
        excluded_models=draft.excluded_models,
        excluded_words=draft.excluded_words,
        required_keywords=draft.required_keywords,
        watched_models=[
            WatchedModel(pattern=wm.pattern, note=wm.note, good_price=wm.good_price, is_ideal=wm.is_ideal)
            for wm in draft.watched_models
        ],
        hard_criteria=draft.hard_criteria,
        soft_criteria=draft.soft_criteria,
        search_phrases=phrases,
        marketplaces=draft.marketplaces,
    )


def collapse_transcript_to_prompt(transcript: List[Dict[str, str]]) -> str:
    """Collapses a finished drafting session's transcript into the one
    editable text blob stored as Search.creation_prompt - the original
    request plus any Q&A, in plain readable form. Editing this later just
    means rewriting this text and running the builder again from scratch."""
    if not transcript:
        return ""
    lines = [transcript[0]["content"]]
    i = 1
    while i < len(transcript):
        entry = transcript[i]
        if entry["role"] == "assistant":
            try:
                turn = BuilderTurn.model_validate_json(entry["content"])
            except Exception:
                i += 1
                continue
            if turn.action == "ask_user" and turn.ask_user is not None and i + 1 < len(transcript):
                answer = transcript[i + 1]["content"]
                lines.append(f"\nClaude: {turn.ask_user.question}\nYou: {answer}")
                i += 2
                continue
        i += 1
    return "\n".join(lines)
