from mempalace.sources.lifecycle import SourceLifecycleStore


def test_generation_is_invisible_until_activated(tmp_path):
    store = SourceLifecycleStore(str(tmp_path / "knowledge_graph.sqlite3"))

    staged = store.begin(adapter_name="fixture", source_file="fixture://one", version="v1")

    assert staged.state == "staging"
    assert store.active(adapter_name="fixture", source_file="fixture://one") is None

    assert store.activate(staged) is None
    assert store.active(adapter_name="fixture", source_file="fixture://one").generation == staged.generation


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
