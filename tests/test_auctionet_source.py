import pytest
import responses
from responses import matchers

from watcher.sources import auctionet
from watcher.sources.base import SourceError


def _search_match(q, page=1):
    """See test_blocket_source.py's _search_match - same reasoning
    (backlog #28): without matching on the query string too, a stray real
    request from an unrelated test's leftover background thread could
    consume one of this test's queued mocks."""
    return [matchers.query_param_matcher({"q": q, "locale": "sv", "hammered": "false", "page": str(page)})]


def _item(**overrides):
    base = dict(
        id=5361509,
        title="TANDBERG TR-2055, Receiver, 1970-tal.",
        description="<p>FM Stereo Receiver.<br />Tandbergs Radiofabrikk AS Oslo.</p>",
        condition="<p>Ytslitage, ytsmuts.<br />En fot saknas.</p>",
        currency="SEK",
        estimate=1500,
        starting_bid_amount=200,
        next_bid_amount=1500,
        ends_at=1791651900,
        published_at=1790944117,
        house="Karlstad Hammarö Auktionsverk",
        location="Karlstad",
        url="https://auctionet.com/sv/5361509-tandberg-tr-2055-receiver-1970-tal",
        hammered=False,
        images=[{"thumb": "https://images.auctionet.com/thumbs/thumb_5361509.jpg", "w640": "https://images.auctionet.com/thumbs/w640_5361509.jpg"}],
    )
    base.update(overrides)
    return base


@responses.activate
def test_fetch_parses_an_item():
    responses.add(
        responses.GET,
        auctionet.SEARCH_URL,
        json={"items": [_item()], "pagination": {"current_page": 1, "total_pages": 1, "total_entries": 1}},
        status=200,
        match=_search_match("receiver", page=1),
    )

    listings = auctionet.fetch("receiver")

    assert len(listings) == 1
    listing = listings[0]
    assert listing.source == "auctionet"
    assert listing.external_id == "5361509"
    assert listing.title == "TANDBERG TR-2055, Receiver, 1970-tal."
    assert listing.price == 1500  # next_bid_amount, not estimate or starting_bid_amount
    assert "FM Stereo Receiver." in listing.description
    assert "Tandbergs Radiofabrikk AS Oslo." in listing.description
    assert "<br" not in listing.description
    assert "Skick: Ytslitage, ytsmuts." in listing.description
    assert listing.url == "https://auctionet.com/sv/5361509-tandberg-tr-2055-receiver-1970-tal"
    assert listing.location == "Karlstad"
    assert listing.auction_ends_at is not None
    assert listing.auction_ends_at.year == 2026
    assert listing.published_at is not None
    assert listing.image_url == "https://images.auctionet.com/thumbs/w640_5361509.jpg"


@responses.activate
def test_fetch_falls_back_to_starting_bid_then_estimate_when_no_next_bid():
    responses.add(
        responses.GET,
        auctionet.SEARCH_URL,
        json={
            "items": [
                _item(id=1, next_bid_amount=None, starting_bid_amount=300),
                _item(id=2, next_bid_amount=None, starting_bid_amount=None, estimate=800),
            ],
            "pagination": {"current_page": 1, "total_pages": 1, "total_entries": 2},
        },
        status=200,
        match=_search_match("receiver", page=1),
    )

    listings = auctionet.fetch("receiver")

    prices = {l.external_id: l.price for l in listings}
    assert prices["1"] == 300
    assert prices["2"] == 800


@responses.activate
def test_fetch_follows_pagination():
    responses.add(
        responses.GET,
        auctionet.SEARCH_URL,
        json={
            "items": [_item(id=1)],
            "pagination": {"current_page": 1, "total_pages": 2, "total_entries": 2},
        },
        status=200,
        match=_search_match("receiver", page=1),
    )
    responses.add(
        responses.GET,
        auctionet.SEARCH_URL,
        json={
            "items": [_item(id=2)],
            "pagination": {"current_page": 2, "total_pages": 2, "total_entries": 2},
        },
        status=200,
        match=_search_match("receiver", page=2),
    )

    listings = auctionet.fetch("receiver", max_pages=5)

    assert {l.external_id for l in listings} == {"1", "2"}


@responses.activate
def test_fetch_stops_at_max_pages():
    responses.add(
        responses.GET,
        auctionet.SEARCH_URL,
        json={
            "items": [_item(id=1)],
            "pagination": {"current_page": 1, "total_pages": 5, "total_entries": 100},
        },
        status=200,
        match=_search_match("receiver", page=1),
    )

    listings = auctionet.fetch("receiver", max_pages=1)

    assert len(listings) == 1
    assert len(responses.calls) == 1


@responses.activate
def test_fetch_skips_item_with_no_id():
    responses.add(
        responses.GET,
        auctionet.SEARCH_URL,
        json={
            "items": [{"title": "No id here"}],
            "pagination": {"current_page": 1, "total_pages": 1, "total_entries": 1},
        },
        status=200,
        match=_search_match("receiver", page=1),
    )

    listings = auctionet.fetch("receiver")

    assert listings == []


@responses.activate
def test_fetch_raises_source_error_on_http_failure():
    responses.add(responses.GET, auctionet.SEARCH_URL, status=500, match=_search_match("receiver", page=1))

    try:
        auctionet.fetch("receiver")
        assert False, "expected SourceError"
    except SourceError:
        pass


@responses.activate
def test_count_reads_total_entries_from_pagination():
    responses.add(
        responses.GET, auctionet.SEARCH_URL,
        json={"items": [], "pagination": {"current_page": 1, "total_pages": 5, "total_entries": 123}},
        status=200,
        match=_search_match("receiver", page=1),
    )

    assert auctionet.count("receiver") == 123


@responses.activate
def test_count_raises_source_error_on_http_failure():
    responses.add(responses.GET, auctionet.SEARCH_URL, status=500, match=_search_match("receiver", page=1))

    with pytest.raises(SourceError):
        auctionet.count("receiver")


@responses.activate
def test_fetch_passes_hammered_false_and_locale_sv():
    responses.add(
        responses.GET,
        auctionet.SEARCH_URL,
        json={"items": [], "pagination": {"current_page": 1, "total_pages": 1, "total_entries": 0}},
        status=200,
        match=_search_match("receiver", page=1),
    )

    auctionet.fetch("receiver")

    matching_calls = [c for c in responses.calls if "q=receiver" in c.request.url]
    assert len(matching_calls) == 1
    request_url = matching_calls[0].request.url
    assert "hammered=false" in request_url
    assert "locale=sv" in request_url
