"""Watched items: track one arbitrary product URL (any retailer, not just a
registered marketplace) for a price drop, on its own daily/weekly/monthly
schedule - independent of the search/marketplace machinery entirely.

Unlike Blocket/Vinted/Rehifi, an arbitrary retailer's markup is unknown and
changes without notice, so there's no per-site parser here - the fetched
page's visible text is handed to Claude with a structured-output schema
(title/description/price/currency/in_stock) and it does the extraction. This
degrades more gracefully than a regex scraper as a site's markup changes, at
the cost of a small Claude call per check - negligible at the volume this is
meant for (a handful of items, checked at most daily).

When present, a page's own schema.org Product JSON-LD block (see
sources/base.extract_jsonld_product) is quoted into the prompt ahead of the
plain text and the extracted image comes from it (falling back to the page's
OpenGraph og:image) - most e-commerce platforms emit this for Google's
rich-snippet eligibility regardless of how the visible page itself is
rendered, so it's often the only reliable source of price/availability on a
JS-heavy page whose plain-text content never shows a price at all (backlog
#23). The extracted description/image are shown in the admin UI as a small
confirmation card ("is this the right item?") - never downloaded/stored,
just linked to by URL.

This is a single plain (non-batch) Claude call per item rather than going
through the Message Batches API like listing scoring does: watched-item
checks happen at a scale (a handful of items, daily/weekly/monthly) where the
Batch API's 50% discount is worth a few cents a month, not worth the
submit/collect bookkeeping it'd add here.

"Find this item used": if a watched item has find_used set, once its product
title has been extracted at least once, a linked "plain" search (see
models.Search.scoring_mode) is created automatically with that title as its
one search phrase, running on every registered marketplace - from then on,
the existing marketplace fetch/prefilter/digest machinery handles finding a
used copy with no further code here. Its instant_alert_price is set to the
watched item's own target_price, i.e. "a good price for this item" applies
regardless of whether the copy that turns up is new or used.
"""
from __future__ import annotations

import datetime
import json
import logging
import re
from typing import Any, Dict, Optional

from anthropic import Anthropic
from pydantic import BaseModel, Field

from watcher import searches as searches_repo
from watcher import storage
from watcher import watched_items as watched_items_repo
from watcher.marketplaces import MARKETPLACES
from watcher.models import Search, WatchedItem
from watcher.notify import slack
from watcher.scoring.claude_scorer import estimate_cost_usd
from watcher.scoring.schema import strict_json_schema
from watcher.settings import Settings
from watcher.sources.base import extract_jsonld_product, extract_og_image, get_text

logger = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Gecko/20100101 Firefox/120.0"
MAX_TOKENS = 1024
# A product page can carry a lot of unrelated chrome (nav, related items,
# reviews) - capping keeps the Claude call cheap and focused. Generous enough
# to comfortably include the actual product block on virtually any retailer.
MAX_PAGE_TEXT_CHARS = 8000
# Caps how much of a found JSON-LD Product block gets quoted into the
# prompt - it's structured data, not prose, so this is generous relative to
# MAX_PAGE_TEXT_CHARS without risking a pathological block (e.g. one that
# embeds a huge review list) blowing up the request.
MAX_JSONLD_HINT_CHARS = 2000

_CHECK_INTERVALS: Dict[str, datetime.timedelta] = {
    "daily": datetime.timedelta(days=1),
    "weekly": datetime.timedelta(days=7),
    # A calendar month has no fixed length - 30 days is a deliberate
    # approximation, not meant to be exact (price drops aren't time-sensitive
    # enough for that to matter).
    "monthly": datetime.timedelta(days=30),
}

_SCRIPT_STYLE_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"[ \t]+")
_BLANK_LINES_RE = re.compile(r"\n\s*\n+")


class _ExtractedProduct(BaseModel):
    title: str = Field(description="The product's name/title as shown on the page.")
    description: Optional[str] = Field(
        default=None,
        description="A short (1-2 sentence) description of the product - enough for someone to confirm this is the right item, e.g. from the page's own product description or key specs. Null if the page has nothing usable.",
    )
    price: Optional[int] = Field(
        default=None, description="The current price as a plain integer (no currency symbol/thousands separator), or null if not found."
    )
    currency: Optional[str] = Field(default=None, description="e.g. 'SEK', 'USD', 'EUR' - null if not found.")
    in_stock: bool = Field(description="Whether the page indicates the product is currently available to buy.")


_RESPONSE_SCHEMA = strict_json_schema(_ExtractedProduct.model_json_schema())


def _html_to_text(html: str) -> str:
    text = _SCRIPT_STYLE_RE.sub(" ", html)
    text = _TAG_RE.sub("\n", text)
    text = _WHITESPACE_RE.sub(" ", text)
    text = _BLANK_LINES_RE.sub("\n", text)
    return text.strip()[:MAX_PAGE_TEXT_CHARS]


def _is_due(item: WatchedItem, now: datetime.datetime) -> bool:
    if item.last_checked_at is None:
        return True
    last = datetime.datetime.strptime(item.last_checked_at, "%Y-%m-%d %H:%M:%S")
    return (now - last) >= _CHECK_INTERVALS[item.check_frequency]


def _extract_product(
    conn, client: Anthropic, settings: Settings, page_text: str, structured_hint: Optional[Dict[str, Any]] = None
) -> Optional[_ExtractedProduct]:
    prompt = "Extract the product name, description, current price, currency and stock status from this product page.\n\n"
    if structured_hint:
        # A schema.org Product JSON-LD block, when present, is far more
        # reliable than the rendered text below for price/availability -
        # many retailer pages render their price client-side in JS, so the
        # plain-text content alone never shows it at all (see backlog #23 -
        # this is what was causing current_price to stay empty).
        hint_json = json.dumps(structured_hint, ensure_ascii=False)[:MAX_JSONLD_HINT_CHARS]
        prompt += f"Structured product data found on the page - prefer this over the free text below when they conflict:\n{hint_json}\n\n"
    prompt += "Page text:\n" + page_text
    message = client.messages.create(
        model=settings.claude_model,
        max_tokens=MAX_TOKENS,
        messages=[{"role": "user", "content": prompt}],
        output_config={"format": {"type": "json_schema", "schema": _RESPONSE_SCHEMA}},
    )
    cost = estimate_cost_usd(
        settings.claude_model, message.usage.input_tokens, message.usage.output_tokens, batch=False
    )
    storage.log_token_usage(conn, None, settings.claude_model, message.usage.input_tokens, message.usage.output_tokens, cost)

    text = next((block.text for block in message.content if getattr(block, "type", None) == "text"), None)
    if text is None:
        return None
    try:
        return _ExtractedProduct.model_validate_json(text)
    except Exception as exc:
        logger.warning("Could not parse Claude's product extraction response: %s", exc)
        return None


def _maybe_create_linked_search(conn, item: WatchedItem) -> None:
    if not item.find_used or item.linked_search_id is not None or not item.extracted_title:
        return
    search = searches_repo.create_search(
        conn,
        Search(
            name=f"Find used: {item.name}",
            scoring_mode="plain",
            search_phrases=[item.extracted_title],
            marketplaces=list(MARKETPLACES.keys()),
            instant_alert_price=item.target_price,
        ),
    )
    watched_items_repo.set_linked_search_id(conn, item.id, search.id)
    logger.info("Created linked 'find used' search %r for watched item %r", search.name, item.name)


def _image_url_from_jsonld(product_jsonld: Dict[str, Any]) -> str:
    image = product_jsonld.get("image")
    if isinstance(image, str):
        return image
    if isinstance(image, list) and image:
        first = image[0]
        return first if isinstance(first, str) else (first.get("url", "") if isinstance(first, dict) else "")
    if isinstance(image, dict):
        return image.get("url", "")
    return ""


def _maybe_send_alert(conn, settings: Settings, item: WatchedItem, price: int, dry_run: bool) -> bool:
    if item.target_price is None or price > item.target_price:
        return False
    if item.last_alert_price is not None and price >= item.last_alert_price:
        return False  # already alerted at this price or lower - don't repeat daily
    if not settings.slack_webhook_url:
        logger.warning("SLACK_WEBHOOK_URL not configured, skipping price-watch alert for %r", item.name)
        return False
    try:
        slack.send_price_watch_instant(
            settings.slack_webhook_url, item.name, item.extracted_title or item.name, price, item.url, item.target_price,
            currency=item.currency or "SEK", dry_run=dry_run,
        )
        if not dry_run:
            watched_items_repo.record_alert(conn, item.id, price)
        return True
    except Exception as exc:
        logger.error("Failed to send price-watch alert for %r: %s", item.name, exc)
        return False


def _maybe_send_dead_alert(
    conn, settings: Settings, item: WatchedItem, consecutive_failures: int, dry_run: bool
) -> None:
    """Liveness/sold-tracking parity with marketplace listings (backlog #6):
    mirrors source_health's "N failures in a row" alert, since a watched
    item's URL otherwise never gets any "this might be dead" signal at
    all - it would just sit there silently showing a stale price forever."""
    if consecutive_failures < settings.health_alert_after_n_failures or item.dead_alert_sent:
        return
    if not settings.slack_webhook_url:
        logger.warning("SLACK_WEBHOOK_URL not configured, skipping dead-link alert for %r", item.name)
        return
    try:
        slack.send_watched_item_dead_alert(
            settings.slack_webhook_url, item.name, item.url, consecutive_failures, dry_run=dry_run
        )
        if not dry_run:
            watched_items_repo.mark_dead_alert_sent(conn, item.id)
    except Exception as exc:
        logger.error("Failed to send dead-link alert for %r: %s", item.name, exc)


def check_item(conn, client: Anthropic, settings: Settings, item: WatchedItem, dry_run: bool = False) -> Dict[str, Any]:
    """Fetches and extracts one watched item's current price, updates its
    check-run state, alerts if it's at/below target_price, and (if
    find_used) provisions its linked marketplace search once a title is
    known. Runs for real regardless of dry_run (the Claude call is the one
    cost dry_run never skips, matching the rest of this project's dry_run
    semantics) - dry_run only gates the Slack post and the state write that
    follows it.
    """
    try:
        html = get_text(item.url, headers={"User-Agent": USER_AGENT})
    except Exception as exc:
        logger.warning("Could not fetch watched item %r (%s): %s", item.name, item.url, exc)
        failures = watched_items_repo.record_check_failure(conn, item.id)
        _maybe_send_dead_alert(conn, settings, item, failures, dry_run)
        return {"checked": False, "alerted": False}

    product_jsonld = extract_jsonld_product(html)
    page_text = _html_to_text(html)
    product = _extract_product(conn, client, settings, page_text, structured_hint=product_jsonld)
    if product is None:
        logger.warning("Could not extract product info for watched item %r", item.name)
        failures = watched_items_repo.record_check_failure(conn, item.id)
        _maybe_send_dead_alert(conn, settings, item, failures, dry_run)
        return {"checked": True, "alerted": False}

    image_url = (_image_url_from_jsonld(product_jsonld) if product_jsonld else "") or extract_og_image(html)
    price = product.price if product.in_stock else None
    logger.info(
        "Watched item %r check: title=%r price=%s currency=%s in_stock=%s image=%s",
        item.name, product.title, product.price, product.currency, product.in_stock, bool(image_url),
    )
    watched_items_repo.record_check_result(
        conn, item.id, price=price, extracted_title=product.title or None, currency=product.currency or None,
        description=product.description or None, image_url=image_url or None,
    )

    alerted = False
    if price is not None:
        item = watched_items_repo.get_watched_item(conn, item.id)  # reload with fresh check-run state
        alerted = _maybe_send_alert(conn, settings, item, price, dry_run)

    item = watched_items_repo.get_watched_item(conn, item.id)
    _maybe_create_linked_search(conn, item)

    return {"checked": True, "alerted": alerted}


def run_price_watch_sweep(conn, client: Anthropic, settings: Settings, dry_run: bool = False) -> Dict[str, Any]:
    now = datetime.datetime.utcnow()
    checked = 0
    alerted = 0
    for item in watched_items_repo.list_watched_items(conn, enabled_only=True):
        if not _is_due(item, now):
            continue
        result = check_item(conn, client, settings, item, dry_run=dry_run)
        if result["checked"]:
            checked += 1
        if result["alerted"]:
            alerted += 1
    return {"items_checked": checked, "items_alerted": alerted}
