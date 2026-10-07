import pytest
from fastapi.testclient import TestClient

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
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client


def test_index_redirects_to_searches(client):
    response = client.get("/", follow_redirects=False)
    assert response.status_code in (301, 302, 303, 307, 308)
    assert response.headers["location"] == "/searches"


def test_searches_list_shows_seeded_defaults(client):
    response = client.get("/searches")
    assert response.status_code == 200
    assert "Stugan hifi" in response.text


def test_searches_list_shows_bucket_counts(client):
    from watcher import searches, storage
    from watcher.models import Listing

    conn = client.app.state.conn
    search = searches.get_search(conn, searches.list_searches(conn)[0].id)
    listing_id = storage.upsert_listing(
        conn, search.id,
        Listing(
            source="blocket", external_id="x1", title="Test item", description="",
            price=100, url="https://example.com/x1", location=None, ships=True,
            published_at=None, raw={},
        ),
    )
    storage.mark_scored(conn, listing_id, 9, "great", [], "good")

    response = client.get("/searches")
    assert response.status_code == 200
    assert f'/searches/{search.id}/listings?bucket=threshold' in response.text


def test_search_listings_shows_matching_bucket(client):
    from watcher import searches, storage
    from watcher.models import Listing

    conn = client.app.state.conn
    search = searches.get_search(conn, searches.list_searches(conn)[0].id)
    listing_id = storage.upsert_listing(
        conn, search.id,
        Listing(
            source="blocket", external_id="x2", title="High score item", description="",
            price=200, url="https://example.com/x2", location=None, ships=True,
            published_at=None, raw={},
        ),
    )
    storage.mark_scored(conn, listing_id, 9, "great", [], "good")

    response = client.get(f"/searches/{search.id}/listings?bucket=threshold")
    assert response.status_code == 200
    assert "High score item" in response.text

    response_wrong_bucket = client.get(f"/searches/{search.id}/listings?bucket=summary")
    assert "High score item" not in response_wrong_bucket.text


def test_search_listings_invalid_bucket_redirects(client):
    from watcher import searches

    conn = client.app.state.conn
    search_id = searches.list_searches(conn)[0].id
    response = client.get(f"/searches/{search_id}/listings?bucket=nonsense", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/searches"


def test_search_listings_unknown_search_redirects(client):
    response = client.get("/searches/999999/listings", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/searches"


def test_create_search_via_form(client):
    response = client.post(
        "/searches/new",
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
            "search_phrases": "pickup",
            "marketplaces": "blocket",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    list_response = client.get("/searches")
    assert "Pickup truck" in list_response.text


def test_watched_model_ideal_flag_round_trips_through_form(client):
    response = client.post(
        "/searches/new",
        data={
            "name": "GPU hunt",
            "enabled": "on",
            "scope": "national",
            "location": "",
            "max_price": "",
            "watched_models": "RTX 4080 | the one I want | 7000-8000 SEK | ideal\nRTX 4070 | fallback | 5000-6000 SEK |",
            "search_phrases": "rtx",
            "marketplaces": "blocket",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    import re

    list_html = client.get("/searches").text
    search_id = re.search(r"/searches/(\d+)/edit", list_html).group(1)

    from watcher import searches as searches_repo

    search = searches_repo.get_search(client.app.state.conn, int(search_id))
    ideal = [wm for wm in search.watched_models if wm.is_ideal]
    other = [wm for wm in search.watched_models if not wm.is_ideal]
    assert [wm.pattern for wm in ideal] == ["RTX 4080"]
    assert [wm.pattern for wm in other] == ["RTX 4070"]

    edit_html = client.get(f"/searches/{search_id}/edit").text
    assert "RTX 4080 | the one I want | 7000-8000 SEK | ideal" in edit_html


def test_toggle_and_delete_search(client):
    client.post(
        "/searches/new",
        data={"name": "Bokhyllor", "enabled": "on", "scope": "local", "location": "", "max_price": ""},
    )
    list_html = client.get("/searches").text
    assert "enabled" in list_html

    import re

    match = re.search(r"/searches/(\d+)/edit", list_html)
    search_id = match.group(1)

    client.post(f"/searches/{search_id}/toggle")
    toggled_html = client.get("/searches").text
    assert "disabled" in toggled_html

    client.post(f"/searches/{search_id}/delete")
    final_html = client.get("/searches").text
    assert "Bokhyllor" not in final_html


def test_marketplaces_list_shows_blocket(client):
    response = client.get("/marketplaces")
    assert response.status_code == 200
    assert "Blocket" in response.text
    assert "240 min" in response.text  # default poll interval


def test_marketplace_edit_updates_poll_interval(client):
    response = client.post(
        "/marketplaces/blocket/edit",
        data={"poll_interval_minutes": "60", "request_delay_seconds": "1.5"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    list_html = client.get("/marketplaces").text
    assert "60 min" in list_html

    from watcher import marketplace_configs

    config = marketplace_configs.get_config(client.app.state.conn, "blocket")
    assert config.poll_interval_minutes == 60
    assert config.request_delay_seconds == 1.5


def test_marketplace_edit_unknown_key_redirects(client):
    response = client.get("/marketplaces/nonexistent/edit", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/marketplaces"


def test_run_marketplace_now_redirects_with_ran_param(client):
    response = client.post("/marketplaces/blocket/run", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/marketplaces?ran=blocket"

    list_html = client.get("/marketplaces?ran=blocket").text
    assert "Triggered a manual run for blocket" in list_html


def test_run_marketplace_now_unknown_key_redirects(client):
    response = client.post("/marketplaces/nonexistent/run", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/marketplaces"


def test_clear_data_removes_listings_keeps_searches(client):
    from watcher import searches, storage
    from watcher.models import Listing

    conn = client.app.state.conn
    search = searches.get_search(conn, searches.list_searches(conn)[0].id)
    storage.upsert_listing(
        conn, search.id,
        Listing(
            source="blocket", external_id="clear-me", title="Temp", description="",
            price=100, url="https://example.com/clear-me", location=None, ships=True,
            published_at=None, raw={},
        ),
    )

    response = client.post("/health/clear", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/health"

    assert conn.execute("SELECT COUNT(*) AS n FROM listings").fetchone()["n"] == 0
    assert len(searches.list_searches(conn)) > 0


def test_healthz_starting_when_no_runs(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "starting"


def test_healthz_ok_while_a_newer_run_is_still_in_progress(client):
    from watcher import storage

    conn = client.app.state.conn
    ok_run_id = storage.start_run(conn)
    storage.finish_run(conn, ok_run_id, status="ok")
    storage.start_run(conn)  # new run kicks off, left "running" (in progress)

    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_healthz_unhealthy_when_last_completed_run_failed(client):
    from watcher import storage

    conn = client.app.state.conn
    failed_run_id = storage.start_run(conn)
    storage.finish_run(conn, failed_run_id, status="error", error_message="boom")

    response = client.get("/healthz")
    assert response.status_code == 503
    assert response.json()["status"] == "unhealthy"
