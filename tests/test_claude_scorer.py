from types import SimpleNamespace
from unittest.mock import MagicMock

from watcher.models import Search, WatchedModel
from watcher.scoring.claude_scorer import _BatchScoreResponse, _ListingScore, estimate_cost_usd, score_batch


def make_client(scores):
    parsed = _BatchScoreResponse(scores=scores)
    response = SimpleNamespace(parsed_output=parsed, usage=SimpleNamespace(input_tokens=120, output_tokens=40))
    client = MagicMock()
    client.messages.parse.return_value = response
    return client


def test_score_batch_maps_results_by_index():
    search = Search(
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

    results, input_tokens, output_tokens = score_batch(client, "claude-haiku-4-5", search, candidates)

    assert results[0].score == 8
    assert results[0].uncertain_specs == ["ingångar"]
    assert results[1].score == 2
    assert input_tokens == 120
    assert output_tokens == 40


def test_score_batch_leaves_missing_index_as_none():
    search = Search(name="Test")
    candidates = [{"title": "A", "description": "", "price": 100, "url": "u"}, {"title": "B", "description": "", "price": 200, "url": "u2"}]
    client = make_client(
        [_ListingScore(listing_index=0, score=5, reasoning="r", uncertain_specs=[], price_assessment="p")]
    )

    results, _, _ = score_batch(client, "claude-haiku-4-5", search, candidates)

    assert results[0].score == 5
    assert results[1] is None


def test_score_batch_ignores_out_of_range_index():
    search = Search(name="Test")
    candidates = [{"title": "A", "description": "", "price": 100, "url": "u"}]
    client = make_client(
        [_ListingScore(listing_index=5, score=5, reasoning="r", uncertain_specs=[], price_assessment="p")]
    )

    results, _, _ = score_batch(client, "claude-haiku-4-5", search, candidates)

    assert results == [None]


def test_score_batch_includes_source_note_in_prompt_when_present():
    search = Search(name="Test")
    candidates = [
        {"title": "A", "description": "", "price": 100, "url": "u", "source_note": "comes with warranty"},
        {"title": "B", "description": "", "price": 200, "url": "u2"},
    ]
    client = make_client(
        [
            _ListingScore(listing_index=0, score=5, reasoning="r", uncertain_specs=[], price_assessment="p"),
            _ListingScore(listing_index=1, score=5, reasoning="r", uncertain_specs=[], price_assessment="p"),
        ]
    )

    score_batch(client, "claude-haiku-4-5", search, candidates)

    sent_prompt = client.messages.parse.call_args.kwargs["messages"][0]["content"]
    assert "comes with warranty" in sent_prompt
    assert sent_prompt.count("note:") == 1  # only the first candidate has a source_note


def test_build_prompt_separates_ideal_from_other_watched_models():
    search = Search(
        name="GPU hunt",
        watched_models=[
            WatchedModel(pattern="RTX 4080", note="the one I want", good_price="7000-8000 SEK", is_ideal=True),
            WatchedModel(pattern="RTX 4070", note="acceptable fallback", good_price="5000-6000 SEK"),
        ],
    )
    candidates = [{"title": "RTX 4080", "description": "", "price": 7500, "url": "u"}]
    client = make_client(
        [_ListingScore(listing_index=0, score=10, reasoning="r", uncertain_specs=[], price_assessment="p")]
    )

    score_batch(client, "claude-haiku-4-5", search, candidates)

    sent_prompt = client.messages.parse.call_args.kwargs["messages"][0]["content"]
    assert "Buy-it-now target(s)" in sent_prompt
    assert "score it 10/10" in sent_prompt
    assert "RTX 4080: the one I want (good price: 7000-8000 SEK)" in sent_prompt
    assert "Specifically watched models" in sent_prompt
    assert "RTX 4070: acceptable fallback (good price: 5000-6000 SEK)" in sent_prompt
    # the ideal entry shouldn't also appear under the plain watched-models section
    watched_section = sent_prompt.split("Specifically watched models")[1].split("\n\n")[0]
    assert "RTX 4080" not in watched_section


def test_build_prompt_omits_ideal_section_when_none_marked():
    search = Search(
        name="Test",
        watched_models=[WatchedModel(pattern="TX-NR6*", note="n", good_price="p")],
    )
    candidates = [{"title": "A", "description": "", "price": 100, "url": "u"}]
    client = make_client(
        [_ListingScore(listing_index=0, score=5, reasoning="r", uncertain_specs=[], price_assessment="p")]
    )

    score_batch(client, "claude-haiku-4-5", search, candidates)

    sent_prompt = client.messages.parse.call_args.kwargs["messages"][0]["content"]
    assert "Buy-it-now target(s)" not in sent_prompt
    assert "Specifically watched models" in sent_prompt


def test_estimate_cost_usd_haiku_rates():
    cost = estimate_cost_usd("claude-haiku-4-5", input_tokens=1_000_000, output_tokens=1_000_000)
    assert round(cost, 2) == 6.00
