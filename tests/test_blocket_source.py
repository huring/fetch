import pytest
import responses
from responses import matchers

from watcher.sources import blocket
from watcher.sources.base import SourceError


def _search_match(q, page=1):
    """A stray real request from an unrelated test's leftover background
    thread (see test_admin_routes.py's client fixture / backlog #28) hits
    this same SEARCH_URL too - without matching on the query string as well,
    `responses` would match purely by URL and could hand that stray request
    one of *this* test's queued mocks (or vice versa), silently throwing off
    pagination/call-count assertions."""
    return [matchers.query_param_matcher({
        "q": q, "cg": blocket.DEFAULT_CATEGORY, "sc": blocket.DEFAULT_SUB_CATEGORY,
        "sort": "PUBLISHED_DESC", "page": str(page),
    })]


@responses.activate
def test_fetch_parses_listings():
    responses.add(
        responses.GET,
        blocket.SEARCH_URL,
        json={
            "docs": [
                {
                    "id": "123",
                    "ad_id": 123,
                    "heading": "Onkyo TX-NR656",
                    "price": {"amount": 2500, "currency_code": "SEK"},
                    "canonical_url": "https://www.blocket.se/recommerce/forsale/item/123",
                    "location": "Stockholm",
                    "timestamp": 1791106631000,
                    "image": {"url": "https://images.blocketcdn.se/item/123.jpg"},
                }
            ]
        },
        status=200,
        match=_search_match("onkyo", page=1),
    )
    responses.add(responses.GET, blocket.SEARCH_URL, json={"docs": []}, status=200, match=_search_match("onkyo", page=2))

    listings = blocket.fetch("onkyo")

    assert len(listings) == 1
    listing = listings[0]
    assert listing.source == "blocket"
    assert listing.external_id == "123"
    assert listing.title == "Onkyo TX-NR656"
    assert listing.price == 2500
    assert listing.location == "Stockholm"
    assert listing.url == "https://www.blocket.se/recommerce/forsale/item/123"
    assert listing.published_at is not None
    assert listing.published_at.year == 2026
    assert listing.image_url == "https://images.blocketcdn.se/item/123.jpg"


@responses.activate
def test_fetch_passes_location_code_when_given():
    """Confirmed live (2026-10): Blocket's search endpoint
    genuinely filters server-side on its own county facet code, unlike the
    plain county name backlog #26 tried."""
    match = [matchers.query_param_matcher({
        "q": "ski-doo", "cg": blocket.DEFAULT_CATEGORY, "sc": blocket.DEFAULT_SUB_CATEGORY,
        "sort": "PUBLISHED_DESC", "page": "1", "location": "0.300025",
    })]
    responses.add(responses.GET, blocket.SEARCH_URL, json={"docs": []}, status=200, match=match)

    blocket.fetch("ski-doo", location_code="0.300025")

    assert len(responses.calls) == 1


@responses.activate
def test_fetch_sorts_by_distance_when_given_a_point():
    """Confirmed live (2026-10): Blocket's own "Closest" sort genuinely
    reorders results around a lat/lon point - used for a "plain" search,
    which has no AI ranking of its own (see marketplaces._blocket_fetch)."""
    match = [matchers.query_param_matcher({
        "q": "förstärkare", "cg": blocket.DEFAULT_CATEGORY, "sc": blocket.DEFAULT_SUB_CATEGORY,
        "sort": "CLOSEST", "page": "1", "lat": "65.80823", "lon": "21.67276",
    })]
    responses.add(
        responses.GET, blocket.SEARCH_URL, status=200,
        json={"docs": [{"ad_id": 1, "heading": "Vitus Ri-101", "distance": 1.42}]},
        match=match,
    )

    listings = blocket.fetch("förstärkare", sort_by_distance_from=(65.80823, 21.67276), max_pages=1)

    assert len(listings) == 1
    assert listings[0].distance_km == 1.42


@responses.activate
def test_fetch_omits_distance_km_when_not_sorting_by_distance():
    responses.add(
        responses.GET, blocket.SEARCH_URL, status=200,
        json={"docs": [{"ad_id": 1, "heading": "Vitus Ri-101", "distance": 0}]},
        match=_search_match("förstärkare", page=1),
    )

    listings = blocket.fetch("förstärkare", max_pages=1)

    assert listings[0].distance_km is None


@responses.activate
def test_fetch_omits_location_param_when_not_given():
    responses.add(responses.GET, blocket.SEARCH_URL, json={"docs": []}, status=200, match=_search_match("onkyo", page=1))

    blocket.fetch("onkyo")

    assert "location=" not in responses.calls[0].request.url


@responses.activate
def test_fetch_stops_on_empty_page():
    responses.add(responses.GET, blocket.SEARCH_URL, json={"docs": []}, status=200, match=_search_match("nonexistent", page=1))

    listings = blocket.fetch("nonexistent")

    assert listings == []
    matching_calls = [c for c in responses.calls if "q=nonexistent" in c.request.url]
    assert len(matching_calls) == 1


@responses.activate
def test_fetch_skips_ad_without_id():
    responses.add(
        responses.GET,
        blocket.SEARCH_URL,
        json={"docs": [{"heading": "No id ad"}]},
        status=200,
        match=_search_match("onkyo", page=1),
    )
    responses.add(responses.GET, blocket.SEARCH_URL, json={"docs": []}, status=200, match=_search_match("onkyo", page=2))

    listings = blocket.fetch("onkyo")

    assert listings == []


@responses.activate
def test_fetch_raises_source_error_on_http_failure():
    responses.add(responses.GET, blocket.SEARCH_URL, status=500, match=_search_match("onkyo", page=1))

    try:
        blocket.fetch("onkyo")
        assert False, "expected SourceError"
    except SourceError:
        pass


AD_URL = "https://www.blocket.se/recommerce/forsale/item/123"


@responses.activate
def test_fetch_ad_description_parses_jsonld():
    html = """
    <html><head>
    <script type="application/ld+json">
    {"@type": "Product", "name": "Marantz SR5010", "description": "Marantz SR5010 AV-receiver i svart, fint skick."}
    </script>
    </head><body></body></html>
    """
    responses.add(responses.GET, AD_URL, body=html, status=200)

    description = blocket.fetch_ad_description(AD_URL)

    assert description == "Marantz SR5010 AV-receiver i svart, fint skick."


@responses.activate
def test_fetch_ad_description_missing_jsonld_returns_empty():
    responses.add(responses.GET, AD_URL, body="<html><body>no jsonld here</body></html>", status=200)

    assert blocket.fetch_ad_description(AD_URL) == ""


@responses.activate
def test_fetch_ad_description_invalid_json_returns_empty():
    html = '<script type="application/ld+json">not valid json</script>'
    responses.add(responses.GET, AD_URL, body=html, status=200)

    assert blocket.fetch_ad_description(AD_URL) == ""


@responses.activate
def test_fetch_ad_description_http_failure_returns_empty():
    responses.add(responses.GET, AD_URL, status=500)

    assert blocket.fetch_ad_description(AD_URL) == ""


def test_fetch_ad_description_empty_url_returns_empty():
    assert blocket.fetch_ad_description("") == ""


@responses.activate
def test_check_active_true_when_jsonld_present():
    html = '<script type="application/ld+json">{"@type": "Product", "name": "x"}</script>'
    responses.add(responses.GET, AD_URL, body=html, status=200)

    assert blocket.check_active(AD_URL) is True


@responses.activate
def test_check_active_false_on_404():
    responses.add(responses.GET, AD_URL, status=404)

    assert blocket.check_active(AD_URL) is False


@responses.activate
def test_check_active_false_when_jsonld_missing_on_200():
    responses.add(responses.GET, AD_URL, body="<html><body>borttagen</body></html>", status=200)

    assert blocket.check_active(AD_URL) is False


def test_check_active_empty_url_returns_false():
    assert blocket.check_active("") is False


@responses.activate
def test_count_reads_match_count_from_metadata():
    match = [matchers.query_param_matcher({
        "q": "förstärkare", "cg": blocket.DEFAULT_CATEGORY, "sc": blocket.DEFAULT_SUB_CATEGORY,
        "sort": "PUBLISHED_DESC", "page": "1",
    })]
    responses.add(
        responses.GET, blocket.SEARCH_URL, status=200,
        json={"docs": [], "metadata": {"result_size": {"match_count": 2789}}},
        match=match,
    )

    assert blocket.count("förstärkare") == 2789


@responses.activate
def test_count_passes_location_code_when_given():
    match = [matchers.query_param_matcher({
        "q": "ski-doo", "cg": blocket.DEFAULT_CATEGORY, "sc": blocket.DEFAULT_SUB_CATEGORY,
        "sort": "PUBLISHED_DESC", "page": "1", "location": "0.300025",
    })]
    responses.add(
        responses.GET, blocket.SEARCH_URL, status=200,
        json={"docs": [], "metadata": {"result_size": {"match_count": 9}}},
        match=match,
    )

    assert blocket.count("ski-doo", location_code="0.300025") == 9


@responses.activate
def test_count_raises_source_error_on_http_failure():
    responses.add(responses.GET, blocket.SEARCH_URL, status=500)

    with pytest.raises(SourceError):
        blocket.count("onkyo")
