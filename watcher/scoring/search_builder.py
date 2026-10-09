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

Each external "turn" is actually up to two Claude calls internally: a tiny
"decide" call (ready to finalize, or what to ask) and, once ready, a
separate "finalize" call that produces the full draft. Originally this was
one combined call whose schema could represent either outcome in the same
response - simpler code, but confirmed live (2026-10) to 400 with "Schema is
too complex" once the draft schema grew nested arrays-of-objects (phrase
provenance, watched models) alongside the question schema in the same
request. Splitting into two calls means neither individual schema ever has
to represent both outcomes at once, which is also why the draft schema
itself (ProposeSearch) uses flat string lists rather than nested objects for
search_phrases/watched_models, even on its own - encoding provenance/ a
watched model's fields as plain strings ("pattern | note | good price |
ideal", the same convention the manual form's textarea already uses) rather
than a list of sub-objects. Both calls use structured-output
(client.messages.create with output_config.format - the same mechanism
claude_scorer.py and price_watch.py already use), not Anthropic tool-use.
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
    min_price: Optional[int] = Field(default=None, description="SEK. Null for no floor - only set if cheap listings should be screened out.")
    scoring_mode: Literal["plain", "rated"] = Field(
        description="'rated': Claude scores every match against criteria below - use whenever the prompt "
        "expresses a quality judgement, preference, or a target to compare against. 'plain': a simple list "
        "with only deterministic filters below, no AI judgement - use for a bare 'show me everything matching X'."
    )
    instant_alert_price: Optional[int] = Field(
        default=None, description="Plain mode only: a match at or below this price alerts immediately. Null if not mentioned."
    )
    digest_style: Literal["itemized", "summary_link"] = Field(
        default="summary_link",
        description="'itemized' lists every digest match; 'summary_link' collapses them into one line - use "
        "'summary_link' for a search likely to generate a lot of matches, 'itemized' otherwise.",
    )
    excluded_models: List[str] = Field(default_factory=list, description="Wildcard patterns for models to exclude entirely, e.g. 'TX-SR6*'.")
    excluded_words: List[str] = Field(default_factory=list, description="Words that disqualify a listing if present in its title/description.")
    required_keywords: List[str] = Field(default_factory=list, description="At least one of these must appear - leave empty to require none.")
    watched_models: List[str] = Field(
        default_factory=list,
        description='Rated mode only, usually empty. One line per watched model: "pattern | note | good '
        'price | ideal" - the last part is the literal word "ideal" only for the single buy-it-now/grail '
        "target if the prompt describes one (a match at or below its good price scores 10/10 and becomes "
        "the benchmark everything else is judged against), omitted otherwise, e.g. "
        '"TX-NR6* | solid mid-range Onkyo | 1500-2500 SEK |" or "RTX 4080 | the one I actually want | 7000-8000 SEK | ideal".',
    )
    hard_criteria: List[str] = Field(default_factory=list, description="Rated mode only: requirements a match must satisfy, free text.")
    soft_criteria: List[str] = Field(default_factory=list, description="Rated mode only: nice-to-haves that boost score without disqualifying.")
    search_phrases: List[str] = Field(description="Phrases taken directly from the prompt - at least one.")
    suggested_phrases: List[str] = Field(
        default_factory=list,
        description="Comparable/similar phrases YOU added beyond what the prompt explicitly asked for "
        "(e.g. other brands/models with a similar spec) - empty if none. Shown to the user as suggestions "
        "they can remove before saving, never silently merged into search_phrases.",
    )
    suggested_phrases_note: str = Field(
        default="", description="If suggested_phrases is non-empty, one short sentence explaining why they're comparable. Empty otherwise."
    )
    marketplaces: List[str] = Field(description="Registry keys of the marketplaces this should run on - see the system prompt for which exist and what they carry.")


class BuilderTurn(BaseModel):
    """The builder's result for one external turn - never sent to or
    received from Claude directly as a single schema (see module docstring);
    run_turn constructs this from whichever of the two internal calls ran."""
    action: Literal["ask_user", "propose_search"]
    ask_user: Optional[AskUser] = None
    propose_search: Optional[ProposeSearch] = None


class SearchBuilderError(Exception):
    """Claude's response couldn't be turned into a valid result even after
    one retry - surfaced to the user as a plain error on the wizard screen
    rather than a 500, since a single bad call shouldn't be a crash."""


class _Decision(BaseModel):
    ready_to_finalize: bool = Field(
        description="True once there's enough information to propose a complete search - most prompts "
        "should resolve immediately. False only when genuinely blocked or offering something non-trivial "
        "the prompt didn't address (see the system prompt's guidance on when to ask)."
    )
    question: Optional[str] = Field(
        default=None, description="Required when ready_to_finalize is False: one clear question for the user, in the same language as their prompt. Null otherwise."
    )


_DECISION_SCHEMA = strict_json_schema(_Decision.model_json_schema())
_PROPOSE_SCHEMA = strict_json_schema(ProposeSearch.model_json_schema())


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
        "free-text request (most likely Swedish). You'll be asked, separately, whether you're ready to "
        "finalize (and if not, what to ask) and then to actually produce the finished search - never "
        "partial fields or placeholders either way.\n\n"
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
        "- search_phrases vs suggested_phrases: search_phrases is only what the prompt directly asked for; "
        "a comparable addition of your own goes in suggested_phrases instead, with one shared reason in "
        "suggested_phrases_note - never mix the two.\n"
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


def _call_with_retry(
    conn, client: Anthropic, settings: Settings, system: str, messages: List[Dict[str, str]],
    schema: Dict[str, Any], model_cls: type, label: str,
) -> BaseModel:
    """Calls Claude with the given schema, retrying once (with the parse
    error fed back) if the response isn't valid JSON matching model_cls.
    Raises SearchBuilderError if it still isn't valid after that retry."""
    current_messages = list(messages)
    for attempt in range(2):
        message = _call(client, settings.claude_model, system, current_messages, schema)
        _log_usage(conn, settings, message)
        text = _text_of(message)
        if text is not None:
            try:
                return model_cls.model_validate_json(text)
            except Exception as exc:
                logger.warning("Search builder %s call returned invalid JSON (attempt %d): %s", label, attempt + 1, exc)
                current_messages = current_messages + [
                    {"role": "assistant", "content": text},
                    {"role": "user", "content": f"That response was invalid: {exc}. Respond again, valid JSON only."},
                ]
                continue
        logger.warning("Search builder %s call had no text content block (attempt %d)", label, attempt + 1)

    raise SearchBuilderError(f"Claude could not produce a valid response for this prompt after a retry ({label}).")


def run_turn(
    conn, client: Anthropic, settings: Settings, transcript: List[Dict[str, str]], *, force_propose: bool = False
) -> BuilderTurn:
    """Runs one external turn of the builder loop against the given
    transcript (a list of {"role": "user"|"assistant", "content": ...}
    dicts - an assistant turn's content is the JSON text of its own
    previous BuilderTurn, so Claude can read back what it already
    asked/proposed). Internally this may be one or two Claude calls (see
    module docstring); externally it's always exactly one BuilderTurn, or a
    SearchBuilderError if a call can't be turned into a valid result even
    after its own retry."""
    system = _system_prompt()

    if not force_propose:
        decision = _call_with_retry(conn, client, settings, system, transcript, _DECISION_SCHEMA, _Decision, "decide")
        if not decision.ready_to_finalize:
            return BuilderTurn(action="ask_user", ask_user=AskUser(question=decision.question or "Can you say more about what you're looking for?"))
        finalize_messages = transcript + [
            {"role": "assistant", "content": decision.model_dump_json()},
            {"role": "user", "content": "Go ahead and propose the full search now."},
        ]
    else:
        finalize_messages = transcript + [
            {"role": "user", "content": "Please finalize the search now - use your best judgement for anything still unresolved."},
        ]

    draft = _call_with_retry(conn, client, settings, system, finalize_messages, _PROPOSE_SCHEMA, ProposeSearch, "finalize")
    return BuilderTurn(action="propose_search", propose_search=draft)


def _parse_watched_model_line(line: str) -> Optional[WatchedModel]:
    parts = [p.strip() for p in line.split("|")]
    pattern = parts[0] if parts else ""
    if not pattern:
        return None
    note = parts[1] if len(parts) > 1 else ""
    good_price = parts[2] if len(parts) > 2 else ""
    is_ideal = bool(parts[3]) if len(parts) > 3 else False
    return WatchedModel(pattern=pattern, note=note, good_price=good_price, is_ideal=is_ideal)


def draft_to_search(draft: ProposeSearch, *, included_phrases: Optional[List[str]] = None) -> Search:
    """Flattens a ProposeSearch draft into a real Search. `included_phrases`,
    if given, is the exact phrase text list to keep from
    search_phrases + suggested_phrases combined (the admin UI lets the user
    uncheck suggested ones before creating); omitted, every phrase is kept."""
    all_phrases = list(draft.search_phrases) + list(draft.suggested_phrases)
    phrases = included_phrases if included_phrases is not None else all_phrases
    watched_models = [m for m in (_parse_watched_model_line(line) for line in draft.watched_models) if m is not None]
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
        watched_models=watched_models,
        hard_criteria=draft.hard_criteria,
        soft_criteria=draft.soft_criteria,
        search_phrases=phrases,
        marketplaces=draft.marketplaces,
    )


def all_draft_phrases(draft: ProposeSearch) -> List[str]:
    """search_phrases + suggested_phrases, in the one combined order the
    admin UI's phrase checkboxes (and draft_to_search's included_phrases)
    index into - see search_prompt_wizard.html."""
    return list(draft.search_phrases) + list(draft.suggested_phrases)


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
