"""Slack notifications via a single incoming webhook.

Two tiers: an instant alert for standout finds, and a once-daily digest for
everything else above the lower score threshold, grouped by search.
"""
from __future__ import annotations

import datetime
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


def _auction_suffix(auction_ends_at: Optional[str]) -> str:
    """A plain-mode listing/digest entry has no Claude reasoning text to
    mention an auction's deadline in (unlike a rated listing - see
    claude_scorer._build_user_text/marketplaces.Marketplace.is_auction's
    scoring_note, which asks Claude to mention it itself), so this is
    appended algorithmically instead, from the same auction_ends_at the
    rated path feeds to Claude."""
    if not auction_ends_at:
        return ""
    ends_at = datetime.datetime.strptime(auction_ends_at, "%Y-%m-%d %H:%M:%S")
    remaining = ends_at - datetime.datetime.utcnow()
    if remaining <= datetime.timedelta(0):
        return " (auction ended)"
    if remaining < datetime.timedelta(hours=1):
        return f" (auction ends in {int(remaining.total_seconds() // 60)} min)"
    if remaining < datetime.timedelta(days=1):
        return f" (auction ends in {int(remaining.total_seconds() // 3600)}h)"
    return f" (auction ends in {remaining.days}d)"


def format_instant(
    search_name: str, title: str, price: Optional[int], url: str, score: int, reasoning: str, price_assessment: str
) -> str:
    return (
        f":star: *Score {score}/10* - {search_name}\n"
        f"*<{url}|{title}>* - {_price_str(price)}\n"
        f"{reasoning}\n"
        f"_Price assessment: {price_assessment}_"
    )


def send_instant(
    webhook_url: str,
    search_name: str,
    title: str,
    price: Optional[int],
    url: str,
    score: int,
    reasoning: str,
    price_assessment: str,
    dry_run: bool = False,
) -> None:
    text = format_instant(search_name, title, price, url, score, reasoning, price_assessment)
    if dry_run:
        logger.info("[dry-run] would send instant Slack alert:\n%s", text)
        return
    _post(webhook_url, text)


DigestEntry = Tuple[str, Optional[int], str, int, str]  # title, price, url, score, reasoning
# title, price, url, auction_ends_at - a "plain" (no-AI) search's match. The
# rated path doesn't need this in its own tuple shape - Claude is asked to
# mention an auction's deadline in its own reasoning text instead.
PlainDigestEntry = Tuple[str, Optional[int], str, Optional[str]]


def format_digest(
    entries_by_search: Dict[str, List[DigestEntry]],
    plain_entries_by_search: Optional[Dict[str, List[PlainDigestEntry]]] = None,
) -> str:
    lines = [":clipboard: *Daily digest*"]
    for search_name, entries in entries_by_search.items():
        lines.append(f"\n*{search_name}*")
        for title, price, url, score, reasoning in entries:
            lines.append(f"- *<{url}|{title}>* ({score}/10, {_price_str(price)}) - {reasoning}")
    for search_name, entries in (plain_entries_by_search or {}).items():
        lines.append(f"\n*{search_name}*")
        for title, price, url, auction_ends_at in entries:
            lines.append(f"- *<{url}|{title}>* - {_price_str(price)}{_auction_suffix(auction_ends_at)}")
    return "\n".join(lines)


def send_digest(
    webhook_url: str,
    entries_by_search: Dict[str, List[DigestEntry]],
    plain_entries_by_search: Optional[Dict[str, List[PlainDigestEntry]]] = None,
    dry_run: bool = False,
) -> None:
    if not entries_by_search and not plain_entries_by_search:
        return
    text = format_digest(entries_by_search, plain_entries_by_search)
    if dry_run:
        logger.info("[dry-run] would send Slack digest:\n%s", text)
        return
    _post(webhook_url, text)


def format_plain_instant(
    search_name: str, title: str, price: Optional[int], url: str, auction_ends_at: Optional[str] = None
) -> str:
    return (
        f":moneybag: *Price alert* - {search_name}\n"
        f"*<{url}|{title}>* - {_price_str(price)}{_auction_suffix(auction_ends_at)}"
    )


def send_plain_instant(
    webhook_url: str,
    search_name: str,
    title: str,
    price: Optional[int],
    url: str,
    auction_ends_at: Optional[str] = None,
    dry_run: bool = False,
) -> None:
    text = format_plain_instant(search_name, title, price, url, auction_ends_at)
    if dry_run:
        logger.info("[dry-run] would send plain Slack price alert:\n%s", text)
        return
    _post(webhook_url, text)


def format_stale_opportunity(
    search_name: str, title: str, price: Optional[int], url: str, score: int, days_active: int
) -> str:
    return (
        f":hourglass: *Still listed after {days_active} days* - {search_name}\n"
        f"*<{url}|{title}>* - {_price_str(price)} (scored {score}/10)\n"
        f"Hasn't sold in a while - might be worth a lower offer."
    )


def send_stale_opportunity(
    webhook_url: str,
    search_name: str,
    title: str,
    price: Optional[int],
    url: str,
    score: int,
    days_active: int,
    dry_run: bool = False,
) -> None:
    text = format_stale_opportunity(search_name, title, price, url, score, days_active)
    if dry_run:
        logger.info("[dry-run] would send stale-opportunity Slack alert:\n%s", text)
        return
    _post(webhook_url, text)


def format_price_watch_instant(
    item_name: str, title: str, price: int, url: str, target_price: Optional[int]
) -> str:
    target_note = f" (target: {target_price} SEK)" if target_price is not None else ""
    return f":moneybag: *Price watch* - {item_name}\n*<{url}|{title}>* is now {price} SEK{target_note}"


def send_price_watch_instant(
    webhook_url: str,
    item_name: str,
    title: str,
    price: int,
    url: str,
    target_price: Optional[int],
    dry_run: bool = False,
) -> None:
    text = format_price_watch_instant(item_name, title, price, url, target_price)
    if dry_run:
        logger.info("[dry-run] would send price-watch Slack alert:\n%s", text)
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
