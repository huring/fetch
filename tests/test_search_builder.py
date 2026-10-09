import json
from types import SimpleNamespace

from watcher import db
from watcher.scoring import search_builder
from watcher.scoring.search_builder import (
    BuilderTurn,
    SearchBuilderError,
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
    retry path) returned one per call to client.messages.create, in order."""
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


ASK_USER_JSON = json.dumps({
    "action": "ask_user",
    "ask_user": {"question": "Vilken prisgräns vill du sätta?"},
    "propose_search": None,
})

PROPOSE_SEARCH_JSON = json.dumps({
    "action": "propose_search",
    "ask_user": None,
    "propose_search": {
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
        "watched_models": [],
        "hard_criteria": [],
        "soft_criteria": [],
        "search_phrases": [
            {"text": "Marantz PM6007", "source": "explicit", "note": None},
            {"text": "Yamaha A-S301", "source": "suggested", "note": "similar integrated amp, same price bracket"},
        ],
        "marketplaces": ["blocket", "vinted"],
    },
})


def test_run_turn_returns_ask_user():
    conn = make_conn()
    client = make_client([ASK_USER_JSON])

    turn = run_turn(conn, client, make_settings(), [{"role": "user", "content": "En bra förstärkare"}])

    assert turn.action == "ask_user"
    assert turn.ask_user.question == "Vilken prisgräns vill du sätta?"


def test_run_turn_returns_propose_search():
    conn = make_conn()
    client = make_client([PROPOSE_SEARCH_JSON])

    turn = run_turn(conn, client, make_settings(), [{"role": "user", "content": "Marantz PM6007, max 3000kr"}])

    assert turn.action == "propose_search"
    assert turn.propose_search.name == "Marantz PM6007"
    assert len(turn.propose_search.search_phrases) == 2
    assert turn.propose_search.search_phrases[1].source == "suggested"


def test_run_turn_logs_token_usage():
    conn = make_conn()
    client = make_client([PROPOSE_SEARCH_JSON])

    run_turn(conn, client, make_settings(), [{"role": "user", "content": "x"}])

    row = conn.execute("SELECT * FROM token_usage").fetchone()
    assert row["input_tokens"] == 200
    assert row["output_tokens"] == 100


def test_run_turn_retries_once_on_invalid_json_then_succeeds():
    conn = make_conn()
    client = make_client(["not valid json", PROPOSE_SEARCH_JSON])

    turn = run_turn(conn, client, make_settings(), [{"role": "user", "content": "x"}])

    assert turn.action == "propose_search"
    assert len(client._calls) == 2


def test_run_turn_raises_after_two_failed_attempts():
    conn = make_conn()
    client = make_client(["not valid json", "still not valid"])

    try:
        run_turn(conn, client, make_settings(), [{"role": "user", "content": "x"}])
        assert False, "expected SearchBuilderError"
    except SearchBuilderError:
        pass


def test_run_turn_force_propose_uses_the_propose_only_schema():
    conn = make_conn()
    propose_only_json = json.loads(PROPOSE_SEARCH_JSON)["propose_search"]
    client = make_client([json.dumps(propose_only_json)])

    turn = run_turn(conn, client, make_settings(), [{"role": "user", "content": "x"}], force_propose=True)

    assert turn.action == "propose_search"
    assert turn.propose_search.name == "Marantz PM6007"
    sent_messages = client._calls[0]["messages"]
    assert "best judgement" in sent_messages[-1]["content"]


def test_draft_to_search_flattens_all_phrases_by_default():
    turn = BuilderTurn.model_validate_json(PROPOSE_SEARCH_JSON)
    search = draft_to_search(turn.propose_search)

    assert search.search_phrases == ["Marantz PM6007", "Yamaha A-S301"]
    assert search.marketplaces == ["blocket", "vinted"]
    assert search.max_price == 3000
    assert search.scoring_mode == "rated"


def test_draft_to_search_keeps_only_the_included_phrases():
    turn = BuilderTurn.model_validate_json(PROPOSE_SEARCH_JSON)
    search = draft_to_search(turn.propose_search, included_phrases=["Marantz PM6007"])

    assert search.search_phrases == ["Marantz PM6007"]


def test_collapse_transcript_to_prompt_with_no_questions():
    transcript = [{"role": "user", "content": "Marantz PM6007, max 3000kr"}]
    assert collapse_transcript_to_prompt(transcript) == "Marantz PM6007, max 3000kr"


def test_collapse_transcript_to_prompt_folds_in_qa():
    transcript = [
        {"role": "user", "content": "En bra förstärkare"},
        {"role": "assistant", "content": ASK_USER_JSON},
        {"role": "user", "content": "Max 3000kr"},
    ]
    collapsed = collapse_transcript_to_prompt(transcript)
    assert collapsed.startswith("En bra förstärkare")
    assert "Vilken prisgräns vill du sätta?" in collapsed
    assert "Max 3000kr" in collapsed
