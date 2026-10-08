from watcher.sources.base import extract_jsonld_product, extract_og_image


def test_extract_jsonld_product_finds_product_block_among_others():
    html = """
    <script type="application/ld+json">{"@type": "BreadcrumbList", "itemListElement": []}</script>
    <script type="application/ld+json">{"@type": "Product", "name": "VU Meter Pro", "image": "https://example.com/vu.jpg"}</script>
    """
    product = extract_jsonld_product(html)
    assert product is not None
    assert product["name"] == "VU Meter Pro"


def test_extract_jsonld_product_handles_graph_wrapper():
    html = """
    <script type="application/ld+json">
    {"@context": "https://schema.org", "@graph": [
        {"@type": "Organization", "name": "Shop"},
        {"@type": "Product", "name": "VU Meter Pro"}
    ]}
    </script>
    """
    product = extract_jsonld_product(html)
    assert product is not None
    assert product["name"] == "VU Meter Pro"


def test_extract_jsonld_product_handles_type_as_list():
    html = """<script type="application/ld+json">{"@type": ["Product", "Thing"], "name": "VU Meter Pro"}</script>"""
    product = extract_jsonld_product(html)
    assert product is not None
    assert product["name"] == "VU Meter Pro"


def test_extract_jsonld_product_returns_none_when_absent():
    html = """<script type="application/ld+json">{"@type": "BreadcrumbList"}</script>"""
    assert extract_jsonld_product(html) is None


def test_extract_jsonld_product_returns_none_on_malformed_json():
    html = """<script type="application/ld+json">{not valid json</script>"""
    assert extract_jsonld_product(html) is None


def test_extract_og_image_finds_content_then_property_order():
    html_a = '<meta property="og:image" content="https://example.com/a.jpg">'
    html_b = '<meta content="https://example.com/b.jpg" property="og:image">'
    assert extract_og_image(html_a) == "https://example.com/a.jpg"
    assert extract_og_image(html_b) == "https://example.com/b.jpg"


def test_extract_og_image_returns_empty_when_absent():
    assert extract_og_image("<html><head></head></html>") == ""
