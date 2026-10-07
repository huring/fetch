"""CRUD operations for search containers, backed by the ``containers`` table."""
from __future__ import annotations

import json
import sqlite3
from typing import List, Optional

from watcher.models import BlocketQuery, Container, TraderaQuery, WatchedModel

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


def _row_to_container(row: sqlite3.Row) -> Container:
    data = dict(row)
    data["enabled"] = bool(data["enabled"])
    data["require_shipping"] = bool(data["require_shipping"])
    for key in _LIST_FIELDS + _STRING_LIST_FIELDS:
        data[key] = json.loads(data[key])
    for key, model_cls in _MODEL_LIST_FIELDS.items():
        data[key] = [model_cls(**item) for item in json.loads(data[key])]
    return Container(**data)


def _container_to_row(container: Container) -> dict:
    data = container.model_dump(exclude={"id", "created_at", "updated_at"})
    for key in _LIST_FIELDS + _STRING_LIST_FIELDS:
        data[key] = json.dumps(data[key])
    for key in _MODEL_LIST_FIELDS:
        data[key] = json.dumps(data[key])
    data["enabled"] = int(data["enabled"])
    data["require_shipping"] = int(data["require_shipping"])
    return data


def list_containers(conn: sqlite3.Connection, enabled_only: bool = False) -> List[Container]:
    query = "SELECT * FROM containers"
    if enabled_only:
        query += " WHERE enabled = 1"
    query += " ORDER BY name"
    rows = conn.execute(query).fetchall()
    return [_row_to_container(row) for row in rows]


def get_container(conn: sqlite3.Connection, container_id: int) -> Optional[Container]:
    row = conn.execute("SELECT * FROM containers WHERE id = ?", (container_id,)).fetchone()
    return _row_to_container(row) if row else None


def create_container(conn: sqlite3.Connection, container: Container) -> Container:
    data = _container_to_row(container)
    columns = ", ".join(data.keys())
    placeholders = ", ".join(["?"] * len(data))
    cursor = conn.execute(
        f"INSERT INTO containers ({columns}) VALUES ({placeholders})",
        list(data.values()),
    )
    conn.commit()
    return get_container(conn, cursor.lastrowid)


def update_container(conn: sqlite3.Connection, container_id: int, container: Container) -> Optional[Container]:
    data = _container_to_row(container)
    assignments = ", ".join(f"{key} = ?" for key in data)
    conn.execute(
        f"UPDATE containers SET {assignments}, updated_at = datetime('now') WHERE id = ?",
        list(data.values()) + [container_id],
    )
    conn.commit()
    return get_container(conn, container_id)


def set_enabled(conn: sqlite3.Connection, container_id: int, enabled: bool) -> None:
    conn.execute(
        "UPDATE containers SET enabled = ?, updated_at = datetime('now') WHERE id = ?",
        (int(enabled), container_id),
    )
    conn.commit()


def delete_container(conn: sqlite3.Connection, container_id: int) -> None:
    conn.execute("DELETE FROM containers WHERE id = ?", (container_id,))
    conn.commit()
