"""CRUD operations for searches, backed by the ``searches`` table."""
from __future__ import annotations

import json
import sqlite3
from typing import List, Optional

from watcher.models import BlocketQuery, Search, TraderaQuery, WatchedModel

_LIST_FIELDS = (
    "excluded_models",
    "excluded_words",
    "required_keywords",
)
_MODEL_LIST_FIELDS = {
    "watched_models": WatchedModel,
    "blocket_queries": BlocketQuery,
    "tradera_queries": TraderaQuery,
}
_STRING_LIST_FIELDS = ("hard_criteria", "soft_criteria")


def _row_to_search(row: sqlite3.Row) -> Search:
    data = dict(row)
    data["enabled"] = bool(data["enabled"])
    data["require_shipping"] = bool(data["require_shipping"])
    for key in _LIST_FIELDS + _STRING_LIST_FIELDS:
        data[key] = json.loads(data[key])
    for key, model_cls in _MODEL_LIST_FIELDS.items():
        data[key] = [model_cls(**item) for item in json.loads(data[key])]
    return Search(**data)


def _search_to_row(search: Search) -> dict:
    data = search.model_dump(exclude={"id", "created_at", "updated_at"})
    for key in _LIST_FIELDS + _STRING_LIST_FIELDS:
        data[key] = json.dumps(data[key])
    for key in _MODEL_LIST_FIELDS:
        data[key] = json.dumps(data[key])
    data["enabled"] = int(data["enabled"])
    data["require_shipping"] = int(data["require_shipping"])
    return data


def list_searches(conn: sqlite3.Connection, enabled_only: bool = False) -> List[Search]:
    query = "SELECT * FROM searches"
    if enabled_only:
        query += " WHERE enabled = 1"
    query += " ORDER BY name"
    rows = conn.execute(query).fetchall()
    return [_row_to_search(row) for row in rows]


def get_search(conn: sqlite3.Connection, search_id: int) -> Optional[Search]:
    row = conn.execute("SELECT * FROM searches WHERE id = ?", (search_id,)).fetchone()
    return _row_to_search(row) if row else None


def create_search(conn: sqlite3.Connection, search: Search) -> Search:
    data = _search_to_row(search)
    columns = ", ".join(data.keys())
    placeholders = ", ".join(["?"] * len(data))
    cursor = conn.execute(
        f"INSERT INTO searches ({columns}) VALUES ({placeholders})",
        list(data.values()),
    )
    conn.commit()
    return get_search(conn, cursor.lastrowid)


def update_search(conn: sqlite3.Connection, search_id: int, search: Search) -> Optional[Search]:
    data = _search_to_row(search)
    assignments = ", ".join(f"{key} = ?" for key in data)
    conn.execute(
        f"UPDATE searches SET {assignments}, updated_at = datetime('now') WHERE id = ?",
        list(data.values()) + [search_id],
    )
    conn.commit()
    return get_search(conn, search_id)


def set_enabled(conn: sqlite3.Connection, search_id: int, enabled: bool) -> None:
    conn.execute(
        "UPDATE searches SET enabled = ?, updated_at = datetime('now') WHERE id = ?",
        (int(enabled), search_id),
    )
    conn.commit()


def delete_search(conn: sqlite3.Connection, search_id: int) -> None:
    conn.execute("DELETE FROM searches WHERE id = ?", (search_id,))
    conn.commit()
