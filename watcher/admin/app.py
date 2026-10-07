"""FastAPI app factory: wires the admin UI, DB connection and the scheduler."""
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
from watcher.admin.routes import router
from watcher.pipeline import run_once, send_digest
from watcher.seed import seed_default_containers
from watcher.settings import Settings, load_settings

logger = logging.getLogger(__name__)


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    settings = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        conn = db.connect(settings.db_path)
        seed_default_containers(conn)
        client = Anthropic(api_key=settings.anthropic_api_key) if settings.anthropic_api_key else None
        app.state.conn = conn
        app.state.settings = settings

        scheduler = BackgroundScheduler()

        def _run_job():
            if client is None:
                logger.error("ANTHROPIC_API_KEY not configured, skipping scheduled run")
                return
            try:
                run_once(conn, client, settings, dry_run=settings.dry_run)
            except Exception:
                logger.exception("Scheduled run failed")

        def _digest_job():
            try:
                send_digest(conn, settings, dry_run=settings.dry_run)
            except Exception:
                logger.exception("Scheduled digest send failed")

        scheduler.add_job(
            _run_job, "interval", minutes=settings.poll_interval_minutes,
            next_run_time=datetime.datetime.now(),
        )
        hour, minute = settings.digest_time.split(":")
        scheduler.add_job(_digest_job, CronTrigger(hour=int(hour), minute=int(minute)))
        scheduler.start()
        app.state.scheduler = scheduler

        yield

        scheduler.shutdown(wait=False)
        conn.close()

    app = FastAPI(title="Watcher admin", lifespan=lifespan)
    app.include_router(router)
    return app
