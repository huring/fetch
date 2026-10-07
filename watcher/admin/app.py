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
from contextlib import asynccontextmanager
from typing import Optional

from anthropic import Anthropic
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI

from watcher import db
from watcher import marketplace_configs as marketplace_configs_repo
from watcher.admin.routes import router
from watcher.liveness import run_liveness_sweep
from watcher.marketplaces import MARKETPLACES
from watcher.pipeline import run_marketplace_cycle, send_digest
from watcher.seed import seed_default_searches
from watcher.settings import Settings, load_settings

logger = logging.getLogger(__name__)

TICK_INTERVAL_MINUTES = 5
LIVENESS_SWEEP_HOUR = 3
LIVENESS_SWEEP_MINUTE = 30


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

        scheduler = BackgroundScheduler()

        def _tick():
            if client is None:
                logger.error("ANTHROPIC_API_KEY not configured, skipping tick")
                return
            now = datetime.datetime.utcnow()
            for key in MARKETPLACES:
                config = marketplace_configs_repo.get_config(conn, key)
                if config is None or not _is_due(config, now):
                    continue
                try:
                    result = run_marketplace_cycle(conn, client, settings, key, dry_run=settings.dry_run)
                    logger.info("Marketplace cycle complete (%s): %s", key, result)
                except Exception:
                    logger.exception("Marketplace cycle failed for %s", key)

        def _digest_job():
            try:
                send_digest(conn, settings, dry_run=settings.dry_run)
            except Exception:
                logger.exception("Scheduled digest send failed")

        def _liveness_job():
            try:
                result = run_liveness_sweep(conn, settings, dry_run=settings.dry_run)
                logger.info("Liveness sweep complete: %s", result)
            except Exception:
                logger.exception("Scheduled liveness sweep failed")

        def _run_now(key: str):
            if client is None:
                logger.error("ANTHROPIC_API_KEY not configured, cannot run %s manually", key)
                return
            try:
                result = run_marketplace_cycle(conn, client, settings, key, dry_run=settings.dry_run)
                logger.info("Manually-triggered marketplace run complete (%s): %s", key, result)
            except Exception:
                logger.exception("Manually-triggered marketplace run failed for %s", key)

        def trigger_marketplace_run(key: str) -> None:
            # Runs on the scheduler's own thread, same as every other
            # scheduled job - never blocks the request that triggered it.
            scheduler.add_job(_run_now, args=[key], next_run_time=datetime.datetime.now())

        scheduler.add_job(_tick, "interval", minutes=TICK_INTERVAL_MINUTES, next_run_time=datetime.datetime.now())
        hour, minute = settings.digest_time.split(":")
        scheduler.add_job(_digest_job, CronTrigger(hour=int(hour), minute=int(minute)))
        scheduler.add_job(_liveness_job, CronTrigger(hour=LIVENESS_SWEEP_HOUR, minute=LIVENESS_SWEEP_MINUTE))
        scheduler.start()
        app.state.scheduler = scheduler
        app.state.trigger_marketplace_run = trigger_marketplace_run

        yield

        scheduler.shutdown(wait=False)
        conn.close()

    app = FastAPI(title="Watcher admin", lifespan=lifespan)
    app.include_router(router)
    return app
