"""Slack notifications via a single incoming webhook.

Two tiers: an instant alert for standout finds, and a once-daily digest for
everything else above the lower score threshold, grouped by container.
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)


class NotifyError(Exception):
    pass


def _post(webhook_url: str, text: str) -> None:
    response = requests.post(webhook_url, json={"text": text}, timeout=10)
    if response.status_code >= 300:
        raise NotifyError(f"Slack webhook returned {response.status_code}: {response.text}")


def _price_str(price: Optional[int]) -> str:
    return f"{price} SEK" if price is not None else "unknown price"


def format_instant(
    container_name: str, title: str, price: Optional[int], url: str, score: int, reasoning: str, price_assessment: str
) -> str:
    return (
        f":star: *Score {score}/10* - {container_name}\n"
        f"*<{url}|{title}>* - {_price_str(price)}\n"
        f"{reasoning}\n"
        f"_Price assessment: {price_assessment}_"
    )


def send_instant(
    webhook_url: str,
    container_name: str,
    title: str,
    price: Optional[int],
    url: str,
    score: int,
    reasoning: str,
    price_assessment: str,
    dry_run: bool = False,
) -> None:
    text = format_instant(container_name, title, price, url, score, reasoning, price_assessment)
    if dry_run:
        logger.info("[dry-run] would send instant Slack alert:\n%s", text)
        return
    _post(webhook_url, text)


DigestEntry = Tuple[str, Optional[int], str, int, str]  # title, price, url, score, reasoning


def format_digest(entries_by_container: Dict[str, List[DigestEntry]]) -> str:
    lines = [":clipboard: *Daily digest*"]
    for container_name, entries in entries_by_container.items():
        lines.append(f"\n*{container_name}*")
        for title, price, url, score, reasoning in entries:
            lines.append(f"- *<{url}|{title}>* ({score}/10, {_price_str(price)}) - {reasoning}")
    return "\n".join(lines)


def send_digest(webhook_url: str, entries_by_container: Dict[str, List[DigestEntry]], dry_run: bool = False) -> None:
    if not entries_by_container:
        return
    text = format_digest(entries_by_container)
    if dry_run:
        logger.info("[dry-run] would send Slack digest:\n%s", text)
        return
    _post(webhook_url, text)


def send_health_alert(
    webhook_url: str, source: str, consecutive_failures: int, last_error: str, dry_run: bool = False
) -> None:
    text = f":warning: *{source}* has failed {consecutive_failures} runs in a row.\nLast error: {last_error}"
    if dry_run:
        logger.info("[dry-run] would send Slack health alert:\n%s", text)
        return
    _post(webhook_url, text)
