"""Cheap, approximate result-size estimates for the NLP search builder
(backlog #21) - "will this prompt give me a handful of results or
thousands?" so Lars can tell whether to narrow or widen it before creating
the search for real. Never touches the database and never runs the
deterministic prefilter - this is the raw marketplace pool size the prefilter
and (for a rated search) Claude would otherwise have to work through, which
is exactly the number that matters for deciding whether to tweak the prompt.

One lightweight request per phrase per marketplace (or free, for Rehifi - see
its count()) - no pagination, no enrichment - so this is fast enough to run
synchronously from an admin UI button click, unlike a real fetch.
"""
from __future__ import annotations

import logging
from typing import Dict

from watcher.marketplaces import CountEstimate, get as get_marketplace
from watcher.models import Search
from watcher.settings import Settings
from watcher.sources.base import SourceError

logger = logging.getLogger(__name__)


def estimate_result_counts(settings: Settings, search: Search) -> Dict[str, CountEstimate]:
    """One CountEstimate per marketplace in search.marketplaces, summed
    across every phrase in search.search_phrases. A marketplace that fails
    (network error, or isn't actually registered) is simply left out of the
    result rather than raising - one broken estimate shouldn't block seeing
    the others, and the admin UI already treats "no entry for this
    marketplace" as "couldn't estimate this one"."""
    results: Dict[str, CountEstimate] = {}
    for key in search.marketplaces:
        marketplace = get_marketplace(key)
        if marketplace is None or marketplace.estimate_count is None:
            continue
        total = 0
        exact = True
        try:
            for phrase in search.search_phrases:
                # None is fine here: none of the current estimate_count
                # implementations read their `config` argument (it's only in
                # the signature for shape-parity with `fetch`) - this avoids
                # this preview-only module needing a database connection just
                # to look up marketplace_configs rows nobody reads.
                estimate = marketplace.estimate_count(phrase, search, None, settings)
                total += estimate.total
                exact = exact and estimate.exact
        except SourceError as exc:
            logger.warning("Could not estimate result count for %s: %s", key, exc)
            continue
        results[key] = CountEstimate(total=total, exact=exact)
    return results
