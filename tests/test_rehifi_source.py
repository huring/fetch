import datetime

import pytest
import responses

from watcher.sources import rehifi
from watcher.sources.base import SourceError


@pytest.fixture(autouse=True)
def reset_catalog_cache():
    rehifi._catalog_cache = None
    rehifi._catalog_cache_fetched_at = None
    yield
    rehifi._catalog_cache = None
    rehifi._catalog_cache_fetched_at = None


SITEMAP_INDEX = """<?xml version="1.0"?>
<sitemapindex>
  <sitemap><loc>https://www.rehifi.se/sitemap-pages.xml</loc></sitemap>
  <sitemap><loc>https://www.rehifi.se/sitemap-products-sv-1.xml</loc></sitemap>
  <sitemap><loc>https://www.rehifi.se/sitemap-products-sv-2.xml</loc></sitemap>
</sitemapindex>"""

SITEMAP_PAGE_1 = """<?xml version="1.0"?>
<urlset>
  <url><loc>https://www.rehifi.se/product/rega-elicit-mk5</loc></url>
  <url><loc>https://www.rehifi.se/product/aad-c-401i</loc></url>
</urlset>"""

SITEMAP_PAGE_2 = """<?xml version="1.0"?>
<urlset>
  <url><loc>https://www.rehifi.se/product/dali-oberon-1</loc></url>
</urlset>"""


def _product_html(name, price, sku, in_stock=True, description="Märke: Test<br>Modell: X", image=""):
    availability = "http://schema.org/InStock" if in_stock else "http://schema.org/OutOfStock"
    jsonld = (
        '[{"@type": "Product", "name": "%s", "sku": "%s", "description": "", "image": "%s", '
        '"offers": {"@type": "Offer", "price": "%s", "priceCurrency": "SEK", '
        '"availability": "%s"}}]' % (name, sku, image, price, availability)
    )
    return f"""
    <html><body>
    <script type="application/ld+json">{jsonld}</script>
    <div class="long description product-long-description">{description}</div>
    </body></html>
    """


def _mock_catalog():
    responses.add(responses.GET, rehifi.SITEMAP_INDEX_URL, body=SITEMAP_INDEX, status=200)
    responses.add(responses.GET, "https://www.rehifi.se/sitemap-products-sv-1.xml", body=SITEMAP_PAGE_1, status=200)
    responses.add(responses.GET, "https://www.rehifi.se/sitemap-products-sv-2.xml", body=SITEMAP_PAGE_2, status=200)


@responses.activate
def test_fetch_matches_phrase_against_slug_and_parses_listing():
    _mock_catalog()
    responses.add(
        responses.GET,
        "https://www.rehifi.se/product/rega-elicit-mk5",
        body=_product_html("Rega Elicit MK5", "17990", "88210", image="https://www.rehifi.se/img/rega.jpg"),
        status=200,
    )

    listings = rehifi.fetch("rega elicit")

    assert len(listings) == 1
    listing = listings[0]
    assert listing.source == "rehifi"
    assert listing.external_id == "88210"
    assert listing.title == "Rega Elicit MK5"
    assert listing.price == 17990
    assert listing.url == "https://www.rehifi.se/product/rega-elicit-mk5"
    assert listing.ships is True
    assert listing.location is None
    assert "Märke: Test" in listing.description
    assert listing.image_url == "https://www.rehifi.se/img/rega.jpg"


@responses.activate
def test_fetch_skips_out_of_stock_products():
    _mock_catalog()
    responses.add(
        responses.GET,
        "https://www.rehifi.se/product/aad-c-401i",
        body=_product_html("AAD C-401i", "1500", "45084", in_stock=False),
        status=200,
    )

    listings = rehifi.fetch("aad c-401i")

    assert listings == []
    # The empty result above must come from the in-stock check, not from the
    # slug match itself silently failing to find the product at all.
    assert responses.calls[-1].request.url == "https://www.rehifi.se/product/aad-c-401i"


@responses.activate
def test_fetch_matches_hyphenated_model_number_in_phrase():
    """Regression test: a product's slug already hyphenates its model number
    (e.g. "aad-c-401i"), and a search phrase naturally written the same way
    (e.g. "aad c-401i") must still match it - the hyphen in the phrase word
    and the hyphen in the slug need to normalize to the same thing."""
    _mock_catalog()
    responses.add(
        responses.GET,
        "https://www.rehifi.se/product/aad-c-401i",
        body=_product_html("AAD C-401i", "1500", "45084"),
        status=200,
    )

    listings = rehifi.fetch("aad c-401i")

    assert len(listings) == 1
    assert listings[0].title == "AAD C-401i"


@responses.activate
def test_fetch_no_slug_match_returns_empty_without_fetching_products():
    _mock_catalog()

    listings = rehifi.fetch("nonexistent brand xyz")

    assert listings == []


def test_fetch_empty_phrase_returns_empty():
    assert rehifi.fetch("") == []
    assert rehifi.fetch("   ") == []


@responses.activate
def test_catalog_is_cached_across_calls():
    _mock_catalog()
    responses.add(
        responses.GET,
        "https://www.rehifi.se/product/rega-elicit-mk5",
        body=_product_html("Rega Elicit MK5", "17990", "88210"),
        status=200,
    )

    rehifi.fetch("rega elicit")
    sitemap_calls_after_first = len(responses.calls)
    rehifi.fetch("rega elicit")

    # second fetch should not re-hit the sitemap index/product-sitemap files
    sitemap_related_calls = [c for c in responses.calls if "sitemap" in c.request.url]
    assert len(sitemap_related_calls) == 3  # index + 2 product-sitemap files, once only


@responses.activate
def test_catalog_falls_back_to_stale_cache_on_refresh_failure():
    _mock_catalog()
    responses.add(
        responses.GET,
        "https://www.rehifi.se/product/rega-elicit-mk5",
        body=_product_html("Rega Elicit MK5", "17990", "88210"),
        status=200,
    )
    rehifi.fetch("rega elicit")  # populates the cache

    # force the cache to look expired, then make the sitemap unreachable
    rehifi._catalog_cache_fetched_at = datetime.datetime.utcnow() - datetime.timedelta(hours=25)
    responses.replace(responses.GET, rehifi.SITEMAP_INDEX_URL, status=500)

    catalog = rehifi._get_product_catalog()

    assert len(catalog) == 3  # stale cache still used


@responses.activate
def test_get_product_catalog_raises_when_no_cache_and_unreachable():
    responses.add(responses.GET, rehifi.SITEMAP_INDEX_URL, status=500)

    with pytest.raises(SourceError):
        rehifi._get_product_catalog()


@responses.activate
def test_fetch_caps_candidates_per_phrase(monkeypatch):
    monkeypatch.setattr(rehifi, "MAX_CANDIDATES_PER_PHRASE", 1)
    responses.add(
        responses.GET, rehifi.SITEMAP_INDEX_URL,
        body="""<?xml version="1.0"?><sitemapindex>
            <sitemap><loc>https://www.rehifi.se/sitemap-products-sv-1.xml</loc></sitemap>
        </sitemapindex>""",
        status=200,
    )
    responses.add(
        responses.GET, "https://www.rehifi.se/sitemap-products-sv-1.xml",
        body="""<?xml version="1.0"?><urlset>
            <url><loc>https://www.rehifi.se/product/onkyo-a</loc></url>
            <url><loc>https://www.rehifi.se/product/onkyo-b</loc></url>
        </urlset>""",
        status=200,
    )
    responses.add(
        responses.GET, "https://www.rehifi.se/product/onkyo-a",
        body=_product_html("Onkyo A", "100", "1"), status=200,
    )

    listings = rehifi.fetch("onkyo")

    assert len(listings) == 1  # only the first match was fetched, per the cap


@responses.activate
def test_check_active_true_in_stock():
    url = "https://www.rehifi.se/product/rega-elicit-mk5"
    responses.add(responses.GET, url, body=_product_html("Rega Elicit MK5", "17990", "88210"), status=200)

    assert rehifi.check_active(url) is True


@responses.activate
def test_check_active_false_out_of_stock():
    url = "https://www.rehifi.se/product/aad-c-401i"
    responses.add(responses.GET, url, body=_product_html("AAD C-401i", "1500", "45084", in_stock=False), status=200)

    assert rehifi.check_active(url) is False


@responses.activate
def test_check_active_false_on_404():
    url = "https://www.rehifi.se/product/gone"
    responses.add(responses.GET, url, status=404)

    assert rehifi.check_active(url) is False


def test_check_active_empty_url_returns_false():
    assert rehifi.check_active("") is False
