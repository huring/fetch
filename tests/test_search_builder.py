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
    run_turn makes exactly one call per external turn (see search_builder's
    module docstring for why this isn't Anthropic's structured-output
    mode), plus one more for each retry on an invalid response."""
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


# What a single run_turn call returns - a bare BuilderTurn, parsed from
# Claude's plain-text JSON response (no API-enforced schema - see module
# docstring). A real response may also arrive wrapped in a markdown fence;
# that's covered separately below.
ASK_USER_TURN_JSON = json.dumps({
    "action": "ask_user",
    "ask_user": {"question": "Vilken prisgräns vill du sätta?"},
    "propose_search": None,
})

PROPOSE_SEARCH_TURN_JSON = json.dumps({
    "action": "propose_search",
    "ask_user": None,
    "propose_search": {
        "summary": "En AI-rankad sökning efter Marantz PM6007 och liknande förstärkare.",
        "name": "Marantz PM6007",
        "scoring_mode": "rated",
        "watched_models": ["Marantz PM6007 | grail target | 2000-3000 SEK | ideal"],
        "hard_criteria": [],
        "soft_criteria": [],
        "search_phrases": [
            "Marantz PM6007",
            "[suggested] Yamaha A-S301 - similar integrated amp, same price bracket",
        ],
    },
})

# The bare ProposeSearch shape alone, used by tests that build a draft
# directly rather than through a parsed BuilderTurn.
PROPOSE_SEARCH_JSON = json.dumps(json.loads(PROPOSE_SEARCH_TURN_JSON)["propose_search"])


def test_run_turn_returns_ask_user_from_a_single_call():
    conn = make_conn()
    client = make_client([ASK_USER_TURN_JSON])

    turn = run_turn(conn, client, make_settings(), [{"role": "user", "content": "En bra förstärkare"}])

    assert turn.action == "ask_user"
    assert turn.ask_user.question == "Vilken prisgräns vill du sätta?"
    assert len(client._calls) == 1


def test_run_turn_returns_propose_search_from_a_single_call():
    conn = make_conn()
    client = make_client([PROPOSE_SEARCH_TURN_JSON])

    turn = run_turn(conn, client, make_settings(), [{"role": "user", "content": "Marantz PM6007, max 3000kr"}])

    assert turn.action == "propose_search"
    assert turn.propose_search.name == "Marantz PM6007"
    assert turn.propose_search.search_phrases[0] == "Marantz PM6007"
    assert turn.propose_search.search_phrases[1].startswith("[suggested] Yamaha A-S301")
    assert len(client._calls) == 1


def test_run_turn_handles_a_response_wrapped_in_a_markdown_fence():
    conn = make_conn()
    fenced = f"```json\n{PROPOSE_SEARCH_TURN_JSON}\n```"
    client = make_client([fenced])

    turn = run_turn(conn, client, make_settings(), [{"role": "user", "content": "x"}])

    assert turn.action == "propose_search"
    assert turn.propose_search.name == "Marantz PM6007"


def test_run_turn_logs_token_usage_for_the_call_made():
    conn = make_conn()
    client = make_client([PROPOSE_SEARCH_TURN_JSON])

    run_turn(conn, client, make_settings(), [{"role": "user", "content": "x"}])

    rows = conn.execute("SELECT * FROM token_usage").fetchall()
    assert len(rows) == 1
    assert rows[0]["input_tokens"] == 200


def test_run_turn_retries_once_on_invalid_json_then_succeeds():
    conn = make_conn()
    client = make_client(["not valid json", ASK_USER_TURN_JSON])

    turn = run_turn(conn, client, make_settings(), [{"role": "user", "content": "x"}])

    assert turn.action == "ask_user"
    assert len(client._calls) == 2


def test_run_turn_raises_after_two_failed_attempts():
    conn = make_conn()
    client = make_client(["not valid json", "still not valid"])

    try:
        run_turn(conn, client, make_settings(), [{"role": "user", "content": "x"}])
        assert False, "expected SearchBuilderError"
    except SearchBuilderError:
        pass


def test_run_turn_force_propose_appends_an_instruction_to_finalize():
    conn = make_conn()
    client = make_client([PROPOSE_SEARCH_TURN_JSON])

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
    from watcher.marketplaces import MARKETPLACES

    draft = ProposeSearch.model_validate_json(PROPOSE_SEARCH_JSON)
    search = draft_to_search(draft)

    assert search.search_phrases == ["Marantz PM6007", "Yamaha A-S301"]
    assert search.scoring_mode == "rated"
    # Not a Claude-generated field (see module docstring) - defaults to
    # every registered marketplace, adjustable afterward via Advanced edit.
    assert set(search.marketplaces) == set(MARKETPLACES.keys())


def test_draft_to_search_with_a_base_preserves_its_non_generated_fields():
    """Editing an existing search via prompt must not silently blank out
    scope/location/price/marketplaces/etc. just because the draft schema
    doesn't cover them (see module docstring) - it should only replace the
    fields Claude actually generates."""
    from watcher.models import Search

    existing = Search(
        id=7, name="Old name", scope="local", location="Norrbotten", max_price=1000,
        marketplaces=["blocket"], scoring_mode="plain", search_phrases=["old phrase"],
    )
    draft = ProposeSearch.model_validate_json(PROPOSE_SEARCH_JSON)

    search = draft_to_search(draft, base=existing)

    assert search.scope == "local"
    assert search.location == "Norrbotten"
    assert search.max_price == 1000
    assert search.marketplaces == ["blocket"]
    assert search.name == "Marantz PM6007"  # overwritten - Claude-generated
    assert search.scoring_mode == "rated"  # overwritten - Claude-generated
    assert search.search_phrases == ["Marantz PM6007", "Yamaha A-S301"]  # overwritten


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
        {"role": "assistant", "content": ASK_USER_TURN_JSON},
        {"role": "user", "content": "Max 3000kr"},
    ]
    collapsed = collapse_transcript_to_prompt(transcript)
    assert collapsed.startswith("En bra förstärkare")
    assert "Vilken prisgräns vill du sätta?" in collapsed
    assert "Max 3000kr" in collapsed
