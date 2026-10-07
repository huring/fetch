from watcher.models import Search
from watcher.scoring.prefilter import passes_prefilter


def make_search(**kwargs):
    return Search(name="Test", **kwargs)


def test_passes_with_no_criteria():
    ok, _ = passes_prefilter("Onkyo TX-NR656", "bra skick", 2000, make_search())
    assert ok is True


def test_fails_on_max_price():
    search = make_search(max_price=1500)
    ok, reason = passes_prefilter("Onkyo TX-NR656", "", 2000, search)
    assert ok is False
    assert "max_price" in reason


def test_price_none_does_not_fail_max_price():
    search = make_search(max_price=1500)
    ok, _ = passes_prefilter("Onkyo TX-NR656", "", None, search)
    assert ok is True


def test_fails_on_excluded_model_wildcard():
    search = make_search(excluded_models=["TX-SR6*"])
    ok, reason = passes_prefilter("Onkyo TX-SR607 receiver", "", 1000, search)
    assert ok is False
    assert "excluded model" in reason


def test_fails_on_excluded_word():
    search = make_search(excluded_words=["trasig"])
    ok, reason = passes_prefilter("Onkyo TX-NR656, trasig display", "", 1000, search)
    assert ok is False
    assert "excluded word" in reason


def test_fails_when_no_required_keyword_present():
    search = make_search(required_keywords=["receiver", "förstärkare"])
    ok, reason = passes_prefilter("Jamo E470 högtalare", "", 1000, search)
    assert ok is False
    assert "required keyword" in reason


def test_passes_when_required_keyword_present_in_description():
    search = make_search(required_keywords=["receiver"])
    ok, _ = passes_prefilter("Onkyo TX-NR656", "Fin receiver", 1000, search)
    assert ok is True
