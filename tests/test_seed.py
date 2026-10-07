from watcher import containers, db
from watcher.models import Container
from watcher.seed import seed_default_containers


def test_seed_populates_default_containers(tmp_path):
    conn = db.connect(str(tmp_path / "t.db"))
    seed_default_containers(conn)
    names = {c.name for c in containers.list_containers(conn)}
    assert "Living room - AV receiver" in names
    assert "Stugan hifi" in names


def test_seed_is_noop_if_containers_exist(tmp_path):
    conn = db.connect(str(tmp_path / "t.db"))
    containers.create_container(conn, Container(name="My own container"))
    seed_default_containers(conn)
    names = {c.name for c in containers.list_containers(conn)}
    assert names == {"My own container"}
