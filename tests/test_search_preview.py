from unittest.mock import patch

from watcher.models import Search
from watcher.scoring.search_preview import estimate_result_counts
from watcher.settings import Settings


def make_settings(**overrides):
    base = dict(
        db_path=":memory:",
        digest_time="08:00",
        score_instant_threshold=8,
        score_digest_min=5,
        health_alert_after_n_failures=3,
        claude_model="claude-haiku-4-5",
        scoring_batch_size=10,
        max_pages_per_query=1,
        anthropic_api_key="x",
        slack_webhook_url="",
        dry_run=False,
        admin_port=8000,
    )
    base.update(overrides)
    return Settings(**base)


def test_sums_counts_across_phrases_for_one_marketplace():
    search = Search(name="S", search_phrases=["förstärkare", "amplifier"], marketplaces=["blocket"])
    with patch("watcher.sources.blocket.count", side_effect=[2789, 50]):
        results = estimate_result_counts(make_settings(), search)

    assert results["blocket"].total == 2839
    assert results["blocket"].exact is True


def test_reports_one_estimate_per_marketplace():
    search = Search(name="S", search_phrases=["onkyo"], marketplaces=["blocket", "rehifi"])
    with patch("watcher.sources.blocket.count", return_value=100), patch("watcher.sources.rehifi.count", return_value=3):
        results = estimate_result_counts(make_settings(), search)

    assert set(results.keys()) == {"blocket", "rehifi"}
    assert results["rehifi"].total == 3


def test_a_single_inexact_phrase_marks_the_whole_marketplace_inexact():
    search = Search(name="S", search_phrases=["onkyo", "sony"], marketplaces=["vinted"])
    with patch("watcher.sources.vinted.count", side_effect=[(96, True), (288, False)]):
        results = estimate_result_counts(make_settings(), search)

    assert results["vinted"].total == 96 + 288
    assert results["vinted"].exact is False


def test_a_failing_marketplace_is_left_out_rather_than_raising():
    from watcher.sources.base import SourceError

    search = Search(name="S", search_phrases=["onkyo"], marketplaces=["blocket", "rehifi"])
    with patch("watcher.sources.blocket.count", side_effect=SourceError("boom")), patch("watcher.sources.rehifi.count", return_value=3):
        results = estimate_result_counts(make_settings(), search)

    assert "blocket" not in results
    assert results["rehifi"].total == 3


def test_an_unregistered_marketplace_key_is_skipped():
    search = Search(name="S", search_phrases=["onkyo"], marketplaces=["not-a-real-marketplace"])
    assert estimate_result_counts(make_settings(), search) == {}
