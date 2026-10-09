import json
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from watcher import searches as searches_repo
from watcher.admin.app import create_app
from watcher.settings import Settings


def make_settings(db_path):
    return Settings(
        db_path=db_path,
        digest_time="08:00",
        score_instant_threshold=8,
        score_digest_min=5,
        health_alert_after_n_failures=3,
        claude_model="claude-haiku-4-5",
        scoring_batch_size=10,
        max_pages_per_query=1,
        anthropic_api_key="",
        slack_webhook_url="",
        dry_run=True,
        admin_port=8000,
    )


@pytest.fixture
def client(tmp_path):
    settings = make_settings(str(tmp_path / "test.db"))
    app = create_app(settings, start_background_jobs=False)
    with TestClient(app) as test_client:
        yield test_client
        test_client.app.state.scheduler.remove_all_jobs()


def make_fake_claude_client(responses_text):
    queue = list(responses_text)

    def create(**kwargs):
        text = queue.pop(0)
        content = [SimpleNamespace(type="text", text=text)]
        return SimpleNamespace(content=content, usage=SimpleNamespace(input_tokens=200, output_tokens=100))

    return SimpleNamespace(messages=SimpleNamespace(create=create))


def submit_wizard_turn(client, url, data, timeout=5):
    """Every wizard-turn POST now redirects to a status page and runs the
    actual Claude call as a background scheduler job (fixes a live 504 -
    see CHANGELOG), so a test has to follow that redirect and poll, same as
    a real browser's meta-refresh would, rather than reading content
    straight off the POST response."""
    response = client.post(url, data=data, follow_redirects=False)
    assert response.status_code == 303, response.text
    status_url = response.headers["location"]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status_response = client.get(status_url)
        if "Claude is working on this" not in status_response.text:
            return status_response
        time.sleep(0.02)
    raise AssertionError(f"Wizard job at {status_url} did not finish within {timeout}s")


# run_turn makes exactly one Claude call per external turn - a bare JSON
# object parsed from the response text, not Anthropic's structured-output
# mode (see search_builder's module docstring for why: three rounds of
# live "Schema is too complex" 400s chasing an undocumented complexity
# limit, 2026-10, settled by dropping the API-enforced schema entirely
# rather than continuing to guess at what it was counting).
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
        "watched_models": [],
        "hard_criteria": [],
        "soft_criteria": [],
        "search_phrases": [
            "Marantz PM6007",
            "[suggested] Yamaha A-S301 - similar integrated amp, same price bracket",
        ],
    },
})


def test_new_search_from_prompt_form_renders(client):
    response = client.get("/searches/new-from-prompt")
    assert response.status_code == 200
    assert "What are you looking for?" in response.text


def test_turn_without_configured_api_key_shows_error(client):
    response = client.post("/searches/new-from-prompt", data={"prompt": "En bra förstärkare"})
    assert response.status_code == 200
    assert "ANTHROPIC_API_KEY" in response.text


def test_turn_redirects_to_a_status_page_instead_of_blocking_on_claude(client):
    """The actual fix for the live 504 Gateway Timeout report (2026-10):
    the triggering request must get an immediate redirect, not wait on
    Claude itself - a slow generation blocking the HTTP response is exactly
    what let a reverse proxy's own timeout cut it off."""
    client.app.state.client = make_fake_claude_client([PROPOSE_SEARCH_TURN_JSON])

    response = client.post("/searches/new-from-prompt", data={"prompt": "x"}, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith("/searches/wizard/")


def test_status_page_shows_generating_while_the_job_is_still_pending(client):
    def slow_create(**kwargs):
        time.sleep(0.3)
        content = [SimpleNamespace(type="text", text=PROPOSE_SEARCH_TURN_JSON)]
        return SimpleNamespace(content=content, usage=SimpleNamespace(input_tokens=1, output_tokens=1))

    client.app.state.client = SimpleNamespace(messages=SimpleNamespace(create=slow_create))

    redirect = client.post("/searches/new-from-prompt", data={"prompt": "x"}, follow_redirects=False)
    status_response = client.get(redirect.headers["location"])

    assert status_response.status_code == 200
    assert "Claude is working on this" in status_response.text
    assert '<meta http-equiv="refresh" content="2">' in status_response.text


def test_status_page_redirects_to_searches_for_an_unknown_job_id(client):
    response = client.get("/searches/wizard/not-a-real-job-id", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/searches"


def test_status_job_is_consumed_after_being_read_once(client):
    client.app.state.client = make_fake_claude_client([PROPOSE_SEARCH_TURN_JSON])
    redirect = client.post("/searches/new-from-prompt", data={"prompt": "x"}, follow_redirects=False)
    status_url = redirect.headers["location"]
    first = None
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        first = client.get(status_url)
        if "Claude is working on this" not in first.text:
            break
        time.sleep(0.02)

    second = client.get(status_url, follow_redirects=False)

    assert "Marantz PM6007" in first.text
    assert second.status_code == 303
    assert second.headers["location"] == "/searches"


def test_first_turn_can_ask_a_question(client):
    client.app.state.client = make_fake_claude_client([ASK_USER_TURN_JSON])

    response = submit_wizard_turn(client, "/searches/new-from-prompt", {"prompt": "En bra förstärkare"})

    assert response.status_code == 200
    assert "Vilken prisgräns vill du sätta?" in response.text
    assert 'name="transcript"' in response.text
    assert 'name="pending_question_json"' in response.text


def test_answering_a_question_leads_to_a_draft(client):
    client.app.state.client = make_fake_claude_client([ASK_USER_TURN_JSON, PROPOSE_SEARCH_TURN_JSON])

    ask_response = submit_wizard_turn(client, "/searches/new-from-prompt", {"prompt": "En bra förstärkare"})
    transcript = _extract_hidden_value(ask_response.text, "transcript")
    pending_question = _extract_hidden_value(ask_response.text, "pending_question_json")

    draft_response = submit_wizard_turn(
        client, "/searches/new-from-prompt",
        {
            "transcript": transcript, "pending_question_json": pending_question,
            "answer": "Max 3000 kr",
        },
    )

    assert draft_response.status_code == 200
    assert "Marantz PM6007" in draft_response.text
    assert "similar integrated amp" in draft_response.text  # the suggested phrase's note


def test_skip_button_answers_with_best_judgement(client):
    client.app.state.client = make_fake_claude_client([ASK_USER_TURN_JSON, PROPOSE_SEARCH_TURN_JSON])

    ask_response = submit_wizard_turn(client, "/searches/new-from-prompt", {"prompt": "En bra förstärkare"})
    transcript = _extract_hidden_value(ask_response.text, "transcript")
    pending_question = _extract_hidden_value(ask_response.text, "pending_question_json")

    draft_response = submit_wizard_turn(
        client, "/searches/new-from-prompt",
        {"transcript": transcript, "pending_question_json": pending_question, "skip": "1"},
    )

    assert draft_response.status_code == 200
    assert "Marantz PM6007" in draft_response.text


def test_create_from_draft_persists_the_search_with_a_collapsed_prompt(client):
    client.app.state.client = make_fake_claude_client([PROPOSE_SEARCH_TURN_JSON])
    draft_response = submit_wizard_turn(client, "/searches/new-from-prompt", {"prompt": "Marantz PM6007, max 3000kr"})
    draft_json = _extract_hidden_value(draft_response.text, "draft_json")
    transcript = _extract_hidden_value(draft_response.text, "transcript")

    create_response = client.post(
        "/searches/new-from-prompt/draft",
        data={
            "draft_json": draft_json, "transcript": transcript, "name": "Marantz PM6007",
            "phrase_included": ["0", "1"], "do": "create",
        },
        follow_redirects=False,
    )

    assert create_response.status_code == 303
    conn = client.app.state.conn
    created = next(s for s in searches_repo.list_searches(conn) if s.name == "Marantz PM6007")
    assert created.search_phrases == ["Marantz PM6007", "Yamaha A-S301"]
    assert created.creation_prompt == "Marantz PM6007, max 3000kr"


def test_create_from_draft_drops_unchecked_phrases(client):
    client.app.state.client = make_fake_claude_client([PROPOSE_SEARCH_TURN_JSON])
    draft_response = submit_wizard_turn(client, "/searches/new-from-prompt", {"prompt": "Marantz PM6007, max 3000kr"})
    draft_json = _extract_hidden_value(draft_response.text, "draft_json")
    transcript = _extract_hidden_value(draft_response.text, "transcript")

    client.post(
        "/searches/new-from-prompt/draft",
        data={
            "draft_json": draft_json, "transcript": transcript, "name": "Marantz PM6007",
            "phrase_included": ["0"], "do": "create",
        },
        follow_redirects=False,
    )

    conn = client.app.state.conn
    created = next(s for s in searches_repo.list_searches(conn) if s.name == "Marantz PM6007")
    assert created.search_phrases == ["Marantz PM6007"]


def test_create_from_draft_with_a_duplicate_name_shows_an_inline_error_not_a_crash(client):
    conn = client.app.state.conn
    from watcher.models import Search
    searches_repo.create_search(conn, Search(name="Marantz PM6007"))
    before_count = len(searches_repo.list_searches(conn))

    client.app.state.client = make_fake_claude_client([PROPOSE_SEARCH_TURN_JSON])
    draft_response = submit_wizard_turn(client, "/searches/new-from-prompt", {"prompt": "Marantz PM6007, max 3000kr"})
    draft_json = _extract_hidden_value(draft_response.text, "draft_json")
    transcript = _extract_hidden_value(draft_response.text, "transcript")

    response = client.post(
        "/searches/new-from-prompt/draft",
        data={
            "draft_json": draft_json, "transcript": transcript, "name": "Marantz PM6007",
            "phrase_included": ["0", "1"], "do": "create",
        },
    )

    assert response.status_code == 200
    assert "already exists" in response.text
    assert len(searches_repo.list_searches(conn)) == before_count


def test_edit_from_prompt_form_prefills_existing_prompt(client):
    conn = client.app.state.conn
    from watcher.models import Search
    created = searches_repo.create_search(conn, Search(name="Existing", creation_prompt="En gammal sökning"))

    response = client.get(f"/searches/{created.id}/edit-from-prompt")

    assert response.status_code == 200
    assert "En gammal sökning" in response.text


def test_edit_from_prompt_warns_when_search_was_never_built_from_a_prompt(client):
    conn = client.app.state.conn
    from watcher.models import Search
    created = searches_repo.create_search(conn, Search(name="Manual search"))

    response = client.get(f"/searches/{created.id}/edit-from-prompt")

    assert "wasn't originally built from a prompt" in response.text


def test_edit_from_prompt_updates_the_existing_search(client):
    conn = client.app.state.conn
    from watcher.models import Search
    created = searches_repo.create_search(conn, Search(name="Old name", max_price=1000))

    client.app.state.client = make_fake_claude_client([PROPOSE_SEARCH_TURN_JSON])
    draft_response = submit_wizard_turn(
        client, f"/searches/{created.id}/edit-from-prompt", {"prompt": "Marantz PM6007"}
    )
    draft_json = _extract_hidden_value(draft_response.text, "draft_json")
    transcript = _extract_hidden_value(draft_response.text, "transcript")

    response = client.post(
        f"/searches/{created.id}/edit-from-prompt/draft",
        data={
            "draft_json": draft_json, "transcript": transcript, "name": "Marantz PM6007",
            "phrase_included": ["0", "1"], "do": "create",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    updated = searches_repo.get_search(conn, created.id)
    assert updated.name == "Marantz PM6007"
    # max_price isn't a Claude-generated field (see search_builder's module
    # docstring) - editing via prompt regenerates phrases/watched_models/
    # criteria only, leaving a manually-set field like this one alone.
    assert updated.max_price == 1000


def test_searches_list_links_to_the_prompt_wizard(client):
    response = client.get("/searches")
    assert "/searches/new-from-prompt" in response.text
    assert "or build it manually" in response.text


def test_advanced_edit_form_shows_warning_banner_for_a_prompt_built_search(client):
    conn = client.app.state.conn
    from watcher.models import Search
    created = searches_repo.create_search(conn, Search(name="Prompt search", creation_prompt="x"))

    response = client.get(f"/searches/{created.id}/edit")

    assert "generated from a prompt" in response.text


def test_advanced_edit_form_has_no_warning_banner_for_a_manual_search(client):
    conn = client.app.state.conn
    from watcher.models import Search
    created = searches_repo.create_search(conn, Search(name="Manual search"))

    response = client.get(f"/searches/{created.id}/edit")

    assert "generated from a prompt" not in response.text


def _extract_hidden_value(html: str, field_name: str) -> str:
    import re

    match = re.search(rf'name="{field_name}" value=\'(.*?)\'', html, re.S)
    assert match is not None, f"hidden field {field_name!r} not found in response"
    import html as html_module
    return html_module.unescape(match.group(1))
