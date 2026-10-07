import responses

from watcher.sources import blocket
from watcher.sources.base import SourceError


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
                }
            ]
        },
        status=200,
    )
    responses.add(responses.GET, blocket.SEARCH_URL, json={"docs": []}, status=200)

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


@responses.activate
def test_fetch_stops_on_empty_page():
    responses.add(responses.GET, blocket.SEARCH_URL, json={"docs": []}, status=200)

    listings = blocket.fetch("nonexistent")

    assert listings == []
    assert len(responses.calls) == 1


@responses.activate
def test_fetch_skips_ad_without_id():
    responses.add(
        responses.GET,
        blocket.SEARCH_URL,
        json={"docs": [{"heading": "No id ad"}]},
        status=200,
    )
    responses.add(responses.GET, blocket.SEARCH_URL, json={"docs": []}, status=200)

    listings = blocket.fetch("onkyo")

    assert listings == []


@responses.activate
def test_fetch_raises_source_error_on_http_failure():
    responses.add(responses.GET, blocket.SEARCH_URL, status=500)

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
