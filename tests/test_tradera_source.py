import responses

from watcher.models import TraderaQuery
from watcher.sources import tradera
from watcher.sources.base import SourceError

SEARCH_URL = f"{tradera.BASE_URL}{tradera._SEARCH_PATH}"


@responses.activate
def test_fetch_parses_listings():
    responses.add(
        responses.GET,
        SEARCH_URL,
        json={
            "items": [
                {
                    "id": "456",
                    "shortDescription": "Marantz SR5010",
                    "longDescription": "Fint skick, HDCP 2.2",
                    "buyItNowPrice": {"value": 3000},
                    "itemUrl": "https://www.tradera.com/item/456",
                    "location": "Göteborg",
                    "shippingOptions": ["post"],
                }
            ]
        },
        status=200,
    )
    responses.add(responses.GET, SEARCH_URL, json={"items": []}, status=200)

    listings = tradera.fetch(TraderaQuery(query="marantz"), app_id="id", app_key="key")

    assert len(listings) == 1
    listing = listings[0]
    assert listing.source == "tradera"
    assert listing.external_id == "456"
    assert listing.title == "Marantz SR5010"
    assert listing.price == 3000
    assert listing.ships is True


@responses.activate
def test_fetch_sends_auth_headers():
    responses.add(responses.GET, SEARCH_URL, json={"items": []}, status=200)

    tradera.fetch(TraderaQuery(query="marantz"), app_id="my-id", app_key="my-key")

    sent_headers = responses.calls[0].request.headers
    assert sent_headers["X-App-Id"] == "my-id"
    assert sent_headers["X-App-Key"] == "my-key"


@responses.activate
def test_fetch_raises_source_error_on_http_failure():
    responses.add(responses.GET, SEARCH_URL, status=500)

    try:
        tradera.fetch(TraderaQuery(query="marantz"), app_id="id", app_key="key")
        assert False, "expected SourceError"
    except SourceError:
        pass
