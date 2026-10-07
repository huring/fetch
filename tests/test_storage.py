import pytest

from watcher import db, searches, storage
from watcher.models import Listing, Search


@pytest.fixture
def conn(tmp_path):
    return db.connect(str(tmp_path / "test.db"))


@pytest.fixture
def search_id(conn):
    created = searches.create_search(conn, Search(name="Test search"))
    return created.id


def make_listing(external_id="abc123", price=1000, source="blocket"):
    return Listing(
        source=source,
        external_id=external_id,
        title="Onkyo TX-NR656",
        description="Fint skick",
        price=price,
        url="https://example.com/abc123",
        location="Stockholm",
        ships=True,
        published_at=None,
        raw={"id": external_id},
    )


def test_new_listing_is_pending(conn, search_id):
    listing_id = storage.upsert_listing(conn, search_id, make_listing())
    pending = storage.get_pending_listings(conn, search_id)
    assert len(pending) == 1
    assert pending[0]["id"] == listing_id
    assert pending[0]["score"] is None


def test_same_price_reseen_does_not_reset_score(conn, search_id):
    listing_id = storage.upsert_listing(conn, search_id, make_listing(price=1000))
    storage.mark_scored(conn, listing_id, 7, "bra skick", [], "rimligt pris")

    storage.upsert_listing(conn, search_id, make_listing(price=1000))
    pending = storage.get_pending_listings(conn, search_id)
    assert len(pending) == 0  # still scored, not reset


def test_price_drop_resets_to_pending(conn, search_id):
    listing_id = storage.upsert_listing(conn, search_id, make_listing(price=1000))
    storage.mark_scored(conn, listing_id, 7, "bra skick", [], "rimligt pris")

    storage.upsert_listing(conn, search_id, make_listing(price=800))
    pending = storage.get_pending_listings(conn, search_id)
    assert len(pending) == 1
    assert pending[0]["price"] == 800


def test_higher_price_does_not_reset(conn, search_id):
    listing_id = storage.upsert_listing(conn, search_id, make_listing(price=1000))
    storage.mark_scored(conn, listing_id, 7, "bra skick", [], "rimligt pris")

    storage.upsert_listing(conn, search_id, make_listing(price=1200))
    pending = storage.get_pending_listings(conn, search_id)
    assert len(pending) == 0


def test_prefiltered_out_excluded_from_pending(conn, search_id):
    listing_id = storage.upsert_listing(conn, search_id, make_listing())
    storage.mark_prefiltered_out(conn, listing_id, "excluded model")
    pending = storage.get_pending_listings(conn, search_id)
    assert len(pending) == 0


def test_digest_queue_respects_score_range_and_instant_exclusion(conn, search_id):
    low = storage.upsert_listing(conn, search_id, make_listing(external_id="low"))
    mid = storage.upsert_listing(conn, search_id, make_listing(external_id="mid"))
    high = storage.upsert_listing(conn, search_id, make_listing(external_id="high"))

    storage.mark_scored(conn, low, 3, "r", [], "p")
    storage.mark_scored(conn, mid, 6, "r", [], "p")
    storage.mark_scored(conn, high, 9, "r", [], "p")
    storage.mark_notified_instant(conn, high)

    digest = storage.get_pending_digest(conn, score_min=5, score_max=7)
    ids = [row["id"] for row in digest]
    assert ids == [mid]


def test_health_alert_threshold_and_reset(conn):
    for _ in range(2):
        storage.record_source_failure(conn, "blocket", "timeout")
    assert storage.should_send_health_alert(conn, "blocket", threshold=3) is False

    storage.record_source_failure(conn, "blocket", "timeout")
    assert storage.should_send_health_alert(conn, "blocket", threshold=3) is True

    storage.mark_health_alert_sent(conn, "blocket")
    assert storage.should_send_health_alert(conn, "blocket", threshold=3) is False

    storage.record_source_success(conn, "blocket")
    storage.record_source_failure(conn, "blocket", "timeout")
    assert storage.should_send_health_alert(conn, "blocket", threshold=3) is False


def test_monthly_cost_summary(conn):
    run_id = storage.start_run(conn)
    storage.log_token_usage(conn, run_id, "claude-haiku-4-5", 1000, 200, 0.0021)
    storage.log_token_usage(conn, run_id, "claude-haiku-4-5", 500, 100, 0.0011)

    summary = storage.monthly_cost_summary(conn)
    assert len(summary) == 1
    assert summary[0].input_tokens == 1500
    assert summary[0].output_tokens == 300
    assert round(summary[0].cost_usd, 4) == 0.0032


def test_get_last_completed_run_skips_in_progress_run(conn):
    ok_run_id = storage.start_run(conn)
    storage.finish_run(conn, ok_run_id, status="ok")

    storage.start_run(conn)  # a second run, still "running", no finish_run call

    last_completed = storage.get_last_completed_run(conn)
    assert last_completed["id"] == ok_run_id
    assert last_completed["status"] == "ok"


def test_get_last_completed_run_none_when_only_in_progress(conn):
    storage.start_run(conn)
    assert storage.get_last_completed_run(conn) is None


def test_bucket_counts(conn, search_id):
    found = storage.upsert_listing(conn, search_id, make_listing(external_id="found"))
    summary = storage.upsert_listing(conn, search_id, make_listing(external_id="summary"))
    threshold = storage.upsert_listing(conn, search_id, make_listing(external_id="threshold"))
    excluded = storage.upsert_listing(conn, search_id, make_listing(external_id="excluded"))

    storage.mark_scored(conn, found, 3, "r", [], "p")  # below digest_min - "found" only
    storage.mark_scored(conn, summary, 6, "r", [], "p")
    storage.mark_scored(conn, threshold, 9, "r", [], "p")
    storage.mark_prefiltered_out(conn, excluded, "too expensive")

    counts = storage.get_search_bucket_counts(conn, score_digest_min=5, score_instant_threshold=8)
    assert counts[search_id] == {"found": 3, "summary": 1, "threshold": 1}


def test_list_bucket_listings_filters_by_bucket(conn, search_id):
    low = storage.upsert_listing(conn, search_id, make_listing(external_id="low"))
    high = storage.upsert_listing(conn, search_id, make_listing(external_id="high"))
    storage.mark_scored(conn, low, 3, "r", [], "p")
    storage.mark_scored(conn, high, 9, "r", [], "p")

    found = storage.list_bucket_listings(conn, search_id, "found", score_digest_min=5, score_instant_threshold=8)
    threshold = storage.list_bucket_listings(conn, search_id, "threshold", score_digest_min=5, score_instant_threshold=8)

    assert {row["id"] for row in found} == {low, high}
    assert {row["id"] for row in threshold} == {high}


def test_get_scored_listings_excludes_pending_and_prefiltered(conn, search_id):
    pending = storage.upsert_listing(conn, search_id, make_listing(external_id="pending"))
    excluded = storage.upsert_listing(conn, search_id, make_listing(external_id="excluded"))
    scored = storage.upsert_listing(conn, search_id, make_listing(external_id="scored"))
    storage.mark_prefiltered_out(conn, excluded, "too expensive")
    storage.mark_scored(conn, scored, 7, "r", [], "p")

    rows = storage.get_scored_listings(conn)
    assert {row["id"] for row in rows} == {scored}
    assert rows[0]["search_name"] == "Test search"


def test_insert_price_history_and_delete_listing(conn, search_id):
    listing_id = storage.upsert_listing(conn, search_id, make_listing(external_id="sold"))
    storage.mark_scored(conn, listing_id, 7, "r", [], "p")
    row = conn.execute("SELECT * FROM listings WHERE id = ?", (listing_id,)).fetchone()

    storage.insert_price_history(conn, "Test search", row)
    storage.delete_listing(conn, listing_id)

    assert conn.execute("SELECT * FROM listings WHERE id = ?", (listing_id,)).fetchone() is None
    history = conn.execute("SELECT * FROM price_history").fetchone()
    assert history["search_name"] == "Test search"
    assert history["title"] == "Onkyo TX-NR656"
    assert history["score"] == 7


def test_clear_operational_data_keeps_searches_and_configs(conn, search_id):
    listing_id = storage.upsert_listing(conn, search_id, make_listing())
    storage.mark_scored(conn, listing_id, 7, "r", [], "p")
    run_id = storage.start_run(conn)
    storage.finish_run(conn, run_id, status="ok")
    storage.log_token_usage(conn, run_id, "claude-haiku-4-5", 100, 50, 0.001)
    storage.record_source_failure(conn, "blocket", "boom")
    row = conn.execute("SELECT * FROM listings WHERE id = ?", (listing_id,)).fetchone()
    storage.insert_price_history(conn, "Test search", row)

    storage.clear_operational_data(conn)

    assert conn.execute("SELECT COUNT(*) AS n FROM listings").fetchone()["n"] == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM runs").fetchone()["n"] == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM token_usage").fetchone()["n"] == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM source_health").fetchone()["n"] == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM price_history").fetchone()["n"] == 0
    # searches and marketplace configs are untouched
    assert conn.execute("SELECT COUNT(*) AS n FROM searches").fetchone()["n"] == 1


def test_mark_stale_notified(conn, search_id):
    listing_id = storage.upsert_listing(conn, search_id, make_listing())
    storage.mark_stale_notified(conn, listing_id)

    row = conn.execute("SELECT stale_notified_at FROM listings WHERE id = ?", (listing_id,)).fetchone()
    assert row["stale_notified_at"] is not None
