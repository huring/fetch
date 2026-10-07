"""SQLite connection and schema management.

SQLite is the single source of truth for both search definitions (edited via
the admin UI) and listing/run/health state. WAL mode lets the scheduler's
background jobs and the admin UI's request handlers read/write concurrently
without lock contention for a workload this small.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS searches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    enabled INTEGER NOT NULL DEFAULT 1,
    scope TEXT NOT NULL DEFAULT 'local',
    location TEXT NOT NULL DEFAULT '',
    require_shipping INTEGER NOT NULL DEFAULT 0,
    max_price INTEGER,
    excluded_models TEXT NOT NULL DEFAULT '[]',
    excluded_words TEXT NOT NULL DEFAULT '[]',
    required_keywords TEXT NOT NULL DEFAULT '[]',
    watched_models TEXT NOT NULL DEFAULT '[]',
    hard_criteria TEXT NOT NULL DEFAULT '[]',
    soft_criteria TEXT NOT NULL DEFAULT '[]',
    blocket_queries TEXT NOT NULL DEFAULT '[]',
    tradera_queries TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS listings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    search_id INTEGER NOT NULL REFERENCES searches(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    external_id TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    price INTEGER,
    lowest_price INTEGER,
    location TEXT,
    ships INTEGER,
    first_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
    score INTEGER,
    reasoning TEXT,
    uncertain_specs TEXT NOT NULL DEFAULT '[]',
    price_assessment TEXT,
    notified_instant_at TEXT,
    included_in_digest_at TEXT,
    raw_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(search_id, source, external_id)
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    searches_processed INTEGER NOT NULL DEFAULT 0,
    listings_fetched INTEGER NOT NULL DEFAULT 0,
    listings_new INTEGER NOT NULL DEFAULT 0,
    listings_scored INTEGER NOT NULL DEFAULT 0,
    error_message TEXT
);

CREATE TABLE IF NOT EXISTS source_health (
    source TEXT PRIMARY KEY,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    last_success_at TEXT,
    alert_sent INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS token_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER REFERENCES runs(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cost_usd REAL NOT NULL
);
"""


def _migrate_legacy_names(conn: sqlite3.Connection) -> None:
    """One-time rename for databases created before "container" became
    "search" (table ``containers`` -> ``searches``, ``listings.container_id``
    -> ``listings.search_id``, ``runs.containers_processed`` ->
    ``runs.searches_processed``). Safe to run on a fresh DB (no-op) or an
    already-migrated one (no-op)."""
    tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    if "containers" in tables and "searches" not in tables:
        conn.execute("ALTER TABLE containers RENAME TO searches")

    if "listings" in tables:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(listings)")}
        if "container_id" in columns and "search_id" not in columns:
            conn.execute("ALTER TABLE listings RENAME COLUMN container_id TO search_id")

    if "runs" in tables:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(runs)")}
        if "containers_processed" in columns and "searches_processed" not in columns:
            conn.execute("ALTER TABLE runs RENAME COLUMN containers_processed TO searches_processed")

    conn.commit()


def connect(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _migrate_legacy_names(conn)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn
