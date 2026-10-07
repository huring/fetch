"""Deployment-level settings, read from environment variables.

Distinct from searches and marketplace config (both of which live in the DB
and are edited via the admin UI): these are operational knobs set once at
deploy time in the Portainer stack. Per-marketplace polling interval, request
delay, and auth live in marketplace_configs.py instead - not here.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


def _bool_env(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    db_path: str
    digest_time: str
    score_instant_threshold: int
    score_digest_min: int
    health_alert_after_n_failures: int
    claude_model: str
    scoring_batch_size: int
    max_pages_per_query: int
    anthropic_api_key: str
    slack_webhook_url: str
    dry_run: bool
    admin_port: int


def load_settings() -> Settings:
    return Settings(
        db_path=os.environ.get("DB_PATH", "/data/watcher.db"),
        digest_time=os.environ.get("DIGEST_TIME", "08:00"),
        score_instant_threshold=int(os.environ.get("SCORE_INSTANT_THRESHOLD", "8")),
        score_digest_min=int(os.environ.get("SCORE_DIGEST_MIN", "5")),
        health_alert_after_n_failures=int(os.environ.get("HEALTH_ALERT_AFTER_N_FAILURES", "3")),
        claude_model=os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5"),
        scoring_batch_size=int(os.environ.get("SCORING_BATCH_SIZE", "25")),
        max_pages_per_query=int(os.environ.get("MAX_PAGES_PER_QUERY", "2")),
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
        slack_webhook_url=os.environ.get("SLACK_WEBHOOK_URL", ""),
        dry_run=_bool_env("DRY_RUN", False),
        admin_port=int(os.environ.get("ADMIN_PORT", "8000")),
    )
