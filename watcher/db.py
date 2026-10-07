"""SQLite connection and schema management.

SQLite is the single source of truth for both container definitions (edited
via the admin UI) and listing/run/health state. WAL mode lets the scheduler's
background jobs and the admin UI's request handlers read/write concurrently
without lock contention for a workload this small.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS containers (
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
    container_id INTEGER NOT NULL REFERENCES containers(id) ON DELETE CASCADE,
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
    UNIQUE(container_id, source, external_id)
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    containers_processed INTEGER NOT NULL DEFAULT 0,
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


def connect(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn
