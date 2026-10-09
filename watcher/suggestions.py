"""Turns a search's accumulated listing feedback (see watcher/feedback.py and
the admin UI's per-listing like/not-interested buttons) into proposed edits
to that search's own config - the "Learned from feedback" panel on the
search edit page (backlog #30).

Every suggestion is {field, value, label, count}: `field`/`value` are exactly
what the Apply/Ignore forms post back (see watcher/admin/routes.py), `label`
is the human-readable text shown next to the buttons. Nothing here is ever
applied automatically - a suggestion only changes the search once Lars clicks
Apply, and only ever proposes values for fields prefilter.py/claude_scorer.py
already read (excluded_models/excluded_words/required_keywords/max_price/
min_price/require_shipping/watched_models) - no new filtering mechanism.
"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from typing import Dict, List, Optional

from watcher.models import Search


def _suggestion_key(field: str, value: str) -> str:
    return f"{field}|{value}"


def _fetch_feedback_rows(conn: sqlite3.Connection, search_id: int) -> List[sqlite3.Row]:
    return conn.execute(
        "SELECT title, price, feedback, feedback_reason, feedback_detail FROM listings "
        "WHERE search_id = ? AND feedback IS NOT NULL",
        (search_id,),
    ).fetchall()


def build_suggestions(conn: sqlite3.Connection, search: Search) -> List[Dict]:
    if search.id is None:
        return []
    rows = _fetch_feedback_rows(conn, search.id)
    ignored = set(search.ignored_suggestions)
    suggestions: List[Dict] = []

    def add(field: str, value: str, label: str, count: int) -> None:
        key = _suggestion_key(field, value)
        if key in ignored:
            return
        suggestions.append({"field": field, "value": value, "label": label, "count": count})

    # --- content reasons with a free-text detail: one suggestion per
    # distinct detail string (trusted as-is, since Lars typed it himself
    # when dismissing - no inference, no minimum-count threshold).
    by_detail: Dict[str, Dict[str, int]] = {
        "wrong_model": defaultdict(int),
        "missing_feature": defaultdict(int),
        "bad_condition": defaultdict(int),
    }
    too_expensive_prices: List[int] = []
    too_cheap_prices: List[int] = []
    no_shipping_count = 0
    liked_patterns: Dict[str, Optional[int]] = {}

    for row in rows:
        feedback = row["feedback"]
        reason = row["feedback_reason"]
        detail = (row["feedback_detail"] or "").strip()
        price = row["price"]

        if feedback == "liked":
            pattern = detail or row["title"]
            if pattern and pattern not in liked_patterns:
                liked_patterns[pattern] = price
            continue

        if feedback != "dismissed":
            continue

        if reason in by_detail and detail:
            by_detail[reason][detail] += 1
        elif reason == "too_expensive" and price is not None:
            too_expensive_prices.append(price)
        elif reason == "too_cheap" and price is not None:
            too_cheap_prices.append(price)
        elif reason == "no_shipping":
            no_shipping_count += 1

    for detail, count in by_detail["wrong_model"].items():
        if not any(detail.lower() == m.lower() for m in search.excluded_models):
            add("excluded_models", detail, f'Exclude model "{detail}" ({count} dismissal(s))', count)

    for detail, count in by_detail["missing_feature"].items():
        if not any(detail.lower() == kw.lower() for kw in search.required_keywords):
            caveat = (
                " (note: required keywords are matched as 'any of', so if this search already has"
                " others, this only tightens things when it's the only one)"
                if search.required_keywords
                else ""
            )
            add(
                "required_keywords", detail,
                f'Require "{detail}" to be mentioned ({count} dismissal(s) for missing it){caveat}', count,
            )

    for detail, count in by_detail["bad_condition"].items():
        if not any(detail.lower() == w.lower() for w in search.excluded_words):
            add("excluded_words", detail, f'Exclude listings mentioning "{detail}" ({count} dismissal(s))', count)

    if too_expensive_prices:
        suggested_max = min(too_expensive_prices) - 1
        if suggested_max > 0 and (search.max_price is None or suggested_max < search.max_price):
            add(
                "max_price", str(suggested_max),
                f"Lower max price to {suggested_max} ({len(too_expensive_prices)} dismissed as too expensive)",
                len(too_expensive_prices),
            )

    if too_cheap_prices:
        suggested_min = max(too_cheap_prices) + 1
        if search.min_price is None or suggested_min > search.min_price:
            add(
                "min_price", str(suggested_min),
                f"Raise min price to {suggested_min} ({len(too_cheap_prices)} dismissed as too cheap/suspicious)",
                len(too_cheap_prices),
            )

    if no_shipping_count and not search.require_shipping:
        add(
            "require_shipping", "true",
            f"Require shipping ({no_shipping_count} dismissed for having none)", no_shipping_count,
        )

    for pattern, price in liked_patterns.items():
        if any(pattern.lower() == wm.pattern.lower() for wm in search.watched_models):
            continue
        value = json.dumps({"pattern": pattern, "good_price": str(price) if price is not None else ""}, sort_keys=True)
        label = f'Add "{pattern}" as an ideal watched model'
        if price is not None:
            label += f" (liked at {price})"
        add("watched_models", value, label, 1)

    return suggestions
