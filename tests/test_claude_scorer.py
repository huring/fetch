from types import SimpleNamespace
from unittest.mock import MagicMock

from watcher.models import Container, WatchedModel
from watcher.scoring.claude_scorer import _BatchScoreResponse, _ListingScore, estimate_cost_usd, score_batch


def make_client(scores):
    parsed = _BatchScoreResponse(scores=scores)
    response = SimpleNamespace(parsed_output=parsed, usage=SimpleNamespace(input_tokens=120, output_tokens=40))
    client = MagicMock()
    client.messages.parse.return_value = response
    return client


def test_score_batch_maps_results_by_index():
    container = Container(
        name="Vardagsrummet AV-receiver",
        hard_criteria=["HDCP 2.2 passthrough required"],
        watched_models=[WatchedModel(pattern="TX-NR6*", note="bra modell", good_price="1500-2500 SEK")],
    )
    candidates = [
        {"title": "Onkyo TX-NR656", "description": "", "price": 2000, "url": "https://example.com/1"},
        {"title": "Onkyo TX-SR607", "description": "", "price": 500, "url": "https://example.com/2"},
    ]
    client = make_client(
        [
            _ListingScore(listing_index=1, score=2, reasoning="saknar HDCP 2.2", uncertain_specs=[], price_assessment="ok"),
            _ListingScore(listing_index=0, score=8, reasoning="bra match", uncertain_specs=["ingångar"], price_assessment="bra pris"),
        ]
    )

    results, input_tokens, output_tokens = score_batch(client, "claude-haiku-4-5", container, candidates)

    assert results[0].score == 8
    assert results[0].uncertain_specs == ["ingångar"]
    assert results[1].score == 2
    assert input_tokens == 120
    assert output_tokens == 40


def test_score_batch_leaves_missing_index_as_none():
    container = Container(name="Test")
    candidates = [{"title": "A", "description": "", "price": 100, "url": "u"}, {"title": "B", "description": "", "price": 200, "url": "u2"}]
    client = make_client(
        [_ListingScore(listing_index=0, score=5, reasoning="r", uncertain_specs=[], price_assessment="p")]
    )

    results, _, _ = score_batch(client, "claude-haiku-4-5", container, candidates)

    assert results[0].score == 5
    assert results[1] is None


def test_score_batch_ignores_out_of_range_index():
    container = Container(name="Test")
    candidates = [{"title": "A", "description": "", "price": 100, "url": "u"}]
    client = make_client(
        [_ListingScore(listing_index=5, score=5, reasoning="r", uncertain_specs=[], price_assessment="p")]
    )

    results, _, _ = score_batch(client, "claude-haiku-4-5", container, candidates)

    assert results == [None]


def test_estimate_cost_usd_haiku_rates():
    cost = estimate_cost_usd("claude-haiku-4-5", input_tokens=1_000_000, output_tokens=1_000_000)
    assert round(cost, 2) == 6.00
