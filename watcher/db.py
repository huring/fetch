"""SQLite connection and schema management.

SQLite is the single source of truth for search definitions, marketplace
config (edited via the admin UI), and listing/run/health state. WAL mode lets
the scheduler's background jobs and the admin UI's request handlers
read/write concurrently without lock contention for a workload this small.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import List

SCHEMA = """
CREATE TABLE IF NOT EXISTS searches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    enabled INTEGER NOT NULL DEFAULT 1,
    scope TEXT NOT NULL DEFAULT 'local',
    location TEXT NOT NULL DEFAULT '',
    require_shipping INTEGER NOT NULL DEFAULT 0,
    max_price INTEGER,
    min_price INTEGER,
    scoring_mode TEXT NOT NULL DEFAULT 'rated',
    instant_alert_price INTEGER,
    digest_style TEXT NOT NULL DEFAULT 'itemized',
    excluded_models TEXT NOT NULL DEFAULT '[]',
    excluded_words TEXT NOT NULL DEFAULT '[]',
    required_keywords TEXT NOT NULL DEFAULT '[]',
    watched_models TEXT NOT NULL DEFAULT '[]',
    hard_criteria TEXT NOT NULL DEFAULT '[]',
    soft_criteria TEXT NOT NULL DEFAULT '[]',
    search_phrases TEXT NOT NULL DEFAULT '[]',
    marketplaces TEXT NOT NULL DEFAULT '[]',
    creation_prompt TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS marketplace_configs (
    key TEXT PRIMARY KEY,
    poll_interval_minutes INTEGER NOT NULL,
    request_delay_seconds REAL NOT NULL,
    auth TEXT NOT NULL DEFAULT '{}',
    last_fetch_at TEXT,
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
    stale_notified_at TEXT,
    auction_ends_at TEXT,
    image_url TEXT,
    distance_km REAL,
    raw_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(search_id, source, external_id)
);

CREATE TABLE IF NOT EXISTS price_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    search_name TEXT NOT NULL,
    source TEXT NOT NULL,
    external_id TEXT NOT NULL,
    title TEXT NOT NULL,
    price INTEGER,
    lowest_price INTEGER,
    score INTEGER,
    first_seen_at TEXT,
    removed_at TEXT NOT NULL DEFAULT (datetime('now'))
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

CREATE TABLE IF NOT EXISTS scoring_batches (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'in_progress',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS scoring_batch_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL REFERENCES scoring_batches(id) ON DELETE CASCADE,
    custom_id TEXT NOT NULL,
    listing_index INTEGER NOT NULL,
    listing_id INTEGER NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
    UNIQUE(batch_id, custom_id, listing_index)
);

CREATE TABLE IF NOT EXISTS watched_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    url TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    target_price INTEGER,
    check_frequency TEXT NOT NULL DEFAULT 'daily',
    find_used INTEGER NOT NULL DEFAULT 0,
    linked_search_id INTEGER REFERENCES searches(id) ON DELETE SET NULL,
    extracted_title TEXT,
    extracted_description TEXT,
    extracted_image_url TEXT,
    currency TEXT,
    current_price INTEGER,
    in_stock INTEGER,
    lowest_price_seen INTEGER,
    last_alert_price INTEGER,
    last_checked_at TEXT,
    consecutive_check_failures INTEGER NOT NULL DEFAULT 0,
    dead_alert_sent INTEGER NOT NULL DEFAULT 0,
    last_error_status INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
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


def _migrate_blocket_tradera_queries(conn: sqlite3.Connection) -> None:
    """One-time collapse of the old fixed ``blocket_queries``/``tradera_queries``
    columns into the generic ``marketplace_queries`` JSON column used by the
    marketplace registry. Tradera queries are dropped (Tradera support was
    removed); Blocket queries carry over under the "blocket" key."""
    tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "searches" not in tables:
        return

    columns = {row["name"] for row in conn.execute("PRAGMA table_info(searches)")}
    if "marketplace_queries" in columns:
        return  # already migrated (or a fresh DB that never had the old columns)

    conn.execute("ALTER TABLE searches ADD COLUMN marketplace_queries TEXT NOT NULL DEFAULT '{}'")

    if "blocket_queries" in columns:
        rows = conn.execute("SELECT id, blocket_queries FROM searches").fetchall()
        for row in rows:
            blocket_queries = json.loads(row["blocket_queries"] or "[]")
            marketplace_queries = {"blocket": blocket_queries} if blocket_queries else {}
            conn.execute(
                "UPDATE searches SET marketplace_queries = ? WHERE id = ?",
                (json.dumps(marketplace_queries), row["id"]),
            )
        conn.execute("ALTER TABLE searches DROP COLUMN blocket_queries")

    if "tradera_queries" in columns:
        conn.execute("ALTER TABLE searches DROP COLUMN tradera_queries")

    conn.commit()


def _migrate_marketplace_queries_to_search_phrases(conn: sqlite3.Connection) -> None:
    """One-time collapse of the per-marketplace ``marketplace_queries`` JSON
    column into two generic columns: ``search_phrases`` (the union of every
    "q" value across all marketplaces that search used, in order, deduped)
    and ``marketplaces`` (which registry keys had any queries at all). This
    trades per-marketplace query variations for a single shared phrase list -
    acceptable since every search in practice only ever used one marketplace
    (Blocket) with identical phrasing."""
    tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "searches" not in tables:
        return

    columns = {row["name"] for row in conn.execute("PRAGMA table_info(searches)")}
    if "search_phrases" in columns:
        return  # already migrated (or a fresh DB that never had the old column)

    conn.execute("ALTER TABLE searches ADD COLUMN search_phrases TEXT NOT NULL DEFAULT '[]'")
    conn.execute("ALTER TABLE searches ADD COLUMN marketplaces TEXT NOT NULL DEFAULT '[]'")

    if "marketplace_queries" in columns:
        rows = conn.execute("SELECT id, marketplace_queries FROM searches").fetchall()
        for row in rows:
            marketplace_queries = json.loads(row["marketplace_queries"] or "{}")
            phrases: List[str] = []
            for queries in marketplace_queries.values():
                for raw_query in queries:
                    q = raw_query.get("q")
                    if q and q not in phrases:
                        phrases.append(q)
            marketplace_keys = sorted(key for key, queries in marketplace_queries.items() if queries)
            conn.execute(
                "UPDATE searches SET search_phrases = ?, marketplaces = ? WHERE id = ?",
                (json.dumps(phrases), json.dumps(marketplace_keys), row["id"]),
            )
        conn.execute("ALTER TABLE searches DROP COLUMN marketplace_queries")

    conn.commit()


def _migrate_add_stale_notified_at(conn: sqlite3.Connection) -> None:
    """Adds the stale_notified_at column (daily liveness sweep's "already
    nudged about this one" flag) to a listings table created before it
    existed. No-op on a fresh DB or an already-migrated one."""
    tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "listings" not in tables:
        return
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(listings)")}
    if "stale_notified_at" not in columns:
        conn.execute("ALTER TABLE listings ADD COLUMN stale_notified_at TEXT")
        conn.commit()


def _migrate_add_min_price(conn: sqlite3.Connection) -> None:
    """Adds the min_price column (deterministic prefilter floor) to a
    searches table created before it existed. No-op on a fresh DB or an
    already-migrated one."""
    tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "searches" not in tables:
        return
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(searches)")}
    if "min_price" not in columns:
        conn.execute("ALTER TABLE searches ADD COLUMN min_price INTEGER")
        conn.commit()


def _migrate_add_scoring_mode(conn: sqlite3.Connection) -> None:
    """Adds the scoring_mode/instant_alert_price columns to a searches table
    created before "plain" (no-AI) searches existed. scoring_mode backfills
    to 'rated' (not the Search model's own 'plain' default) so every
    already-configured search - which relied on Claude scoring before this
    column existed - keeps doing exactly that; only a freshly created search
    defaults to 'plain'. No-op on a fresh DB or an already-migrated one."""
    tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "searches" not in tables:
        return
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(searches)")}
    if "scoring_mode" not in columns:
        conn.execute("ALTER TABLE searches ADD COLUMN scoring_mode TEXT NOT NULL DEFAULT 'rated'")
    if "instant_alert_price" not in columns:
        conn.execute("ALTER TABLE searches ADD COLUMN instant_alert_price INTEGER")
    conn.commit()


def _migrate_add_digest_style(conn: sqlite3.Connection) -> None:
    """Adds the digest_style column to a searches table created before
    per-search digest formatting existed. Backfills to 'itemized' - the
    behavior every existing search already has - so nothing changes for an
    already-configured search; only a freshly created search's admin-UI
    form defaults its toggle to 'summary_link'. No-op on a fresh DB or an
    already-migrated one."""
    tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "searches" not in tables:
        return
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(searches)")}
    if "digest_style" not in columns:
        conn.execute("ALTER TABLE searches ADD COLUMN digest_style TEXT NOT NULL DEFAULT 'itemized'")
        conn.commit()


def _migrate_add_auction_ends_at(conn: sqlite3.Connection) -> None:
    """Adds the auction_ends_at column (an auction marketplace's hard
    deadline, see models.Listing) to a listings table created before auction
    marketplaces existed. No-op on a fresh DB or an already-migrated one."""
    tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "listings" not in tables:
        return
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(listings)")}
    if "auction_ends_at" not in columns:
        conn.execute("ALTER TABLE listings ADD COLUMN auction_ends_at TEXT")
        conn.commit()


def _migrate_add_listing_image_url(conn: sqlite3.Connection) -> None:
    """Adds image_url (see models.Listing) to a listings table created
    before it existed - a thumbnail URL straight from the marketplace's own
    response, used by the "Top ads" cards on the searches page. No-op on a
    fresh DB or an already-migrated one."""
    tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "listings" not in tables:
        return
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(listings)")}
    if "image_url" not in columns:
        conn.execute("ALTER TABLE listings ADD COLUMN image_url TEXT")
        conn.commit()


def _migrate_add_listing_distance_km(conn: sqlite3.Connection) -> None:
    """Adds distance_km (see models.Listing) to a listings table created
    before it existed - distance from Lars's own location, only populated
    for a "plain" search's Blocket listings (see marketplaces._blocket_fetch),
    used to sort those the same way Blocket's own "Closest" filter would.
    No-op on a fresh DB or an already-migrated one."""
    tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "listings" not in tables:
        return
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(listings)")}
    if "distance_km" not in columns:
        conn.execute("ALTER TABLE listings ADD COLUMN distance_km REAL")
        conn.commit()


def _migrate_add_search_creation_prompt(conn: sqlite3.Connection) -> None:
    """Adds creation_prompt (see models.Search) to a searches table created
    before it existed - the free-text prompt a search was generated from, if
    it was built with the NLP search builder rather than the manual form.
    No-op on a fresh DB or an already-migrated one."""
    tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "searches" not in tables:
        return
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(searches)")}
    if "creation_prompt" not in columns:
        conn.execute("ALTER TABLE searches ADD COLUMN creation_prompt TEXT")
        conn.commit()


def _migrate_add_watched_item_currency(conn: sqlite3.Connection) -> None:
    """Adds the currency column to a watched_items table created before
    price_watch.py tracked it (it previously only ever showed "SEK" in
    Slack alerts regardless of what the page actually said). No-op on a
    fresh DB or an already-migrated one."""
    tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "watched_items" not in tables:
        return
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(watched_items)")}
    if "currency" not in columns:
        conn.execute("ALTER TABLE watched_items ADD COLUMN currency TEXT")
        conn.commit()


def _migrate_add_watched_item_failure_tracking(conn: sqlite3.Connection) -> None:
    """Adds consecutive_check_failures/dead_alert_sent to a watched_items
    table created before check failures were tracked (backlog #6 - a
    watched item's URL never got the same "confirmed gone"/dead-link
    handling a marketplace's own source_health tracking already has). No-op
    on a fresh DB or an already-migrated one."""
    tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "watched_items" not in tables:
        return
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(watched_items)")}
    if "consecutive_check_failures" not in columns:
        conn.execute("ALTER TABLE watched_items ADD COLUMN consecutive_check_failures INTEGER NOT NULL DEFAULT 0")
    if "dead_alert_sent" not in columns:
        conn.execute("ALTER TABLE watched_items ADD COLUMN dead_alert_sent INTEGER NOT NULL DEFAULT 0")
    conn.commit()


def _migrate_add_watched_item_enrichment(conn: sqlite3.Connection) -> None:
    """Adds extracted_description/extracted_image_url to a watched_items
    table created before the "confirm this is the right item" card existed
    (backlog #23) - a short description and a link to the product's own
    image (never downloaded/stored, just the URL), captured from the page
    alongside the title/price every check already extracts. No-op on a
    fresh DB or an already-migrated one."""
    tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "watched_items" not in tables:
        return
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(watched_items)")}
    if "extracted_description" not in columns:
        conn.execute("ALTER TABLE watched_items ADD COLUMN extracted_description TEXT")
    if "extracted_image_url" not in columns:
        conn.execute("ALTER TABLE watched_items ADD COLUMN extracted_image_url TEXT")
    conn.commit()


def _migrate_add_watched_item_in_stock(conn: sqlite3.Connection) -> None:
    """Adds in_stock to a watched_items table created before it existed -
    without it, "genuinely out of stock as of the last check" (current_price
    is None *because* of this) looked identical in the admin UI to "never
    successfully checked yet" (current_price is also None). No-op on a
    fresh DB or an already-migrated one."""
    tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "watched_items" not in tables:
        return
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(watched_items)")}
    if "in_stock" not in columns:
        conn.execute("ALTER TABLE watched_items ADD COLUMN in_stock INTEGER")
        conn.commit()


def _migrate_add_watched_item_last_error_status(conn: sqlite3.Connection) -> None:
    """Adds last_error_status to a watched_items table created before it
    existed - the most recent failed check's HTTP status code, so the admin
    UI can show a distinct "blocked" badge for 403/429 (confirmed live,
    2026-10, on a Shopify storefront that rate-limits automated requests)
    instead of lumping it in with every other failure reason. No-op on a
    fresh DB or an already-migrated one."""
    tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "watched_items" not in tables:
        return
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(watched_items)")}
    if "last_error_status" not in columns:
        conn.execute("ALTER TABLE watched_items ADD COLUMN last_error_status INTEGER")
        conn.commit()


def connect(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _migrate_legacy_names(conn)
    _migrate_blocket_tradera_queries(conn)
    _migrate_marketplace_queries_to_search_phrases(conn)
    _migrate_add_stale_notified_at(conn)
    _migrate_add_min_price(conn)
    _migrate_add_scoring_mode(conn)
    _migrate_add_digest_style(conn)
    _migrate_add_auction_ends_at(conn)
    _migrate_add_listing_image_url(conn)
    _migrate_add_listing_distance_km(conn)
    _migrate_add_search_creation_prompt(conn)
    _migrate_add_watched_item_currency(conn)
    _migrate_add_watched_item_failure_tracking(conn)
    _migrate_add_watched_item_enrichment(conn)
    _migrate_add_watched_item_in_stock(conn)
    _migrate_add_watched_item_last_error_status(conn)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn
