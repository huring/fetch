from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import responses

from watcher import containers, db, pipeline, storage
from watcher.models import BlocketQuery, Container
from watcher.scoring.claude_scorer import _BatchScoreResponse, _ListingScore
from watcher.settings import Settings

WEBHOOK = "https://hooks.slack.com/services/T000/B000/XXXX"


@pytest.fixture(autouse=True)
def no_ad_detail_fetch_by_default():
    """Most tests don't care about Blocket's description-enrichment step -
    stub it to empty so they don't need to mock a detail-page HTTP call.
    Tests that exercise enrichment itself override this with their own patch."""
    with patch("watcher.sources.blocket.fetch_ad_description", return_value=""):
        yield


def make_settings(**overrides):
    base = dict(
        db_path=":memory:",
        poll_interval_minutes=20,
        digest_time="08:00",
        score_instant_threshold=8,
        score_digest_min=5,
        health_alert_after_n_failures=3,
        claude_model="claude-haiku-4-5",
        scoring_batch_size=10,
        blocket_request_delay_seconds=0,
        tradera_request_delay_seconds=0,
        max_pages_per_query=1,
        anthropic_api_key="x",
        tradera_app_id="",
        tradera_app_key="",
        slack_webhook_url=WEBHOOK,
        dry_run=False,
        admin_port=8000,
    )
    base.update(overrides)
    return Settings(**base)


def make_anthropic_client(scores):
    parsed = _BatchScoreResponse(scores=scores)
    response = SimpleNamespace(parsed_output=parsed, usage=SimpleNamespace(input_tokens=100, output_tokens=50))
    client = MagicMock()
    client.messages.parse.return_value = response
    return client


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
        raw={},
    )


@responses.activate
def test_run_once_sends_instant_notification_for_high_score():
    conn = db.connect(":memory:")
    container = containers.create_container(
        conn, Container(name="Vardagsrummet AV-receiver", blocket_queries=[BlocketQuery(q="onkyo")])
    )
    responses.add(responses.POST, WEBHOOK, json={"ok": True}, status=200)

    with patch("watcher.sources.blocket.fetch", return_value=[make_listing("1", "Onkyo TX-NR656")]):
        client = make_anthropic_client(
            [_ListingScore(listing_index=0, score=9, reasoning="great", uncertain_specs=[], price_assessment="good")]
        )
        result = pipeline.run_once(conn, client, make_settings())

    assert result["instant_notifications"] == 1
    listing_row = conn.execute("SELECT * FROM listings").fetchone()
    assert listing_row["score"] == 9
    assert listing_row["notified_instant_at"] is not None
    assert len(responses.calls) == 1


def test_run_once_prefilter_excludes_without_calling_claude():
    conn = db.connect(":memory:")
    container = containers.create_container(
        conn,
        Container(
            name="Vardagsrummet AV-receiver",
            excluded_words=["trasig"],
            blocket_queries=[BlocketQuery(q="onkyo")],
        ),
    )
    with patch("watcher.sources.blocket.fetch", return_value=[make_listing("1", "Onkyo TX-SR607, trasig")]):
        client = make_anthropic_client([])
        result = pipeline.run_once(conn, client, make_settings())

    assert result["listings_scored"] == 0
    client.messages.parse.assert_not_called()
    listing_row = conn.execute("SELECT * FROM listings").fetchone()
    assert listing_row["score"] == 0


@responses.activate
def test_run_once_dry_run_does_not_post_or_mark_notified():
    conn = db.connect(":memory:")
    containers.create_container(
        conn, Container(name="Stugan hifi", blocket_queries=[BlocketQuery(q="leak")])
    )
    with patch("watcher.sources.blocket.fetch", return_value=[make_listing("1", "Leak Stereo 130")]):
        client = make_anthropic_client(
            [_ListingScore(listing_index=0, score=9, reasoning="great", uncertain_specs=[], price_assessment="good")]
        )
        result = pipeline.run_once(conn, client, make_settings(), dry_run=True)

    assert result["instant_notifications"] == 1
    assert len(responses.calls) == 0
    listing_row = conn.execute("SELECT * FROM listings").fetchone()
    assert listing_row["notified_instant_at"] is None


def test_run_once_marks_source_failure_and_alerts_after_threshold():
    conn = db.connect(":memory:")
    containers.create_container(
        conn, Container(name="C", blocket_queries=[BlocketQuery(q="x")])
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
def test_send_digest_groups_by_container_and_marks_digested():
    conn = db.connect(":memory:")
    container = containers.create_container(conn, Container(name="Stugan hifi"))
    listing_id = storage.upsert_listing(
        conn,
        container.id,
        make_listing("1", "Leak Stereo 130"),
    )
    storage.mark_scored(conn, listing_id, 6, "nice amp", [], "fair price")
    responses.add(responses.POST, WEBHOOK, json={"ok": True}, status=200)

    result = pipeline.send_digest(conn, make_settings())

    assert result["entries"] == 1
    row = conn.execute("SELECT * FROM listings WHERE id = ?", (listing_id,)).fetchone()
    assert row["included_in_digest_at"] is not None


def test_run_once_enriches_blocket_description_before_scoring():
    conn = db.connect(":memory:")
    containers.create_container(
        conn, Container(name="C", blocket_queries=[BlocketQuery(q="marantz")])
    )

    with patch("watcher.sources.blocket.fetch", return_value=[make_listing("1", "Marantz SR5010")]):
        with patch("watcher.sources.blocket.fetch_ad_description", return_value="Fint skick, HDCP 2.2"):
            client = make_anthropic_client(
                [_ListingScore(listing_index=0, score=7, reasoning="r", uncertain_specs=[], price_assessment="p")]
            )
            pipeline.run_once(conn, client, make_settings(slack_webhook_url=""))

    row = conn.execute("SELECT * FROM listings").fetchone()
    assert row["description"] == "Fint skick, HDCP 2.2"
    sent_prompt = client.messages.parse.call_args.kwargs["messages"][0]["content"]
    assert "Fint skick, HDCP 2.2" in sent_prompt


def test_run_once_rejects_after_enrichment_reveals_excluded_word():
    conn = db.connect(":memory:")
    containers.create_container(
        conn,
        Container(name="C", excluded_words=["trasig"], blocket_queries=[BlocketQuery(q="marantz")]),
    )

    with patch("watcher.sources.blocket.fetch", return_value=[make_listing("1", "Marantz SR5010")]):
        with patch("watcher.sources.blocket.fetch_ad_description", return_value="Trasig display, annars ok"):
            client = make_anthropic_client([])
            result = pipeline.run_once(conn, client, make_settings(slack_webhook_url=""))

    assert result["listings_scored"] == 0
    client.messages.parse.assert_not_called()
    row = conn.execute("SELECT * FROM listings").fetchone()
    assert row["score"] == 0
    assert row["description"] == "Trasig display, annars ok"
