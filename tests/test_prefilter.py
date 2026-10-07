from watcher.models import Container
from watcher.scoring.prefilter import passes_prefilter


def make_container(**kwargs):
    return Container(name="Test", **kwargs)


def test_passes_with_no_criteria():
    ok, _ = passes_prefilter("Onkyo TX-NR656", "bra skick", 2000, make_container())
    assert ok is True


def test_fails_on_max_price():
    container = make_container(max_price=1500)
    ok, reason = passes_prefilter("Onkyo TX-NR656", "", 2000, container)
    assert ok is False
    assert "max_price" in reason


def test_price_none_does_not_fail_max_price():
    container = make_container(max_price=1500)
    ok, _ = passes_prefilter("Onkyo TX-NR656", "", None, container)
    assert ok is True


def test_fails_on_excluded_model_wildcard():
    container = make_container(excluded_models=["TX-SR6*"])
    ok, reason = passes_prefilter("Onkyo TX-SR607 receiver", "", 1000, container)
    assert ok is False
    assert "excluded model" in reason


def test_fails_on_excluded_word():
    container = make_container(excluded_words=["trasig"])
    ok, reason = passes_prefilter("Onkyo TX-NR656, trasig display", "", 1000, container)
    assert ok is False
    assert "excluded word" in reason


def test_fails_when_no_required_keyword_present():
    container = make_container(required_keywords=["receiver", "förstärkare"])
    ok, reason = passes_prefilter("Jamo E470 högtalare", "", 1000, container)
    assert ok is False
    assert "required keyword" in reason


def test_passes_when_required_keyword_present_in_description():
    container = make_container(required_keywords=["receiver"])
    ok, _ = passes_prefilter("Onkyo TX-NR656", "Fin receiver", 1000, container)
    assert ok is True
