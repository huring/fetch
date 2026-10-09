import pytest

from watcher import db, searches, storage
from watcher.models import Listing, Search


@pytest.fixture
def conn(tmp_path):
    return db.connect(str(tmp_path / "test.db"))


@pytest.fixture
def search_id(conn):
    # scoring_mode="rated" explicit (not just the model's own default) since
    # most tests using this fixture call mark_scored - Search's Python-level
    # default is "plain" (see models.py), so leaving this implicit would
    # silently stop matching a currently-rated search once storage.py
    # started checking scoring_mode (see get_top_listings/list_feed_listings).
    created = searches.create_search(conn, Search(name="Test search", scoring_mode="rated"))
    return created.id


def make_listing(external_id="abc123", price=1000, source="blocket", distance_km=None):
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
        distance_km=distance_km,
        raw={"id": external_id},
    )


def test_new_listing_is_pending(conn, search_id):
    listing_id = storage.upsert_listing(conn, search_id, make_listing())
    pending = storage.get_pending_listings(conn, search_id)
    assert len(pending) == 1
    assert pending[0]["id"] == listing_id
    assert pending[0]["score"] is None


def test_upsert_listing_stores_and_updates_image_url(conn, search_id):
    listing = make_listing()
    listing.image_url = "https://example.com/first.jpg"
    listing_id = storage.upsert_listing(conn, search_id, listing)

    pending = storage.get_pending_listings(conn, search_id)
    assert pending[0]["image_url"] == "https://example.com/first.jpg"

    listing.image_url = "https://example.com/second.jpg"
    storage.upsert_listing(conn, search_id, listing)
    pending = storage.get_pending_listings(conn, search_id)
    assert pending[0]["image_url"] == "https://example.com/second.jpg"


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
    assert counts[search_id] == {"found": 3, "daily_roundup": 1, "instant_alert": 1}


def test_list_bucket_listings_filters_by_bucket(conn, search_id):
    low = storage.upsert_listing(conn, search_id, make_listing(external_id="low"))
    high = storage.upsert_listing(conn, search_id, make_listing(external_id="high"))
    storage.mark_scored(conn, low, 3, "r", [], "p")
    storage.mark_scored(conn, high, 9, "r", [], "p")

    found = storage.list_bucket_listings(conn, search_id, "found", score_digest_min=5, score_instant_threshold=8)
    threshold = storage.list_bucket_listings(conn, search_id, "instant_alert", score_digest_min=5, score_instant_threshold=8)

    assert {row["id"] for row in found} == {low, high}
    assert {row["id"] for row in threshold} == {high}


def test_list_bucket_listings_orders_plain_matches_by_distance_closest_first(conn, search_id):
    """Confirmed live (2026-10): a plain search's listings all share the same
    score (-1, PLAIN_SURFACED_SCORE), so distance is the only thing left to
    order them by - matching Blocket's own "Closest" sort (see
    marketplaces._blocket_fetch)."""
    far = storage.upsert_listing(conn, search_id, make_listing(external_id="far", distance_km=30.0))
    unknown = storage.upsert_listing(conn, search_id, make_listing(external_id="unknown", distance_km=None))
    near = storage.upsert_listing(conn, search_id, make_listing(external_id="near", distance_km=1.4))
    for listing_id in (far, unknown, near):
        storage.mark_surfaced_plain(conn, listing_id)

    found = storage.list_bucket_listings(conn, search_id, "found", score_digest_min=5, score_instant_threshold=8)

    assert [row["id"] for row in found] == [near, far, unknown]


def test_get_surfaced_listings_excludes_pending_and_prefiltered(conn, search_id):
    pending = storage.upsert_listing(conn, search_id, make_listing(external_id="pending"))
    excluded = storage.upsert_listing(conn, search_id, make_listing(external_id="excluded"))
    scored = storage.upsert_listing(conn, search_id, make_listing(external_id="scored"))
    storage.mark_prefiltered_out(conn, excluded, "too expensive")
    storage.mark_scored(conn, scored, 7, "r", [], "p")

    rows = storage.get_surfaced_listings(conn)
    assert {row["id"] for row in rows} == {scored}
    assert rows[0]["search_name"] == "Test search"


def test_get_surfaced_listings_includes_plain_surfaced(conn, search_id):
    plain = storage.upsert_listing(conn, search_id, make_listing(external_id="plain"))
    storage.mark_surfaced_plain(conn, plain)

    rows = storage.get_surfaced_listings(conn)
    assert {row["id"] for row in rows} == {plain}


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


def test_clear_operational_data_resets_watched_item_check_state_but_keeps_the_item(conn):
    from watcher import watched_items
    from watcher.models import WatchedItem

    item = watched_items.create_watched_item(
        conn, WatchedItem(name="VU meter", url="https://example.com/vu", target_price=1000)
    )
    watched_items.record_check_result(conn, item.id, price=900, extracted_title="VU Meter Pro")
    watched_items.record_alert(conn, item.id, 900)

    storage.clear_operational_data(conn)

    refreshed = watched_items.get_watched_item(conn, item.id)
    assert refreshed is not None
    assert refreshed.name == "VU meter"
    assert refreshed.target_price == 1000
    assert refreshed.current_price is None
    assert refreshed.lowest_price_seen is None
    assert refreshed.last_alert_price is None
    assert refreshed.last_checked_at is None
    assert refreshed.extracted_title == "VU Meter Pro"  # not check-run noise - kept


def test_mark_stale_notified(conn, search_id):
    listing_id = storage.upsert_listing(conn, search_id, make_listing())
    storage.mark_stale_notified(conn, listing_id)

    row = conn.execute("SELECT stale_notified_at FROM listings WHERE id = ?", (listing_id,)).fetchone()
    assert row["stale_notified_at"] is not None


def test_scoring_batch_bookkeeping(conn, search_id):
    unbatched = storage.upsert_listing(conn, search_id, make_listing(external_id="unbatched"))
    in_progress = storage.upsert_listing(conn, search_id, make_listing(external_id="in-progress"))
    scored = storage.upsert_listing(conn, search_id, make_listing(external_id="scored"))
    storage.mark_scored(conn, scored, 7, "r", [], "p")

    unbatched_ids = {row["id"] for row in storage.get_unbatched_pending_listings(conn)}
    assert unbatched_ids == {unbatched, in_progress}
    assert storage.count_listings_awaiting_scoring(conn) == 2

    storage.create_scoring_batch(conn, "batch-1", [("custom-1", 0, in_progress)])

    assert storage.count_listings_awaiting_scoring(conn) == 1
    assert storage.count_listings_in_progress_scoring(conn) == 1
    assert storage.get_in_progress_batch_ids(conn) == ["batch-1"]
    assert storage.get_batch_items(conn, "batch-1") == {("custom-1", 0): in_progress}

    storage.delete_scoring_batch(conn, "batch-1")

    assert storage.get_in_progress_batch_ids(conn) == []
    assert storage.count_listings_awaiting_scoring(conn) == 2  # back on the unbatched list


def test_get_overview_stats(conn, search_id):
    a = storage.upsert_listing(conn, search_id, make_listing(external_id="a"))
    b = storage.upsert_listing(conn, search_id, make_listing(external_id="b"))
    c = storage.upsert_listing(conn, search_id, make_listing(external_id="c"))
    storage.mark_scored(conn, a, 3, "r", [], "p")  # found-only
    storage.mark_scored(conn, b, 6, "r", [], "p")  # daily_roundup
    storage.mark_prefiltered_out(conn, c, "too expensive")  # excluded entirely

    run_id = storage.start_run(conn)
    storage.log_token_usage(conn, run_id, "claude-haiku-4-5", 1000, 200, 0.0021)

    stats = storage.get_overview_stats(conn, score_digest_min=5, score_instant_threshold=8)

    assert stats["total_scanned"] == 3
    assert stats["total_found"] == 2  # a and b, not the prefiltered-out c
    assert stats["total_daily_roundup"] == 1  # just b
    assert round(stats["monthly_cost_usd"], 4) == 0.0021


def test_get_top_listings_only_includes_instant_alert_bucket(conn, search_id):
    below_threshold = storage.upsert_listing(conn, search_id, make_listing(external_id="below"))
    high = storage.upsert_listing(conn, search_id, make_listing(external_id="high"))
    plain = storage.upsert_listing(conn, search_id, make_listing(external_id="plain"))
    storage.mark_scored(conn, below_threshold, 7, "r", [], "p")  # "Maybe", not "Yes!"
    storage.mark_scored(conn, high, 9, "r", [], "p")
    storage.mark_surfaced_plain(conn, plain)  # score -1, never "top"

    top = storage.get_top_listings(conn, score_instant_threshold=8, limit=5)

    assert [row["id"] for row in top] == [high]


def test_get_top_listings_excludes_a_search_switched_from_rated_to_plain(conn, search_id):
    # Confirmed live (2026-10): switching scoring_mode never touches a
    # search's existing listings, so an old high score from before the
    # switch would otherwise keep surfacing here forever, crowding out
    # genuinely current standouts from searches that are actually rated.
    stale_high = storage.upsert_listing(conn, search_id, make_listing(external_id="stale-high"))
    storage.mark_scored(conn, stale_high, 10, "r", [], "p")
    searches.update_search(conn, search_id, Search(name="Test search", scoring_mode="plain"))

    other_search = searches.create_search(conn, Search(name="Still rated", scoring_mode="rated"))
    current_high = storage.upsert_listing(conn, other_search.id, make_listing(external_id="current-high"))
    storage.mark_scored(conn, current_high, 9, "r", [], "p")

    top = storage.get_top_listings(conn, score_instant_threshold=8, limit=5)

    assert [row["id"] for row in top] == [current_high]


def test_list_feed_listings_filters_by_bucket_and_search(conn, search_id):
    search_b = searches.create_search(conn, Search(name="Other search", scoring_mode="rated"))
    roundup_a = storage.upsert_listing(conn, search_id, make_listing(external_id="roundup-a"))
    roundup_b = storage.upsert_listing(conn, search_b.id, make_listing(external_id="roundup-b"))
    alert_a = storage.upsert_listing(conn, search_id, make_listing(external_id="alert-a"))
    storage.mark_scored(conn, roundup_a, 6, "r", [], "p")
    storage.mark_scored(conn, roundup_b, 6, "r", [], "p")
    storage.mark_scored(conn, alert_a, 9, "r", [], "p")

    all_roundup = storage.list_feed_listings(conn, "daily_roundup", score_digest_min=5, score_instant_threshold=8)
    assert {row["id"] for row in all_roundup} == {roundup_a, roundup_b}

    only_search = storage.list_feed_listings(
        conn, "daily_roundup", score_digest_min=5, score_instant_threshold=8, search_id=search_id
    )
    assert {row["id"] for row in only_search} == {roundup_a}
    assert only_search[0]["search_name"] == "Test search"

    only_alerts = storage.list_feed_listings(conn, "instant_alert", score_digest_min=5, score_instant_threshold=8)
    assert {row["id"] for row in only_alerts} == {alert_a}


def test_list_feed_listings_rated_buckets_exclude_a_search_switched_to_plain(conn, search_id):
    stale_alert = storage.upsert_listing(conn, search_id, make_listing(external_id="stale-alert"))
    storage.mark_scored(conn, stale_alert, 9, "r", [], "p")
    searches.update_search(conn, search_id, Search(name="Test search", scoring_mode="plain"))

    assert storage.list_feed_listings(conn, "instant_alert", score_digest_min=5, score_instant_threshold=8) == []
    assert storage.list_feed_listings(conn, "daily_roundup", score_digest_min=5, score_instant_threshold=8) == []
    # "found" is deliberately unaffected by scoring_mode - it means "passed
    # the deterministic prefilter" regardless of score or current mode, so
    # both the stale-scored listing and a freshly plain-surfaced one count.
    plain_match = storage.upsert_listing(conn, search_id, make_listing(external_id="plain-match"))
    storage.mark_surfaced_plain(conn, plain_match)
    found = storage.list_feed_listings(conn, "found", score_digest_min=5, score_instant_threshold=8)
    assert {row["id"] for row in found} == {stale_alert, plain_match}


def test_get_ended_auction_listings(conn, search_id):
    from watcher.models import Listing

    def make_auction_listing(external_id, auction_ends_at):
        return Listing(
            source="auctionet", external_id=external_id, title="Lot", description="",
            price=1000, url=f"https://auctionet.com/{external_id}", location=None, ships=None,
            published_at=None, auction_ends_at=auction_ends_at, raw={},
        )

    import datetime
    past = datetime.datetime.utcnow() - datetime.timedelta(days=1)
    future = datetime.datetime.utcnow() + datetime.timedelta(days=1)

    ended_id = storage.upsert_listing(conn, search_id, make_auction_listing("ended", past))
    still_live_id = storage.upsert_listing(conn, search_id, make_auction_listing("live", future))
    non_auction_id = storage.upsert_listing(conn, search_id, make_listing(external_id="non-auction"))

    ended = storage.get_ended_auction_listings(conn)

    assert {row["id"] for row in ended} == {ended_id}
    assert ended[0]["search_name"] == "Test search"
    assert still_live_id not in {row["id"] for row in ended}
    assert non_auction_id not in {row["id"] for row in ended}


def test_get_listing_with_search_name(conn, search_id):
    listing_id = storage.upsert_listing(conn, search_id, make_listing())
    row = storage.get_listing_with_search_name(conn, listing_id)
    assert row["search_name"] == "Test search"


def test_mark_surfaced_plain_is_distinct_from_pending_and_prefiltered(conn, search_id):
    listing_id = storage.upsert_listing(conn, search_id, make_listing())
    storage.mark_surfaced_plain(conn, listing_id)

    row = conn.execute("SELECT score FROM listings WHERE id = ?", (listing_id,)).fetchone()
    assert row["score"] == storage.PLAIN_SURFACED_SCORE
    assert storage.get_pending_listings(conn, search_id) == []  # no longer pending


def test_get_pending_plain_digest_excludes_notified_and_already_digested(conn, search_id):
    pending = storage.upsert_listing(conn, search_id, make_listing(external_id="pending"))
    notified = storage.upsert_listing(conn, search_id, make_listing(external_id="notified"))
    digested = storage.upsert_listing(conn, search_id, make_listing(external_id="digested"))
    not_surfaced = storage.upsert_listing(conn, search_id, make_listing(external_id="still-pending"))

    for listing_id in (pending, notified, digested):
        storage.mark_surfaced_plain(conn, listing_id)
    storage.mark_notified_instant(conn, notified)
    storage.mark_digested(conn, [digested])

    rows = storage.get_pending_plain_digest(conn)
    assert {row["id"] for row in rows} == {pending}
    assert rows[0]["search_name"] == "Test search"
