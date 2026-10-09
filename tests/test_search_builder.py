import json
from types import SimpleNamespace

from watcher import db
from watcher.scoring import search_builder
from watcher.scoring.search_builder import (
    ProposeSearch,
    SearchBuilderError,
    all_draft_phrases,
    collapse_transcript_to_prompt,
    draft_to_search,
    run_turn,
)
from watcher.settings import Settings


def make_conn():
    return db.connect(":memory:")


def make_settings(**overrides):
    base = dict(
        db_path=":memory:",
        digest_time="08:00",
        score_instant_threshold=8,
        score_digest_min=5,
        health_alert_after_n_failures=3,
        claude_model="claude-haiku-4-5",
        scoring_batch_size=10,
        max_pages_per_query=1,
        anthropic_api_key="x",
        slack_webhook_url="",
        dry_run=False,
        admin_port=8000,
    )
    base.update(overrides)
    return Settings(**base)


def make_client(responses_text):
    """responses_text: a list of JSON strings (or non-JSON, to test the
    retry path) returned one per call to client.messages.create, in order -
    run_turn makes up to two calls per external turn (a "decide" call and,
    once ready, a separate "finalize" call - see search_builder's module
    docstring for why this is two small schemas rather than one combined
    one)."""
    queue = list(responses_text)
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        text = queue.pop(0)
        content = [SimpleNamespace(type="text", text=text)]
        return SimpleNamespace(content=content, usage=SimpleNamespace(input_tokens=200, output_tokens=100))

    client = SimpleNamespace(messages=SimpleNamespace(create=create))
    client._calls = calls
    return client


# What the "decide" call returns - see search_builder._Decision.
DECISION_ASK_JSON = json.dumps({"ready_to_finalize": False, "question": "Vilken prisgräns vill du sätta?"})
DECISION_READY_JSON = json.dumps({"ready_to_finalize": True, "question": None})

# What the "finalize" call returns - see search_builder.ProposeSearch. Note
# this is the bare ProposeSearch shape, not wrapped in a BuilderTurn envelope -
# run_turn constructs the BuilderTurn itself in Python, it's never what
# Claude actually returns (see module docstring: that's exactly what used to
# make the combined schema "too complex", confirmed live, 2026-10).
PROPOSE_SEARCH_JSON = json.dumps({
    "summary": "En AI-rankad sökning efter Marantz PM6007 och liknande förstärkare, max 3000 kr.",
    "name": "Marantz PM6007",
    "scope": "national",
    "location": "",
    "require_shipping": False,
    "max_price": 3000,
    "min_price": None,
    "scoring_mode": "rated",
    "instant_alert_price": None,
    "digest_style": "summary_link",
    "excluded_models": [],
    "excluded_words": [],
    "required_keywords": [],
    "watched_models": ["Marantz PM6007 | grail target | 2000-3000 SEK | ideal"],
    "hard_criteria": [],
    "soft_criteria": [],
    "search_phrases": ["Marantz PM6007"],
    "suggested_phrases": ["Yamaha A-S301"],
    "suggested_phrases_note": "similar integrated amp, same price bracket",
    "marketplaces": ["blocket", "vinted"],
})

# This is still a full BuilderTurn envelope, not a raw "decide" response -
# it's what gets stored as an assistant transcript entry (pending_question_json
# in routes.py), a format collapse_transcript_to_prompt parses directly and
# that is unaffected by the internal one-call/two-call split above.
STORED_ASK_USER_TURN_JSON = json.dumps({
    "action": "ask_user",
    "ask_user": {"question": "Vilken prisgräns vill du sätta?"},
    "propose_search": None,
})


def test_run_turn_returns_ask_user_from_a_single_decide_call():
    conn = make_conn()
    client = make_client([DECISION_ASK_JSON])

    turn = run_turn(conn, client, make_settings(), [{"role": "user", "content": "En bra förstärkare"}])

    assert turn.action == "ask_user"
    assert turn.ask_user.question == "Vilken prisgräns vill du sätta?"
    assert len(client._calls) == 1  # no finalize call needed


def test_run_turn_returns_propose_search_after_decide_and_finalize():
    conn = make_conn()
    client = make_client([DECISION_READY_JSON, PROPOSE_SEARCH_JSON])

    turn = run_turn(conn, client, make_settings(), [{"role": "user", "content": "Marantz PM6007, max 3000kr"}])

    assert turn.action == "propose_search"
    assert turn.propose_search.name == "Marantz PM6007"
    assert turn.propose_search.search_phrases == ["Marantz PM6007"]
    assert turn.propose_search.suggested_phrases == ["Yamaha A-S301"]
    assert len(client._calls) == 2


def test_run_turn_logs_token_usage_for_every_call_made():
    conn = make_conn()
    client = make_client([DECISION_READY_JSON, PROPOSE_SEARCH_JSON])

    run_turn(conn, client, make_settings(), [{"role": "user", "content": "x"}])

    rows = conn.execute("SELECT * FROM token_usage").fetchall()
    assert len(rows) == 2
    assert all(row["input_tokens"] == 200 for row in rows)


def test_run_turn_retries_the_decide_call_once_on_invalid_json_then_succeeds():
    conn = make_conn()
    client = make_client(["not valid json", DECISION_ASK_JSON])

    turn = run_turn(conn, client, make_settings(), [{"role": "user", "content": "x"}])

    assert turn.action == "ask_user"
    assert len(client._calls) == 2


def test_run_turn_raises_after_two_failed_decide_attempts():
    conn = make_conn()
    client = make_client(["not valid json", "still not valid"])

    try:
        run_turn(conn, client, make_settings(), [{"role": "user", "content": "x"}])
        assert False, "expected SearchBuilderError"
    except SearchBuilderError:
        pass


def test_run_turn_raises_after_two_failed_finalize_attempts():
    conn = make_conn()
    client = make_client([DECISION_READY_JSON, "not valid json", "still not valid"])

    try:
        run_turn(conn, client, make_settings(), [{"role": "user", "content": "x"}])
        assert False, "expected SearchBuilderError"
    except SearchBuilderError:
        pass


def test_run_turn_force_propose_skips_the_decide_call_entirely():
    conn = make_conn()
    client = make_client([PROPOSE_SEARCH_JSON])

    turn = run_turn(conn, client, make_settings(), [{"role": "user", "content": "x"}], force_propose=True)

    assert turn.action == "propose_search"
    assert turn.propose_search.name == "Marantz PM6007"
    assert len(client._calls) == 1
    sent_messages = client._calls[0]["messages"]
    assert "best judgement" in sent_messages[-1]["content"]


def test_all_draft_phrases_combines_explicit_and_suggested_in_order():
    draft = ProposeSearch.model_validate_json(PROPOSE_SEARCH_JSON)
    assert all_draft_phrases(draft) == ["Marantz PM6007", "Yamaha A-S301"]


def test_draft_to_search_flattens_all_phrases_by_default():
    draft = ProposeSearch.model_validate_json(PROPOSE_SEARCH_JSON)
    search = draft_to_search(draft)

    assert search.search_phrases == ["Marantz PM6007", "Yamaha A-S301"]
    assert search.marketplaces == ["blocket", "vinted"]
    assert search.max_price == 3000
    assert search.scoring_mode == "rated"


def test_draft_to_search_keeps_only_the_included_phrases():
    draft = ProposeSearch.model_validate_json(PROPOSE_SEARCH_JSON)
    search = draft_to_search(draft, included_phrases=["Marantz PM6007"])

    assert search.search_phrases == ["Marantz PM6007"]


def test_draft_to_search_parses_pipe_delimited_watched_model_lines():
    draft = ProposeSearch.model_validate_json(PROPOSE_SEARCH_JSON)
    search = draft_to_search(draft)

    assert len(search.watched_models) == 1
    wm = search.watched_models[0]
    assert wm.pattern == "Marantz PM6007"
    assert wm.note == "grail target"
    assert wm.good_price == "2000-3000 SEK"
    assert wm.is_ideal is True


def test_draft_to_search_skips_a_blank_watched_model_line():
    draft = ProposeSearch.model_validate_json(PROPOSE_SEARCH_JSON)
    draft.watched_models = ["", "   "]
    search = draft_to_search(draft)

    assert search.watched_models == []


def test_collapse_transcript_to_prompt_with_no_questions():
    transcript = [{"role": "user", "content": "Marantz PM6007, max 3000kr"}]
    assert collapse_transcript_to_prompt(transcript) == "Marantz PM6007, max 3000kr"


def test_collapse_transcript_to_prompt_folds_in_qa():
    transcript = [
        {"role": "user", "content": "En bra förstärkare"},
        {"role": "assistant", "content": STORED_ASK_USER_TURN_JSON},
        {"role": "user", "content": "Max 3000kr"},
    ]
    collapsed = collapse_transcript_to_prompt(transcript)
    assert collapsed.startswith("En bra förstärkare")
    assert "Vilken prisgräns vill du sätta?" in collapsed
    assert "Max 3000kr" in collapsed
