"""CRUD + check-run state for watched items (single-URL price watches),
backed by the ``watched_items`` table.

Mirrors the split searches.py/storage.py already uses for searches: the
admin-edited fields (name/url/target_price/check_frequency/find_used/enabled)
go through create_watched_item/update_watched_item, while fields mutated by
the price-watch check run itself (current_price/lowest_price_seen/
extracted_title/last_alert_price/last_checked_at/linked_search_id) go through
their own narrow setters below rather than a full-row replace, so a check run
can never clobber an admin edit made in between (or vice versa).
"""
from __future__ import annotations

import sqlite3
from typing import List, Optional

from watcher.models import WatchedItem

_EDITABLE_FIELDS = ("name", "url", "enabled", "target_price", "check_frequency", "find_used")


def _row_to_item(row: sqlite3.Row) -> WatchedItem:
    data = dict(row)
    data["enabled"] = bool(data["enabled"])
    data["find_used"] = bool(data["find_used"])
    data["dead_alert_sent"] = bool(data["dead_alert_sent"])
    return WatchedItem(**data)


def list_watched_items(conn: sqlite3.Connection, enabled_only: bool = False) -> List[WatchedItem]:
    query = "SELECT * FROM watched_items"
    if enabled_only:
        query += " WHERE enabled = 1"
    query += " ORDER BY name"
    return [_row_to_item(row) for row in conn.execute(query).fetchall()]


def get_watched_item(conn: sqlite3.Connection, item_id: int) -> Optional[WatchedItem]:
    row = conn.execute("SELECT * FROM watched_items WHERE id = ?", (item_id,)).fetchone()
    return _row_to_item(row) if row else None


def _editable_row(item: WatchedItem) -> dict:
    data = {field: getattr(item, field) for field in _EDITABLE_FIELDS}
    data["enabled"] = int(data["enabled"])
    data["find_used"] = int(data["find_used"])
    return data


def create_watched_item(conn: sqlite3.Connection, item: WatchedItem) -> WatchedItem:
    data = _editable_row(item)
    columns = ", ".join(data.keys())
    placeholders = ", ".join(["?"] * len(data))
    cursor = conn.execute(
        f"INSERT INTO watched_items ({columns}) VALUES ({placeholders})", list(data.values())
    )
    conn.commit()
    return get_watched_item(conn, cursor.lastrowid)


def update_watched_item(conn: sqlite3.Connection, item_id: int, item: WatchedItem) -> Optional[WatchedItem]:
    data = _editable_row(item)
    assignments = ", ".join(f"{key} = ?" for key in data)
    conn.execute(
        f"UPDATE watched_items SET {assignments}, updated_at = datetime('now') WHERE id = ?",
        list(data.values()) + [item_id],
    )
    conn.commit()
    return get_watched_item(conn, item_id)


def set_enabled(conn: sqlite3.Connection, item_id: int, enabled: bool) -> None:
    conn.execute(
        "UPDATE watched_items SET enabled = ?, updated_at = datetime('now') WHERE id = ?",
        (int(enabled), item_id),
    )
    conn.commit()


def delete_watched_item(conn: sqlite3.Connection, item_id: int) -> None:
    conn.execute("DELETE FROM watched_items WHERE id = ?", (item_id,))
    conn.commit()


def record_check_result(
    conn: sqlite3.Connection,
    item_id: int,
    price: Optional[int],
    extracted_title: Optional[str],
    currency: Optional[str] = None,
    description: Optional[str] = None,
    image_url: Optional[str] = None,
) -> None:
    """Updates check-run state after a *successful* check (the page was
    reachable and Claude could extract a product from it) - price=None here
    just means the item is currently out of stock, not that anything failed.
    current_price is cleared in that case (so an old price doesn't look
    still current) but lowest_price_seen is left untouched. currency,
    description and image_url are all kept (COALESCE) the same way
    extracted_title is - they shouldn't change check to check, and a check
    that doesn't find one (e.g. a page with no image) shouldn't blank out
    what an earlier check already confirmed (backlog #23's "is this the
    right item?" card). Resets the check-failure streak (see
    record_check_failure) - a genuinely failed check never reaches this
    function."""
    row = conn.execute(
        "SELECT lowest_price_seen FROM watched_items WHERE id = ?", (item_id,)
    ).fetchone()
    lowest = row["lowest_price_seen"] if row else None
    if price is not None:
        lowest = price if lowest is None else min(lowest, price)
    conn.execute(
        """
        UPDATE watched_items SET
            current_price = ?, extracted_title = COALESCE(?, extracted_title),
            extracted_description = COALESCE(?, extracted_description),
            extracted_image_url = COALESCE(?, extracted_image_url),
            currency = COALESCE(?, currency),
            lowest_price_seen = ?, last_checked_at = datetime('now'),
            consecutive_check_failures = 0, dead_alert_sent = 0
        WHERE id = ?
        """,
        (price, extracted_title, description, image_url, currency, lowest, item_id),
    )
    conn.commit()


def record_check_failure(conn: sqlite3.Connection, item_id: int) -> int:
    """Updates check-run state after a *failed* check (the page couldn't be
    fetched, or Claude couldn't extract a product from it) - mirrors
    source_health's consecutive_failures tracking for marketplaces (backlog
    #6: a watched item's URL never got the same "confirmed gone"/dead-link
    handling). Returns the new consecutive-failure count."""
    conn.execute(
        """
        UPDATE watched_items SET
            consecutive_check_failures = consecutive_check_failures + 1,
            last_checked_at = datetime('now')
        WHERE id = ?
        """,
        (item_id,),
    )
    conn.commit()
    row = conn.execute(
        "SELECT consecutive_check_failures FROM watched_items WHERE id = ?", (item_id,)
    ).fetchone()
    return row["consecutive_check_failures"]


def mark_dead_alert_sent(conn: sqlite3.Connection, item_id: int) -> None:
    conn.execute("UPDATE watched_items SET dead_alert_sent = 1 WHERE id = ?", (item_id,))
    conn.commit()


def record_alert(conn: sqlite3.Connection, item_id: int, alert_price: int) -> None:
    conn.execute("UPDATE watched_items SET last_alert_price = ? WHERE id = ?", (alert_price, item_id))
    conn.commit()


def set_linked_search_id(conn: sqlite3.Connection, item_id: int, search_id: Optional[int]) -> None:
    conn.execute("UPDATE watched_items SET linked_search_id = ? WHERE id = ?", (search_id, item_id))
    conn.commit()
