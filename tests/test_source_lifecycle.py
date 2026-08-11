from mempalace.sources.lifecycle import SourceLifecycleStore


def test_generation_is_invisible_until_activated(tmp_path):
    store = SourceLifecycleStore(str(tmp_path / "knowledge_graph.sqlite3"))

    staged = store.begin(adapter_name="fixture", source_file="fixture://one", version="v1")

    assert staged.state == "staging"
    assert store.active(adapter_name="fixture", source_file="fixture://one") is None

    assert store.activate(staged) is None
    assert (
        store.active(adapter_name="fixture", source_file="fixture://one").generation
        == staged.generation
    )


def test_activation_retires_prior_generation(tmp_path):
    store = SourceLifecycleStore(str(tmp_path / "knowledge_graph.sqlite3"))
    first = store.begin(adapter_name="fixture", source_file="fixture://one", version="v1")
    store.activate(first)
    second = store.begin(adapter_name="fixture", source_file="fixture://one", version="v2")

    previous = store.activate(second)

    assert previous is not None
    assert previous.generation == first.generation
    active = store.active(adapter_name="fixture", source_file="fixture://one")
    assert active.generation == second.generation
    assert active.version == "v2"


def test_abandon_keeps_active_generation(tmp_path):
    store = SourceLifecycleStore(str(tmp_path / "knowledge_graph.sqlite3"))
    first = store.begin(adapter_name="fixture", source_file="fixture://one", version="v1")
    store.activate(first)
    failed = store.begin(adapter_name="fixture", source_file="fixture://one", version="v2")

    store.abandon(failed)

    active = store.active(adapter_name="fixture", source_file="fixture://one")
    assert active.generation == first.generation


def test_read_only_lookup_does_not_create_lifecycle_schema(tmp_path):
    db_path = tmp_path / "knowledge_graph.sqlite3"
    # A pre-existing KG database without RFC 002 lifecycle state is normal.
    import sqlite3

    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE existing (id INTEGER PRIMARY KEY)")

    store = SourceLifecycleStore(str(db_path), initialize=False)

    assert store.active(adapter_name="fixture", source_file="fixture://one") is None
    with sqlite3.connect(db_path) as conn:
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    assert tables == {"existing"}


def test_tombstone_supersedes_active_generation(tmp_path):
    store = SourceLifecycleStore(str(tmp_path / "knowledge_graph.sqlite3"))
    first = store.begin(adapter_name="fixture", source_file="fixture://one", version="v1")
    store.activate(first)

    tombstone = store.tombstone(adapter_name="fixture", source_file="fixture://one")

    active = store.active(adapter_name="fixture", source_file="fixture://one")
    assert active.generation == tombstone.generation
    assert active.version == "__deleted__"
