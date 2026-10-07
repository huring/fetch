import pytest

from watcher import containers, db, storage
from watcher.models import Container, Listing


@pytest.fixture
def conn(tmp_path):
    return db.connect(str(tmp_path / "test.db"))


@pytest.fixture
def container_id(conn):
    created = containers.create_container(conn, Container(name="Test container"))
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


def test_new_listing_is_pending(conn, container_id):
    listing_id = storage.upsert_listing(conn, container_id, make_listing())
    pending = storage.get_pending_listings(conn, container_id)
    assert len(pending) == 1
    assert pending[0]["id"] == listing_id
    assert pending[0]["score"] is None


def test_same_price_reseen_does_not_reset_score(conn, container_id):
    listing_id = storage.upsert_listing(conn, container_id, make_listing(price=1000))
    storage.mark_scored(conn, listing_id, 7, "bra skick", [], "rimligt pris")

    storage.upsert_listing(conn, container_id, make_listing(price=1000))
    pending = storage.get_pending_listings(conn, container_id)
    assert len(pending) == 0  # still scored, not reset


def test_price_drop_resets_to_pending(conn, container_id):
    listing_id = storage.upsert_listing(conn, container_id, make_listing(price=1000))
    storage.mark_scored(conn, listing_id, 7, "bra skick", [], "rimligt pris")

    storage.upsert_listing(conn, container_id, make_listing(price=800))
    pending = storage.get_pending_listings(conn, container_id)
    assert len(pending) == 1
    assert pending[0]["price"] == 800


def test_higher_price_does_not_reset(conn, container_id):
    listing_id = storage.upsert_listing(conn, container_id, make_listing(price=1000))
    storage.mark_scored(conn, listing_id, 7, "bra skick", [], "rimligt pris")

    storage.upsert_listing(conn, container_id, make_listing(price=1200))
    pending = storage.get_pending_listings(conn, container_id)
    assert len(pending) == 0


def test_prefiltered_out_excluded_from_pending(conn, container_id):
    listing_id = storage.upsert_listing(conn, container_id, make_listing())
    storage.mark_prefiltered_out(conn, listing_id, "excluded model")
    pending = storage.get_pending_listings(conn, container_id)
    assert len(pending) == 0


def test_digest_queue_respects_score_range_and_instant_exclusion(conn, container_id):
    low = storage.upsert_listing(conn, container_id, make_listing(external_id="low"))
    mid = storage.upsert_listing(conn, container_id, make_listing(external_id="mid"))
    high = storage.upsert_listing(conn, container_id, make_listing(external_id="high"))

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
