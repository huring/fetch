"""Orchestrates one marketplace's fetch-score-notify cycle, and the separate
daily digest send.

Each registered marketplace polls on its own cadence (see marketplace_configs
and admin/app.py's scheduler tick), so a "cycle" here is scoped to one
marketplace at a time: fetch that marketplace's queries across every enabled
search that uses it, then prefilter/score/notify whatever's now pending for
those searches (which may also include earlier items still pending from a
prior failed scoring attempt, regardless of which marketplace sourced them).
"""
from __future__ import annotations

import logging
import sqlite3
import time
from typing import Any, Dict, List

from anthropic import Anthropic

from watcher import marketplace_configs as marketplace_configs_repo
from watcher import searches as searches_repo
from watcher import storage
from watcher.marketplaces import MARKETPLACES, get as get_marketplace
from watcher.models import Listing, Search
from watcher.notify import slack
from watcher.scoring.claude_scorer import estimate_cost_usd, score_batch
from watcher.scoring.prefilter import passes_prefilter
from watcher.settings import Settings
from watcher.sources.base import SourceError

logger = logging.getLogger(__name__)


def _filter_by_scope(listings: List[Listing], search: Search) -> List[Listing]:
    """For local-scope searches, drop listings whose location is known and
    doesn't match - listings with no location info are kept (can't exclude
    what we can't check)."""
    if search.scope != "local" or not search.location:
        return listings
    loc = search.location.lower()
    return [l for l in listings if l.location is None or loc in l.location.lower()]


def run_marketplace_cycle(
    conn: sqlite3.Connection, client: Anthropic, settings: Settings, marketplace_key: str, dry_run: bool = False
) -> Dict[str, Any]:
    marketplace = get_marketplace(marketplace_key)
    if marketplace is None:
        raise ValueError(f"Unknown marketplace {marketplace_key!r}")
    config = marketplace_configs_repo.get_config(conn, marketplace_key)
    if config is None:
        raise ValueError(f"No marketplace_configs row for {marketplace_key!r} - call ensure_defaults() first")

    run_id = storage.start_run(conn)
    touched_searches = [
        s for s in searches_repo.list_searches(conn, enabled_only=True) if marketplace_key in s.marketplaces
    ]

    errors = 0
    attempts = 0
    items_fetched = 0
    total_fetched = 0
    total_pending_scored_attempt = 0
    total_scored = 0
    instant_notifications: List[Dict[str, Any]] = []

    try:
        for search in touched_searches:
            listings: List[Listing] = []
            for phrase in search.search_phrases:
                attempts += 1
                try:
                    fetched = marketplace.fetch(phrase, search=search, config=config, settings=settings)
                    items_fetched += len(fetched)
                    listings.extend(fetched)
                except SourceError as exc:
                    logger.error(
                        "%s fetch failed for search %r: %s", marketplace.display_name, search.name, exc
                    )
                    errors += 1
                time.sleep(config.request_delay_seconds)

            listings = _filter_by_scope(listings, search)
            total_fetched += len(listings)
            for listing in listings:
                storage.upsert_listing(conn, search.id, listing)

            pending_rows = storage.get_pending_listings(conn, search.id)
            to_score = []
            for row in pending_rows:
                ok, reason = passes_prefilter(row["title"], row["description"], row["price"], search)
                if not ok:
                    storage.mark_prefiltered_out(conn, row["id"], reason)
                    continue

                if marketplace.enrich_description and not row["description"]:
                    description = marketplace.enrich_description(row["url"])
                    time.sleep(config.request_delay_seconds)
                    if description:
                        storage.update_description(conn, row["id"], description)
                        row = dict(row)
                        row["description"] = description
                        ok, reason = passes_prefilter(row["title"], row["description"], row["price"], search)
                        if not ok:
                            storage.mark_prefiltered_out(conn, row["id"], reason)
                            continue

                to_score.append(row)
            total_pending_scored_attempt += len(to_score)

            for batch_start in range(0, len(to_score), settings.scoring_batch_size):
                batch_rows = to_score[batch_start : batch_start + settings.scoring_batch_size]
                candidates = [
                    {"title": r["title"], "description": r["description"], "price": r["price"], "url": r["url"]}
                    for r in batch_rows
                ]
                try:
                    results, input_tokens, output_tokens = score_batch(
                        client, settings.claude_model, search, candidates
                    )
                except Exception as exc:
                    logger.error("Claude scoring failed for search %r: %s", search.name, exc)
                    continue  # rows stay pending (score IS NULL), retried next cycle

                cost = estimate_cost_usd(settings.claude_model, input_tokens, output_tokens)
                storage.log_token_usage(conn, run_id, settings.claude_model, input_tokens, output_tokens, cost)

                for row, result in zip(batch_rows, results):
                    if result is None:
                        continue  # left pending, retried next cycle
                    storage.mark_scored(
                        conn, row["id"], result.score, result.reasoning, result.uncertain_specs, result.price_assessment
                    )
                    total_scored += 1
                    if result.score >= settings.score_instant_threshold:
                        instant_notifications.append(
                            {
                                "search_name": search.name,
                                "title": row["title"],
                                "price": row["price"],
                                "url": row["url"],
                                "score": result.score,
                                "reasoning": result.reasoning,
                                "price_assessment": result.price_assessment,
                                "listing_id": row["id"],
                            }
                        )

        for notif in instant_notifications:
            if not settings.slack_webhook_url:
                logger.warning("SLACK_WEBHOOK_URL not configured, skipping instant notification for %r", notif["title"])
                continue
            try:
                slack.send_instant(
                    settings.slack_webhook_url,
                    notif["search_name"],
                    notif["title"],
                    notif["price"],
                    notif["url"],
                    notif["score"],
                    notif["reasoning"],
                    notif["price_assessment"],
                    dry_run=dry_run,
                )
                if not dry_run:
                    storage.mark_notified_instant(conn, notif["listing_id"])
            except Exception as exc:
                logger.error("Failed to send instant Slack notification: %s", exc)

        if attempts > 0:
            healthy = errors < attempts and items_fetched > 0
            if healthy:
                storage.record_source_success(conn, marketplace_key)
            else:
                error_summary = f"{errors}/{attempts} queries failed, {items_fetched} items fetched"
                failures = storage.record_source_failure(conn, marketplace_key, error_summary)
                if storage.should_send_health_alert(conn, marketplace_key, settings.health_alert_after_n_failures):
                    if settings.slack_webhook_url:
                        slack.send_health_alert(
                            settings.slack_webhook_url, marketplace_key, failures, error_summary, dry_run=dry_run
                        )
                        if not dry_run:
                            storage.mark_health_alert_sent(conn, marketplace_key)

        if not dry_run:
            marketplace_configs_repo.mark_fetched(conn, marketplace_key)

        storage.finish_run(
            conn,
            run_id,
            status="ok",
            searches_processed=len(touched_searches),
            listings_fetched=total_fetched,
            listings_new=total_pending_scored_attempt,
            listings_scored=total_scored,
        )
        return {
            "run_id": run_id,
            "marketplace": marketplace_key,
            "searches_processed": len(touched_searches),
            "listings_fetched": total_fetched,
            "listings_scored": total_scored,
            "instant_notifications": len(instant_notifications),
        }
    except Exception as exc:
        logger.exception("Run %s (%s) failed", run_id, marketplace_key)
        storage.finish_run(conn, run_id, status="error", error_message=str(exc))
        raise


def run_once(conn: sqlite3.Connection, client: Anthropic, settings: Settings, dry_run: bool = False) -> Dict[str, Any]:
    """Runs every registered marketplace's cycle once, ignoring each one's own
    due-time - used for --once / manual testing, not by the scheduler."""
    aggregate: Dict[str, Any] = {
        "searches_processed": 0,
        "listings_fetched": 0,
        "listings_scored": 0,
        "instant_notifications": 0,
        "marketplaces": {},
    }
    for marketplace_key in MARKETPLACES:
        result = run_marketplace_cycle(conn, client, settings, marketplace_key, dry_run=dry_run)
        aggregate["marketplaces"][marketplace_key] = result
        aggregate["searches_processed"] += result["searches_processed"]
        aggregate["listings_fetched"] += result["listings_fetched"]
        aggregate["listings_scored"] += result["listings_scored"]
        aggregate["instant_notifications"] += result["instant_notifications"]
    return aggregate


def send_digest(conn: sqlite3.Connection, settings: Settings, dry_run: bool = False) -> Dict[str, Any]:
    rows = storage.get_pending_digest(conn, settings.score_digest_min, settings.score_instant_threshold - 1)

    grouped: Dict[str, List[slack.DigestEntry]] = {}
    ids: List[int] = []
    for row in rows:
        grouped.setdefault(row["search_name"], []).append(
            (row["title"], row["price"], row["url"], row["score"], row["reasoning"])
        )
        ids.append(row["id"])

    if grouped:
        if settings.slack_webhook_url:
            slack.send_digest(settings.slack_webhook_url, grouped, dry_run=dry_run)
        else:
            logger.warning("SLACK_WEBHOOK_URL not configured, skipping digest with %d entries", len(ids))

    if not dry_run:
        storage.mark_digested(conn, ids)

    return {"entries": len(ids), "searches": len(grouped)}
