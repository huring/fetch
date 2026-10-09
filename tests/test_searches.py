import pytest

from watcher import db, searches
from watcher.models import Search, WatchedModel


@pytest.fixture
def conn(tmp_path):
    return db.connect(str(tmp_path / "test.db"))


def test_create_and_get_round_trip(conn):
    search = Search(
        name="Pickup truck",
        scope="national",
        require_shipping=False,
        max_price=150000,
        excluded_models=["*rostskadad*"],
        watched_models=[WatchedModel(pattern="Toyota Hilux*", note="reliable", good_price="80000-120000 SEK")],
        hard_criteria=["4x4", "diesel"],
        search_phrases=["pickup"],
        marketplaces=["blocket"],
    )
    created = searches.create_search(conn, search)
    assert created.id is not None

    fetched = searches.get_search(conn, created.id)
    assert fetched.name == "Pickup truck"
    assert fetched.scope == "national"
    assert fetched.watched_models[0].pattern == "Toyota Hilux*"
    assert fetched.search_phrases == ["pickup"]
    assert fetched.marketplaces == ["blocket"]


def test_list_enabled_only(conn):
    searches.create_search(conn, Search(name="Enabled one", enabled=True))
    searches.create_search(conn, Search(name="Disabled one", enabled=False))

    all_searches = searches.list_searches(conn)
    enabled = searches.list_searches(conn, enabled_only=True)

    assert len(all_searches) == 2
    assert len(enabled) == 1
    assert enabled[0].name == "Enabled one"


def test_set_enabled_toggle(conn):
    created = searches.create_search(conn, Search(name="Stugan hifi", enabled=True))
    searches.set_enabled(conn, created.id, False)
    fetched = searches.get_search(conn, created.id)
    assert fetched.enabled is False


def test_update_search(conn):
    created = searches.create_search(conn, Search(name="Bokhyllor", max_price=500))
    created.max_price = 800
    created.hard_criteria = ["bred minst 80cm"]
    updated = searches.update_search(conn, created.id, created)
    assert updated.max_price == 800
    assert updated.hard_criteria == ["bred minst 80cm"]


def test_creation_prompt_round_trips(conn):
    created = searches.create_search(
        conn, Search(name="Förstärkare", creation_prompt="En bra stereoförstärkare, max 3000kr")
    )
    assert created.creation_prompt == "En bra stereoförstärkare, max 3000kr"
    fetched = searches.get_search(conn, created.id)
    assert fetched.creation_prompt == "En bra stereoförstärkare, max 3000kr"


def test_creation_prompt_defaults_to_none_for_a_manually_built_search(conn):
    created = searches.create_search(conn, Search(name="Manual search"))
    assert created.creation_prompt is None


def test_delete_search(conn):
    created = searches.create_search(conn, Search(name="Temp"))
    searches.delete_search(conn, created.id)
    assert searches.get_search(conn, created.id) is None
