import pytest

from watcher import containers, db
from watcher.models import BlocketQuery, Container, WatchedModel


@pytest.fixture
def conn(tmp_path):
    return db.connect(str(tmp_path / "test.db"))


def test_create_and_get_round_trip(conn):
    container = Container(
        name="Pickup truck",
        scope="national",
        require_shipping=False,
        max_price=150000,
        excluded_models=["*rostskadad*"],
        watched_models=[WatchedModel(pattern="Toyota Hilux*", note="reliable", good_price="80000-120000 SEK")],
        hard_criteria=["4x4", "diesel"],
        blocket_queries=[BlocketQuery(q="pickup", category="bilar")],
    )
    created = containers.create_container(conn, container)
    assert created.id is not None

    fetched = containers.get_container(conn, created.id)
    assert fetched.name == "Pickup truck"
    assert fetched.scope == "national"
    assert fetched.watched_models[0].pattern == "Toyota Hilux*"
    assert fetched.blocket_queries[0].q == "pickup"


def test_list_enabled_only(conn):
    containers.create_container(conn, Container(name="Enabled one", enabled=True))
    containers.create_container(conn, Container(name="Disabled one", enabled=False))

    all_containers = containers.list_containers(conn)
    enabled = containers.list_containers(conn, enabled_only=True)

    assert len(all_containers) == 2
    assert len(enabled) == 1
    assert enabled[0].name == "Enabled one"


def test_set_enabled_toggle(conn):
    created = containers.create_container(conn, Container(name="Stugan hifi", enabled=True))
    containers.set_enabled(conn, created.id, False)
    fetched = containers.get_container(conn, created.id)
    assert fetched.enabled is False


def test_update_container(conn):
    created = containers.create_container(conn, Container(name="Bokhyllor", max_price=500))
    created.max_price = 800
    created.hard_criteria = ["bred minst 80cm"]
    updated = containers.update_container(conn, created.id, created)
    assert updated.max_price == 800
    assert updated.hard_criteria == ["bred minst 80cm"]


def test_delete_container(conn):
    created = containers.create_container(conn, Container(name="Temp"))
    containers.delete_container(conn, created.id)
    assert containers.get_container(conn, created.id) is None
