from watcher import db, searches
from watcher.models import Search
from watcher.seed import seed_default_searches


def test_seed_populates_default_searches(tmp_path):
    conn = db.connect(str(tmp_path / "t.db"))
    seed_default_searches(conn)
    names = {s.name for s in searches.list_searches(conn)}
    assert "Living room - AV receiver" in names
    assert "Stugan hifi" in names


def test_seed_is_noop_if_searches_exist(tmp_path):
    conn = db.connect(str(tmp_path / "t.db"))
    searches.create_search(conn, Search(name="My own search"))
    seed_default_searches(conn)
    names = {s.name for s in searches.list_searches(conn)}
    assert names == {"My own search"}
