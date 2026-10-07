"""CRUD + defaults for per-marketplace runtime config (poll interval, request
delay, auth), backed by the ``marketplace_configs`` table.

One row per marketplace registered in ``watcher.marketplaces``. Rows are
created with code-level defaults on first boot (``ensure_defaults``) and from
then on are only ever edited through the admin UI - the registry itself
never changes these values.
"""
from __future__ import annotations

import json
import sqlite3
from typing import List, Optional

from watcher.marketplaces import MARKETPLACES
from watcher.models import MarketplaceConfig


def _row_to_config(row: sqlite3.Row) -> MarketplaceConfig:
    data = dict(row)
    data["auth"] = json.loads(data["auth"])
    return MarketplaceConfig(**data)


def ensure_defaults(conn: sqlite3.Connection) -> None:
    for marketplace in MARKETPLACES.values():
        conn.execute(
            """
            INSERT OR IGNORE INTO marketplace_configs (key, poll_interval_minutes, request_delay_seconds, auth)
            VALUES (?, ?, ?, '{}')
            """,
            (marketplace.key, marketplace.default_poll_interval_minutes, marketplace.default_request_delay_seconds),
        )
    conn.commit()


def list_configs(conn: sqlite3.Connection) -> List[MarketplaceConfig]:
    rows = conn.execute("SELECT * FROM marketplace_configs ORDER BY key").fetchall()
    return [_row_to_config(row) for row in rows]


def get_config(conn: sqlite3.Connection, key: str) -> Optional[MarketplaceConfig]:
    row = conn.execute("SELECT * FROM marketplace_configs WHERE key = ?", (key,)).fetchone()
    return _row_to_config(row) if row else None


def update_config(
    conn: sqlite3.Connection,
    key: str,
    poll_interval_minutes: int,
    request_delay_seconds: float,
    auth_updates: dict,
) -> MarketplaceConfig:
    """``auth_updates`` only overwrites keys with a non-empty value, so an
    admin form that leaves a secret field blank (to avoid re-displaying it)
    doesn't wipe the previously stored value."""
    current = get_config(conn, key)
    auth = dict(current.auth) if current else {}
    for field_key, value in auth_updates.items():
        if value:
            auth[field_key] = value

    conn.execute(
        """
        UPDATE marketplace_configs SET
            poll_interval_minutes = ?, request_delay_seconds = ?, auth = ?, updated_at = datetime('now')
        WHERE key = ?
        """,
        (poll_interval_minutes, request_delay_seconds, json.dumps(auth), key),
    )
    conn.commit()
    return get_config(conn, key)


def mark_fetched(conn: sqlite3.Connection, key: str) -> None:
    conn.execute(
        "UPDATE marketplace_configs SET last_fetch_at = datetime('now') WHERE key = ?",
        (key,),
    )
    conn.commit()
