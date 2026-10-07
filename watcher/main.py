"""CLI entrypoint.

--once runs a single fetch-score-notify cycle and exits (for manual testing
or ad-hoc runs). Without --once, starts the long-lived service: the admin UI
plus the background scheduler (polling interval + daily digest).
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

from anthropic import Anthropic

from watcher import db
from watcher import marketplace_configs
from watcher.pipeline import run_once
from watcher.seed import seed_default_searches
from watcher.settings import load_settings


def _configure_logging() -> None:
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def main(argv=None) -> int:
    _configure_logging()
    logger = logging.getLogger("watcher.main")

    parser = argparse.ArgumentParser(description="Secondhand marketplace watcher")
    parser.add_argument("--once", action="store_true", help="Run a single fetch-score-notify cycle and exit")
    parser.add_argument("--dry-run", action="store_true", help="Don't actually send notifications")
    args = parser.parse_args(argv)

    settings = load_settings()
    dry_run = args.dry_run or settings.dry_run
    once = args.once or _env_flag("ONCE")

    if once:
        if not settings.anthropic_api_key:
            logger.error("ANTHROPIC_API_KEY is required")
            return 1
        conn = db.connect(settings.db_path)
        seed_default_searches(conn)
        marketplace_configs.ensure_defaults(conn)
        client = Anthropic(api_key=settings.anthropic_api_key)
        result = run_once(conn, client, settings, dry_run=dry_run)
        logger.info("Run complete: %s", result)
        conn.close()
        return 0

    import uvicorn

    from watcher.admin.app import create_app

    app = create_app(settings)
    uvicorn.run(app, host="0.0.0.0", port=settings.admin_port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
