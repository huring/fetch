from unittest.mock import patch

from watcher.marketplaces import MARKETPLACES, get as get_marketplace
from watcher.models import MarketplaceConfig, Search
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


def make_config(key):
    return MarketplaceConfig(key=key, poll_interval_minutes=240, request_delay_seconds=0)


def test_every_registered_marketplace_has_an_estimate_count_and_category_note():
    """The NLP search builder (backlog #21) relies on every marketplace
    offering both - a marketplace added later without them would silently
    be unusable for a prompt-generated search's count preview and marketplace
    selection, so this is worth asserting as a registry-wide contract."""
    for marketplace in MARKETPLACES.values():
        assert marketplace.estimate_count is not None, marketplace.key
        assert marketplace.category_note, marketplace.key


def test_blocket_estimate_count_uses_county_code_for_a_local_search():
    search = Search(name="S", scope="local", location="Norrbotten")
    with patch("watcher.sources.blocket.count", return_value=11) as count:
        estimate = get_marketplace("blocket").estimate_count("ski-doo", search, make_config("blocket"), make_settings())

    count.assert_called_once_with("ski-doo", location_code="0.300025")
    assert estimate.total == 11
    assert estimate.exact is True


def test_blocket_estimate_count_has_no_location_code_for_a_national_search():
    search = Search(name="S", scope="national")
    with patch("watcher.sources.blocket.count", return_value=2789) as count:
        get_marketplace("blocket").estimate_count("förstärkare", search, make_config("blocket"), make_settings())

    count.assert_called_once_with("förstärkare", location_code=None)


def test_vinted_estimate_count_passes_through_exactness():
    search = Search(name="S")
    with patch("watcher.sources.vinted.count", return_value=(288, False)):
        estimate = get_marketplace("vinted").estimate_count("onkyo", search, make_config("vinted"), make_settings())

    assert estimate.total == 288
    assert estimate.exact is False


def test_rehifi_estimate_count_is_always_exact():
    search = Search(name="S")
    with patch("watcher.sources.rehifi.count", return_value=3):
        estimate = get_marketplace("rehifi").estimate_count("rega", search, make_config("rehifi"), make_settings())

    assert estimate.total == 3
    assert estimate.exact is True


def test_auctionet_estimate_count_is_always_exact():
    search = Search(name="S")
    with patch("watcher.sources.auctionet.count", return_value=42):
        estimate = get_marketplace("auctionet").estimate_count("receiver", search, make_config("auctionet"), make_settings())

    assert estimate.total == 42
    assert estimate.exact is True
