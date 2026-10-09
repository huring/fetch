import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import responses

from watcher import db, marketplace_configs, pipeline, searches, storage
from watcher.models import Search
from watcher.settings import Settings

WEBHOOK = "https://hooks.slack.com/services/T000/B000/XXXX"


@pytest.fixture(autouse=True)
def no_ad_detail_fetch_by_default():
    """Most tests don't care about Blocket's description-enrichment step -
    stub it to empty so they don't need to mock a detail-page HTTP call.
    Tests that exercise enrichment itself override this with their own patch."""
    with patch("watcher.sources.blocket.fetch_ad_description", return_value=""):
        yield


def make_conn():
    """A fresh in-memory DB with marketplace_configs seeded and request
    delays zeroed out, so pipeline tests don't actually sleep."""
    conn = db.connect(":memory:")
    marketplace_configs.ensure_defaults(conn)
    for config in marketplace_configs.list_configs(conn):
        marketplace_configs.update_config(conn, config.key, config.poll_interval_minutes, 0.0, {})
    return conn


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


class _FakeBatchesResource:
    """Minimal fake of client.messages.batches: every submitted batch
    "completes" immediately (processing_status='ended' from the moment
    it's created), and each request gets `scores` applied positionally -
    tests here always submit at most one request per batch, so this
    mirrors the old single-call `make_anthropic_client(scores)` helper's
    semantics closely enough to keep tests readable."""

    def __init__(self, scores):
        self.scores = scores
        self._requests_by_batch = {}
        self.create_calls = []

    def create(self, requests):
        batch_id = f"batch_{len(self._requests_by_batch)}"
        self._requests_by_batch[batch_id] = requests
        self.create_calls.append(requests)
        return SimpleNamespace(id=batch_id, processing_status="ended")

    def retrieve(self, batch_id):
        return SimpleNamespace(id=batch_id, processing_status="ended")

    def results(self, batch_id):
        requests = self._requests_by_batch[batch_id]
        content = [SimpleNamespace(type="text", text=json.dumps({"scores": self.scores}))]
        message = SimpleNamespace(content=content, usage=SimpleNamespace(input_tokens=100, output_tokens=50))
        for request in requests:
            yield SimpleNamespace(
                custom_id=request["custom_id"],
                result=SimpleNamespace(type="succeeded", message=message),
            )


def make_anthropic_client(scores):
    """scores: a list of score dicts (listing_index/score/reasoning/
    uncertain_specs/price_assessment), applied to every submitted batch
    request - matches how many listings that request actually covers."""
    return SimpleNamespace(messages=SimpleNamespace(batches=_FakeBatchesResource(scores)))


def make_listing(external_id, title, price=1000, description=""):
    return SimpleNamespace(
        source="blocket",
        external_id=external_id,
        title=title,
        description=description,
        price=price,
        url=f"https://example.com/{external_id}",
        location="Stockholm",
        ships=None,
        published_at=None,
        auction_ends_at=None,
        image_url=None,
        raw={},
    )


@responses.activate
def test_run_once_sends_instant_notification_for_high_score():
    conn = make_conn()
    searches.create_search(
        conn, Search(name="Vardagsrummet AV-receiver", scoring_mode="rated", search_phrases=["onkyo"], marketplaces=["blocket"])
    )
    responses.add(responses.POST, WEBHOOK, json={"ok": True}, status=200)

    with patch("watcher.sources.blocket.fetch", return_value=[make_listing("1", "Onkyo TX-NR656")]):
        client = make_anthropic_client(
            [{"listing_index": 0, "score": 9, "reasoning": "great", "uncertain_specs": [], "price_assessment": "good"}]
        )
        result = pipeline.run_once(conn, client, make_settings())

    assert result["instant_notifications"] == 1
    listing_row = conn.execute("SELECT * FROM listings").fetchone()
    assert listing_row["score"] == 9
    assert listing_row["notified_instant_at"] is not None
    assert len(responses.calls) == 1


def test_run_once_skips_searches_without_this_marketplace():
    conn = make_conn()
    searches.create_search(conn, Search(name="No marketplace yet", scoring_mode="rated"))  # marketplaces=[]

    with patch("watcher.sources.blocket.fetch") as mock_fetch:
        client = make_anthropic_client([])
        result = pipeline.run_once(conn, client, make_settings())

    mock_fetch.assert_not_called()
    assert result["searches_processed"] == 0


def test_run_once_prefilter_excludes_without_calling_claude():
    conn = make_conn()
    searches.create_search(
        conn,
        Search(
            name="Vardagsrummet AV-receiver", scoring_mode="rated",
            excluded_words=["trasig"],
            search_phrases=["onkyo"],
            marketplaces=["blocket"],
        ),
    )
    with patch("watcher.sources.blocket.fetch", return_value=[make_listing("1", "Onkyo TX-SR607, trasig")]):
        client = make_anthropic_client([])
        result = pipeline.run_once(conn, client, make_settings())

    assert result["listings_scored"] == 0
    assert client.messages.batches.create_calls == []
    listing_row = conn.execute("SELECT * FROM listings").fetchone()
    assert listing_row["score"] == 0


@responses.activate
def test_run_once_dry_run_does_not_post_or_mark_notified():
    conn = make_conn()
    searches.create_search(
        conn, Search(name="Stugan hifi", scoring_mode="rated", search_phrases=["leak"], marketplaces=["blocket"])
    )
    with patch("watcher.sources.blocket.fetch", return_value=[make_listing("1", "Leak Stereo 130")]):
        client = make_anthropic_client(
            [{"listing_index": 0, "score": 9, "reasoning": "great", "uncertain_specs": [], "price_assessment": "good"}]
        )
        result = pipeline.run_once(conn, client, make_settings(), dry_run=True)

    assert result["instant_notifications"] == 1
    assert len(responses.calls) == 0
    listing_row = conn.execute("SELECT * FROM listings").fetchone()
    assert listing_row["notified_instant_at"] is None


def test_run_once_marks_source_failure_and_alerts_after_threshold():
    conn = make_conn()
    searches.create_search(
        conn, Search(name="C", scoring_mode="rated", search_phrases=["x"], marketplaces=["blocket"])
    )
    settings = make_settings(health_alert_after_n_failures=2)
    client = make_anthropic_client([])

    from watcher.sources.base import SourceError

    with patch("watcher.sources.blocket.fetch", side_effect=SourceError("boom")):
        pipeline.run_once(conn, client, settings)
        health_after_1 = conn.execute("SELECT * FROM source_health WHERE source='blocket'").fetchone()
        assert health_after_1["consecutive_failures"] == 1
        assert health_after_1["alert_sent"] == 0

        with responses.RequestsMock() as rsps:
            rsps.add(responses.POST, WEBHOOK, json={"ok": True}, status=200)
            pipeline.run_once(conn, client, settings)
            assert len(rsps.calls) == 1  # health alert sent on 2nd consecutive failure

        health_after_2 = conn.execute("SELECT * FROM source_health WHERE source='blocket'").fetchone()
        assert health_after_2["alert_sent"] == 1


@responses.activate
def test_send_digest_groups_by_search_and_marks_digested():
    conn = make_conn()
    search = searches.create_search(conn, Search(name="Stugan hifi", scoring_mode="rated"))
    listing_id = storage.upsert_listing(
        conn,
        search.id,
        make_listing("1", "Leak Stereo 130"),
    )
    storage.mark_scored(conn, listing_id, 6, "nice amp", [], "fair price")
    responses.add(responses.POST, WEBHOOK, json={"ok": True}, status=200)

    result = pipeline.send_digest(conn, make_settings())

    assert result["entries"] == 1
    row = conn.execute("SELECT * FROM listings WHERE id = ?", (listing_id,)).fetchone()
    assert row["included_in_digest_at"] is not None


@responses.activate
def test_send_digest_summary_link_search_collapses_entries_with_admin_url():
    conn = make_conn()
    search = searches.create_search(
        conn, Search(name="Vinyl hunt", scoring_mode="rated", digest_style="summary_link")
    )
    listing_id = storage.upsert_listing(conn, search.id, make_listing("1", "Kind of Blue"))
    storage.mark_scored(conn, listing_id, 6, "nice pressing", [], "fair price")
    responses.add(responses.POST, WEBHOOK, json={"ok": True}, status=200)

    result = pipeline.send_digest(conn, make_settings(public_base_url="https://fetch.home"))

    assert result["entries"] == 1
    body = responses.calls[0].request.body.decode()
    assert "1 new item in Vinyl hunt" in body
    assert f"https://fetch.home/searches/{search.id}/listings?bucket=daily_roundup" in body
    assert "Kind of Blue" not in body


@responses.activate
def test_send_digest_itemized_search_default_has_no_summary_link_text():
    conn = make_conn()
    search = searches.create_search(conn, Search(name="Vinyl hunt", scoring_mode="rated"))
    listing_id = storage.upsert_listing(conn, search.id, make_listing("1", "Kind of Blue"))
    storage.mark_scored(conn, listing_id, 6, "nice pressing", [], "fair price")
    responses.add(responses.POST, WEBHOOK, json={"ok": True}, status=200)

    pipeline.send_digest(conn, make_settings())

    body = responses.calls[0].request.body.decode()
    assert "Kind of Blue" in body
    assert "new item" not in body


def test_run_once_enriches_blocket_description_before_scoring():
    conn = make_conn()
    searches.create_search(
        conn, Search(name="C", scoring_mode="rated", search_phrases=["marantz"], marketplaces=["blocket"])
    )

    with patch("watcher.sources.blocket.fetch", return_value=[make_listing("1", "Marantz SR5010")]):
        with patch("watcher.sources.blocket.fetch_ad_description", return_value="Fint skick, HDCP 2.2"):
            client = make_anthropic_client(
                [{"listing_index": 0, "score": 7, "reasoning": "r", "uncertain_specs": [], "price_assessment": "p"}]
            )
            pipeline.run_once(conn, client, make_settings(slack_webhook_url=""))

    row = conn.execute("SELECT * FROM listings").fetchone()
    assert row["description"] == "Fint skick, HDCP 2.2"
    sent_request = client.messages.batches.create_calls[0][0]
    assert "Fint skick, HDCP 2.2" in sent_request["params"]["messages"][0]["content"]


def test_run_once_rejects_after_enrichment_reveals_excluded_word():
    conn = make_conn()
    searches.create_search(
        conn,
        Search(name="C", scoring_mode="rated", excluded_words=["trasig"], search_phrases=["marantz"], marketplaces=["blocket"]),
    )

    with patch("watcher.sources.blocket.fetch", return_value=[make_listing("1", "Marantz SR5010")]):
        with patch("watcher.sources.blocket.fetch_ad_description", return_value="Trasig display, annars ok"):
            client = make_anthropic_client([])
            result = pipeline.run_once(conn, client, make_settings(slack_webhook_url=""))

    assert result["listings_scored"] == 0
    assert client.messages.batches.create_calls == []
    row = conn.execute("SELECT * FROM listings").fetchone()
    assert row["score"] == 0
    assert row["description"] == "Trasig display, annars ok"


def test_run_marketplace_cycle_marks_fetched():
    conn = make_conn()
    searches.create_search(conn, Search(name="C", scoring_mode="rated", search_phrases=["x"], marketplaces=["blocket"]))

    with patch("watcher.sources.blocket.fetch", return_value=[]):
        pipeline.run_marketplace_cycle(conn, make_settings(), "blocket")

    config = marketplace_configs.get_config(conn, "blocket")
    assert config.last_fetch_at is not None


def test_run_marketplace_cycle_never_touches_claude():
    """run_marketplace_cycle only fetches/prefilters - scoring happens in
    submit_pending_scoring/collect_finished_batches, called separately."""
    conn = make_conn()
    searches.create_search(conn, Search(name="C", scoring_mode="rated", search_phrases=["onkyo"], marketplaces=["blocket"]))

    with patch("watcher.sources.blocket.fetch", return_value=[make_listing("1", "Onkyo TX-NR656")]):
        result = pipeline.run_marketplace_cycle(conn, make_settings(), "blocket")

    assert result["listings_pending_scoring"] == 1
    listing_row = conn.execute("SELECT * FROM listings").fetchone()
    assert listing_row["score"] is None  # still pending - not scored by the fetch cycle


def test_run_marketplace_cycle_works_for_vinted():
    conn = make_conn()
    searches.create_search(conn, Search(name="C", scoring_mode="rated", search_phrases=["onkyo"], marketplaces=["vinted"]))

    with patch("watcher.sources.vinted.fetch", return_value=[make_listing("1", "Onkyo A-9010", description="x")]):
        with patch("watcher.sources.vinted.fetch_item_description", return_value=""):
            result = pipeline.run_marketplace_cycle(conn, make_settings(slack_webhook_url=""), "vinted")

    assert result["searches_processed"] == 1
    assert result["listings_pending_scoring"] == 1
    config = marketplace_configs.get_config(conn, "vinted")
    assert config.last_fetch_at is not None


@responses.activate
def test_plain_search_surfaces_listing_without_calling_claude():
    conn = make_conn()
    searches.create_search(
        conn, Search(name="Vinyl hunt", scoring_mode="plain", search_phrases=["kind of blue"], marketplaces=["blocket"])
    )

    with patch("watcher.sources.blocket.fetch", return_value=[make_listing("1", "Miles Davis - Kind of Blue")]):
        client = make_anthropic_client([])
        result = pipeline.run_once(conn, client, make_settings())

    assert result["scoring_submitted"] == {"listings_submitted": 0, "requests": 0}
    assert client.messages.batches.create_calls == []
    listing_row = conn.execute("SELECT * FROM listings").fetchone()
    assert listing_row["score"] == storage.PLAIN_SURFACED_SCORE


@responses.activate
def test_plain_search_instant_alert_price_notifies_immediately():
    conn = make_conn()
    searches.create_search(
        conn,
        Search(
            name="Vinyl hunt", scoring_mode="plain", instant_alert_price=1200,
            search_phrases=["kind of blue"], marketplaces=["blocket"],
        ),
    )
    responses.add(responses.POST, WEBHOOK, json={"ok": True}, status=200)

    with patch("watcher.sources.blocket.fetch", return_value=[make_listing("1", "Kind of Blue", price=1000)]):
        pipeline.run_marketplace_cycle(conn, make_settings(), "blocket")

    assert len(responses.calls) == 1
    assert "Price alert" in responses.calls[0].request.body.decode()
    listing_row = conn.execute("SELECT * FROM listings").fetchone()
    assert listing_row["notified_instant_at"] is not None


def test_plain_search_above_instant_alert_price_does_not_notify():
    conn = make_conn()
    searches.create_search(
        conn,
        Search(
            name="Vinyl hunt", scoring_mode="plain", instant_alert_price=500,
            search_phrases=["kind of blue"], marketplaces=["blocket"],
        ),
    )

    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        with patch("watcher.sources.blocket.fetch", return_value=[make_listing("1", "Kind of Blue", price=1000)]):
            pipeline.run_marketplace_cycle(conn, make_settings(), "blocket")
        assert len(rsps.calls) == 0

    listing_row = conn.execute("SELECT * FROM listings").fetchone()
    assert listing_row["score"] == storage.PLAIN_SURFACED_SCORE
    assert listing_row["notified_instant_at"] is None


@responses.activate
def test_send_digest_includes_plain_search_entries():
    conn = make_conn()
    search = searches.create_search(conn, Search(name="Vinyl hunt", scoring_mode="plain"))
    listing_id = storage.upsert_listing(conn, search.id, make_listing("1", "Kind of Blue", price=300))
    storage.mark_surfaced_plain(conn, listing_id)
    responses.add(responses.POST, WEBHOOK, json={"ok": True}, status=200)

    result = pipeline.send_digest(conn, make_settings())

    assert result["entries"] == 1
    body = responses.calls[0].request.body.decode()
    assert "Kind of Blue" in body
    row = conn.execute("SELECT * FROM listings WHERE id = ?", (listing_id,)).fetchone()
    assert row["included_in_digest_at"] is not None


def test_run_marketplace_cycle_unknown_key_raises():
    conn = make_conn()
    with pytest.raises(ValueError):
        pipeline.run_marketplace_cycle(conn, make_settings(), "nonexistent")


def test_run_search_cycle_fetches_only_that_search():
    conn = make_conn()
    search = searches.create_search(
        conn, Search(name="C", scoring_mode="rated", search_phrases=["onkyo"], marketplaces=["blocket"])
    )
    other_search = searches.create_search(
        conn, Search(name="Other", scoring_mode="rated", search_phrases=["onkyo"], marketplaces=["blocket"])
    )

    with patch("watcher.sources.blocket.fetch", return_value=[make_listing("1", "Onkyo TX-NR656")]) as mock_fetch:
        result = pipeline.run_search_cycle(conn, make_settings(), search.id)

    mock_fetch.assert_called_once()  # not called once per search sharing that marketplace
    assert result["search"] == "C"
    assert result["listings_pending_scoring"] == 1
    rows = conn.execute("SELECT * FROM listings").fetchall()
    assert len(rows) == 1
    assert rows[0]["search_id"] == search.id
    assert rows[0]["search_id"] != other_search.id


def test_run_search_cycle_covers_every_marketplace_the_search_is_attached_to():
    conn = make_conn()
    search = searches.create_search(
        conn, Search(name="C", scoring_mode="rated", search_phrases=["onkyo"], marketplaces=["blocket", "vinted"])
    )

    with patch("watcher.sources.blocket.fetch", return_value=[make_listing("1", "Onkyo A")]):
        with patch("watcher.sources.vinted.fetch", return_value=[make_listing("2", "Onkyo B", description="x")]):
            with patch("watcher.sources.vinted.fetch_item_description", return_value=""):
                result = pipeline.run_search_cycle(conn, make_settings(), search.id)

    assert result["listings_fetched"] == 2
    assert result["listings_pending_scoring"] == 2


def test_run_search_cycle_does_not_touch_marketplace_health_or_last_fetch_at():
    conn = make_conn()
    search = searches.create_search(
        conn, Search(name="C", scoring_mode="rated", search_phrases=["onkyo"], marketplaces=["blocket"])
    )

    with patch("watcher.sources.blocket.fetch", return_value=[]):
        pipeline.run_search_cycle(conn, make_settings(), search.id)

    config = marketplace_configs.get_config(conn, "blocket")
    assert config.last_fetch_at is None
    assert conn.execute("SELECT * FROM source_health").fetchone() is None


def test_run_search_cycle_unknown_search_id_raises():
    conn = make_conn()
    with pytest.raises(ValueError):
        pipeline.run_search_cycle(conn, make_settings(), 999999)


def test_run_search_cycle_uses_each_row_s_own_marketplace_for_enrichment():
    """Regression test: a search attached to more than one marketplace
    produces pending rows from different sources in the same
    get_pending_listings() result - enrichment must look up each row's own
    marketplace, not assume whichever marketplace most recently fetched."""
    conn = make_conn()
    search = searches.create_search(
        conn, Search(name="C", scoring_mode="rated", search_phrases=["onkyo"], marketplaces=["blocket", "vinted"])
    )
    blocket_listing = SimpleNamespace(
        source="blocket", external_id="1", title="Onkyo A", description="",
        price=1000, url="https://example.com/1", location=None, ships=None,
        published_at=None, auction_ends_at=None, image_url=None, raw={},
    )
    vinted_listing = SimpleNamespace(
        source="vinted", external_id="2", title="Onkyo B", description="",
        price=1000, url="https://example.com/2", location=None, ships=None,
        published_at=None, auction_ends_at=None, image_url=None, raw={},
    )

    with patch("watcher.sources.blocket.fetch", return_value=[blocket_listing]):
        with patch("watcher.sources.vinted.fetch", return_value=[vinted_listing]):
            with patch("watcher.sources.blocket.fetch_ad_description", return_value="Blocket desc") as blocket_enrich:
                with patch("watcher.sources.vinted.fetch_item_description", return_value="Vinted desc") as vinted_enrich:
                    pipeline.run_search_cycle(conn, make_settings(), search.id)

    blocket_enrich.assert_called_once()
    vinted_enrich.assert_called_once()
    rows = {row["external_id"]: row["description"] for row in conn.execute("SELECT * FROM listings")}
    assert rows["1"] == "Blocket desc"
    assert rows["2"] == "Vinted desc"


def test_run_search_cycle_skips_a_marketplace_no_longer_registered():
    conn = make_conn()
    search = searches.create_search(
        conn,
        Search(
            name="C", scoring_mode="rated", search_phrases=["onkyo"],
            marketplaces=["blocket", "nonexistent-marketplace"],
        ),
    )

    with patch("watcher.sources.blocket.fetch", return_value=[make_listing("1", "Onkyo A")]):
        result = pipeline.run_search_cycle(conn, make_settings(), search.id)

    assert result["listings_fetched"] == 1


def test_submit_pending_scoring_batches_per_search_chunked():
    conn = make_conn()
    search = searches.create_search(
        conn, Search(name="C", scoring_mode="rated", search_phrases=["onkyo"], marketplaces=["blocket"], max_price=None)
    )
    for i in range(3):
        storage.upsert_listing(conn, search.id, make_listing(str(i), f"Onkyo {i}"))
    client = make_anthropic_client([])
    settings = make_settings(scoring_batch_size=2)

    result = pipeline.submit_pending_scoring(conn, client, settings)

    assert result["listings_submitted"] == 3
    assert result["requests"] == 2  # chunked into 2 + 1
    assert len(storage.get_in_progress_batch_ids(conn)) == 1


def test_submit_pending_scoring_noop_when_nothing_pending():
    conn = make_conn()
    client = make_anthropic_client([])

    result = pipeline.submit_pending_scoring(conn, client, make_settings())

    assert result == {"listings_submitted": 0, "requests": 0}
    assert client.messages.batches.create_calls == []


def test_submitted_listing_is_not_resubmitted_before_collection():
    conn = make_conn()
    search = searches.create_search(conn, Search(name="C", scoring_mode="rated", search_phrases=["onkyo"], marketplaces=["blocket"]))
    storage.upsert_listing(conn, search.id, make_listing("1", "Onkyo TX-NR656"))
    client = make_anthropic_client([])

    pipeline.submit_pending_scoring(conn, client, make_settings())
    result = pipeline.submit_pending_scoring(conn, client, make_settings())

    assert result["listings_submitted"] == 0  # already in an in-progress batch


def test_collect_finished_batches_scores_and_notifies():
    conn = make_conn()
    search = searches.create_search(conn, Search(name="C", scoring_mode="rated", search_phrases=["onkyo"], marketplaces=["blocket"]))
    storage.upsert_listing(conn, search.id, make_listing("1", "Onkyo TX-NR656"))
    client = make_anthropic_client(
        [{"listing_index": 0, "score": 9, "reasoning": "great", "uncertain_specs": [], "price_assessment": "good"}]
    )
    pipeline.submit_pending_scoring(conn, client, make_settings())

    with responses.RequestsMock() as rsps:
        rsps.add(responses.POST, WEBHOOK, json={"ok": True}, status=200)
        result = pipeline.collect_finished_batches(conn, client, make_settings())
        assert len(rsps.calls) == 1

    assert result["batches_collected"] == 1
    assert result["listings_scored"] == 1
    assert result["instant_notifications"] == 1
    assert storage.get_in_progress_batch_ids(conn) == []  # batch cleaned up after collection

    listing_row = conn.execute("SELECT * FROM listings").fetchone()
    assert listing_row["score"] == 9
    assert listing_row["notified_instant_at"] is not None


def test_collect_finished_batches_leaves_errored_item_pending_for_retry():
    conn = make_conn()
    search = searches.create_search(conn, Search(name="C", scoring_mode="rated", search_phrases=["onkyo"], marketplaces=["blocket"]))
    storage.upsert_listing(conn, search.id, make_listing("1", "Onkyo TX-NR656"))
    client = make_anthropic_client([])

    pipeline.submit_pending_scoring(conn, client, make_settings())

    # simulate an errored batch item
    batch_id = storage.get_in_progress_batch_ids(conn)[0]
    original_results = client.messages.batches.results

    def errored_results(bid):
        for r in original_results(bid):
            yield SimpleNamespace(custom_id=r.custom_id, result=SimpleNamespace(type="errored"))

    client.messages.batches.results = errored_results

    result = pipeline.collect_finished_batches(conn, client, make_settings())

    assert result["listings_errored"] == 1
    assert result["listings_scored"] == 0
    listing_row = conn.execute("SELECT * FROM listings").fetchone()
    assert listing_row["score"] is None  # left pending, retried on the next submit sweep


def test_collect_finished_batches_skips_batches_still_in_progress():
    conn = make_conn()
    search = searches.create_search(conn, Search(name="C", scoring_mode="rated", search_phrases=["onkyo"], marketplaces=["blocket"]))
    storage.upsert_listing(conn, search.id, make_listing("1", "Onkyo TX-NR656"))
    client = make_anthropic_client([])
    pipeline.submit_pending_scoring(conn, client, make_settings())
    client.messages.batches.retrieve = lambda batch_id: SimpleNamespace(id=batch_id, processing_status="in_progress")

    result = pipeline.collect_finished_batches(conn, client, make_settings())

    assert result["batches_collected"] == 0
    assert len(storage.get_in_progress_batch_ids(conn)) == 1
