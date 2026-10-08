import datetime
from unittest.mock import patch

from watcher import db, liveness, searches, storage
from watcher.models import Listing, Search
from watcher.settings import Settings


def make_conn(tmp_path):
    return db.connect(str(tmp_path / "test.db"))


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


def make_listing(external_id, title="Onkyo TX-NR656", price=1000, source="blocket"):
    return Listing(
        source=source,
        external_id=external_id,
        title=title,
        description="",
        price=price,
        url=f"https://example.com/{external_id}",
        location=None,
        ships=True,
        published_at=None,
        raw={},
    )


def make_auction_listing(external_id, auction_ends_at, price=1000):
    return Listing(
        source="auctionet",
        external_id=external_id,
        title="Lot",
        description="",
        price=price,
        url=f"https://auctionet.com/{external_id}",
        location=None,
        ships=None,
        published_at=None,
        auction_ends_at=auction_ends_at,
        raw={},
    )


def _backdate_first_seen(conn, listing_id, days):
    then = (datetime.datetime.utcnow() - datetime.timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("UPDATE listings SET first_seen_at = ? WHERE id = ?", (then, listing_id))
    conn.commit()


def test_sweep_removes_inactive_listing_and_records_price_history(tmp_path):
    conn = make_conn(tmp_path)
    search = searches.create_search(conn, Search(name="C", search_phrases=["onkyo"], marketplaces=["blocket"]))
    listing_id = storage.upsert_listing(conn, search.id, make_listing("1"))
    storage.mark_scored(conn, listing_id, 7, "good", [], "fair")

    with patch("watcher.sources.blocket.check_active", return_value=False):
        result = liveness.run_liveness_sweep(conn, make_settings())

    assert result["checked"] == 1
    assert result["removed"] == 1
    assert conn.execute("SELECT * FROM listings WHERE id = ?", (listing_id,)).fetchone() is None
    history = conn.execute("SELECT * FROM price_history").fetchone()
    assert history["search_name"] == "C"
    assert history["score"] == 7


def test_sweep_leaves_active_recent_listing_untouched(tmp_path):
    conn = make_conn(tmp_path)
    search = searches.create_search(conn, Search(name="C", search_phrases=["onkyo"], marketplaces=["blocket"]))
    listing_id = storage.upsert_listing(conn, search.id, make_listing("1"))
    storage.mark_scored(conn, listing_id, 7, "good", [], "fair")

    with patch("watcher.sources.blocket.check_active", return_value=True):
        result = liveness.run_liveness_sweep(conn, make_settings())

    assert result["checked"] == 1
    assert result["removed"] == 0
    assert result["stale_notices_sent"] == 0
    assert conn.execute("SELECT * FROM listings WHERE id = ?", (listing_id,)).fetchone() is not None


def test_sweep_sends_stale_notice_once_for_old_active_listing(tmp_path):
    conn = make_conn(tmp_path)
    search = searches.create_search(conn, Search(name="C", search_phrases=["onkyo"], marketplaces=["blocket"]))
    listing_id = storage.upsert_listing(conn, search.id, make_listing("1"))
    storage.mark_scored(conn, listing_id, 7, "good", [], "fair")
    _backdate_first_seen(conn, listing_id, days=22)

    with patch("watcher.sources.blocket.check_active", return_value=True):
        with patch("watcher.notify.slack.send_stale_opportunity") as mock_send:
            result = liveness.run_liveness_sweep(conn, make_settings(slack_webhook_url="https://hooks.slack.com/x"))
            assert result["stale_notices_sent"] == 1
            mock_send.assert_called_once()

            # second sweep shouldn't notify again
            result2 = liveness.run_liveness_sweep(conn, make_settings(slack_webhook_url="https://hooks.slack.com/x"))
            assert result2["stale_notices_sent"] == 0
            mock_send.assert_called_once()


def test_sweep_ignores_low_scored_old_listing(tmp_path):
    conn = make_conn(tmp_path)
    search = searches.create_search(conn, Search(name="C", search_phrases=["onkyo"], marketplaces=["blocket"]))
    listing_id = storage.upsert_listing(conn, search.id, make_listing("1"))
    storage.mark_scored(conn, listing_id, 3, "meh", [], "fair")  # below score_digest_min
    _backdate_first_seen(conn, listing_id, days=30)

    with patch("watcher.sources.blocket.check_active", return_value=True):
        result = liveness.run_liveness_sweep(conn, make_settings())

    assert result["stale_notices_sent"] == 0


def test_sweep_skips_listing_with_no_check_active_hook(tmp_path):
    conn = make_conn(tmp_path)
    search = searches.create_search(conn, Search(name="C", search_phrases=["x"], marketplaces=["blocket"]))
    listing_id = storage.upsert_listing(conn, search.id, make_listing("1", source="unknown_marketplace"))
    storage.mark_scored(conn, listing_id, 7, "good", [], "fair")

    result = liveness.run_liveness_sweep(conn, make_settings())

    assert result["checked"] == 0
    assert conn.execute("SELECT * FROM listings WHERE id = ?", (listing_id,)).fetchone() is not None


def test_sweep_leaves_listing_untouched_on_check_error(tmp_path):
    from watcher.sources.base import SourceError

    conn = make_conn(tmp_path)
    search = searches.create_search(conn, Search(name="C", search_phrases=["x"], marketplaces=["blocket"]))
    listing_id = storage.upsert_listing(conn, search.id, make_listing("1"))
    storage.mark_scored(conn, listing_id, 7, "good", [], "fair")

    with patch("watcher.sources.blocket.check_active", side_effect=SourceError("boom")):
        result = liveness.run_liveness_sweep(conn, make_settings())

    assert result["errors"] == 1
    assert result["checked"] == 0
    assert conn.execute("SELECT * FROM listings WHERE id = ?", (listing_id,)).fetchone() is not None


def test_sweep_dry_run_does_not_delete_or_mark(tmp_path):
    conn = make_conn(tmp_path)
    search = searches.create_search(conn, Search(name="C", search_phrases=["x"], marketplaces=["blocket"]))
    listing_id = storage.upsert_listing(conn, search.id, make_listing("1"))
    storage.mark_scored(conn, listing_id, 7, "good", [], "fair")

    with patch("watcher.sources.blocket.check_active", return_value=False):
        result = liveness.run_liveness_sweep(conn, make_settings(dry_run=True), dry_run=True)

    assert result["removed"] == 1
    assert conn.execute("SELECT * FROM listings WHERE id = ?", (listing_id,)).fetchone() is not None
    assert conn.execute("SELECT * FROM price_history").fetchone() is None


def test_auction_end_sweep_removes_ended_auctions_regardless_of_score_state(tmp_path):
    conn = make_conn(tmp_path)
    search = searches.create_search(
        conn, Search(name="C", search_phrases=["x"], marketplaces=["auctionet"])
    )
    past = datetime.datetime.utcnow() - datetime.timedelta(hours=1)
    ended_pending_id = storage.upsert_listing(conn, search.id, make_auction_listing("ended-pending", past))
    ended_scored_id = storage.upsert_listing(conn, search.id, make_auction_listing("ended-scored", past))
    storage.mark_scored(conn, ended_scored_id, 7, "good", [], "fair")

    result = liveness.run_auction_end_sweep(conn)

    assert result["removed"] == 2
    assert conn.execute("SELECT * FROM listings WHERE id = ?", (ended_pending_id,)).fetchone() is None
    assert conn.execute("SELECT * FROM listings WHERE id = ?", (ended_scored_id,)).fetchone() is None
    history_names = {row["title"] for row in conn.execute("SELECT * FROM price_history")}
    assert history_names == {"Lot"}


def test_auction_end_sweep_leaves_still_live_auction_untouched(tmp_path):
    conn = make_conn(tmp_path)
    search = searches.create_search(
        conn, Search(name="C", search_phrases=["x"], marketplaces=["auctionet"])
    )
    future = datetime.datetime.utcnow() + datetime.timedelta(hours=1)
    listing_id = storage.upsert_listing(conn, search.id, make_auction_listing("still-live", future))

    result = liveness.run_auction_end_sweep(conn)

    assert result["removed"] == 0
    assert conn.execute("SELECT * FROM listings WHERE id = ?", (listing_id,)).fetchone() is not None


def test_auction_end_sweep_ignores_non_auction_listings(tmp_path):
    conn = make_conn(tmp_path)
    search = searches.create_search(conn, Search(name="C", search_phrases=["x"], marketplaces=["blocket"]))
    listing_id = storage.upsert_listing(conn, search.id, make_listing("1"))

    result = liveness.run_auction_end_sweep(conn)

    assert result["removed"] == 0
    assert conn.execute("SELECT * FROM listings WHERE id = ?", (listing_id,)).fetchone() is not None


def test_auction_end_sweep_dry_run_does_not_delete(tmp_path):
    conn = make_conn(tmp_path)
    search = searches.create_search(
        conn, Search(name="C", search_phrases=["x"], marketplaces=["auctionet"])
    )
    past = datetime.datetime.utcnow() - datetime.timedelta(hours=1)
    listing_id = storage.upsert_listing(conn, search.id, make_auction_listing("ended", past))

    result = liveness.run_auction_end_sweep(conn, dry_run=True)

    assert result["removed"] == 1
    assert conn.execute("SELECT * FROM listings WHERE id = ?", (listing_id,)).fetchone() is not None
    assert conn.execute("SELECT * FROM price_history").fetchone() is None
