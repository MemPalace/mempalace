"""CLI coverage for explicit RFC 002 source-adapter dispatch (#2062)."""

import argparse
import contextlib
import multiprocessing
import os
import sys
import time

import pytest

from mempalace import cli
from mempalace.sources import (
    AdapterSchema,
    BaseSourceAdapter,
    DrawerRecord,
    SourceItemMetadata,
    register,
    reset_adapters,
    unregister,
)


class _FixtureAdapter(BaseSourceAdapter):
    name = "fixture"
    adapter_version = "0.1.0"
    instances = []

    def __init__(self):
        self.source = None
        self.palace = None
        self.__class__.instances.append(self)

    def ingest(self, *, source, palace):
        self.source = source
        self.palace = palace
        yield DrawerRecord(content="fixture content", source_file="fixture://record")

    def describe_schema(self):
        return AdapterSchema(version="1.0", fields={})


class _FakeCollection:
    def __init__(self):
        self.upserts = []

    def upsert(self, **kwargs):
        self.upserts.append(kwargs)


class _FakeKnowledgeGraph:
    instances = []

    def __init__(self, db_path):
        self.db_path = db_path
        self.closed = False
        self.__class__.instances.append(self)

    def close(self):
        self.closed = True


class _FakeConfig:
    def __init__(self, palace_path=None):
        self.palace_path = palace_path or "/fake/palace"


class _DirectMutationAdapter(BaseSourceAdapter):
    name = "direct-mutation"
    adapter_version = "0.1.0"
    instances = []

    def __init__(self):
        self.palace = None
        self.__class__.instances.append(self)

    def ingest(self, *, source, palace):
        self.palace = palace
        palace.drawer_collection.upsert(
            documents=["direct content"], ids=["direct"], metadatas=[{}]
        )
        palace.knowledge_graph.add_triple("direct", "writes", "kg")
        yield DrawerRecord(content="fixture content", source_file="fixture://record")

    def describe_schema(self):
        return AdapterSchema(version="1.0", fields={})


class _ReadAwareAdapter(BaseSourceAdapter):
    name = "read-aware"
    observed_count = None

    def ingest(self, *, source, palace):
        self.__class__.observed_count = palace.drawer_collection.count()
        yield DrawerRecord(content="fixture content", source_file="fixture://record")

    def describe_schema(self):
        return AdapterSchema(version="1.0", fields={})


class _IncrementalAdapter(BaseSourceAdapter):
    name = "incremental"
    capabilities = frozenset({"supports_incremental"})

    def ingest(self, *, source, palace):
        raise AssertionError("incremental adapters must be rejected before ingest")

    def describe_schema(self):
        return AdapterSchema(version="1.0", fields={})


class _MetadataAdapter(BaseSourceAdapter):
    name = "metadata"

    def ingest(self, *, source, palace):
        yield DrawerRecord(content="before metadata", source_file="fixture://record")
        yield SourceItemMetadata(source_file="fixture://record", version="v1")

    def describe_schema(self):
        return AdapterSchema(version="1.0", fields={})


class _InvalidResultAdapter(BaseSourceAdapter):
    name = "invalid-result"

    def ingest(self, *, source, palace):
        yield object()

    def describe_schema(self):
        return AdapterSchema(version="1.0", fields={})


def _hold_palace_lock(palace_path, ready_flag, release_flag):
    """Hold a writer lease in a separate process for contention coverage."""
    from mempalace.palace import mine_palace_lock

    with mine_palace_lock(palace_path):
        open(ready_flag, "w").close()
        for _ in range(500):
            if os.path.exists(release_flag):
                return
            time.sleep(0.01)


@pytest.fixture(autouse=True)
def _isolated_fixture_adapter():
    _FixtureAdapter.instances.clear()
    _DirectMutationAdapter.instances.clear()
    _ReadAwareAdapter.observed_count = None
    _FakeKnowledgeGraph.instances.clear()
    reset_adapters()
    try:
        yield
    finally:
        unregister("fixture")
        reset_adapters()


def _mine_args(*, source=None, mode=None, dry_run=False, palace=None):
    return argparse.Namespace(
        dir="/source",
        palace=palace,
        source=source,
        mode=mode,
        wing=None,
        agent="mempalace",
        limit=0,
        dry_run=dry_run,
        no_gitignore=False,
        include_ignored=[],
        extract="exchange",
        daemon=False,
        background=False,
        max_chunks_per_file=None,
        redetect_origin=False,
    )


def test_cmd_mine_source_dispatches_registered_adapter_through_palace_context(monkeypatch):
    from mempalace import knowledge_graph, palace

    collection = _FakeCollection()
    register("fixture", _FixtureAdapter)
    monkeypatch.setattr(cli, "MempalaceConfig", _FakeConfig)
    monkeypatch.setattr(palace, "get_collection", lambda palace_path: collection)
    monkeypatch.setattr(knowledge_graph, "KnowledgeGraph", _FakeKnowledgeGraph)

    cli.cmd_mine(_mine_args(source="fixture"))

    adapter = _FixtureAdapter.instances[0]
    assert adapter.source.local_path == "/source"
    assert adapter.palace.palace_path == "/fake/palace"
    assert adapter.palace.adapter_name == "fixture"
    assert adapter.palace.adapter_version == "0.1.0"
    assert adapter.palace.drawer_collection is collection
    assert adapter.palace.knowledge_graph is _FakeKnowledgeGraph.instances[0]
    assert _FakeKnowledgeGraph.instances[0].closed is True
    assert collection.upserts[0]["documents"] == ["fixture content"]
    assert collection.upserts[0]["metadatas"][0]["adapter_name"] == "fixture"


def test_cmd_mine_source_rejects_unknown_adapter(capsys):
    with pytest.raises(SystemExit) as excinfo:
        cli.cmd_mine(_mine_args(source="not-installed"))

    assert excinfo.value.code == 2
    assert "unknown source adapter 'not-installed'" in capsys.readouterr().err


@pytest.mark.skipif(
    sys.platform == "win32", reason="cross-process lock semantics differ on Windows"
)
def test_cmd_mine_source_reports_real_contention_without_traceback(tmp_path, capsys):
    """The CLI converts a real competing writer lease into a clean exit."""
    register("fixture", _FixtureAdapter)
    palace_path = str(tmp_path / "palace")
    ready = str(tmp_path / "ready")
    release = str(tmp_path / "release")
    ctx = multiprocessing.get_context("spawn")
    holder = ctx.Process(target=_hold_palace_lock, args=(palace_path, ready, release))
    holder.start()
    try:
        for _ in range(500):
            if os.path.exists(ready):
                break
            time.sleep(0.01)
        assert os.path.exists(ready), "lock holder did not become ready"

        with pytest.raises(SystemExit) as excinfo:
            cli.cmd_mine(_mine_args(source="fixture", palace=palace_path))

        assert excinfo.value.code == 1
        error = capsys.readouterr().err
        assert error.startswith(f"mempalace: palace {palace_path} is held by PID ")
        assert "Traceback" not in error
    finally:
        open(release, "w").close()
        holder.join(timeout=10)
        if holder.is_alive():
            holder.terminate()
        assert holder.exitcode == 0


def test_mine_source_dry_run_prevents_direct_collection_and_kg_mutations(monkeypatch):
    from mempalace import knowledge_graph, palace

    register("direct-mutation", _DirectMutationAdapter)
    monkeypatch.setattr(cli, "MempalaceConfig", _FakeConfig)
    # Dry runs must never reach the backend at all (see
    # test_mine_source_dry_run_never_opens_existing_collection for the
    # dedicated assertion); failing here catches a regression that
    # reintroduces a real `get_collection` call in the dry-run path.
    monkeypatch.setattr(palace, "get_collection", lambda *_a, **_k: pytest.fail("must not open"))
    monkeypatch.setattr(knowledge_graph, "KnowledgeGraph", _FakeKnowledgeGraph)

    drawers_written = cli.mine_source_adapter(
        source_name="direct-mutation",
        source_path="/source",
        palace_path="/fake/palace",
        dry_run=True,
    )

    adapter = _DirectMutationAdapter.instances[0]
    assert drawers_written == 1
    assert _FakeKnowledgeGraph.instances == []
    assert [operation[0] for operation in adapter.palace.drawer_collection.operations] == [
        "upsert",
        "upsert",
    ]
    assert [operation[0] for operation in adapter.palace.knowledge_graph.operations] == [
        "add_triple"
    ]


def test_mine_source_dry_run_never_opens_existing_collection(monkeypatch):
    from mempalace import knowledge_graph, palace

    register("read-aware", _ReadAwareAdapter)
    monkeypatch.setattr(cli, "MempalaceConfig", _FakeConfig)
    monkeypatch.setattr(
        palace, "get_collection", lambda *_args, **_kwargs: pytest.fail("must not open")
    )
    monkeypatch.setattr(knowledge_graph, "KnowledgeGraph", _FakeKnowledgeGraph)

    assert (
        cli.mine_source_adapter(
            source_name="read-aware",
            source_path="/source",
            palace_path="/fake/palace",
            dry_run=True,
        )
        == 1
    )

    assert _ReadAwareAdapter.observed_count == 0


def test_dry_run_collection_proxy_returns_backend_result_types():
    from mempalace.backends import GetResult, QueryResult

    collection = cli._DryRunCollectionProxy()

    get_result = collection.get()
    query_result = collection.query(query_texts=["one", "two"], include=["embeddings"])

    assert isinstance(get_result, GetResult)
    assert isinstance(query_result, QueryResult)
    assert query_result.ids == [[], []]
    assert query_result.embeddings == [[], []]


def test_mine_source_dry_run_existing_uninitialized_palace_creates_no_chroma_artifacts(
    tmp_path, monkeypatch
):
    """Dry runs must not initialize Chroma in an existing empty palace dir."""
    register("fixture", _FixtureAdapter)
    palace = tmp_path / "existing-palace"
    palace.mkdir()
    monkeypatch.setenv("MEMPALACE_BACKEND", "chroma")

    assert (
        cli.mine_source_adapter(
            source_name="fixture",
            source_path="/source",
            palace_path=str(palace),
            dry_run=True,
        )
        == 1
    )

    assert list(palace.iterdir()) == []
    assert not (palace / "chroma.sqlite3").exists()


def test_mine_source_dry_run_fresh_palace_creates_no_backend_artifacts(tmp_path, monkeypatch):
    """Dry runs must not materialize a nonexistent Chroma palace."""
    register("fixture", _FixtureAdapter)
    palace = tmp_path / "fresh-palace"
    monkeypatch.setenv("MEMPALACE_BACKEND", "chroma")

    assert (
        cli.mine_source_adapter(
            source_name="fixture",
            source_path="/source",
            palace_path=str(palace),
            dry_run=True,
        )
        == 1
    )

    assert not palace.exists()


def test_mine_source_dry_run_preserves_initialized_sqlite_exact_artifacts(tmp_path, monkeypatch):
    """Dry runs must not open or alter an existing sqlite_exact backend."""
    from mempalace.backends.base import PalaceRef
    from mempalace.backends.sqlite_exact import SQLiteExactBackend

    register("fixture", _FixtureAdapter)
    palace = tmp_path / "sqlite-palace"
    backend = SQLiteExactBackend()
    backend.get_collection(
        palace=PalaceRef(id=str(palace), local_path=str(palace)),
        collection_name="mempalace_drawers",
        create=True,
    )
    backend.close()
    monkeypatch.setenv("MEMPALACE_BACKEND", "sqlite_exact")

    before = {path.name: path.read_bytes() for path in palace.iterdir()}
    assert "sqlite_exact.sqlite3" in before

    assert (
        cli.mine_source_adapter(
            source_name="fixture",
            source_path="/source",
            palace_path=str(palace),
            dry_run=True,
        )
        == 1
    )

    after = {path.name: path.read_bytes() for path in palace.iterdir()}
    assert after == before


def test_mine_source_rejects_incremental_adapter_before_ingest():
    register("incremental", _IncrementalAdapter)

    with pytest.raises(cli.UnsupportedSourceAdapterProtocolError, match="incremental ingestion"):
        cli.mine_source_adapter(
            source_name="incremental",
            source_path="/source",
            palace_path="/fake/palace",
        )


def test_mine_source_accepts_non_incremental_metadata(monkeypatch, recwarn):
    from mempalace import knowledge_graph, palace

    register("metadata", _MetadataAdapter)
    monkeypatch.setattr(cli, "MempalaceConfig", _FakeConfig)
    collection = _FakeCollection()
    monkeypatch.setattr(palace, "get_collection", lambda palace_path: collection)
    monkeypatch.setattr(knowledge_graph, "KnowledgeGraph", _FakeKnowledgeGraph)

    drawers_written = cli.mine_source_adapter(
        source_name="metadata",
        source_path="/source",
        palace_path="/fake/palace",
    )

    assert drawers_written == 1
    assert collection.upserts[0]["documents"] == ["before metadata"]
    assert "non-incremental item metadata" in str(recwarn.pop(RuntimeWarning).message)


def test_mine_source_rejects_unsupported_adapter_results():
    """Unexpected adapter yields must fail rather than being silently dropped."""
    register("invalid-result", _InvalidResultAdapter)

    with pytest.raises(TypeError, match="unsupported result type object"):
        cli.mine_source_adapter(
            source_name="invalid-result",
            source_path="/source",
            palace_path="/fake/palace",
            dry_run=True,
        )


def test_mine_source_holds_writer_lease_before_opening_handles(monkeypatch):
    from mempalace import knowledge_graph, palace

    collection = _FakeCollection()
    active = False

    @contextlib.contextmanager
    def lock(_palace_path):
        nonlocal active
        active = True
        try:
            yield
        finally:
            active = False

    def get_collection(_palace_path):
        assert active
        return collection

    register("fixture", _FixtureAdapter)
    monkeypatch.setattr(cli, "MempalaceConfig", _FakeConfig)
    monkeypatch.setattr(palace, "mine_palace_lock", lock)
    monkeypatch.setattr(palace, "get_collection", get_collection)
    monkeypatch.setattr(knowledge_graph, "KnowledgeGraph", _FakeKnowledgeGraph)

    assert (
        cli.mine_source_adapter(
            source_name="fixture", source_path="/source", palace_path="/fake/palace"
        )
        == 1
    )


@pytest.mark.skipif(
    sys.platform == "win32", reason="cross-process lock semantics differ on Windows"
)
def test_mine_source_refuses_held_writer_lease_before_opening_handles(tmp_path, monkeypatch):
    """A competing writer prevents adapter ingest and all handle creation."""
    from mempalace import knowledge_graph, palace

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    palace_path = str(tmp_path / "palace")
    ready = str(tmp_path / "ready")
    release = str(tmp_path / "release")
    opened_collection = False
    opened_kg = False

    def get_collection(_palace_path):
        nonlocal opened_collection
        opened_collection = True
        return _FakeCollection()

    class TrackingKnowledgeGraph(_FakeKnowledgeGraph):
        def __init__(self, db_path):
            nonlocal opened_kg
            opened_kg = True
            super().__init__(db_path)

    register("fixture", _FixtureAdapter)
    monkeypatch.setattr(cli, "MempalaceConfig", _FakeConfig)
    monkeypatch.setattr(palace, "get_collection", get_collection)
    monkeypatch.setattr(knowledge_graph, "KnowledgeGraph", TrackingKnowledgeGraph)

    ctx = multiprocessing.get_context("spawn")
    holder = ctx.Process(target=_hold_palace_lock, args=(palace_path, ready, release))
    holder.start()
    try:
        for _ in range(500):
            if os.path.exists(ready):
                break
            time.sleep(0.01)
        assert os.path.exists(ready), "lock holder did not become ready"

        with pytest.raises(palace.MineAlreadyRunning):
            cli.mine_source_adapter(
                source_name="fixture", source_path="/source", palace_path=palace_path
            )

        assert len(_FixtureAdapter.instances) == 1
        assert _FixtureAdapter.instances[0].palace is None
        assert not opened_collection
        assert not opened_kg
    finally:
        open(release, "w").close()
        holder.join(timeout=10)
        if holder.is_alive():
            holder.terminate()
        assert holder.exitcode == 0


def test_cmd_mine_without_mode_preserves_projects_legacy_path(monkeypatch):
    from unittest.mock import patch

    monkeypatch.setattr(cli, "MempalaceConfig", _FakeConfig)
    with patch("mempalace.miner.mine") as mine:
        cli.cmd_mine(_mine_args())

    mine.assert_called_once_with(
        project_dir="/source",
        palace_path="/fake/palace",
        wing_override=None,
        agent="mempalace",
        limit=0,
        dry_run=False,
        respect_gitignore=True,
        include_ignored=[],
        max_chunks_per_file=None,
    )


def test_mine_parser_rejects_explicit_source_and_mode(monkeypatch, capsys):
    monkeypatch.setattr(
        sys,
        "argv",
        ["mempalace", "mine", "/source", "--source", "fixture", "--mode", "projects"],
    )

    with pytest.raises(SystemExit) as excinfo:
        cli.main()

    assert excinfo.value.code == 2
    assert "not allowed with argument" in capsys.readouterr().err
