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
from typing import Dict, List, Optional

from watcher.models import Listing


def upsert_listing(conn: sqlite3.Connection, search_id: int, listing: Listing) -> int:
    row = conn.execute(
        "SELECT id, price, lowest_price FROM listings WHERE search_id = ? AND source = ? AND external_id = ?",
        (search_id, listing.source, listing.external_id),
    ).fetchone()

    if row is None:
        cursor = conn.execute(
            """
            INSERT INTO listings (
                search_id, source, external_id, title, description, url,
                price, lowest_price, location, ships, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                search_id,
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


def get_pending_listings(conn: sqlite3.Connection, search_id: int) -> List[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM listings WHERE search_id = ? AND score IS NULL",
        (search_id,),
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
        SELECT listings.*, searches.name AS search_name
        FROM listings
        JOIN searches ON searches.id = listings.search_id
        WHERE listings.score BETWEEN ? AND ?
          AND listings.included_in_digest_at IS NULL
          AND listings.notified_instant_at IS NULL
        ORDER BY searches.name, listings.score DESC
        """,
        (score_min, score_max),
    ).fetchall()


BUCKETS = ("found", "summary", "threshold")


def _bucket_predicate(bucket: str, score_digest_min: int, score_instant_threshold: int) -> str:
    if bucket == "threshold":
        return f"score >= {score_instant_threshold}"
    if bucket == "summary":
        return f"score BETWEEN {score_digest_min} AND {score_instant_threshold - 1}"
    if bucket == "found":
        return "score IS NOT NULL AND score != 0"
    raise ValueError(f"Unknown bucket {bucket!r}")


def get_search_bucket_counts(
    conn: sqlite3.Connection, score_digest_min: int, score_instant_threshold: int
) -> Dict[int, Dict[str, int]]:
    """Counts of currently-active (not removed/sold) listings per search, for
    each of the three admin-UI buckets: "found" (passed the deterministic
    prefilter, i.e. within max-price/excluded-model thresholds, regardless of
    score), "summary" (scored in the digest range) and "threshold" (scored at
    or above the instant-notify threshold)."""
    rows = conn.execute(
        f"""
        SELECT
            search_id,
            SUM(CASE WHEN {_bucket_predicate('found', score_digest_min, score_instant_threshold)} THEN 1 ELSE 0 END) AS found,
            SUM(CASE WHEN {_bucket_predicate('summary', score_digest_min, score_instant_threshold)} THEN 1 ELSE 0 END) AS summary,
            SUM(CASE WHEN {_bucket_predicate('threshold', score_digest_min, score_instant_threshold)} THEN 1 ELSE 0 END) AS threshold
        FROM listings
        GROUP BY search_id
        """
    ).fetchall()
    return {
        row["search_id"]: {"found": row["found"], "summary": row["summary"], "threshold": row["threshold"]}
        for row in rows
    }


def list_bucket_listings(
    conn: sqlite3.Connection, search_id: int, bucket: str, score_digest_min: int, score_instant_threshold: int
) -> List[sqlite3.Row]:
    predicate = _bucket_predicate(bucket, score_digest_min, score_instant_threshold)
    return conn.execute(
        f"SELECT * FROM listings WHERE search_id = ? AND {predicate} ORDER BY score DESC, first_seen_at DESC",
        (search_id,),
    ).fetchall()


def get_scored_listings(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    """Every listing that was actually scored by Claude (1-10, not pending
    and not deterministically prefiltered out) across all searches, with its
    search's name - the set the daily liveness sweep checks. Listings never
    shown to the user (pending or prefiltered-out) aren't worth the extra
    request."""
    return conn.execute(
        """
        SELECT listings.*, searches.name AS search_name
        FROM listings
        JOIN searches ON searches.id = listings.search_id
        WHERE listings.score BETWEEN 1 AND 10
        """
    ).fetchall()


def insert_price_history(conn: sqlite3.Connection, search_name: str, listing_row: sqlite3.Row) -> None:
    conn.execute(
        """
        INSERT INTO price_history (search_name, source, external_id, title, price, lowest_price, score, first_seen_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            search_name,
            listing_row["source"],
            listing_row["external_id"],
            listing_row["title"],
            listing_row["price"],
            listing_row["lowest_price"],
            listing_row["score"],
            listing_row["first_seen_at"],
        ),
    )
    conn.commit()


def delete_listing(conn: sqlite3.Connection, listing_id: int) -> None:
    conn.execute("DELETE FROM listings WHERE id = ?", (listing_id,))
    conn.commit()


def mark_stale_notified(conn: sqlite3.Connection, listing_id: int) -> None:
    conn.execute("UPDATE listings SET stale_notified_at = datetime('now') WHERE id = ?", (listing_id,))
    conn.commit()


def mark_digested(conn: sqlite3.Connection, listing_ids: List[int]) -> None:
    if not listing_ids:
        return
    placeholders = ", ".join(["?"] * len(listing_ids))
    conn.execute(
        f"UPDATE listings SET included_in_digest_at = datetime('now') WHERE id IN ({placeholders})",
        listing_ids,
    )
    conn.commit()


def clear_operational_data(conn: sqlite3.Connection) -> None:
    """Deletes all accumulated listing/run/cost-tracking data - everything
    fetched/scored/logged so far - while leaving search definitions and
    marketplace configs (poll interval, auth) untouched. Used by the admin
    UI's "clear data" action, mainly to get a clean slate for re-testing
    after a change to what gets fetched or how it's scored."""
    conn.execute("DELETE FROM listings")
    conn.execute("DELETE FROM runs")
    conn.execute("DELETE FROM token_usage")
    conn.execute("DELETE FROM source_health")
    conn.execute("DELETE FROM price_history")
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
    searches_processed: int = 0,
    listings_fetched: int = 0,
    listings_new: int = 0,
    listings_scored: int = 0,
    error_message: Optional[str] = None,
) -> None:
    conn.execute(
        """
        UPDATE runs SET
            finished_at = datetime('now'), status = ?, searches_processed = ?,
            listings_fetched = ?, listings_new = ?, listings_scored = ?, error_message = ?
        WHERE id = ?
        """,
        (status, searches_processed, listings_fetched, listings_new, listings_scored, error_message, run_id),
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
