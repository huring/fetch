"""Orchestrates one fetch-score-notify cycle, and the separate daily digest send."""
from __future__ import annotations

import logging
import sqlite3
import time
from typing import Any, Dict, List

from anthropic import Anthropic

from watcher import containers as containers_repo
from watcher import storage
from watcher.models import Container, Listing
from watcher.notify import slack
from watcher.scoring.claude_scorer import estimate_cost_usd, score_batch
from watcher.scoring.prefilter import passes_prefilter
from watcher.settings import Settings
from watcher.sources import blocket, tradera
from watcher.sources.base import SourceError

logger = logging.getLogger(__name__)


def _filter_by_scope(listings: List[Listing], container: Container) -> List[Listing]:
    """For local-scope containers, drop listings whose location is known and
    doesn't match - listings with no location info are kept (can't exclude
    what we can't check)."""
    if container.scope != "local" or not container.location:
        return listings
    loc = container.location.lower()
    return [l for l in listings if l.location is None or loc in l.location.lower()]


def run_once(conn: sqlite3.Connection, client: Anthropic, settings: Settings, dry_run: bool = False) -> Dict[str, Any]:
    run_id = storage.start_run(conn)
    enabled_containers = containers_repo.list_containers(conn, enabled_only=True)

    source_stats = {
        "blocket": {"errors": 0, "attempts": 0, "items": 0},
        "tradera": {"errors": 0, "attempts": 0, "items": 0},
    }
    total_fetched = 0
    total_pending_scored_attempt = 0
    total_scored = 0
    instant_notifications: List[Dict[str, Any]] = []

    try:
        for container in enabled_containers:
            listings: List[Listing] = []

            for q in container.blocket_queries:
                source_stats["blocket"]["attempts"] += 1
                location = container.location if container.scope == "local" else ""
                try:
                    fetched = blocket.fetch(q, location=location, max_pages=settings.max_pages_per_query)
                    source_stats["blocket"]["items"] += len(fetched)
                    listings.extend(fetched)
                except SourceError as exc:
                    logger.error("Blocket fetch failed for container %r: %s", container.name, exc)
                    source_stats["blocket"]["errors"] += 1
                time.sleep(settings.blocket_request_delay_seconds)

            for q in container.tradera_queries:
                if not (settings.tradera_app_id and settings.tradera_app_key):
                    logger.warning(
                        "Skipping Tradera query for %r: TRADERA_APP_ID/TRADERA_APP_KEY not configured",
                        container.name,
                    )
                    continue
                source_stats["tradera"]["attempts"] += 1
                try:
                    fetched = tradera.fetch(
                        q, app_id=settings.tradera_app_id, app_key=settings.tradera_app_key,
                        max_pages=settings.max_pages_per_query,
                    )
                    source_stats["tradera"]["items"] += len(fetched)
                    listings.extend(fetched)
                except SourceError as exc:
                    logger.error("Tradera fetch failed for container %r: %s", container.name, exc)
                    source_stats["tradera"]["errors"] += 1
                time.sleep(settings.tradera_request_delay_seconds)

            listings = _filter_by_scope(listings, container)
            total_fetched += len(listings)
            for listing in listings:
                storage.upsert_listing(conn, container.id, listing)

            pending_rows = storage.get_pending_listings(conn, container.id)
            to_score = []
            for row in pending_rows:
                ok, reason = passes_prefilter(row["title"], row["description"], row["price"], container)
                if not ok:
                    storage.mark_prefiltered_out(conn, row["id"], reason)
                    continue

                # Blocket's search results carry no description text - for
                # candidates that already passed prefilter on title/price
                # alone, fetch a short description snippet from the ad's own
                # detail page and re-check prefilter against the fuller text
                # before handing it to Claude.
                if row["source"] == "blocket" and not row["description"]:
                    description = blocket.fetch_ad_description(row["url"])
                    time.sleep(settings.blocket_request_delay_seconds)
                    if description:
                        storage.update_description(conn, row["id"], description)
                        row = dict(row)
                        row["description"] = description
                        ok, reason = passes_prefilter(row["title"], row["description"], row["price"], container)
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
                        client, settings.claude_model, container, candidates
                    )
                except Exception as exc:
                    logger.error("Claude scoring failed for container %r: %s", container.name, exc)
                    continue  # rows stay pending (score IS NULL), retried next run

                cost = estimate_cost_usd(settings.claude_model, input_tokens, output_tokens)
                storage.log_token_usage(conn, run_id, settings.claude_model, input_tokens, output_tokens, cost)

                for row, result in zip(batch_rows, results):
                    if result is None:
                        continue  # left pending, retried next run
                    storage.mark_scored(
                        conn, row["id"], result.score, result.reasoning, result.uncertain_specs, result.price_assessment
                    )
                    total_scored += 1
                    if result.score >= settings.score_instant_threshold:
                        instant_notifications.append(
                            {
                                "container_name": container.name,
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
                    notif["container_name"],
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

        for source, stats in source_stats.items():
            if stats["attempts"] == 0:
                continue
            healthy = stats["errors"] < stats["attempts"] and stats["items"] > 0
            if healthy:
                storage.record_source_success(conn, source)
            else:
                error_summary = f"{stats['errors']}/{stats['attempts']} queries failed, {stats['items']} items fetched"
                failures = storage.record_source_failure(conn, source, error_summary)
                if storage.should_send_health_alert(conn, source, settings.health_alert_after_n_failures):
                    if settings.slack_webhook_url:
                        slack.send_health_alert(settings.slack_webhook_url, source, failures, error_summary, dry_run=dry_run)
                        if not dry_run:
                            storage.mark_health_alert_sent(conn, source)

        storage.finish_run(
            conn,
            run_id,
            status="ok",
            containers_processed=len(enabled_containers),
            listings_fetched=total_fetched,
            listings_new=total_pending_scored_attempt,
            listings_scored=total_scored,
        )
        return {
            "run_id": run_id,
            "containers_processed": len(enabled_containers),
            "listings_fetched": total_fetched,
            "listings_scored": total_scored,
            "instant_notifications": len(instant_notifications),
        }
    except Exception as exc:
        logger.exception("Run %s failed", run_id)
        storage.finish_run(conn, run_id, status="error", error_message=str(exc))
        raise


def send_digest(conn: sqlite3.Connection, settings: Settings, dry_run: bool = False) -> Dict[str, Any]:
    rows = storage.get_pending_digest(conn, settings.score_digest_min, settings.score_instant_threshold - 1)

    grouped: Dict[str, List[slack.DigestEntry]] = {}
    ids: List[int] = []
    for row in rows:
        grouped.setdefault(row["container_name"], []).append(
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

    return {"entries": len(ids), "containers": len(grouped)}
