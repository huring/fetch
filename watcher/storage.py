"""Listing dedupe/state, run tracking, source health and token-cost logging.

Score convention on the ``listings`` table:
  * NULL -> pending, still needs to go through prefilter + Claude scoring.
  * 0    -> deterministically excluded by the prefilter (never sent to Claude).
  * 1-10 -> Claude's assessment.

A listing that drops in price is reset back to NULL (pending) so it goes
through prefilter + scoring + notification again, per the dedupe rule that a
price drop counts as a "new" sighting.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import List, Optional

from watcher.models import Listing


def upsert_listing(conn: sqlite3.Connection, container_id: int, listing: Listing) -> int:
    row = conn.execute(
        "SELECT id, price, lowest_price FROM listings WHERE container_id = ? AND source = ? AND external_id = ?",
        (container_id, listing.source, listing.external_id),
    ).fetchone()

    if row is None:
        cursor = conn.execute(
            """
            INSERT INTO listings (
                container_id, source, external_id, title, description, url,
                price, lowest_price, location, ships, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                container_id,
                listing.source,
                listing.external_id,
                listing.title,
                listing.description,
                listing.url,
                listing.price,
                listing.price,
                listing.location,
                None if listing.ships is None else int(listing.ships),
                json.dumps(listing.raw),
            ),
        )
        conn.commit()
        return cursor.lastrowid

    listing_id = row["id"]
    price_dropped = (
        listing.price is not None
        and row["lowest_price"] is not None
        and listing.price < row["lowest_price"]
    )
    new_lowest = listing.price if price_dropped else row["lowest_price"]

    if price_dropped:
        conn.execute(
            """
            UPDATE listings SET
                title = ?, description = ?, url = ?, price = ?, lowest_price = ?,
                location = ?, ships = ?, raw_json = ?, last_seen_at = datetime('now'),
                score = NULL, reasoning = NULL, uncertain_specs = '[]', price_assessment = NULL,
                notified_instant_at = NULL, included_in_digest_at = NULL
            WHERE id = ?
            """,
            (
                listing.title,
                listing.description,
                listing.url,
                listing.price,
                new_lowest,
                listing.location,
                None if listing.ships is None else int(listing.ships),
                json.dumps(listing.raw),
                listing_id,
            ),
        )
    else:
        conn.execute(
            """
            UPDATE listings SET
                title = ?, description = ?, url = ?, price = ?,
                location = ?, ships = ?, raw_json = ?, last_seen_at = datetime('now')
            WHERE id = ?
            """,
            (
                listing.title,
                listing.description,
                listing.url,
                listing.price,
                listing.location,
                None if listing.ships is None else int(listing.ships),
                json.dumps(listing.raw),
                listing_id,
            ),
        )
    conn.commit()
    return listing_id


def get_pending_listings(conn: sqlite3.Connection, container_id: int) -> List[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM listings WHERE container_id = ? AND score IS NULL",
        (container_id,),
    ).fetchall()


def update_description(conn: sqlite3.Connection, listing_id: int, description: str) -> None:
    conn.execute("UPDATE listings SET description = ? WHERE id = ?", (description, listing_id))
    conn.commit()


def mark_prefiltered_out(conn: sqlite3.Connection, listing_id: int, reason: str) -> None:
    conn.execute(
        "UPDATE listings SET score = 0, reasoning = ? WHERE id = ?",
        (reason, listing_id),
    )
    conn.commit()


def mark_scored(
    conn: sqlite3.Connection,
    listing_id: int,
    score: int,
    reasoning: str,
    uncertain_specs: List[str],
    price_assessment: str,
) -> None:
    conn.execute(
        """
        UPDATE listings SET score = ?, reasoning = ?, uncertain_specs = ?, price_assessment = ?
        WHERE id = ?
        """,
        (score, reasoning, json.dumps(uncertain_specs), price_assessment, listing_id),
    )
    conn.commit()


def mark_notified_instant(conn: sqlite3.Connection, listing_id: int) -> None:
    conn.execute(
        "UPDATE listings SET notified_instant_at = datetime('now') WHERE id = ?",
        (listing_id,),
    )
    conn.commit()


def get_pending_digest(conn: sqlite3.Connection, score_min: int, score_max: int) -> List[sqlite3.Row]:
    return conn.execute(
        """
        SELECT listings.*, containers.name AS container_name
        FROM listings
        JOIN containers ON containers.id = listings.container_id
        WHERE listings.score BETWEEN ? AND ?
          AND listings.included_in_digest_at IS NULL
          AND listings.notified_instant_at IS NULL
        ORDER BY containers.name, listings.score DESC
        """,
        (score_min, score_max),
    ).fetchall()


def mark_digested(conn: sqlite3.Connection, listing_ids: List[int]) -> None:
    if not listing_ids:
        return
    placeholders = ", ".join(["?"] * len(listing_ids))
    conn.execute(
        f"UPDATE listings SET included_in_digest_at = datetime('now') WHERE id IN ({placeholders})",
        listing_ids,
    )
    conn.commit()


# --- Run tracking -----------------------------------------------------------

def start_run(conn: sqlite3.Connection) -> int:
    cursor = conn.execute(
        "INSERT INTO runs (started_at, status) VALUES (datetime('now'), 'running')"
    )
    conn.commit()
    return cursor.lastrowid


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    status: str,
    containers_processed: int = 0,
    listings_fetched: int = 0,
    listings_new: int = 0,
    listings_scored: int = 0,
    error_message: Optional[str] = None,
) -> None:
    conn.execute(
        """
        UPDATE runs SET
            finished_at = datetime('now'), status = ?, containers_processed = ?,
            listings_fetched = ?, listings_new = ?, listings_scored = ?, error_message = ?
        WHERE id = ?
        """,
        (status, containers_processed, listings_fetched, listings_new, listings_scored, error_message, run_id),
    )
    conn.commit()


def get_last_run(conn: sqlite3.Connection) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()


def get_last_completed_run(conn: sqlite3.Connection) -> Optional[sqlite3.Row]:
    """Like get_last_run, but skips a run still in progress - a run that's
    simply taking a few minutes to fetch/score isn't "unhealthy"."""
    return conn.execute(
        "SELECT * FROM runs WHERE status != 'running' ORDER BY id DESC LIMIT 1"
    ).fetchone()


# --- Source health -----------------------------------------------------------

def record_source_success(conn: sqlite3.Connection, source: str) -> None:
    conn.execute(
        """
        INSERT INTO source_health (source, consecutive_failures, last_error, last_success_at, alert_sent)
        VALUES (?, 0, NULL, datetime('now'), 0)
        ON CONFLICT(source) DO UPDATE SET
            consecutive_failures = 0, last_error = NULL,
            last_success_at = datetime('now'), alert_sent = 0
        """,
        (source,),
    )
    conn.commit()


def record_source_failure(conn: sqlite3.Connection, source: str, error_message: str) -> int:
    conn.execute(
        """
        INSERT INTO source_health (source, consecutive_failures, last_error, alert_sent)
        VALUES (?, 1, ?, 0)
        ON CONFLICT(source) DO UPDATE SET
            consecutive_failures = consecutive_failures + 1, last_error = ?
        """,
        (source, error_message, error_message),
    )
    conn.commit()
    row = conn.execute(
        "SELECT consecutive_failures FROM source_health WHERE source = ?", (source,)
    ).fetchone()
    return row["consecutive_failures"]


def should_send_health_alert(conn: sqlite3.Connection, source: str, threshold: int) -> bool:
    row = conn.execute(
        "SELECT consecutive_failures, alert_sent FROM source_health WHERE source = ?", (source,)
    ).fetchone()
    if row is None:
        return False
    return row["consecutive_failures"] >= threshold and not row["alert_sent"]


def mark_health_alert_sent(conn: sqlite3.Connection, source: str) -> None:
    conn.execute("UPDATE source_health SET alert_sent = 1 WHERE source = ?", (source,))
    conn.commit()


# --- Token usage / cost ------------------------------------------------------

def log_token_usage(
    conn: sqlite3.Connection,
    run_id: int,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cost_usd: float,
) -> None:
    conn.execute(
        """
        INSERT INTO token_usage (run_id, model, input_tokens, output_tokens, cost_usd)
        VALUES (?, ?, ?, ?, ?)
        """,
        (run_id, model, input_tokens, output_tokens, cost_usd),
    )
    conn.commit()


@dataclass
class MonthlyCost:
    month: str
    input_tokens: int
    output_tokens: int
    cost_usd: float


def monthly_cost_summary(conn: sqlite3.Connection) -> List[MonthlyCost]:
    rows = conn.execute(
        """
        SELECT strftime('%Y-%m', created_at) AS month,
               SUM(input_tokens) AS input_tokens,
               SUM(output_tokens) AS output_tokens,
               SUM(cost_usd) AS cost_usd
        FROM token_usage
        GROUP BY month
        ORDER BY month DESC
        """
    ).fetchall()
    return [
        MonthlyCost(
            month=row["month"],
            input_tokens=row["input_tokens"] or 0,
            output_tokens=row["output_tokens"] or 0,
            cost_usd=row["cost_usd"] or 0.0,
        )
        for row in rows
    ]
