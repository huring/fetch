import pytest
from fastapi.testclient import TestClient

from watcher.admin.app import create_app
from watcher.settings import Settings


def make_settings(db_path):
    return Settings(
        db_path=db_path,
        poll_interval_minutes=20,
        digest_time="08:00",
        score_instant_threshold=8,
        score_digest_min=5,
        health_alert_after_n_failures=3,
        claude_model="claude-haiku-4-5",
        scoring_batch_size=10,
        blocket_request_delay_seconds=0,
        tradera_request_delay_seconds=0,
        max_pages_per_query=1,
        anthropic_api_key="",
        tradera_app_id="",
        tradera_app_key="",
        slack_webhook_url="",
        dry_run=True,
        admin_port=8000,
    )


@pytest.fixture
def client(tmp_path):
    settings = make_settings(str(tmp_path / "test.db"))
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client


def test_index_redirects_to_containers(client):
    response = client.get("/", follow_redirects=False)
    assert response.status_code in (301, 302, 303, 307, 308)
    assert response.headers["location"] == "/containers"


def test_containers_list_shows_seeded_defaults(client):
    response = client.get("/containers")
    assert response.status_code == 200
    assert "Stugan hifi" in response.text


def test_create_container_via_form(client):
    response = client.post(
        "/containers/new",
        data={
            "name": "Pickup truck",
            "enabled": "on",
            "scope": "national",
            "location": "",
            "max_price": "150000",
            "excluded_models": "",
            "excluded_words": "rostskadad",
            "required_keywords": "",
            "hard_criteria": "4x4\ndiesel",
            "soft_criteria": "",
            "watched_models": "Toyota Hilux* | reliable | 80000-120000 SEK",
            "blocket_queries": "pickup | bilar | ",
            "tradera_queries": "",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    list_response = client.get("/containers")
    assert "Pickup truck" in list_response.text


def test_toggle_and_delete_container(client):
    client.post(
        "/containers/new",
        data={"name": "Bokhyllor", "enabled": "on", "scope": "local", "location": "", "max_price": ""},
    )
    list_html = client.get("/containers").text
    assert "enabled" in list_html

    import re

    match = re.search(r"/containers/(\d+)/edit", list_html)
    container_id = match.group(1)

    client.post(f"/containers/{container_id}/toggle")
    toggled_html = client.get("/containers").text
    assert "disabled" in toggled_html

    client.post(f"/containers/{container_id}/delete")
    final_html = client.get("/containers").text
    assert "Bokhyllor" not in final_html


def test_healthz_starting_when_no_runs(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "starting"
