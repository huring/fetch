"""Listing dedupe/state, run tracking, source health and token-cost logging.

Score convention on the ``listings`` table:
  * NULL -> pending, still needs to go through prefilter + Claude scoring.
  * 0    -> deterministically excluded by the prefilter (never sent to Claude).
  * 1-10 -> Claude's assessment (only for a "rated" search).
  * -1   -> passed the deterministic prefilter for a "plain" (no-AI) search -
            see PLAIN_SURFACED_SCORE. Never sent to Claude at all.

A listing that drops in price is reset back to NULL (pending) so it goes
through prefilter + scoring + notification again, per the dedupe rule that a
price drop counts as a "new" sighting - this applies equally to plain-mode
listings, which get a fresh chance to pass the prefilter and clear
instant_alert_price.
"""
from __future__ import annotations

import datetime
import json
import sqlite3
from dataclasses import dataclass
from typing import Dict, List, Optional

from watcher.models import Listing


def _format_dt(dt: Optional[datetime.datetime]) -> Optional[str]:
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt is not None else None


def upsert_listing(conn: sqlite3.Connection, search_id: int, listing: Listing) -> int:
    row = conn.execute(
        "SELECT id, price, lowest_price FROM listings WHERE search_id = ? AND source = ? AND external_id = ?",
        (search_id, listing.source, listing.external_id),
    ).fetchone()
    auction_ends_at = _format_dt(listing.auction_ends_at)

    if row is None:
        cursor = conn.execute(
            """
            INSERT INTO listings (
                search_id, source, external_id, title, description, url,
                price, lowest_price, location, ships, auction_ends_at, image_url, distance_km, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                auction_ends_at,
                listing.image_url,
                listing.distance_km,
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
                location = ?, ships = ?, auction_ends_at = ?, image_url = ?, distance_km = ?, raw_json = ?, last_seen_at = datetime('now'),
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
                auction_ends_at,
                listing.image_url,
                listing.distance_km,
                json.dumps(listing.raw),
                listing_id,
            ),
        )
    else:
        conn.execute(
            """
            UPDATE listings SET
                title = ?, description = ?, url = ?, price = ?,
                location = ?, ships = ?, auction_ends_at = ?, image_url = ?, distance_km = ?, raw_json = ?, last_seen_at = datetime('now')
            WHERE id = ?
            """,
            (
                listing.title,
                listing.description,
                listing.url,
                listing.price,
                listing.location,
                None if listing.ships is None else int(listing.ships),
                auction_ends_at,
                listing.image_url,
                listing.distance_km,
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


def get_unbatched_pending_listings(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    """Every pending listing (score IS NULL) across every search that isn't
    already part of an in-progress scoring batch - the set the scoring
    submit sweep (watcher/pipeline.py's submit_pending_scoring) picks up."""
    return conn.execute(
        """
        SELECT listings.* FROM listings
        WHERE listings.score IS NULL
          AND NOT EXISTS (
              SELECT 1 FROM scoring_batch_items sbi
              JOIN scoring_batches sb ON sb.id = sbi.batch_id
              WHERE sbi.listing_id = listings.id AND sb.status = 'in_progress'
          )
        """
    ).fetchall()


def create_scoring_batch(conn: sqlite3.Connection, batch_id: str, items: List[tuple]) -> None:
    """items: an iterable of (custom_id, listing_index, listing_id) tuples,
    one per listing included in the submitted batch."""
    conn.execute("INSERT INTO scoring_batches (id, status) VALUES (?, 'in_progress')", (batch_id,))
    conn.executemany(
        "INSERT INTO scoring_batch_items (batch_id, custom_id, listing_index, listing_id) VALUES (?, ?, ?, ?)",
        [(batch_id, custom_id, listing_index, listing_id) for custom_id, listing_index, listing_id in items],
    )
    conn.commit()


def get_in_progress_batch_ids(conn: sqlite3.Connection) -> List[str]:
    return [row["id"] for row in conn.execute("SELECT id FROM scoring_batches WHERE status = 'in_progress'").fetchall()]


def get_batch_items(conn: sqlite3.Connection, batch_id: str) -> Dict[tuple, int]:
    rows = conn.execute(
        "SELECT custom_id, listing_index, listing_id FROM scoring_batch_items WHERE batch_id = ?", (batch_id,)
    ).fetchall()
    return {(row["custom_id"], row["listing_index"]): row["listing_id"] for row in rows}


def delete_scoring_batch(conn: sqlite3.Connection, batch_id: str) -> None:
    conn.execute("DELETE FROM scoring_batches WHERE id = ?", (batch_id,))
    conn.commit()


def count_listings_awaiting_scoring(conn: sqlite3.Connection) -> int:
    """Pending listings (score IS NULL) not yet submitted in a scoring
    batch - will be picked up by the next submit sweep."""
    return conn.execute(
        """
        SELECT COUNT(*) AS n FROM listings
        WHERE score IS NULL
          AND NOT EXISTS (
              SELECT 1 FROM scoring_batch_items sbi
              JOIN scoring_batches sb ON sb.id = sbi.batch_id
              WHERE sbi.listing_id = listings.id AND sb.status = 'in_progress'
          )
        """
    ).fetchone()["n"]


def count_listings_in_progress_scoring(conn: sqlite3.Connection) -> int:
    """Listings currently submitted in a scoring batch awaiting results."""
    return conn.execute(
        """
        SELECT COUNT(DISTINCT sbi.listing_id) AS n
        FROM scoring_batch_items sbi
        JOIN scoring_batches sb ON sb.id = sbi.batch_id
        WHERE sb.status = 'in_progress'
        """
    ).fetchone()["n"]


def get_listing_with_search_name(conn: sqlite3.Connection, listing_id: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        """
        SELECT listings.*, searches.name AS search_name
        FROM listings JOIN searches ON searches.id = listings.search_id
        WHERE listings.id = ?
        """,
        (listing_id,),
    ).fetchone()


def update_description(conn: sqlite3.Connection, listing_id: int, description: str) -> None:
    conn.execute("UPDATE listings SET description = ? WHERE id = ?", (description, listing_id))
    conn.commit()


def mark_prefiltered_out(conn: sqlite3.Connection, listing_id: int, reason: str) -> None:
    conn.execute(
        "UPDATE listings SET score = 0, reasoning = ? WHERE id = ?",
        (reason, listing_id),
    )
    conn.commit()


PLAIN_SURFACED_SCORE = -1


def mark_surfaced_plain(conn: sqlite3.Connection, listing_id: int) -> None:
    """A "plain" (no-AI) search's listing passed the deterministic prefilter -
    there's no Claude call to wait for, so it's immediately surfaced."""
    conn.execute(
        "UPDATE listings SET score = ? WHERE id = ?",
        (PLAIN_SURFACED_SCORE, listing_id),
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
        SELECT listings.*, searches.name AS search_name, searches.digest_style AS search_digest_style
        FROM listings
        JOIN searches ON searches.id = listings.search_id
        WHERE listings.score BETWEEN ? AND ?
          AND listings.included_in_digest_at IS NULL
          AND listings.notified_instant_at IS NULL
        ORDER BY searches.name, listings.score DESC
        """,
        (score_min, score_max),
    ).fetchall()


def get_pending_plain_digest(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    """Plain-mode listings (surfaced by the deterministic prefilter, no
    Claude involved) not yet shown via an instant price alert or an earlier
    digest - the plain-search counterpart to get_pending_digest."""
    return conn.execute(
        """
        SELECT listings.*, searches.name AS search_name, searches.digest_style AS search_digest_style
        FROM listings
        JOIN searches ON searches.id = listings.search_id
        WHERE listings.score = ?
          AND listings.included_in_digest_at IS NULL
          AND listings.notified_instant_at IS NULL
        ORDER BY searches.name, listings.price ASC
        """,
        (PLAIN_SURFACED_SCORE,),
    ).fetchall()


BUCKETS = ("found", "daily_roundup", "instant_alert", "dismissed")

# UI display labels only (decided 2026-10-08, backlog item 18) - code/internal
# references use "daily_roundup"/"instant_alert" (named after when you're
# notified, matching SCORE_DIGEST_MIN/SCORE_INSTANT_THRESHOLD directly), the
# admin UI itself shows the terser "Maybe"/"Yes!" instead.
BUCKET_LABELS = {"found": "Found", "daily_roundup": "Maybe", "instant_alert": "Yes!", "dismissed": "Not interested"}


def _bucket_predicate(bucket: str, score_digest_min: int, score_instant_threshold: int) -> str:
    # A listing marked "not interested" (see watcher/feedback.py) drops out of
    # every normal bucket and only shows up under its own "dismissed" bucket,
    # so it stops competing for attention without being deleted outright.
    if bucket == "dismissed":
        return "feedback = 'dismissed'"
    not_dismissed = "(feedback IS NULL OR feedback != 'dismissed')"
    if bucket == "instant_alert":
        return f"score >= {score_instant_threshold} AND {not_dismissed}"
    if bucket == "daily_roundup":
        return f"score BETWEEN {score_digest_min} AND {score_instant_threshold - 1} AND {not_dismissed}"
    if bucket == "found":
        return f"score IS NOT NULL AND score != 0 AND {not_dismissed}"
    raise ValueError(f"Unknown bucket {bucket!r}")


def get_search_bucket_counts(
    conn: sqlite3.Connection, score_digest_min: int, score_instant_threshold: int
) -> Dict[int, Dict[str, int]]:
    """Counts of currently-active (not removed/sold) listings per search, for
    each of the three admin-UI buckets: "found" (passed the deterministic
    prefilter, i.e. within max-price/excluded-model thresholds, regardless of
    score), "daily_roundup" (scored in the digest range) and "instant_alert"
    (scored at or above the instant-notify threshold)."""
    rows = conn.execute(
        f"""
        SELECT
            search_id,
            SUM(CASE WHEN {_bucket_predicate('found', score_digest_min, score_instant_threshold)} THEN 1 ELSE 0 END) AS found,
            SUM(CASE WHEN {_bucket_predicate('daily_roundup', score_digest_min, score_instant_threshold)} THEN 1 ELSE 0 END) AS daily_roundup,
            SUM(CASE WHEN {_bucket_predicate('instant_alert', score_digest_min, score_instant_threshold)} THEN 1 ELSE 0 END) AS instant_alert
        FROM listings
        GROUP BY search_id
        """
    ).fetchall()
    return {
        row["search_id"]: {
            "found": row["found"], "daily_roundup": row["daily_roundup"], "instant_alert": row["instant_alert"]
        }
        for row in rows
    }


# Within a score tier (plain-surfaced listings all share score -1, so this is
# the only ordering that applies to them), a listing with a known distance
# from home sorts closest-first, the same way Blocket's own "Closest" filter
# would (see marketplaces._blocket_fetch) - a listing with no distance (not
# fetched with that sort, or from a marketplace that doesn't support it)
# falls back to newest-first, same as before this existed.
_LISTING_ORDER_BY = "score DESC, distance_km IS NULL, distance_km ASC, first_seen_at DESC"

# Column headers a listing table lets Lars click to sort by (admin UI's
# search_listings.html/feed.html) - mapped to the underlying column(s) to
# order by, NULLs always sorted last regardless of direction so an unset
# price/score/location never dominates one end of the list.
SORT_COLUMNS = {
    "title": "title COLLATE NOCASE",
    "price": "price",
    "score": "score",
    "source": "source",
    "location": "location",
    "age": "first_seen_at",
}


def _custom_order_by(sort: Optional[str], sort_dir: str, qualify: bool = False) -> Optional[str]:
    if sort not in SORT_COLUMNS:
        return None
    prefix = "listings." if qualify else ""
    bare_column = SORT_COLUMNS[sort].split(" ", 1)[0]  # strip "COLLATE NOCASE" etc for the NULL check
    direction = "DESC" if sort_dir == "desc" else "ASC"
    return f"{prefix}{bare_column} IS NULL, {prefix}{SORT_COLUMNS[sort]} {direction}"


def list_bucket_listings(
    conn: sqlite3.Connection,
    search_id: int,
    bucket: str,
    score_digest_min: int,
    score_instant_threshold: int,
    sort: Optional[str] = None,
    sort_dir: str = "asc",
) -> List[sqlite3.Row]:
    predicate = _bucket_predicate(bucket, score_digest_min, score_instant_threshold)
    order_by = _custom_order_by(sort, sort_dir) or _LISTING_ORDER_BY
    return conn.execute(
        f"SELECT * FROM listings WHERE search_id = ? AND {predicate} ORDER BY {order_by}",
        (search_id,),
    ).fetchall()


def set_listing_feedback(
    conn: sqlite3.Connection, listing_id: int, feedback: str, reason: Optional[str], detail: str
) -> None:
    """Records a "like" or "dismiss" (feedback) from the admin UI's
    per-listing feedback form - see watcher/feedback.py for the dismiss
    reason taxonomy. reason is None for a like (or a dismiss with no reason
    picked); detail is the optional free-text note (e.g. "no 4K support"),
    stored even when empty so a later edit can tell "cleared" from "never
    set" if that ever matters."""
    conn.execute(
        """
        UPDATE listings
        SET feedback = ?, feedback_reason = ?, feedback_detail = ?, feedback_at = datetime('now')
        WHERE id = ?
        """,
        (feedback, reason, detail, listing_id),
    )
    conn.commit()


def clear_listing_feedback(conn: sqlite3.Connection, listing_id: int) -> None:
    conn.execute(
        "UPDATE listings SET feedback = NULL, feedback_reason = NULL, feedback_detail = NULL, feedback_at = NULL "
        "WHERE id = ?",
        (listing_id,),
    )
    conn.commit()


FEED_BUCKETS = ("yes_and_maybe", "daily_roundup", "instant_alert")


def list_feed_listings(
    conn: sqlite3.Connection,
    bucket: str,
    score_digest_min: int,
    score_instant_threshold: int,
    search_id: Optional[int] = None,
    sort: Optional[str] = None,
    sort_dir: str = "asc",
) -> List[sqlite3.Row]:
    """Like list_bucket_listings, but across every search at once (optionally
    narrowed to one) rather than one search at a time - the cross-search feed
    (backlog item 17). "yes_and_maybe" (the feed's default) combines the
    "Yes!"/"Maybe" buckets into one list, ordered tier-first (every "Yes!"
    listing before any "Maybe" one) and by distance from home within each
    tier, rather than by score - Lars cares more about "closest genuinely
    good option" than fine-grained score ordering once something's already
    cleared the "Maybe" bar."""
    if bucket == "yes_and_maybe":
        yes_predicate = _bucket_predicate("instant_alert", score_digest_min, score_instant_threshold)
        maybe_predicate = _bucket_predicate("daily_roundup", score_digest_min, score_instant_threshold)
        predicate = f"(({yes_predicate}) OR ({maybe_predicate})) AND searches.scoring_mode = 'rated'"
        order_by = (
            f"CASE WHEN listings.score >= {score_instant_threshold} THEN 0 ELSE 1 END, "
            "listings.distance_km IS NULL, listings.distance_km ASC, listings.first_seen_at DESC"
        )
    else:
        predicate = _bucket_predicate(bucket, score_digest_min, score_instant_threshold)
        # "daily_roundup"/"instant_alert" only make sense for a currently
        # rated search - without this, a search switched from rated to plain
        # can keep surfacing its old high-scored listings here forever, since
        # update_search never touches existing listings' scores (same bug as
        # get_top_listings, see its docstring). "found" deliberately stays
        # unscoped - it's meant to include both rated and plain-surfaced
        # matches.
        if bucket in ("daily_roundup", "instant_alert"):
            predicate += " AND searches.scoring_mode = 'rated'"
        order_by = "listings.score DESC, listings.distance_km IS NULL, listings.distance_km ASC, listings.first_seen_at DESC"
    order_by = _custom_order_by(sort, sort_dir, qualify=True) or order_by
    query = f"""
        SELECT listings.*, searches.name AS search_name
        FROM listings
        JOIN searches ON searches.id = listings.search_id
        WHERE {predicate}
    """
    params: List[int] = []
    if search_id is not None:
        query += " AND listings.search_id = ?"
        params.append(search_id)
    query += f" ORDER BY {order_by}"
    return conn.execute(query, params).fetchall()


def get_overview_stats(conn: sqlite3.Connection, score_digest_min: int, score_instant_threshold: int) -> dict:
    """Aggregate, across-all-searches numbers for the searches page's
    overview panel (backlog item 16): current month's Claude cost (cost only
    - the token counts aren't the interesting number for an at-a-glance
    panel, see monthly_cost_summary for those), and total scanned/found/
    daily-roundup-bucket listing counts."""
    total_scanned = conn.execute("SELECT COUNT(*) AS n FROM listings").fetchone()["n"]
    found_predicate = _bucket_predicate("found", score_digest_min, score_instant_threshold)
    roundup_predicate = _bucket_predicate("daily_roundup", score_digest_min, score_instant_threshold)
    row = conn.execute(
        f"""
        SELECT
            SUM(CASE WHEN {found_predicate} THEN 1 ELSE 0 END) AS found,
            SUM(CASE WHEN {roundup_predicate} THEN 1 ELSE 0 END) AS daily_roundup
        FROM listings
        """
    ).fetchone()
    cost_row = conn.execute(
        "SELECT SUM(cost_usd) AS cost FROM token_usage WHERE strftime('%Y-%m', created_at) = strftime('%Y-%m', 'now')"
    ).fetchone()
    return {
        "monthly_cost_usd": cost_row["cost"] or 0.0,
        "total_scanned": total_scanned,
        "total_found": row["found"] or 0,
        "total_daily_roundup": row["daily_roundup"] or 0,
    }


def get_top_listings(conn: sqlite3.Connection, score_instant_threshold: int, limit: int = 5) -> List[sqlite3.Row]:
    """The "Yes!" (instant_alert) bucket's listings across every currently
    AI-rated search, for the searches page's overview panel's "top ads"
    (backlog item 16) - these are specifically the standout finds, not just
    whatever's highest-scored if nothing cleared the instant-alert bar. A
    plain search's surfaced matches (score -1) are never "top", there's
    nothing to rank them by.

    Explicitly scoped to searches.scoring_mode = 'rated' (confirmed live,
    2026-10, as a real bug otherwise): switching a search from rated to
    plain doesn't retroactively clear whatever real scores its *existing*
    listings already had - update_search only touches the search's own row,
    never its listings. Without this filter, a search that used to be rated
    can keep surfacing its old high-scored listings here indefinitely, even
    though it's plain now and nothing has scored anything since - crowding
    out genuinely current standouts from searches that are actually rated
    today.

    Also excludes a listing dismissed via the admin UI's "not interested"
    feedback (see watcher/feedback.py) - confirmed live, 2026-10, as a real
    bug otherwise: a listing hidden from the feed/bucket views for exactly
    that reason (too far away, no shipping, etc.) kept showing up here
    regardless, since this query never looked at listings.feedback at all."""
    return conn.execute(
        """
        SELECT listings.*, searches.name AS search_name
        FROM listings
        JOIN searches ON searches.id = listings.search_id
        WHERE listings.score >= ? AND searches.scoring_mode = 'rated'
          AND (listings.feedback IS NULL OR listings.feedback != 'dismissed')
        ORDER BY listings.score DESC, listings.first_seen_at DESC
        LIMIT ?
        """,
        (score_instant_threshold, limit),
    ).fetchall()


def get_surfaced_listings(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    """Every listing actually surfaced to the user - Claude-scored (1-10) or
    plain-surfaced (-1, PLAIN_SURFACED_SCORE) - across all searches, with its
    search's name. Excludes pending (NULL) and deterministically
    prefiltered-out (0) listings, which were never shown to anyone and
    aren't worth the extra liveness-check request. This is the set
    watcher/liveness.py's run_liveness_sweep network-checks; a plain search's
    matches need the same "confirmed gone"/stale handling a rated search's
    do, not just Claude-scored ones."""
    return conn.execute(
        """
        SELECT listings.*, searches.name AS search_name
        FROM listings
        JOIN searches ON searches.id = listings.search_id
        WHERE listings.score IS NOT NULL AND listings.score != 0
        """
    ).fetchall()


def get_ended_auction_listings(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    """Every active listing (regardless of score state - pending/rated/plain)
    whose auction has ended. Unlike get_scored_listings (what the network-
    based liveness sweep checks), this needs no network call at all: an
    auction's own auction_ends_at, captured the moment it was first fetched,
    is itself the removal signal once it's passed - see
    watcher/liveness.py's run_auction_end_sweep."""
    return conn.execute(
        """
        SELECT listings.*, searches.name AS search_name
        FROM listings
        JOIN searches ON searches.id = listings.search_id
        WHERE listings.auction_ends_at IS NOT NULL
          AND listings.auction_ends_at <= datetime('now')
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
    conn.execute(
        """
        UPDATE watched_items SET
            current_price = NULL, lowest_price_seen = NULL,
            last_alert_price = NULL, last_checked_at = NULL
        """
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
    run_id: Optional[int],
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
