"""Orchestrates marketplace fetch cycles, async Claude-batch scoring, and the
separate daily digest send.

Fetching and scoring are two independent concerns on two independent
schedules:

- run_marketplace_cycle fetches and deterministically prefilters one
  marketplace's searches, on that marketplace's own poll cadence (see
  marketplace_configs and admin/app.py's scheduler tick). It never calls
  Claude - a listing that passes the prefilter is simply left pending
  (score IS NULL).
- submit_pending_scoring sweeps every currently-pending listing across every
  search/marketplace and submits it to the Claude Message Batches API -
  50% cheaper per token than a normal call, which matters for an unattended,
  latency-insensitive background tool like this one. It runs independently
  of any single marketplace's fetch cadence (see admin/app.py's tick).
- collect_finished_batches checks previously-submitted batches and, for
  whichever have finished (usually minutes, up to 24h), marks the listings
  scored and sends instant Slack notifications - also independent of fetch
  cadence.

This split means a listing can sit "pending" for a little while after being
fetched, waiting for the next scoring sweep and then for that batch to
finish - normal and expected, not a bug.
"""
from __future__ import annotations

import logging
import sqlite3
import time
import uuid
from typing import Any, Dict, List

from anthropic import Anthropic

from watcher import marketplace_configs as marketplace_configs_repo
from watcher import searches as searches_repo
from watcher import storage
from watcher.marketplaces import MARKETPLACES, get as get_marketplace
from watcher.models import Listing, Search
from watcher.notify import slack
from watcher.scoring import claude_scorer
from watcher.scoring.claude_scorer import estimate_cost_usd
from watcher.scoring.prefilter import passes_prefilter
from watcher.settings import Settings
from watcher.sources.base import SourceError

logger = logging.getLogger(__name__)

# How long `--once` blocks waiting for a submitted scoring batch to finish,
# before giving up and letting a later scheduled collection pass finish it.
ONCE_SCORING_TIMEOUT_SECONDS = 600
ONCE_SCORING_POLL_SECONDS = 15


def _filter_by_scope(listings: List[Listing], search: Search) -> List[Listing]:
    """For local-scope searches, drop listings whose location is known and
    doesn't match - listings with no location info are kept (can't exclude
    what we can't check)."""
    if search.scope != "local" or not search.location:
        return listings
    loc = search.location.lower()
    return [l for l in listings if l.location is None or loc in l.location.lower()]


def _maybe_send_plain_instant_alert(
    conn: sqlite3.Connection, settings: Settings, search: Search, row: sqlite3.Row, dry_run: bool
) -> None:
    if search.instant_alert_price is None:
        return
    if row["price"] is None or row["price"] > search.instant_alert_price:
        return
    if not settings.slack_webhook_url:
        logger.warning("SLACK_WEBHOOK_URL not configured, skipping plain price alert for %r", row["title"])
        return
    try:
        slack.send_plain_instant(
            settings.slack_webhook_url, search.name, row["title"], row["price"], row["url"],
            auction_ends_at=row["auction_ends_at"], dry_run=dry_run,
        )
        if not dry_run:
            storage.mark_notified_instant(conn, row["id"])
    except Exception as exc:
        logger.error("Failed to send plain price alert: %s", exc)


def run_marketplace_cycle(
    conn: sqlite3.Connection, settings: Settings, marketplace_key: str, dry_run: bool = False
) -> Dict[str, Any]:
    """Fetches and prefilters one marketplace's searches. Never calls Claude -
    see the module docstring for where scoring happens."""
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
    total_pending = 0

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

                # A "rated" listing is simply left pending (score IS NULL) for
                # the next scoring submit sweep; a "plain" one has no Claude
                # step at all, so it's surfaced immediately.
                if search.scoring_mode == "plain":
                    storage.mark_surfaced_plain(conn, row["id"])
                    _maybe_send_plain_instant_alert(conn, settings, search, row, dry_run)

                total_pending += 1

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
            listings_new=total_pending,
        )
        return {
            "run_id": run_id,
            "marketplace": marketplace_key,
            "searches_processed": len(touched_searches),
            "listings_fetched": total_fetched,
            "listings_pending_scoring": total_pending,
        }
    except Exception as exc:
        logger.exception("Run %s (%s) failed", run_id, marketplace_key)
        storage.finish_run(conn, run_id, status="error", error_message=str(exc))
        raise


def submit_pending_scoring(conn: sqlite3.Connection, client: Anthropic, settings: Settings) -> Dict[str, Any]:
    """Batches every currently-pending, not-already-submitted listing across
    every search into chunks of settings.scoring_batch_size and submits them
    as one Claude Message Batch. Always runs for real - scoring is the one
    Claude-costing step dry_run never skips (matching this project's
    long-standing dry_run semantics: it gates notifications, not scoring)."""
    rows = storage.get_unbatched_pending_listings(conn)
    if not rows:
        return {"listings_submitted": 0, "requests": 0}

    by_search: Dict[int, List[sqlite3.Row]] = {}
    for row in rows:
        by_search.setdefault(row["search_id"], []).append(row)

    requests: List[Dict[str, Any]] = []
    item_rows: List[tuple] = []  # (custom_id, listing_index, listing_id)
    for search_id, search_rows in by_search.items():
        search = searches_repo.get_search(conn, search_id)
        if search is None:
            continue  # search deleted since these listings were fetched
        for chunk_start in range(0, len(search_rows), settings.scoring_batch_size):
            chunk = search_rows[chunk_start : chunk_start + settings.scoring_batch_size]
            candidates = [
                {
                    "title": r["title"],
                    "description": r["description"],
                    "price": r["price"],
                    "url": r["url"],
                    "auction_ends_at": r["auction_ends_at"],
                    "source_note": getattr(get_marketplace(r["source"]), "scoring_note", None),
                }
                for r in chunk
            ]
            custom_id = f"search{search_id}-{uuid.uuid4().hex}"
            requests.append(claude_scorer.build_batch_request(custom_id, settings.claude_model, search, candidates))
            for listing_index, row in enumerate(chunk):
                item_rows.append((custom_id, listing_index, row["id"]))

    if not requests:
        return {"listings_submitted": 0, "requests": 0}

    batch = client.messages.batches.create(requests=requests)
    storage.create_scoring_batch(conn, batch.id, item_rows)
    logger.info(
        "Submitted scoring batch %s with %d requests (%d listings)", batch.id, len(requests), len(item_rows)
    )
    return {"listings_submitted": len(item_rows), "requests": len(requests), "batch_id": batch.id}


def collect_finished_batches(
    conn: sqlite3.Connection, client: Anthropic, settings: Settings, dry_run: bool = False
) -> Dict[str, Any]:
    """Checks every in-progress scoring batch and, for whichever have
    finished, marks their listings scored and sends instant notifications."""
    batches_collected = 0
    listings_scored = 0
    listings_errored = 0
    instant_notifications = 0

    for batch_id in storage.get_in_progress_batch_ids(conn):
        try:
            batch = client.messages.batches.retrieve(batch_id)
        except Exception as exc:
            logger.error("Could not check scoring batch %s: %s", batch_id, exc)
            continue
        if batch.processing_status != "ended":
            continue

        items = storage.get_batch_items(conn, batch_id)  # {(custom_id, listing_index): listing_id}
        counts: Dict[str, int] = {}
        for custom_id, _listing_index in items:
            counts[custom_id] = counts.get(custom_id, 0) + 1

        try:
            results_iter = list(client.messages.batches.results(batch_id))
        except Exception as exc:
            logger.error("Could not fetch results for scoring batch %s: %s", batch_id, exc)
            continue

        for result in results_iter:
            num_candidates = counts.get(result.custom_id, 0)
            if num_candidates == 0:
                continue  # unknown custom_id - shouldn't happen

            if result.result.type != "succeeded":
                listings_errored += num_candidates
                logger.warning(
                    "Scoring batch item %s did not succeed: %s", result.custom_id, result.result.type
                )
                continue  # listings stay pending, retried on the next submit sweep

            message = result.result.message
            cost = estimate_cost_usd(settings.claude_model, message.usage.input_tokens, message.usage.output_tokens)
            storage.log_token_usage(
                conn, None, settings.claude_model, message.usage.input_tokens, message.usage.output_tokens, cost
            )

            scores = claude_scorer.parse_score_message(message.content, num_candidates)
            for listing_index, score_result in enumerate(scores):
                if score_result is None:
                    continue  # left pending, retried on the next submit sweep
                listing_id = items.get((result.custom_id, listing_index))
                if listing_id is None:
                    continue
                listing_row = storage.get_listing_with_search_name(conn, listing_id)
                if listing_row is None:
                    continue  # removed (e.g. by the liveness sweep) since submission

                storage.mark_scored(
                    conn,
                    listing_id,
                    score_result.score,
                    score_result.reasoning,
                    score_result.uncertain_specs,
                    score_result.price_assessment,
                )
                listings_scored += 1

                if score_result.score >= settings.score_instant_threshold:
                    if not settings.slack_webhook_url:
                        logger.warning(
                            "SLACK_WEBHOOK_URL not configured, skipping instant notification for %r",
                            listing_row["title"],
                        )
                    else:
                        try:
                            slack.send_instant(
                                settings.slack_webhook_url,
                                listing_row["search_name"],
                                listing_row["title"],
                                listing_row["price"],
                                listing_row["url"],
                                score_result.score,
                                score_result.reasoning,
                                score_result.price_assessment,
                                dry_run=dry_run,
                            )
                            if not dry_run:
                                storage.mark_notified_instant(conn, listing_id)
                            instant_notifications += 1
                        except Exception as exc:
                            logger.error("Failed to send instant Slack notification: %s", exc)

        storage.delete_scoring_batch(conn, batch_id)
        batches_collected += 1

    return {
        "batches_collected": batches_collected,
        "listings_scored": listings_scored,
        "listings_errored": listings_errored,
        "instant_notifications": instant_notifications,
    }


def run_once(conn: sqlite3.Connection, client: Anthropic, settings: Settings, dry_run: bool = False) -> Dict[str, Any]:
    """Runs every registered marketplace's fetch cycle once, then submits
    pending scoring and blocks (polling) until it finishes or a timeout
    elapses - used for --once / manual testing. The live scheduler never
    blocks like this; it submits and collects on its own ongoing schedule."""
    aggregate: Dict[str, Any] = {
        "searches_processed": 0,
        "listings_fetched": 0,
        "listings_pending_scoring": 0,
        "marketplaces": {},
    }
    for marketplace_key in MARKETPLACES:
        result = run_marketplace_cycle(conn, settings, marketplace_key, dry_run=dry_run)
        aggregate["marketplaces"][marketplace_key] = result
        aggregate["searches_processed"] += result["searches_processed"]
        aggregate["listings_fetched"] += result["listings_fetched"]
        aggregate["listings_pending_scoring"] += result["listings_pending_scoring"]

    submit_result = submit_pending_scoring(conn, client, settings)
    aggregate["scoring_submitted"] = submit_result

    collected = {"batches_collected": 0, "listings_scored": 0, "listings_errored": 0, "instant_notifications": 0}
    if submit_result["requests"] > 0:
        deadline = time.monotonic() + ONCE_SCORING_TIMEOUT_SECONDS
        while True:
            result = collect_finished_batches(conn, client, settings, dry_run=dry_run)
            for key in collected:
                collected[key] += result[key]
            if not storage.get_in_progress_batch_ids(conn):
                break
            if time.monotonic() >= deadline:
                logger.warning(
                    "Scoring batch still in progress after %ss - exiting; a later scheduled run will finish it",
                    ONCE_SCORING_TIMEOUT_SECONDS,
                )
                break
            time.sleep(ONCE_SCORING_POLL_SECONDS)

    aggregate["listings_scored"] = collected["listings_scored"]
    aggregate["instant_notifications"] = collected["instant_notifications"]
    return aggregate


def send_digest(conn: sqlite3.Connection, settings: Settings, dry_run: bool = False) -> Dict[str, Any]:
    rows = storage.get_pending_digest(conn, settings.score_digest_min, settings.score_instant_threshold - 1)
    plain_rows = storage.get_pending_plain_digest(conn)

    grouped: Dict[str, List[slack.DigestEntry]] = {}
    plain_grouped: Dict[str, List[slack.PlainDigestEntry]] = {}
    ids: List[int] = []
    for row in rows:
        grouped.setdefault(row["search_name"], []).append(
            (row["title"], row["price"], row["url"], row["score"], row["reasoning"])
        )
        ids.append(row["id"])
    for row in plain_rows:
        plain_grouped.setdefault(row["search_name"], []).append(
            (row["title"], row["price"], row["url"], row["auction_ends_at"])
        )
        ids.append(row["id"])

    if grouped or plain_grouped:
        if settings.slack_webhook_url:
            slack.send_digest(settings.slack_webhook_url, grouped, plain_grouped, dry_run=dry_run)
        else:
            logger.warning("SLACK_WEBHOOK_URL not configured, skipping digest with %d entries", len(ids))

    if not dry_run:
        storage.mark_digested(conn, ids)

    return {"entries": len(ids), "searches": len(grouped) + len(plain_grouped)}
