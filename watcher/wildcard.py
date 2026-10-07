"""Glob-style wildcard matching for model-number patterns like 'TX-NR6*' or 'SR?010'.

Case-insensitive substring-style match: the pattern does not have to match the
whole text, only be found within it (e.g. pattern 'tx-nr6*' matches the title
"Onkyo TX-NR656 AV-receiver, bra skick").
"""
from __future__ import annotations

import re
from fnmatch import translate
from functools import lru_cache


@lru_cache(maxsize=512)
def _compiled(pattern: str) -> re.Pattern:
    # fnmatch.translate anchors with \Z; strip that so the pattern can match
    # anywhere inside a larger string rather than requiring a full match.
    regex = translate(pattern)
    regex = regex.replace(r"\Z", "")
    return re.compile(regex, re.IGNORECASE)


def matches_any(text: str, patterns: list[str]) -> str | None:
    """Returns the first pattern that matches ``text``, or None."""
    if not text:
        return None
    for pattern in patterns:
        if _compiled(pattern).search(text):
            return pattern
    return None
