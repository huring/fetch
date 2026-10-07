"""Deterministic, non-AI prefilter: price bounds, phrase relevance, excluded
models/words, required keywords.

Runs before any Claude call so obviously irrelevant or disqualified listings
never cost a token.
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from watcher.models import Search
from watcher.wildcard import matches_any


def _phrase_words(phrases: List[str]) -> List[str]:
    words: List[str] = []
    for phrase in phrases:
        words.extend(w for w in phrase.lower().split() if w)
    return words


def passes_prefilter(
    title: str, description: str, price: Optional[int], search: Search
) -> Tuple[bool, str]:
    text = f"{title}\n{description}"

    if search.max_price is not None and price is not None and price > search.max_price:
        return False, f"Price {price} exceeds max_price {search.max_price}"

    if search.min_price is not None and price is not None and price < search.min_price:
        return False, f"Price {price} is below min_price {search.min_price}"

    text_lower = text.lower()
    phrase_words = _phrase_words(search.search_phrases)
    if phrase_words and not any(w in text_lower for w in phrase_words):
        # A marketplace's own search can be fuzzy enough to return results
        # that don't actually mention anything we searched for (confirmed
        # live on Vinted - a plain "onkyo" search once returned a t-shirt).
        # If the title/description contains none of the words we searched
        # for, it's almost certainly that kind of noise, not a genuine hit
        # using different terminology.
        return False, "Title/description doesn't mention any word from the search phrases"

    excluded = matches_any(text, search.excluded_models)
    if excluded:
        return False, f"Matched excluded model pattern '{excluded}'"

    for word in search.excluded_words:
        if word.lower() in text_lower:
            return False, f"Matched excluded word '{word}'"

    if search.required_keywords:
        if not any(kw.lower() in text_lower for kw in search.required_keywords):
            return False, "No required keyword matched"

    return True, "Passed prefilter"
