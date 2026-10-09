import datetime
import json
from types import SimpleNamespace
from unittest.mock import patch

import requests
import responses

from watcher import db, price_watch, searches, watched_items
from watcher.models import WatchedItem
from watcher.settings import Settings

WEBHOOK = "https://hooks.slack.com/services/T000/B000/XXXX"


def make_conn():
    return db.connect(":memory:")


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
        slack_webhook_url=WEBHOOK,
        dry_run=False,
        admin_port=8000,
    )
    base.update(overrides)
    return Settings(**base)


def make_client(title="VU Meter Pro", price=900, currency="SEK", in_stock=True, description=None, capture_prompt_into=None):
    payload = {"title": title, "description": description, "price": price, "currency": currency, "in_stock": in_stock}
    content = [SimpleNamespace(type="text", text=json.dumps(payload))]
    message = SimpleNamespace(content=content, usage=SimpleNamespace(input_tokens=500, output_tokens=50))

    def create(**kwargs):
        if capture_prompt_into is not None:
            capture_prompt_into.append(kwargs["messages"][0]["content"])
        return message

    return SimpleNamespace(messages=SimpleNamespace(create=create))


def make_item(conn, **overrides):
    base = dict(name="VU meter", url="https://example.com/vu-meter", target_price=1000)
    base.update(overrides)
    return watched_items.create_watched_item(conn, WatchedItem(**base))


HTML = "<html><head><style>.x{color:red}</style></head><body><h1>VU Meter Pro</h1><p>900 SEK</p></body></html>"


def test_html_to_text_strips_scripts_and_tags():
    html = "<html><script>evil()</script><style>.a{}</style><body>  Hello   <b>World</b>  </body></html>"
    text = price_watch._html_to_text(html)
    assert "evil()" not in text
    assert "color" not in text
    assert "Hello" in text and "World" in text


def test_html_to_text_caps_length():
    html = "<p>" + ("x" * 20000) + "</p>"
    assert len(price_watch._html_to_text(html)) == price_watch.MAX_PAGE_TEXT_CHARS


def test_is_due_true_when_never_checked():
    item = WatchedItem(name="n", url="u", check_frequency="daily")
    assert price_watch._is_due(item, datetime.datetime.utcnow()) is True


def test_is_due_respects_frequency():
    now = datetime.datetime.utcnow()
    checked_2_days_ago = (now - datetime.timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
    daily = WatchedItem(name="n", url="u", check_frequency="daily", last_checked_at=checked_2_days_ago)
    weekly = WatchedItem(name="n", url="u", check_frequency="weekly", last_checked_at=checked_2_days_ago)

    assert price_watch._is_due(daily, now) is True
    assert price_watch._is_due(weekly, now) is False


@responses.activate
def test_check_item_records_price_and_title():
    responses.add(responses.POST, WEBHOOK, json={"ok": True}, status=200)
    conn = make_conn()
    item = make_item(conn)
    client = make_client(title="VU Meter Pro", price=900)

    with patch("watcher.price_watch.get_text", return_value=HTML):
        result = price_watch.check_item(conn, client, make_settings(), item)

    assert result == {"checked": True, "alerted": True}  # 900 <= target_price 1000
    updated = watched_items.get_watched_item(conn, item.id)
    assert updated.current_price == 900
    assert updated.extracted_title == "VU Meter Pro"
    assert updated.last_checked_at is not None


@responses.activate
def test_check_item_records_description_and_image_from_jsonld():
    responses.add(responses.POST, WEBHOOK, json={"ok": True}, status=200)
    conn = make_conn()
    item = make_item(conn)
    html = (
        '<html><head><script type="application/ld+json">'
        '{"@type": "Product", "name": "VU Meter Pro", "image": "https://example.com/vu.jpg"}'
        "</script></head><body><h1>VU Meter Pro</h1><p>900 SEK</p></body></html>"
    )
    client = make_client(title="VU Meter Pro", price=900, description="A classic VU meter.")

    with patch("watcher.price_watch.get_text", return_value=html):
        price_watch.check_item(conn, client, make_settings(), item)

    updated = watched_items.get_watched_item(conn, item.id)
    assert updated.extracted_description == "A classic VU meter."
    assert updated.extracted_image_url == "https://example.com/vu.jpg"


@responses.activate
def test_check_item_falls_back_to_og_image_without_jsonld():
    responses.add(responses.POST, WEBHOOK, json={"ok": True}, status=200)
    conn = make_conn()
    item = make_item(conn)
    html = (
        '<html><head><meta property="og:image" content="https://example.com/og.jpg"></head>'
        "<body><h1>VU Meter Pro</h1><p>900 SEK</p></body></html>"
    )
    client = make_client(title="VU Meter Pro", price=900)

    with patch("watcher.price_watch.get_text", return_value=html):
        price_watch.check_item(conn, client, make_settings(), item)

    assert watched_items.get_watched_item(conn, item.id).extracted_image_url == "https://example.com/og.jpg"


@responses.activate
def test_check_item_passes_jsonld_product_as_a_hint_to_claude():
    responses.add(responses.POST, WEBHOOK, json={"ok": True}, status=200)
    conn = make_conn()
    item = make_item(conn)
    html = (
        '<html><head><script type="application/ld+json">'
        '{"@type": "Product", "name": "VU Meter Pro", "offers": {"price": "900", "priceCurrency": "SEK"}}'
        "</script></head><body>irrelevant rendered text</body></html>"
    )
    prompts = []
    client = make_client(title="VU Meter Pro", price=900, capture_prompt_into=prompts)

    with patch("watcher.price_watch.get_text", return_value=html):
        price_watch.check_item(conn, client, make_settings(), item)

    assert len(prompts) == 1
    assert "Structured product data" in prompts[0]
    assert "900" in prompts[0]


def test_check_item_alerts_when_at_or_below_target_price():
    conn = make_conn()
    item = make_item(conn, target_price=1000)
    client = make_client(price=900)

    with patch("watcher.price_watch.get_text", return_value=HTML):
        with patch("watcher.notify.slack.send_price_watch_instant") as send:
            price_watch.check_item(conn, client, make_settings(), item)

    send.assert_called_once()
    assert watched_items.get_watched_item(conn, item.id).last_alert_price == 900


def test_check_item_records_currency_and_passes_it_to_the_alert():
    conn = make_conn()
    item = make_item(conn, target_price=1000)
    client = make_client(price=900, currency="USD")

    with patch("watcher.price_watch.get_text", return_value=HTML):
        with patch("watcher.notify.slack.send_price_watch_instant") as send:
            price_watch.check_item(conn, client, make_settings(), item)

    assert watched_items.get_watched_item(conn, item.id).currency == "USD"
    _, kwargs = send.call_args
    assert kwargs.get("currency") == "USD"


def test_check_item_does_not_alert_above_target_price():
    conn = make_conn()
    item = make_item(conn, target_price=500)
    client = make_client(price=900)

    with patch("watcher.price_watch.get_text", return_value=HTML):
        with patch("watcher.notify.slack.send_price_watch_instant") as send:
            result = price_watch.check_item(conn, client, make_settings(), item)

    send.assert_not_called()
    assert result["alerted"] is False
    assert watched_items.get_watched_item(conn, item.id).last_alert_price is None


def test_check_item_does_not_repeat_alert_at_the_same_or_higher_price():
    conn = make_conn()
    item = make_item(conn, target_price=1000)

    with patch("watcher.price_watch.get_text", return_value=HTML):
        with patch("watcher.notify.slack.send_price_watch_instant") as send:
            price_watch.check_item(conn, make_client(price=900), make_settings(), item)
            item = watched_items.get_watched_item(conn, item.id)
            price_watch.check_item(conn, make_client(price=900), make_settings(), item)

    assert send.call_count == 1  # second check at the same price doesn't re-alert


def test_check_item_alerts_again_on_a_further_price_drop():
    conn = make_conn()
    item = make_item(conn, target_price=1000)

    with patch("watcher.price_watch.get_text", return_value=HTML):
        with patch("watcher.notify.slack.send_price_watch_instant") as send:
            price_watch.check_item(conn, make_client(price=900), make_settings(), item)
            item = watched_items.get_watched_item(conn, item.id)
            price_watch.check_item(conn, make_client(price=800), make_settings(), item)

    assert send.call_count == 2


def test_check_item_out_of_stock_still_records_price_but_does_not_alert():
    # Knowing the price of something out of stock is still useful - only
    # alerting is gated on in_stock, not whether the price gets recorded.
    conn = make_conn()
    item = make_item(conn, target_price=1000)
    client = make_client(price=900, in_stock=False)

    with patch("watcher.price_watch.get_text", return_value=HTML):
        with patch("watcher.notify.slack.send_price_watch_instant") as send:
            result = price_watch.check_item(conn, client, make_settings(), item)

    send.assert_not_called()
    assert result["alerted"] is False
    updated = watched_items.get_watched_item(conn, item.id)
    assert updated.current_price == 900
    assert updated.in_stock is False


@responses.activate
def test_check_item_records_in_stock_true_on_success():
    responses.add(responses.POST, WEBHOOK, json={"ok": True}, status=200)
    conn = make_conn()
    item = make_item(conn)
    client = make_client(price=900, in_stock=True)

    with patch("watcher.price_watch.get_text", return_value=HTML):
        price_watch.check_item(conn, client, make_settings(), item)

    assert watched_items.get_watched_item(conn, item.id).in_stock is True


def test_check_item_fetch_failure_is_handled_gracefully():
    conn = make_conn()
    item = make_item(conn)

    with patch("watcher.price_watch.get_text", side_effect=Exception("boom")):
        result = price_watch.check_item(conn, make_client(), make_settings(), item)

    assert result == {"checked": False, "alerted": False}
    assert watched_items.get_watched_item(conn, item.id).consecutive_check_failures == 1


def test_check_item_records_blocked_status_from_an_http_error():
    conn = make_conn()
    item = make_item(conn)
    response = SimpleNamespace(status_code=429)
    error = requests.HTTPError("429 Client Error: Too Many Requests")
    error.response = response

    with patch("watcher.price_watch.get_text", side_effect=error):
        price_watch.check_item(conn, make_client(), make_settings(), item)

    assert watched_items.get_watched_item(conn, item.id).last_error_status == 429


def test_check_item_fetch_failure_sends_dead_alert_after_threshold():
    conn = make_conn()
    item = make_item(conn)
    settings = make_settings(health_alert_after_n_failures=2)

    with patch("watcher.price_watch.get_text", side_effect=Exception("boom")):
        with patch("watcher.notify.slack.send_watched_item_dead_alert") as send:
            price_watch.check_item(conn, make_client(), settings, item)
            send.assert_not_called()
            item = watched_items.get_watched_item(conn, item.id)

            price_watch.check_item(conn, make_client(), settings, item)
            send.assert_called_once()

    assert watched_items.get_watched_item(conn, item.id).dead_alert_sent is True


def test_check_item_does_not_repeat_the_dead_alert_once_already_sent():
    conn = make_conn()
    item = make_item(conn)
    settings = make_settings(health_alert_after_n_failures=1)

    with patch("watcher.price_watch.get_text", side_effect=Exception("boom")):
        with patch("watcher.notify.slack.send_watched_item_dead_alert") as send:
            price_watch.check_item(conn, make_client(), settings, item)
            item = watched_items.get_watched_item(conn, item.id)
            price_watch.check_item(conn, make_client(), settings, item)

    send.assert_called_once()


def test_check_item_success_resets_the_failure_streak():
    conn = make_conn()
    item = make_item(conn)
    settings = make_settings(health_alert_after_n_failures=1)

    with patch("watcher.price_watch.get_text", side_effect=Exception("boom")):
        with patch("watcher.notify.slack.send_watched_item_dead_alert"):
            price_watch.check_item(conn, make_client(), settings, item)
    item = watched_items.get_watched_item(conn, item.id)
    assert item.dead_alert_sent is True

    with patch("watcher.price_watch.get_text", return_value=HTML):
        with patch("watcher.notify.slack.send_price_watch_instant"):
            price_watch.check_item(conn, make_client(), settings, item)

    refreshed = watched_items.get_watched_item(conn, item.id)
    assert refreshed.consecutive_check_failures == 0
    assert refreshed.dead_alert_sent is False


def test_check_item_unparsable_response_leaves_item_checked_but_not_priced():
    conn = make_conn()
    item = make_item(conn)
    bad_content = [SimpleNamespace(type="text", text="not json")]
    bad_message = SimpleNamespace(content=bad_content, usage=SimpleNamespace(input_tokens=10, output_tokens=5))
    client = SimpleNamespace(messages=SimpleNamespace(create=lambda **kwargs: bad_message))

    with patch("watcher.price_watch.get_text", return_value=HTML):
        result = price_watch.check_item(conn, client, make_settings(), item)

    assert result == {"checked": True, "alerted": False}
    assert watched_items.get_watched_item(conn, item.id).current_price is None


def test_check_item_with_find_used_creates_linked_plain_search():
    conn = make_conn()
    item = make_item(conn, find_used=True, target_price=1000)
    client = make_client(title="VU Meter Pro", price=900)

    with patch("watcher.price_watch.get_text", return_value=HTML):
        with patch("watcher.notify.slack.send_price_watch_instant"):
            price_watch.check_item(conn, client, make_settings(), item)

    updated = watched_items.get_watched_item(conn, item.id)
    assert updated.linked_search_id is not None
    linked = searches.get_search(conn, updated.linked_search_id)
    assert linked.name == "Find used: VU meter"
    assert linked.scoring_mode == "plain"
    assert linked.search_phrases == ["VU Meter Pro"]
    assert linked.instant_alert_price == 1000
    # Vinted is excluded from "every marketplace enabled by default" - see
    # marketplaces.py (off by default since 2026-10, Cloudflare blocks it).
    assert set(linked.marketplaces) == {"blocket", "rehifi", "auctionet"}


def test_check_item_without_find_used_does_not_create_linked_search():
    conn = make_conn()
    item = make_item(conn, find_used=False)
    client = make_client()

    with patch("watcher.price_watch.get_text", return_value=HTML):
        with patch("watcher.notify.slack.send_price_watch_instant"):
            price_watch.check_item(conn, client, make_settings(), item)

    assert watched_items.get_watched_item(conn, item.id).linked_search_id is None


def test_check_item_find_used_does_not_create_a_second_linked_search():
    conn = make_conn()
    item = make_item(conn, find_used=True)
    client = make_client()

    with patch("watcher.price_watch.get_text", return_value=HTML):
        with patch("watcher.notify.slack.send_price_watch_instant"):
            price_watch.check_item(conn, client, make_settings(), item)
            item = watched_items.get_watched_item(conn, item.id)
            price_watch.check_item(conn, client, make_settings(), item)

    assert len([s for s in searches.list_searches(conn) if s.name == "Find used: VU meter"]) == 1


def test_run_price_watch_sweep_skips_items_not_due():
    conn = make_conn()
    now = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    due = make_item(conn, name="Due item")
    not_due_item = make_item(conn, name="Not due item", check_frequency="monthly")
    watched_items.record_check_result(conn, not_due_item.id, price=100, extracted_title="t")
    # record_check_result sets last_checked_at to "now" via SQL - already not due for "monthly".

    with patch("watcher.price_watch.get_text", return_value=HTML) as mock_get_text:
        with patch("watcher.notify.slack.send_price_watch_instant"):
            result = price_watch.run_price_watch_sweep(conn, make_client(), make_settings())

    assert result["items_checked"] == 1
    mock_get_text.assert_called_once()


def test_run_price_watch_sweep_skips_disabled_items():
    conn = make_conn()
    make_item(conn, enabled=False)

    with patch("watcher.price_watch.get_text", return_value=HTML) as mock_get_text:
        result = price_watch.run_price_watch_sweep(conn, make_client(), make_settings())

    assert result == {"items_checked": 0, "items_alerted": 0}
    mock_get_text.assert_not_called()
