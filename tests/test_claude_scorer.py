import json
from types import SimpleNamespace

from watcher.models import Search, WatchedModel
from watcher.scoring.claude_scorer import build_batch_request, estimate_cost_usd, parse_score_message


def _text_content(scores):
    return [SimpleNamespace(type="text", text=json.dumps({"scores": scores}))]


def test_build_batch_request_shape():
    search = Search(name="Test", hard_criteria=["HDCP 2.2 required"])
    candidates = [{"title": "Onkyo TX-NR656", "description": "", "price": 2000, "url": "u"}]

    request = build_batch_request("custom-1", "claude-haiku-4-5", search, candidates)

    assert request["custom_id"] == "custom-1"
    params = request["params"]
    assert params["model"] == "claude-haiku-4-5"
    assert params["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "HDCP 2.2 required" in params["system"][0]["text"]
    assert "Onkyo TX-NR656" in params["messages"][0]["content"]
    assert params["output_config"]["format"]["type"] == "json_schema"


def test_build_batch_request_includes_source_note_per_candidate():
    search = Search(name="Test")
    candidates = [
        {"title": "A", "description": "", "price": 100, "url": "u", "source_note": "comes with warranty"},
        {"title": "B", "description": "", "price": 200, "url": "u2"},
    ]

    request = build_batch_request("custom-1", "claude-haiku-4-5", search, candidates)

    user_text = request["params"]["messages"][0]["content"]
    assert "comes with warranty" in user_text
    assert user_text.count("note:") == 1


def test_build_batch_request_separates_ideal_from_other_watched_models():
    search = Search(
        name="GPU hunt",
        watched_models=[
            WatchedModel(pattern="RTX 4080", note="the one I want", good_price="7000-8000 SEK", is_ideal=True),
            WatchedModel(pattern="RTX 4070", note="acceptable fallback", good_price="5000-6000 SEK"),
        ],
    )
    candidates = [{"title": "RTX 4080", "description": "", "price": 7500, "url": "u"}]

    request = build_batch_request("custom-1", "claude-haiku-4-5", search, candidates)

    system_text = request["params"]["system"][0]["text"]
    assert "Buy-it-now target(s)" in system_text
    assert "score it 10/10" in system_text
    assert "RTX 4080: the one I want (good price: 7000-8000 SEK)" in system_text
    assert "Specifically watched models" in system_text
    assert "RTX 4070: acceptable fallback (good price: 5000-6000 SEK)" in system_text
    watched_section = system_text.split("Specifically watched models")[1]
    assert "RTX 4080" not in watched_section


def test_build_batch_request_omits_ideal_section_when_none_marked():
    search = Search(name="Test", watched_models=[WatchedModel(pattern="TX-NR6*", note="n", good_price="p")])
    request = build_batch_request("custom-1", "claude-haiku-4-5", search, [{"title": "A", "description": "", "price": 100, "url": "u"}])

    system_text = request["params"]["system"][0]["text"]
    assert "Buy-it-now target(s)" not in system_text
    assert "Specifically watched models" in system_text


def test_build_batch_request_includes_brevity_instruction():
    search = Search(name="Test")
    request = build_batch_request("custom-1", "claude-haiku-4-5", search, [{"title": "A", "description": "", "price": 100, "url": "u"}])

    assert "short sentence" in request["params"]["system"][0]["text"]


def test_parse_score_message_maps_results_by_index():
    content = _text_content(
        [
            {"listing_index": 1, "score": 2, "reasoning": "saknar HDCP 2.2", "uncertain_specs": [], "price_assessment": "ok"},
            {"listing_index": 0, "score": 8, "reasoning": "bra match", "uncertain_specs": ["ingångar"], "price_assessment": "bra pris"},
        ]
    )

    results = parse_score_message(content, num_candidates=2)

    assert results[0].score == 8
    assert results[0].uncertain_specs == ["ingångar"]
    assert results[1].score == 2


def test_parse_score_message_leaves_missing_index_as_none():
    content = _text_content([{"listing_index": 0, "score": 5, "reasoning": "r", "uncertain_specs": [], "price_assessment": "p"}])

    results = parse_score_message(content, num_candidates=2)

    assert results[0].score == 5
    assert results[1] is None


def test_parse_score_message_ignores_out_of_range_index():
    content = _text_content([{"listing_index": 5, "score": 5, "reasoning": "r", "uncertain_specs": [], "price_assessment": "p"}])

    results = parse_score_message(content, num_candidates=1)

    assert results == [None]


def test_parse_score_message_no_text_block_returns_all_none():
    results = parse_score_message([SimpleNamespace(type="tool_use")], num_candidates=2)
    assert results == [None, None]


def test_parse_score_message_invalid_json_returns_all_none():
    content = [SimpleNamespace(type="text", text="not valid json")]
    results = parse_score_message(content, num_candidates=2)
    assert results == [None, None]


def test_estimate_cost_usd_haiku_batch_rate_is_half_normal():
    normal = estimate_cost_usd("claude-haiku-4-5", input_tokens=1_000_000, output_tokens=1_000_000, batch=False)
    batch = estimate_cost_usd("claude-haiku-4-5", input_tokens=1_000_000, output_tokens=1_000_000, batch=True)
    assert round(normal, 2) == 6.00
    assert round(batch, 2) == 3.00


def test_estimate_cost_usd_defaults_to_batch_rate():
    cost = estimate_cost_usd("claude-haiku-4-5", input_tokens=1_000_000, output_tokens=1_000_000)
    assert round(cost, 2) == 3.00


def test_response_schema_matches_the_sdks_own_structured_output_transform():
    """Locks our hand-maintained _strict_json_schema against the real SDK
    transform client.messages.parse()'s output_format kwarg uses internally -
    we can't call that convenience path for Batch API requests (they're built
    from raw params), so this mirrors it independently. If this ever fails,
    the SDK's transform changed or our _ListingScore/_BatchScoreResponse
    models changed - regenerate _RESPONSE_SCHEMA's expectations accordingly."""
    from anthropic.lib._parse._transform import transform_schema

    from watcher.scoring.claude_scorer import _BatchScoreResponse, _RESPONSE_SCHEMA

    def strip_titles(value):
        if isinstance(value, dict):
            return {k: strip_titles(v) for k, v in value.items() if k != "title"}
        if isinstance(value, list):
            return [strip_titles(v) for v in value]
        return value

    real = strip_titles(transform_schema(_BatchScoreResponse))
    assert real == _RESPONSE_SCHEMA
