from watcher.wildcard import matches_any


def test_wildcard_matches_substring():
    assert matches_any("Onkyo TX-NR656 AV-receiver, bra skick", ["TX-NR6*"]) == "TX-NR6*"


def test_wildcard_case_insensitive():
    assert matches_any("onkyo tx-sr607", ["TX-SR607"]) is not None


def test_wildcard_no_match_returns_none():
    assert matches_any("Marantz SR5010", ["TX-NR6*", "TX-SR607"]) is None


def test_wildcard_question_mark_single_char():
    assert matches_any("Wharfedale Diamond 12.1i", ["Diamond 12.?i"]) is not None


def test_empty_text_returns_none():
    assert matches_any("", ["*anything*"]) is None
