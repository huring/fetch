"""Deterministic, non-AI prefilter: max price, excluded models/words, required keywords.

Runs before any Claude call so obviously irrelevant or disqualified listings
never cost a token.
"""
from __future__ import annotations

from typing import Optional, Tuple

from watcher.models import Search
from watcher.wildcard import matches_any


def passes_prefilter(
    title: str, description: str, price: Optional[int], search: Search
) -> Tuple[bool, str]:
    text = f"{title}\n{description}"

    if search.max_price is not None and price is not None and price > search.max_price:
        return False, f"Price {price} exceeds max_price {search.max_price}"

    excluded = matches_any(text, search.excluded_models)
    if excluded:
        return False, f"Matched excluded model pattern '{excluded}'"

    text_lower = text.lower()
    for word in search.excluded_words:
        if word.lower() in text_lower:
            return False, f"Matched excluded word '{word}'"

    if search.required_keywords:
        if not any(kw.lower() in text_lower for kw in search.required_keywords):
            return False, "No required keyword matched"

    return True, "Passed prefilter"
