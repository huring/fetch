"""Turns a free-text prompt (backlog #21) into a Search's search_phrases,
watched_models, hard_criteria and soft_criteria - the fields that actually
benefit from an LLM's judgement (comparable models/brands, pricing, turning
prose into a criteria list) - instead of hand-typing them into the manual
form. Every other field (scope, location, price bounds, marketplaces,
digest style, etc.) gets a plain default and is left for the manual/
Advanced form, which already exists for exactly this.

This is a short back-and-forth, not one call: Claude can either ask a
clarifying question (genuinely blocked, or offering something non-trivial
the prompt didn't address either way, like whether to also search for
comparable models) or propose a finished draft. The caller (admin/routes.py)
drives this loop turn by turn across page loads, carrying the transcript in
a hidden form field - there's no server-side session/conversation state,
consistent with the rest of this server-rendered (no JS) admin app.

Each external turn is exactly one Claude call. This deliberately does NOT
use the Messages API's structured-output mode (output_config.format=
json_schema) - we spent three rounds (2026-10) chasing a live "Schema is
too complex" 400 through that mode, first assuming nesting was the cause,
then field count, cutting the draft schema down each time without ever
being sure which one actually mattered (or whether it was really schema
*size*, via the verbose per-field descriptions, all along) - a guessing
game we have no way to settle without a live API key in the sandbox this
runs in. Rather than keep guessing, this version asks Claude for a bare
JSON object in the system prompt and parses the response text directly
against the same Pydantic models, the same way every other text response
in this codebase gets parsed. There's no schema for the API to reject, so
this whole failure class is gone outright; `_call_with_retry` below already
validates-and-retries-once on bad JSON, same as before.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Literal, NamedTuple, Optional

from anthropic import Anthropic
from pydantic import BaseModel, Field

from watcher.models import Search, WatchedModel
from watcher.scoring.claude_scorer import estimate_cost_usd
from watcher.settings import Settings

logger = logging.getLogger(__name__)

MAX_TOKENS = 2048
# How many "ask_user" rounds the builder allows before forcing a final
# proposal regardless - keeps a confused or contradictory prompt from
# turning into an endless back-and-forth; Claude is told to use its best
# judgement for anything still unresolved once forced.
MAX_QUESTION_ROUNDS = 4
# Marks a search_phrases line as Claude's own comparable addition rather
# than something the prompt asked for directly, optionally followed by
# " - a short reason", e.g. "[suggested] Yamaha A-S301 - similar integrated
# amp". One field, not a separate suggested_phrases list, just to keep
# things simple - see module docstring.
SUGGESTED_PREFIX = "[suggested] "


class AskUser(BaseModel):
    question: str


class ProposeSearch(BaseModel):
    summary: str
    name: str
    scoring_mode: Literal["plain", "rated"]
    search_phrases: List[str]
    watched_models: List[str] = Field(default_factory=list)
    hard_criteria: List[str] = Field(default_factory=list)
    soft_criteria: List[str] = Field(default_factory=list)


class BuilderTurn(BaseModel):
    """The builder's result for one external turn - exactly what Claude's
    response text is parsed into (see module docstring)."""
    action: Literal["ask_user", "propose_search"]
    ask_user: Optional[AskUser] = None
    propose_search: Optional[ProposeSearch] = None


class SearchBuilderError(Exception):
    """Claude's response couldn't be turned into a valid result even after
    one retry - surfaced to the user as a plain error on the wizard screen
    rather than a 500, since a single bad call shouldn't be a crash."""


def _system_prompt() -> str:
    return (
        "You are helping configure a search for Fetch, a personal secondhand-marketplace watcher, from a "
        "free-text request (most likely Swedish). You only ever produce: a name, whether this should be "
        "AI-rated or a plain list, the search phrases to run, and (rated mode only) watched models and "
        "hard/soft criteria - every other setting (location, price limits, which marketplaces, etc.) is "
        "configured separately afterward, not by you.\n\n"
        "Respond with ONLY a single JSON object - no markdown code fences, no text before or after it - in "
        "exactly this shape:\n"
        "{\n"
        '  "action": "ask_user" or "propose_search",\n'
        '  "ask_user": {"question": "..."} or null,\n'
        '  "propose_search": {\n'
        '    "summary": "...", "name": "...", "scoring_mode": "plain" or "rated",\n'
        '    "search_phrases": ["...", ...], "watched_models": ["...", ...],\n'
        '    "hard_criteria": ["...", ...], "soft_criteria": ["...", ...]\n'
        "  } or null\n"
        "}\n"
        'Use "ask_user" with propose_search set to null when you need to ask something first. Use '
        '"propose_search" with ask_user set to null once you have enough to finalize. Never fill in both, '
        "or neither.\n\n"
        "When to ask vs. just proceed: ask only when genuinely blocked (the prompt is contradictory, or a "
        "choice meaningfully changes the result and there's no reasonable default) or when you want to offer "
        "something non-trivial the prompt didn't address - most commonly, suggesting comparable/similar "
        "models or brands when the prompt names one specific item. If the prompt already says to include "
        "(or not include) comparable alternatives, just do what it says - don't ask to do what was already "
        "asked for. If the prompt clearly wants only that one specific item (e.g. \"I already have everything "
        "else, just need exactly this part\"), don't suggest alternatives at all. Otherwise, use a sensible "
        "default and proceed rather than asking - most prompts should resolve in one turn.\n\n"
        "Field guidance:\n"
        "- scoring_mode: 'rated' whenever the prompt expresses a preference, quality bar, or a specific "
        "target to judge other listings against; 'plain' for a bare 'show me everything matching X' - in "
        "that case leave watched_models/hard_criteria/soft_criteria empty, they're not used.\n"
        "- search_phrases: at least one. Each is either exactly what the prompt asked for, or - if you're "
        f'adding a comparable option beyond what was explicitly asked - prefixed "{SUGGESTED_PREFIX}" plus '
        f'" - a short reason", e.g. "Marantz PM6007" or "{SUGGESTED_PREFIX}Yamaha A-S301 - similar integrated '
        'amp, same price bracket".\n'
        "- watched_models: rated mode only, usually empty. One string per watched model: "
        '"pattern | note | good price | ideal" - the last part is the literal word "ideal" only for the '
        "single buy-it-now/grail target if the prompt describes one (a match at or below its good price "
        "scores 10/10 and becomes the benchmark everything else is judged against), omitted otherwise, e.g. "
        '"TX-NR6* | solid mid-range Onkyo | 1500-2500 SEK |" or "RTX 4080 | the one I actually want | '
        '7000-8000 SEK | ideal".\n'
        "- hard_criteria / soft_criteria: rated mode only, free text, one requirement or nice-to-have per "
        "string.\n"
        "- summary: 2-4 plain-language sentences for a human reading a confirmation screen, not a JSON dump "
        "- it's fine (and worth doing) to mention that location/price/marketplace settings use defaults they "
        "can adjust afterward, since you don't set those yourself."
    )


def _call(client: Anthropic, model: str, system: str, messages: List[Dict[str, str]]) -> Any:
    return client.messages.create(model=model, max_tokens=MAX_TOKENS, system=system, messages=messages)


def _log_usage(conn, settings: Settings, message: Any) -> None:
    from watcher import storage

    cost = estimate_cost_usd(settings.claude_model, message.usage.input_tokens, message.usage.output_tokens, batch=False)
    storage.log_token_usage(conn, None, settings.claude_model, message.usage.input_tokens, message.usage.output_tokens, cost)


def _text_of(message: Any) -> Optional[str]:
    return next((block.text for block in message.content if getattr(block, "type", None) == "text"), None)


def _extract_json_object(text: str) -> str:
    """Claude is asked for a bare JSON object but may still wrap it in a
    markdown code fence or add a stray word around it - slicing from the
    first "{" to the last "}" strips any of that without needing to know
    which, if either, is actually present."""
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return text
    return text[start : end + 1]


def _call_with_retry(
    conn, client: Anthropic, settings: Settings, system: str, messages: List[Dict[str, str]],
) -> BuilderTurn:
    """Calls Claude, retrying once (with the parse error fed back) if the
    response isn't valid JSON matching BuilderTurn. Raises SearchBuilderError
    if it still isn't valid after that retry."""
    current_messages = list(messages)
    for attempt in range(2):
        message = _call(client, settings.claude_model, system, current_messages)
        _log_usage(conn, settings, message)
        text = _text_of(message)
        if text is not None:
            try:
                return BuilderTurn.model_validate_json(_extract_json_object(text))
            except Exception as exc:
                logger.warning("Search builder call returned invalid JSON (attempt %d): %s", attempt + 1, exc)
                current_messages = current_messages + [
                    {"role": "assistant", "content": text},
                    {"role": "user", "content": f"That response was invalid: {exc}. Respond again with ONLY the JSON object, no other text."},
                ]
                continue
        logger.warning("Search builder call had no text content block (attempt %d)", attempt + 1)

    raise SearchBuilderError("Claude could not produce a valid response for this prompt after a retry.")


def run_turn(
    conn, client: Anthropic, settings: Settings, transcript: List[Dict[str, str]], *, force_propose: bool = False
) -> BuilderTurn:
    """Runs one external turn of the builder loop against the given
    transcript (a list of {"role": "user"|"assistant", "content": ...}
    dicts - an assistant turn's content is the JSON text of its own
    previous BuilderTurn, so Claude can read back what it already
    asked/proposed). Exactly one Claude call; raises SearchBuilderError if
    it can't be turned into a valid result even after its own retry."""
    messages = list(transcript)
    if force_propose:
        messages = messages + [
            {
                "role": "user",
                "content": 'Please finalize the search now - use your best judgement for anything still '
                'unresolved. Respond with action "propose_search", not a question.',
            }
        ]
    return _call_with_retry(conn, client, settings, _system_prompt(), messages)


class ParsedPhrase(NamedTuple):
    text: str
    suggested: bool
    note: str


def parsed_phrases(draft: ProposeSearch) -> List[ParsedPhrase]:
    """Splits each search_phrases line into (text, suggested, note) - see
    SUGGESTED_PREFIX. Blank lines are dropped."""
    result = []
    for line in draft.search_phrases:
        line = line.strip()
        if not line:
            continue
        if line.startswith(SUGGESTED_PREFIX):
            rest = line[len(SUGGESTED_PREFIX):]
            text, _, note = rest.partition(" - ")
            result.append(ParsedPhrase(text=text.strip(), suggested=True, note=note.strip()))
        else:
            result.append(ParsedPhrase(text=line, suggested=False, note=""))
    return result


def all_draft_phrases(draft: ProposeSearch) -> List[str]:
    """The plain text of every parsed phrase, in order - what the admin UI's
    phrase checkboxes (and draft_to_search's included_phrases) index into."""
    return [p.text for p in parsed_phrases(draft)]


def _parse_watched_model_line(line: str) -> Optional[WatchedModel]:
    parts = [p.strip() for p in line.split("|")]
    pattern = parts[0] if parts else ""
    if not pattern:
        return None
    note = parts[1] if len(parts) > 1 else ""
    good_price = parts[2] if len(parts) > 2 else ""
    is_ideal = bool(parts[3]) if len(parts) > 3 else False
    return WatchedModel(pattern=pattern, note=note, good_price=good_price, is_ideal=is_ideal)


def draft_to_search(draft: ProposeSearch, *, included_phrases: Optional[List[str]] = None, base: Optional[Search] = None) -> Search:
    """Turns a ProposeSearch draft into a real Search. `included_phrases`,
    if given, is the exact phrase text list to keep (the admin UI lets the
    user uncheck suggested ones before creating); omitted, every phrase is
    kept.

    `base`, when editing an existing search via prompt, is that search as it
    currently stands - its fields the draft doesn't cover (scope, location,
    price limits, marketplaces, etc. - see module docstring) are kept as-is
    rather than reset to blank defaults, since the draft was never asked to
    produce them and has no opinion on them either way. Omitted (a brand new
    search), those fields get the same default a fresh manual-form search
    would, with marketplaces defaulting to every marketplace enabled by
    default (see marketplaces.default_marketplace_keys)."""
    phrases = included_phrases if included_phrases is not None else all_draft_phrases(draft)
    watched_models = [m for m in (_parse_watched_model_line(line) for line in draft.watched_models) if m is not None]
    search = base.model_copy(deep=True) if base is not None else Search(name=draft.name, marketplaces=_default_marketplace_keys())
    search.name = draft.name
    search.scoring_mode = draft.scoring_mode
    search.watched_models = watched_models
    search.hard_criteria = draft.hard_criteria
    search.soft_criteria = draft.soft_criteria
    search.search_phrases = phrases
    return search


def _default_marketplace_keys() -> List[str]:
    from watcher.marketplaces import default_marketplace_keys

    return default_marketplace_keys()


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
