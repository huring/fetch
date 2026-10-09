"""Reason taxonomy for the per-listing like/not-interested feedback buttons
(admin UI "Feedback" column, see search_listings.html).

A dismissal reason is tagged as either a content reason (about the item
itself - a future suggestion-review panel, backlog #30, could turn these into
excluded_models/excluded_words/required_keywords/price edits on the search)
or a logistics reason (about the listing, not the item - these map to search
settings like require_shipping instead, or to nothing at all for pure taste).
Keeping the two apart stops "no shipping" from ever polluting a search's
title-matching exclusion lists.
"""
from __future__ import annotations

CONTENT_REASONS = (
    ("wrong_model", "Wrong model/variant"),
    ("missing_feature", "Missing a feature"),
    ("bad_condition", "Bad condition/description"),
    ("too_expensive", "Too expensive"),
    ("too_cheap", "Too cheap/suspicious"),
)

LOGISTICS_REASONS = (
    ("no_shipping", "No shipping"),
    ("too_far", "Too far away"),
    ("not_interested", "Just not interested"),
)

DISMISS_REASONS = CONTENT_REASONS + LOGISTICS_REASONS
DISMISS_REASON_KEYS = {key for key, _ in DISMISS_REASONS}
DISMISS_REASON_LABELS = dict(DISMISS_REASONS)
