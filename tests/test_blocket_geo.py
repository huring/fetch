from watcher.sources.blocket_geo import resolve_county_code


def test_resolve_county_code_matches_known_county():
    assert resolve_county_code("Norrbotten") == "0.300025"


def test_resolve_county_code_is_case_insensitive_and_trims_whitespace():
    assert resolve_county_code("  norrbotten  ") == "0.300025"


def test_resolve_county_code_strips_trailing_lan_suffix():
    assert resolve_county_code("Norrbottens län") == "0.300025"


def test_resolve_county_code_returns_none_for_a_city_name():
    # "Luleå" is a municipality within Norrbotten, not a county itself - a
    # city-level location should fall back to client-side filtering rather
    # than guessing a county.
    assert resolve_county_code("Luleå") is None


def test_resolve_county_code_returns_none_for_unknown_input():
    assert resolve_county_code("Not a real place") is None
