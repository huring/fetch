"""Verifies the legacy container->search rename preserves data from a
database created by a pre-rename version of this app (i.e. your actual
running deployment's /data volume)."""
import sqlite3

from watcher import db, searches
from watcher.models import Search


def _create_legacy_schema(path: str) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE containers (
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
        CREATE TABLE listings (
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
        CREATE TABLE runs (
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
        """
    )
    conn.execute(
        """
        INSERT INTO containers (name, max_price, blocket_queries, tradera_queries)
        VALUES ('Living room - AV receiver', 3000, '[{"q": "onkyo tx-nr"}]', '[{"query": "onkyo receiver"}]')
        """
    )
    conn.execute(
        """
        INSERT INTO listings (container_id, source, external_id, title, price, score)
        VALUES (1, 'blocket', 'abc123', 'Onkyo TX-NR656', 2000, 9)
        """
    )
    conn.execute(
        "INSERT INTO runs (started_at, status, containers_processed) VALUES (datetime('now'), 'ok', 1)"
    )
    conn.commit()
    conn.close()


def test_legacy_database_migrates_and_preserves_data(tmp_path):
    db_path = str(tmp_path / "legacy.db")
    _create_legacy_schema(db_path)

    conn = db.connect(db_path)

    all_searches = searches.list_searches(conn)
    assert len(all_searches) == 1
    assert all_searches[0].name == "Living room - AV receiver"
    assert all_searches[0].max_price == 3000
    # Blocket's query text carries over into the shared search_phrases list and
    # "blocket" into marketplaces; Tradera queries are dropped (Tradera support
    # was removed) rather than migrated.
    assert all_searches[0].search_phrases == ["onkyo tx-nr"]
    assert all_searches[0].marketplaces == ["blocket"]

    listing_row = conn.execute("SELECT * FROM listings").fetchone()
    assert listing_row["search_id"] == all_searches[0].id
    assert listing_row["title"] == "Onkyo TX-NR656"
    assert listing_row["score"] == 9

    run_row = conn.execute("SELECT * FROM runs").fetchone()
    assert run_row["searches_processed"] == 1


def _create_marketplace_queries_schema(path: str) -> None:
    """Recreates the schema from the marketplace-registry refactor (one
    generation newer than _create_legacy_schema above): searches already
    renamed, blocket_queries/tradera_queries already collapsed into the
    per-marketplace marketplace_queries JSON column."""
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE searches (
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
            marketplace_queries TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE listings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            search_id INTEGER NOT NULL REFERENCES searches(id) ON DELETE CASCADE,
            source TEXT NOT NULL,
            external_id TEXT NOT NULL,
            UNIQUE(search_id, source, external_id)
        );
        CREATE TABLE runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL DEFAULT 'running',
            searches_processed INTEGER NOT NULL DEFAULT 0
        );
        """
    )
    conn.execute(
        """
        INSERT INTO searches (name, marketplace_queries)
        VALUES ('Living room - AV receiver', ?)
        """,
        ('{"blocket": [{"q": "onkyo tx-nr"}, {"q": "marantz sr"}]}',),
    )
    conn.commit()
    conn.close()


def test_marketplace_queries_migrates_to_search_phrases(tmp_path):
    db_path = str(tmp_path / "pre_search_phrases.db")
    _create_marketplace_queries_schema(db_path)

    conn = db.connect(db_path)

    all_searches = searches.list_searches(conn)
    assert len(all_searches) == 1
    assert all_searches[0].search_phrases == ["onkyo tx-nr", "marantz sr"]
    assert all_searches[0].marketplaces == ["blocket"]


def test_stale_notified_at_column_added_to_pre_existing_listings(tmp_path):
    db_path = str(tmp_path / "pre_stale_column.db")
    _create_marketplace_queries_schema(db_path)  # listings table predates stale_notified_at too

    conn = db.connect(db_path)

    columns = {row["name"] for row in conn.execute("PRAGMA table_info(listings)")}
    assert "stale_notified_at" in columns


def test_migration_is_idempotent_on_already_migrated_db(tmp_path):
    db_path = str(tmp_path / "fresh.db")
    conn = db.connect(db_path)  # fresh DB, created with new schema directly
    searches.create_search(conn, Search(name="Test"))
    conn.close()

    # Reconnecting re-runs the migration check - should be a no-op.
    conn2 = db.connect(db_path)
    assert len(searches.list_searches(conn2)) == 1
