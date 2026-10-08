import pytest

from watcher import db, searches, watched_items
from watcher.models import Search, WatchedItem


@pytest.fixture
def conn(tmp_path):
    return db.connect(str(tmp_path / "test.db"))


def make_item(**overrides):
    base = dict(name="VU meter", url="https://example.com/vu-meter", target_price=1000)
    base.update(overrides)
    return WatchedItem(**base)


def test_create_and_get_watched_item(conn):
    created = watched_items.create_watched_item(conn, make_item())
    fetched = watched_items.get_watched_item(conn, created.id)

    assert fetched.name == "VU meter"
    assert fetched.url == "https://example.com/vu-meter"
    assert fetched.target_price == 1000
    assert fetched.check_frequency == "daily"
    assert fetched.find_used is False
    assert fetched.current_price is None
    assert fetched.last_checked_at is None


def test_list_watched_items_enabled_only(conn):
    watched_items.create_watched_item(conn, make_item(name="A", enabled=True))
    watched_items.create_watched_item(conn, make_item(name="B", enabled=False))

    assert {i.name for i in watched_items.list_watched_items(conn)} == {"A", "B"}
    assert {i.name for i in watched_items.list_watched_items(conn, enabled_only=True)} == {"A"}


def test_update_watched_item_replaces_editable_fields_only(conn):
    created = watched_items.create_watched_item(conn, make_item())
    watched_items.record_check_result(conn, created.id, price=900, extracted_title="VU Meter Pro")

    updated = watched_items.update_watched_item(
        conn, created.id, make_item(name="VU meter (renamed)", target_price=800)
    )

    assert updated.name == "VU meter (renamed)"
    assert updated.target_price == 800
    # check-run state untouched by an admin-form update
    assert updated.current_price == 900
    assert updated.extracted_title == "VU Meter Pro"


def test_set_enabled_and_delete(conn):
    created = watched_items.create_watched_item(conn, make_item())

    watched_items.set_enabled(conn, created.id, False)
    assert watched_items.get_watched_item(conn, created.id).enabled is False

    watched_items.delete_watched_item(conn, created.id)
    assert watched_items.get_watched_item(conn, created.id) is None


def test_record_check_result_tracks_lowest_price_seen(conn):
    created = watched_items.create_watched_item(conn, make_item())

    watched_items.record_check_result(conn, created.id, price=1000, extracted_title="VU Meter Pro")
    assert watched_items.get_watched_item(conn, created.id).lowest_price_seen == 1000

    watched_items.record_check_result(conn, created.id, price=1200, extracted_title="VU Meter Pro")
    item = watched_items.get_watched_item(conn, created.id)
    assert item.current_price == 1200
    assert item.lowest_price_seen == 1000  # doesn't rise back up

    watched_items.record_check_result(conn, created.id, price=800, extracted_title="VU Meter Pro")
    item = watched_items.get_watched_item(conn, created.id)
    assert item.current_price == 800
    assert item.lowest_price_seen == 800


def test_record_check_result_with_no_price_clears_current_but_keeps_lowest(conn):
    created = watched_items.create_watched_item(conn, make_item())
    watched_items.record_check_result(conn, created.id, price=900, extracted_title="VU Meter Pro")

    watched_items.record_check_result(conn, created.id, price=None, extracted_title=None)

    item = watched_items.get_watched_item(conn, created.id)
    assert item.current_price is None
    assert item.lowest_price_seen == 900
    assert item.extracted_title == "VU Meter Pro"  # COALESCE keeps the last known title
    assert item.last_checked_at is not None


def test_record_check_result_stores_and_keeps_currency(conn):
    created = watched_items.create_watched_item(conn, make_item())
    watched_items.record_check_result(conn, created.id, price=900, extracted_title="VU Meter Pro", currency="USD")

    assert watched_items.get_watched_item(conn, created.id).currency == "USD"

    # A later check that doesn't resolve a currency (e.g. extraction failed)
    # keeps the last known one rather than blanking it out.
    watched_items.record_check_result(conn, created.id, price=None, extracted_title=None, currency=None)
    assert watched_items.get_watched_item(conn, created.id).currency == "USD"


def test_record_check_failure_increments_and_is_reset_by_success(conn):
    created = watched_items.create_watched_item(conn, make_item())

    assert watched_items.record_check_failure(conn, created.id) == 1
    assert watched_items.record_check_failure(conn, created.id) == 2
    assert watched_items.get_watched_item(conn, created.id).consecutive_check_failures == 2

    watched_items.record_check_result(conn, created.id, price=900, extracted_title="VU Meter Pro")

    item = watched_items.get_watched_item(conn, created.id)
    assert item.consecutive_check_failures == 0
    assert item.dead_alert_sent is False


def test_mark_dead_alert_sent(conn):
    created = watched_items.create_watched_item(conn, make_item())
    assert watched_items.get_watched_item(conn, created.id).dead_alert_sent is False

    watched_items.mark_dead_alert_sent(conn, created.id)

    assert watched_items.get_watched_item(conn, created.id).dead_alert_sent is True


def test_record_alert_and_set_linked_search_id(conn):
    created = watched_items.create_watched_item(conn, make_item())

    watched_items.record_alert(conn, created.id, 950)
    assert watched_items.get_watched_item(conn, created.id).last_alert_price == 950

    linked_search = searches.create_search(conn, Search(name="Find used: VU meter", scoring_mode="plain"))
    watched_items.set_linked_search_id(conn, created.id, linked_search.id)
    assert watched_items.get_watched_item(conn, created.id).linked_search_id == linked_search.id
