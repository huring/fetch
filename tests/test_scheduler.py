import datetime

from watcher.admin.app import _is_due
from watcher.models import MarketplaceConfig


def make_config(**overrides):
    base = dict(key="blocket", poll_interval_minutes=240, request_delay_seconds=2.0, auth={}, last_fetch_at=None)
    base.update(overrides)
    return MarketplaceConfig(**base)


def test_due_when_never_fetched():
    assert _is_due(make_config(last_fetch_at=None), datetime.datetime.utcnow()) is True


def test_not_due_before_interval_elapses():
    now = datetime.datetime(2026, 1, 1, 12, 0, 0)
    last_fetch_at = (now - datetime.timedelta(minutes=60)).strftime("%Y-%m-%d %H:%M:%S")
    config = make_config(poll_interval_minutes=240, last_fetch_at=last_fetch_at)
    assert _is_due(config, now) is False


def test_due_once_interval_elapses():
    now = datetime.datetime(2026, 1, 1, 12, 0, 0)
    last_fetch_at = (now - datetime.timedelta(minutes=241)).strftime("%Y-%m-%d %H:%M:%S")
    config = make_config(poll_interval_minutes=240, last_fetch_at=last_fetch_at)
    assert _is_due(config, now) is True


def test_due_exactly_at_interval_boundary():
    now = datetime.datetime(2026, 1, 1, 12, 0, 0)
    last_fetch_at = (now - datetime.timedelta(minutes=240)).strftime("%Y-%m-%d %H:%M:%S")
    config = make_config(poll_interval_minutes=240, last_fetch_at=last_fetch_at)
    assert _is_due(config, now) is True
