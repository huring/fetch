from unittest.mock import patch

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


@pytest.fixture(scope="module", autouse=True)
def no_real_marketplace_fetches():
    """The admin app's scheduler fires an immediate tick on startup
    (next_run_time=now) regardless of whether an API key is configured -
    fetching doesn't need one, only scoring does. Route tests care about the
    HTTP layer, not live marketplace data, so every source's fetch is
    stubbed out here rather than relying on a missing API key to prevent
    real network calls (it no longer does, now that fetch and scoring run
    independently).

    module-scoped (not the usual function scope) deliberately: the admin
    app's scheduler keeps running on its own background thread after a test
    function returns (BackgroundScheduler.shutdown(wait=False) in the
    lifespan doesn't block for an in-progress job, by design - a slow job
    shouldn't hold up a production container's shutdown). A function-scoped
    patch can close before that straggler thread gets to the fetch call it's
    mid-way through, at which point it hits the real network and - since
    some other test's @responses.activate is active globally for the whole
    process by then - gets intercepted by a mock meant for an unrelated
    test, breaking it in a confusing, hard-to-reproduce way. Keeping the
    patch open for this whole module closes that race for every test in it;
    only the module boundary remains a (much narrower) residual risk."""
    with patch("watcher.sources.blocket.fetch", return_value=[]), \
         patch("watcher.sources.vinted.fetch", return_value=[]), \
         patch("watcher.sources.rehifi.fetch", return_value=[]), \
         patch("watcher.sources.auctionet.fetch", return_value=[]):
        yield


@pytest.fixture
def client(tmp_path):
    settings = make_settings(str(tmp_path / "test.db"))
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client
        # Cancels future recurring firings (the 5-min tick, daily jobs)
        # immediately rather than waiting for the lifespan's own shutdown.
        # Deliberately non-blocking - a wait=True here was tried and
        # reverted, since it could hang indefinitely (APScheduler's shutdown
        # join appears to deadlock against its own worker threads in this
        # setup, not just wait a bounded, predictable amount of time). This
        # narrows the remaining race (see no_real_marketplace_fetches above)
        # without that risk, but doesn't close it completely - a residual,
        # low-frequency flake is a known, accepted characteristic of testing
        # a real background scheduler this way (see CLAUDE.md/this comment
        # if it resurfaces).
        test_client.app.state.scheduler.remove_all_jobs()


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
    assert f'/searches/{search.id}/listings?bucket=instant_alert' in response.text


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

    response = client.get(f"/searches/{search.id}/listings?bucket=instant_alert")
    assert response.status_code == 200
    assert "High score item" in response.text

    response_wrong_bucket = client.get(f"/searches/{search.id}/listings?bucket=daily_roundup")
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


def test_searches_list_shows_overview_panel(client):
    from watcher import searches, storage
    from watcher.models import Listing

    conn = client.app.state.conn
    search = searches.get_search(conn, searches.list_searches(conn)[0].id)
    listing_id = storage.upsert_listing(
        conn, search.id,
        Listing(
            source="blocket", external_id="ov1", title="Overview test item", description="",
            price=100, url="https://example.com/ov1", location=None, ships=True,
            published_at=None, raw={},
        ),
    )
    storage.mark_scored(conn, listing_id, 9, "great", [], "good")

    response = client.get("/searches")

    assert response.status_code == 200
    assert "This month's Claude cost" in response.text
    assert "Total scanned" in response.text
    assert "Overview test item" in response.text  # shows up in "Top ads"


def test_feed_shows_daily_roundup_by_default(client):
    from watcher import searches, storage
    from watcher.models import Listing

    conn = client.app.state.conn
    search = searches.get_search(conn, searches.list_searches(conn)[0].id)
    roundup_id = storage.upsert_listing(
        conn, search.id,
        Listing(
            source="blocket", external_id="f1", title="Roundup item", description="",
            price=100, url="https://example.com/f1", location=None, ships=True,
            published_at=None, raw={},
        ),
    )
    alert_id = storage.upsert_listing(
        conn, search.id,
        Listing(
            source="blocket", external_id="f2", title="Alert item", description="",
            price=100, url="https://example.com/f2", location=None, ships=True,
            published_at=None, raw={},
        ),
    )
    storage.mark_scored(conn, roundup_id, 6, "r", [], "p")
    storage.mark_scored(conn, alert_id, 9, "r", [], "p")

    response = client.get("/feed")
    assert response.status_code == 200
    assert "Roundup item" in response.text
    assert "Alert item" not in response.text

    alert_response = client.get("/feed?bucket=instant_alert")
    assert "Alert item" in alert_response.text
    assert "Roundup item" not in alert_response.text

    from watcher.models import Search
    other_search = searches.create_search(conn, Search(name="Other search"))
    filtered_response = client.get(f"/feed?search_id={other_search.id}")
    assert "Roundup item" not in filtered_response.text


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


def test_min_price_round_trips_through_form(client):
    response = client.post(
        "/searches/new",
        data={
            "name": "GPU hunt min price",
            "enabled": "on",
            "scope": "national",
            "location": "",
            "max_price": "10000",
            "min_price": "2000",
            "search_phrases": "rtx",
            "marketplaces": "blocket",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    from watcher import searches as searches_repo

    conn = client.app.state.conn
    search = next(s for s in searches_repo.list_searches(conn) if s.name == "GPU hunt min price")
    assert search.min_price == 2000

    edit_html = client.get(f"/searches/{search.id}/edit").text
    assert 'name="min_price" value="2000"' in edit_html


def test_new_search_form_defaults_to_plain_scoring_mode(client):
    edit_html = client.get("/searches/new").text
    assert 'name="scoring_mode" value="rated"' in edit_html
    assert 'checked' not in edit_html.split('name="scoring_mode"')[1].split('>')[0]


def test_scoring_mode_round_trips_through_form(client):
    response = client.post(
        "/searches/new",
        data={
            "name": "Vinyl hunt",
            "enabled": "on",
            "scope": "national",
            "location": "",
            "max_price": "",
            "scoring_mode": "rated",
            "search_phrases": "kind of blue",
            "marketplaces": "blocket",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    from watcher import searches as searches_repo

    conn = client.app.state.conn
    search = next(s for s in searches_repo.list_searches(conn) if s.name == "Vinyl hunt")
    assert search.scoring_mode == "rated"

    edit_html = client.get(f"/searches/{search.id}/edit").text
    assert 'name="scoring_mode" value="rated" checked' in edit_html


def test_plain_search_with_instant_alert_price_round_trips_through_form(client):
    response = client.post(
        "/searches/new",
        data={
            "name": "Vinyl hunt plain",
            "enabled": "on",
            "scope": "national",
            "location": "",
            "max_price": "",
            "instant_alert_price": "300",
            "search_phrases": "kind of blue",
            "marketplaces": "blocket",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    from watcher import searches as searches_repo

    conn = client.app.state.conn
    search = next(s for s in searches_repo.list_searches(conn) if s.name == "Vinyl hunt plain")
    assert search.scoring_mode == "plain"
    assert search.instant_alert_price == 300

    edit_html = client.get(f"/searches/{search.id}/edit").text
    assert 'name="instant_alert_price" value="300"' in edit_html


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


def test_create_and_list_watched_item(client):
    response = client.post(
        "/watched-items/new",
        data={
            "name": "VU meter", "url": "https://example.com/vu-meter", "enabled": "on",
            "target_price": "1000", "check_frequency": "weekly",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    list_html = client.get("/watched-items").text
    assert "VU meter" in list_html
    assert "weekly" in list_html


def test_watched_item_round_trips_through_form(client):
    client.post(
        "/watched-items/new",
        data={
            "name": "VU meter", "url": "https://example.com/vu-meter", "enabled": "on",
            "target_price": "1000", "check_frequency": "monthly", "find_used": "on",
        },
    )

    from watcher import watched_items as watched_items_repo

    conn = client.app.state.conn
    item = next(i for i in watched_items_repo.list_watched_items(conn) if i.name == "VU meter")
    assert item.target_price == 1000
    assert item.check_frequency == "monthly"
    assert item.find_used is True

    edit_html = client.get(f"/watched-items/{item.id}/edit").text
    assert 'value="1000"' in edit_html
    assert 'value="monthly" selected' in edit_html


def test_toggle_and_delete_watched_item(client):
    client.post(
        "/watched-items/new",
        data={"name": "VU meter", "url": "https://example.com/vu-meter", "enabled": "on"},
    )
    from watcher import watched_items as watched_items_repo

    conn = client.app.state.conn
    item = next(i for i in watched_items_repo.list_watched_items(conn) if i.name == "VU meter")

    client.post(f"/watched-items/{item.id}/toggle")
    assert watched_items_repo.get_watched_item(conn, item.id).enabled is False

    client.post(f"/watched-items/{item.id}/delete")
    assert watched_items_repo.get_watched_item(conn, item.id) is None


def test_check_watched_item_now_redirects_with_checked_param(client):
    client.post(
        "/watched-items/new",
        data={"name": "VU meter", "url": "https://example.com/vu-meter", "enabled": "on"},
    )
    from watcher import watched_items as watched_items_repo

    conn = client.app.state.conn
    item = next(i for i in watched_items_repo.list_watched_items(conn) if i.name == "VU meter")

    response = client.post(f"/watched-items/{item.id}/check", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == f"/watched-items?checked={item.id}"


def test_check_watched_item_now_unknown_id_redirects(client):
    response = client.post("/watched-items/999999/check", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/watched-items"


def test_marketplaces_list_shows_blocket(client):
    response = client.get("/marketplaces")
    assert response.status_code == 200
    assert "Blocket" in response.text
    assert "240 min" in response.text  # default poll interval


def test_marketplaces_list_flags_auctionet_as_an_auction(client):
    response = client.get("/marketplaces")
    assert response.status_code == 200
    assert "Auctionet" in response.text
    auctionet_row = response.text.split("Auctionet")[1].split("</tr>")[0]
    assert "auction" in auctionet_row
    blocket_row = response.text.split(">Blocket<")[0].split("<tr>")[-1]
    assert "auction" not in blocket_row


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
