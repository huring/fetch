"""FastAPI app factory: wires the admin UI, DB connection and the scheduler.

Each marketplace polls on its own cadence (poll_interval_minutes in its
marketplace_configs row, admin-UI-editable). Rather than one APScheduler job
per marketplace with a fixed interval - which would need rescheduling
whenever the admin changes that interval - a single lightweight "tick" job
runs every TICK_INTERVAL_MINUTES and checks each registered marketplace's
config fresh from the DB: if enough time has passed since its last fetch, it
runs that marketplace's cycle. A config change takes effect on the next tick,
no restart needed.
"""
from __future__ import annotations

import datetime
import logging
import threading
from contextlib import asynccontextmanager
from typing import Optional

from anthropic import Anthropic
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI

from watcher import db
from watcher import marketplace_configs as marketplace_configs_repo
from watcher.admin.routes import router
from watcher.liveness import run_auction_end_sweep, run_liveness_sweep
from watcher.marketplaces import MARKETPLACES
from watcher.pipeline import collect_finished_batches, run_marketplace_cycle, send_digest, submit_pending_scoring
from watcher.price_watch import check_item, run_price_watch_sweep
from watcher.seed import seed_default_searches
from watcher.settings import Settings, load_settings
from watcher import watched_items as watched_items_repo

logger = logging.getLogger(__name__)

TICK_INTERVAL_MINUTES = 5
LIVENESS_SWEEP_HOUR = 3
LIVENESS_SWEEP_MINUTE = 30
# An hour before the default digest_time - price-watch alerts are always
# instant (there's no watched-item digest tier), so the exact hour only
# matters in that it shouldn't collide with other scheduled jobs.
PRICE_WATCH_HOUR = 7
PRICE_WATCH_MINUTE = 0


def _is_due(config, now: datetime.datetime) -> bool:
    if config.last_fetch_at is None:
        return True
    last = datetime.datetime.strptime(config.last_fetch_at, "%Y-%m-%d %H:%M:%S")
    return (now - last) >= datetime.timedelta(minutes=config.poll_interval_minutes)


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    settings = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        conn = db.connect(settings.db_path)
        seed_default_searches(conn)
        marketplace_configs_repo.ensure_defaults(conn)
        client = Anthropic(api_key=settings.anthropic_api_key) if settings.anthropic_api_key else None
        app.state.conn = conn
        app.state.settings = settings

        # Background jobs (tick, digest, liveness sweep, manually-triggered
        # runs) get their own connection, separate from the one HTTP routes
        # use - a bare sqlite3.Connection isn't safe for genuinely concurrent
        # use from multiple threads even with check_same_thread=False (that
        # flag only disables a safety check, it doesn't add thread-safety),
        # and more than one of these jobs can legitimately fire at once (e.g.
        # two "Run now" clicks, or a scheduled tick overlapping with one).
        # scheduler_lock then serializes the jobs sharing scheduler_conn
        # against each other; two *separate* sqlite3.Connection objects in
        # WAL mode (this one and the HTTP routes' conn) can safely be used
        # concurrently from different threads, which a single shared
        # Connection object cannot.
        scheduler_conn = db.connect(settings.db_path)
        scheduler_lock = threading.Lock()

        scheduler = BackgroundScheduler()

        def _tick():
            with scheduler_lock:
                now = datetime.datetime.utcnow()
                for key in MARKETPLACES:
                    config = marketplace_configs_repo.get_config(scheduler_conn, key)
                    if config is None or not _is_due(config, now):
                        continue
                    try:
                        result = run_marketplace_cycle(scheduler_conn, settings, key, dry_run=settings.dry_run)
                        logger.info("Marketplace cycle complete (%s): %s", key, result)
                    except Exception:
                        logger.exception("Marketplace cycle failed for %s", key)

                try:
                    auction_sweep_result = run_auction_end_sweep(scheduler_conn, dry_run=settings.dry_run)
                    if auction_sweep_result["removed"] > 0:
                        logger.info("Auction-end sweep complete: %s", auction_sweep_result)
                except Exception:
                    logger.exception("Auction-end sweep failed")

                if client is None:
                    logger.error("ANTHROPIC_API_KEY not configured, skipping scoring submit/collect")
                    return
                try:
                    submit_result = submit_pending_scoring(scheduler_conn, client, settings)
                    if submit_result["requests"] > 0:
                        logger.info("Scoring submit: %s", submit_result)
                except Exception:
                    logger.exception("Scoring submit failed")
                try:
                    collect_result = collect_finished_batches(scheduler_conn, client, settings, dry_run=settings.dry_run)
                    if collect_result["batches_collected"] > 0:
                        logger.info("Scoring collect: %s", collect_result)
                except Exception:
                    logger.exception("Scoring collect failed")

        def _digest_job():
            with scheduler_lock:
                try:
                    send_digest(scheduler_conn, settings, dry_run=settings.dry_run)
                except Exception:
                    logger.exception("Scheduled digest send failed")

        def _liveness_job():
            with scheduler_lock:
                try:
                    result = run_liveness_sweep(scheduler_conn, settings, dry_run=settings.dry_run)
                    logger.info("Liveness sweep complete: %s", result)
                except Exception:
                    logger.exception("Scheduled liveness sweep failed")

        def _price_watch_job():
            with scheduler_lock:
                if client is None:
                    logger.error("ANTHROPIC_API_KEY not configured, skipping price-watch sweep")
                    return
                try:
                    result = run_price_watch_sweep(scheduler_conn, client, settings, dry_run=settings.dry_run)
                    logger.info("Price-watch sweep complete: %s", result)
                except Exception:
                    logger.exception("Scheduled price-watch sweep failed")

        def _run_now(key: str):
            with scheduler_lock:
                try:
                    result = run_marketplace_cycle(scheduler_conn, settings, key, dry_run=settings.dry_run)
                    logger.info("Manually-triggered marketplace run complete (%s): %s", key, result)
                except Exception:
                    logger.exception("Manually-triggered marketplace run failed for %s", key)
                    return

                try:
                    auction_sweep_result = run_auction_end_sweep(scheduler_conn, dry_run=settings.dry_run)
                    if auction_sweep_result["removed"] > 0:
                        logger.info("Auction-end sweep complete: %s", auction_sweep_result)
                except Exception:
                    logger.exception("Auction-end sweep failed")

                if client is None:
                    logger.error("ANTHROPIC_API_KEY not configured, cannot submit/collect scoring")
                    return
                try:
                    submit_result = submit_pending_scoring(scheduler_conn, client, settings)
                    logger.info("Scoring submit: %s", submit_result)
                    collect_result = collect_finished_batches(scheduler_conn, client, settings, dry_run=settings.dry_run)
                    logger.info("Scoring collect: %s", collect_result)
                except Exception:
                    logger.exception("Scoring submit/collect failed after manual run of %s", key)

        def _check_watched_item_now(item_id: int):
            with scheduler_lock:
                if client is None:
                    logger.error("ANTHROPIC_API_KEY not configured, cannot check watched item %s", item_id)
                    return
                item = watched_items_repo.get_watched_item(scheduler_conn, item_id)
                if item is None:
                    return
                try:
                    result = check_item(scheduler_conn, client, settings, item, dry_run=settings.dry_run)
                    logger.info("Manually-triggered watched-item check complete (%s): %s", item.name, result)
                except Exception:
                    logger.exception("Manually-triggered watched-item check failed for %s", item.name)

        def trigger_marketplace_run(key: str) -> None:
            # Runs on the scheduler's own thread, same as every other
            # scheduled job - never blocks the request that triggered it.
            # scheduler_lock means a run triggered while another job is in
            # progress queues up and runs right after, rather than racing it.
            scheduler.add_job(_run_now, args=[key], next_run_time=datetime.datetime.now())

        def trigger_watched_item_check(item_id: int) -> None:
            scheduler.add_job(_check_watched_item_now, args=[item_id], next_run_time=datetime.datetime.now())

        scheduler.add_job(_tick, "interval", minutes=TICK_INTERVAL_MINUTES, next_run_time=datetime.datetime.now())
        hour, minute = settings.digest_time.split(":")
        scheduler.add_job(_digest_job, CronTrigger(hour=int(hour), minute=int(minute)))
        scheduler.add_job(_liveness_job, CronTrigger(hour=LIVENESS_SWEEP_HOUR, minute=LIVENESS_SWEEP_MINUTE))
        scheduler.add_job(_price_watch_job, CronTrigger(hour=PRICE_WATCH_HOUR, minute=PRICE_WATCH_MINUTE))
        scheduler.start()
        app.state.scheduler = scheduler
        app.state.trigger_marketplace_run = trigger_marketplace_run
        app.state.trigger_watched_item_check = trigger_watched_item_check

        yield

        if scheduler.running:
            scheduler.shutdown(wait=False)
        conn.close()
        scheduler_conn.close()

    app = FastAPI(title="Watcher admin", lifespan=lifespan)
    app.include_router(router)
    return app
