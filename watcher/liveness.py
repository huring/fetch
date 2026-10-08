"""Daily liveness sweep: confirms whether previously-surfaced listings (see
storage.get_surfaced_listings - Claude-scored *or* a plain search's matches,
not just rated ones) are still live, rather than inferring removal from
search-result absence (which is unreliable - both Blocket and Vinted sort
newest-first, so an old-but-still-active listing falls off the pages a
regular fetch cycle sees purely because newer matches buried it, well
before it's actually sold).

For each listing a marketplace knows how to re-check (``check_active`` in
its registry entry):
  * still live, and active >= STALE_AFTER_DAYS with a score that was ever
    worth surfacing -> a one-time "consider a lower offer" Slack notice.
    (A plain search's listings never qualify here - they have no score to
    compare against score_digest_min - they just get the removal check.)
  * no longer live -> captured into ``price_history`` (the raw material for
    future price-comparison stats) and deleted from ``listings``.

A listing whose marketplace has no ``check_active`` hook, or whose check
fails (network error, etc.), is left untouched this cycle rather than
guessed at - never delete data on an uncertain check.
"""
from __future__ import annotations

import datetime
import logging
import sqlite3
from typing import Any, Dict

from watcher import storage
from watcher.marketplaces import get as get_marketplace
from watcher.notify import slack
from watcher.settings import Settings
from watcher.sources.base import SourceError

logger = logging.getLogger(__name__)

STALE_AFTER_DAYS = 21


def _age_days(first_seen_at: str, now: datetime.datetime) -> int:
    first_seen = datetime.datetime.strptime(first_seen_at, "%Y-%m-%d %H:%M:%S")
    return (now - first_seen).days


def run_auction_end_sweep(conn: sqlite3.Connection, dry_run: bool = False) -> Dict[str, Any]:
    """Removes every listing whose auction has ended (see
    models.Listing.auction_ends_at / marketplaces.Marketplace.is_auction).

    Unlike run_liveness_sweep, this needs no network call and no
    `check_active` hook at all - an auction's own deadline, captured at fetch
    time, is itself the removal signal once it's passed. Covers every active
    listing regardless of score state (pending/rated/plain), not just
    Claude-scored ones, so it's cheap enough to run on every scheduler tick
    rather than waiting for the once-daily liveness job."""
    removed = 0
    for row in storage.get_ended_auction_listings(conn):
        if not dry_run:
            storage.insert_price_history(conn, row["search_name"], row)
            storage.delete_listing(conn, row["id"])
        removed += 1
    return {"removed": removed}


def run_liveness_sweep(conn: sqlite3.Connection, settings: Settings, dry_run: bool = False) -> Dict[str, Any]:
    now = datetime.datetime.utcnow()
    checked = 0
    removed = 0
    stale_notices_sent = 0
    errors = 0

    for row in storage.get_surfaced_listings(conn):
        marketplace = get_marketplace(row["source"])
        if marketplace is None or marketplace.check_active is None:
            continue

        try:
            is_active = marketplace.check_active(row["url"])
        except SourceError as exc:
            logger.warning("Liveness check failed for listing %s (%s): %s", row["id"], row["url"], exc)
            errors += 1
            continue

        checked += 1

        if not is_active:
            if not dry_run:
                storage.insert_price_history(conn, row["search_name"], row)
                storage.delete_listing(conn, row["id"])
            removed += 1
            continue

        if (
            row["first_seen_at"]
            and row["stale_notified_at"] is None
            and row["score"] >= settings.score_digest_min
            and _age_days(row["first_seen_at"], now) >= STALE_AFTER_DAYS
        ):
            days_active = _age_days(row["first_seen_at"], now)
            if settings.slack_webhook_url:
                try:
                    slack.send_stale_opportunity(
                        settings.slack_webhook_url,
                        row["search_name"],
                        row["title"],
                        row["price"],
                        row["url"],
                        row["score"],
                        days_active,
                        dry_run=dry_run,
                    )
                except Exception as exc:
                    logger.error("Failed to send stale-opportunity Slack notification: %s", exc)
            if not dry_run:
                storage.mark_stale_notified(conn, row["id"])
            stale_notices_sent += 1

    return {
        "checked": checked,
        "removed": removed,
        "stale_notices_sent": stale_notices_sent,
        "errors": errors,
    }
