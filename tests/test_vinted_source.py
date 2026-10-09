import json

import responses
from responses import matchers

from watcher.sources import vinted
from watcher.sources.base import SourceError


def _search_match(q, page=1):
    """See test_blocket_source.py's _search_match - same reasoning
    (backlog #28): without matching on the query string too, a stray real
    request from an unrelated test's leftover background thread could
    consume one of this test's queued mocks."""
    return [matchers.query_param_matcher({"search_text": q, "order": "newest_first", "page": str(page)})]


def _item(item_id, title, amount="500.00", url_slug=None, thumbnail_url=None):
    url_slug = url_slug or f"{item_id}-item"
    return {
        "id": item_id,
        "productItem": {
            "id": item_id,
            "title": title,
            "url": f"/items/{url_slug}",
            "price": {"amount": amount, "currencyCode": "SEK"},
            "itemBox": {"accessibilityLabel": f"{title}, Skick: Bra, {amount} kr"},
            **({"thumbnailUrl": thumbnail_url} if thumbnail_url else {}),
        },
    }


def _catalog_html(items, current_page=1, total_pages=1):
    """A minimal stand-in for Vinted's real server-rendered catalog page: the
    item data embedded as a self.__next_f.push([1, "<payload>"]) call, where
    the payload is a string containing the "items":{"items":[...]} marker
    this adapter looks for."""
    payload = (
        '1:"$Sreact.fragment"\n'
        '28:{"catalog":{"items":{"items":'
        + json.dumps(items)
        + f',"pagination":{{"current_page":{current_page},"per_page":96,'
        + f'"total_entries":{len(items)},"total_pages":{total_pages}}}}}}}'
    )
    inner = json.dumps([1, payload])
    return f"<html><body><script>self.__next_f.push({inner})</script></body></html>"


@responses.activate
def test_fetch_parses_listings():
    items = [_item(
        111, "Onkyo A-9010", amount="390.62", url_slug="111-onkyo-a-9010",
        thumbnail_url="https://images1.vinted.net/t/111.jpg",
    )]
    responses.add(responses.GET, vinted.SEARCH_URL, body=_catalog_html(items), status=200, match=_search_match("onkyo", page=1))

    listings = vinted.fetch("onkyo")

    assert len(listings) == 1
    listing = listings[0]
    assert listing.source == "vinted"
    assert listing.external_id == "111"
    assert listing.title == "Onkyo A-9010"
    assert listing.price == 391  # rounded from "390.62"
    assert listing.url == "https://www.vinted.se/items/111-onkyo-a-9010"
    assert listing.ships is True
    assert listing.location is None
    assert listing.image_url == "https://images1.vinted.net/t/111.jpg"
    assert len(responses.calls) == 1  # fewer than a full page - no page 2 request


@responses.activate
def test_fetch_stops_on_empty_page():
    responses.add(responses.GET, vinted.SEARCH_URL, body=_catalog_html([]), status=200, match=_search_match("nonexistent", page=1))

    listings = vinted.fetch("nonexistent")

    assert listings == []
    assert len(responses.calls) == 1


@responses.activate
def test_fetch_continues_to_next_page_when_full():
    page1_items = [_item(i, f"Item {i}") for i in range(1, vinted.ITEMS_PER_PAGE + 1)]
    page2_items = [_item(9999, "Last item")]
    responses.add(responses.GET, vinted.SEARCH_URL, body=_catalog_html(page1_items), status=200, match=_search_match("onkyo", page=1))
    responses.add(responses.GET, vinted.SEARCH_URL, body=_catalog_html(page2_items), status=200, match=_search_match("onkyo", page=2))

    listings = vinted.fetch("onkyo", max_pages=2)

    assert len(listings) == vinted.ITEMS_PER_PAGE + 1
    assert len(responses.calls) == 2


@responses.activate
def test_fetch_skips_item_without_id():
    items = [{"productItem": {"title": "No id item"}}]
    responses.add(responses.GET, vinted.SEARCH_URL, body=_catalog_html(items), status=200, match=_search_match("onkyo", page=1))

    listings = vinted.fetch("onkyo")

    assert listings == []


@responses.activate
def test_fetch_raises_source_error_on_http_failure():
    responses.add(responses.GET, vinted.SEARCH_URL, status=500, match=_search_match("onkyo", page=1))

    try:
        vinted.fetch("onkyo")
        assert False, "expected SourceError"
    except SourceError:
        pass


def test_fetch_handles_missing_embedded_data():
    assert vinted._extract_catalog_items("<html><body>no flight data here</body></html>") == []


AD_URL = "https://www.vinted.se/items/111-onkyo-a-9010"


@responses.activate
def test_fetch_item_description_parses_jsonld():
    html = """
    <html><head>
    <script type="application/ld+json">
    {"@type": "Product", "name": "Onkyo A-9010", "description": "Fint skick, fungerar perfekt."}
    </script>
    </head><body></body></html>
    """
    responses.add(responses.GET, AD_URL, body=html, status=200)

    assert vinted.fetch_item_description(AD_URL) == "Fint skick, fungerar perfekt."


@responses.activate
def test_fetch_item_description_missing_jsonld_returns_empty():
    responses.add(responses.GET, AD_URL, body="<html><body>no jsonld here</body></html>", status=200)

    assert vinted.fetch_item_description(AD_URL) == ""


@responses.activate
def test_fetch_item_description_http_failure_returns_empty():
    responses.add(responses.GET, AD_URL, status=500)

    assert vinted.fetch_item_description(AD_URL) == ""


def test_fetch_item_description_empty_url_returns_empty():
    assert vinted.fetch_item_description("") == ""


@responses.activate
def test_check_active_true_when_jsonld_present():
    html = '<script type="application/ld+json">{"@type": "Product", "name": "x"}</script>'
    responses.add(responses.GET, AD_URL, body=html, status=200)

    assert vinted.check_active(AD_URL) is True


@responses.activate
def test_check_active_false_on_404():
    responses.add(responses.GET, AD_URL, status=404)

    assert vinted.check_active(AD_URL) is False


@responses.activate
def test_check_active_false_when_jsonld_missing_on_200():
    responses.add(responses.GET, AD_URL, body="<html><body>item not found</body></html>", status=200)

    assert vinted.check_active(AD_URL) is False


def test_check_active_empty_url_returns_false():
    assert vinted.check_active("") is False


def _pagination_nav(page_numbers, truncated=False):
    links = "".join(f'<a data-testid="catalog-pagination--page-{n}">{n}</a>' for n in page_numbers)
    ellipsis = '<li class="web_ui__Pagination__ellipsis"></li>' if truncated else ""
    return f'<nav data-testid="catalog-pagination"><ul>{links}{ellipsis}</ul></nav>'


@responses.activate
def test_count_is_exact_when_pagination_has_no_ellipsis():
    html = f"<html><body>{_pagination_nav([1, 2, 3])}</body></html>"
    responses.add(responses.GET, vinted.SEARCH_URL, body=html, status=200, match=_search_match("onkyo", page=1))

    total, exact = vinted.count("onkyo")

    assert total == 3 * vinted.ITEMS_PER_PAGE
    assert exact is True


@responses.activate
def test_count_is_a_lower_bound_when_pagination_is_truncated():
    html = f"<html><body>{_pagination_nav([1, 2, 3], truncated=True)}</body></html>"
    responses.add(responses.GET, vinted.SEARCH_URL, body=html, status=200, match=_search_match("forstarkare", page=1))

    total, exact = vinted.count("forstarkare")

    assert total == 3 * vinted.ITEMS_PER_PAGE
    assert exact is False


@responses.activate
def test_count_falls_back_to_counting_items_when_theres_no_pagination_nav():
    items = [_item(1, "Onkyo A-9010"), _item(2, "Onkyo A-9010 B")]
    responses.add(responses.GET, vinted.SEARCH_URL, body=_catalog_html(items), status=200, match=_search_match("onkyo", page=1))

    total, exact = vinted.count("onkyo")

    assert total == 2
    assert exact is True
