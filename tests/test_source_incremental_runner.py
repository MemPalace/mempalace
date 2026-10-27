"""Synthetic incremental adapter lifecycle through the actual CLI runner."""

import contextlib
import sqlite3
import subprocess
import sys

import pytest

from mempalace import cli, palace
from mempalace.sources import (
    AdapterSchema,
    BaseSourceAdapter,
    DrawerRecord,
    SourceItemMetadata,
    register,
    reset_adapters,
)
from mempalace.sources.incremental import IncrementalProtocolError
from mempalace.sources.lifecycle import (
    LifecycleConflictError,
    LifecycleStateError,
    SourceLifecycleSnapshot,
    SourceLifecycleStore,
    lifecycle_path,
)
from mempalace.sources.runner import run_incremental_adapter


class FixtureAdapter(BaseSourceAdapter):
    name = "incremental-fixture"
    adapter_version = "1.2.3"
    capabilities = frozenset({"supports_incremental", "supports_deletion_tombstones"})

    def __init__(self):
        self.version = "v1"
        self.chunks = ["first exact text", "second exact text", "obsolete tail"]
        self.source_file = "fixture://same-source"
        self.extractions = 0
        self.cursors = []
        self.action = None
        self.failure = None
        self.context = None

    def describe_schema(self):
        return AdapterSchema(fields={}, version="1")

    def is_current(self, *, item, existing_metadata):
        # Generic #1944 consumer: no adapter-specific version key is required.
        self.cursors.append(existing_metadata)
        return bool(
            existing_metadata
            and existing_metadata["adapter_name"] == self.name
            and existing_metadata["source_file"] == item.source_file
            and existing_metadata["source_version"] == item.version
            and existing_metadata["source_deleted"] == (item.version == "__deleted__")
        )

    def ingest(self, *, source, palace):
        self.context = palace
        yield SourceItemMetadata(self.source_file, self.version)
        if palace._skip_requested:
            return
        self.extractions += 1
        if self.action:
            self.action(palace)
        if self.failure == "before":
            raise RuntimeError("extraction before writes")
        for i, text in enumerate(self.chunks):
            yield DrawerRecord(text, self.source_file, i)
            if self.failure == "after":
                raise RuntimeError("extraction after writes")


@pytest.fixture(params=["sqlite_exact", "chroma"])
def setup_runner(tmp_path, monkeypatch, request):
    monkeypatch.setenv("MEMPALACE_BACKEND", request.param)
    reset_adapters()
    adapter = FixtureAdapter()
    register(adapter.name, lambda: adapter)
    path = str(tmp_path / "synthetic-palace")

    def run(**kwargs):
        return cli.mine_source_adapter(
            source_name=adapter.name, source_path="/synthetic", palace_path=path, **kwargs
        )

    yield adapter, path, run
    reset_adapters()


def contents(path):
    return set(palace.get_collection(path).get(include=["documents"]).documents)


def current(path, adapter):
    with SourceLifecycleSnapshot(path) as snapshot:
        return snapshot.current(adapter.name, adapter.source_file)


def test_unchanged_changed_shortened_empty_deleted_recreated(setup_runner):
    adapter, path, run = setup_runner
    assert run() == 3
    first = current(path, adapter)
    assert run() == 0
    assert adapter.extractions == 1
    assert current(path, adapter) == first
    adapter.version, adapter.chunks = "v2", ["short replacement"]
    assert run() == 1
    assert contents(path) == {"short replacement"}
    adapter.version, adapter.chunks = "v3", []
    assert run() == 0
    assert contents(path) == set()
    assert run() == 0
    assert adapter.cursors[-1]["source_version"] == "v3"
    assert "openclaw_session_version" not in adapter.cursors[-1]
    adapter.version, adapter.chunks = "v4", ["reappeared exact text"]
    assert run() == 1
    adapter.version, adapter.chunks = "__deleted__", []
    assert run() == 0
    assert current(path, adapter).deleted
    assert contents(path) == set()
    assert run() == 0
    adapter.version, adapter.chunks = "v4", ["reappeared exact text"]
    assert run() == 1
    assert contents(path) == {"reappeared exact text"}
    assert not current(path, adapter).deleted


@pytest.mark.parametrize("failure", ["before", "after"])
def test_failed_extraction_preserves_last_good_and_retry(setup_runner, failure):
    adapter, path, run = setup_runner
    run()
    old = current(path, adapter)
    adapter.version, adapter.chunks, adapter.failure = "v2", ["unpublished"], failure
    with pytest.raises(RuntimeError, match="extraction"):
        run()
    assert current(path, adapter) == old
    assert contents(path) == {"first exact text", "second exact text", "obsolete tail"}
    adapter.failure = None
    assert run() == 1
    assert contents(path) == {"unpublished"}


def test_activation_failure_rolls_back_then_retry(setup_runner):
    adapter, path, run = setup_runner
    run()
    old = current(path, adapter)
    with contextlib.closing(sqlite3.connect(lifecycle_path(path))) as conn, conn:
        conn.execute(
            "CREATE TRIGGER fail_activation BEFORE UPDATE ON source_generations "
            "WHEN NEW.state='active' BEGIN SELECT RAISE(ABORT, 'injected activation'); END"
        )
    adapter.version, adapter.chunks = "v2", ["replacement"]
    with pytest.raises(sqlite3.IntegrityError, match="injected activation"):
        run()
    assert current(path, adapter) == old
    assert "replacement" not in contents(path)
    with contextlib.closing(sqlite3.connect(lifecycle_path(path))) as conn, conn:
        conn.execute("DROP TRIGGER fail_activation")
    assert run() == 1


def test_cleanup_failure_does_not_undo_activation(setup_runner, monkeypatch, caplog):
    adapter, path, run = setup_runner
    run()
    collection = palace.get_collection(path)
    real_delete = collection.delete
    monkeypatch.setattr(palace, "get_collection", lambda _path: collection)

    def fail_delete(**kwargs):
        raise OSError("synthetic cleanup failure")

    monkeypatch.setattr(collection, "delete", fail_delete)
    adapter.version, adapter.chunks = "v2", ["new current"]
    assert run() == 1
    assert current(path, adapter).version == "v2"
    assert contents(path) == {"new current"}
    assert "cleanup deferred" in caplog.text
    adapter.version = "v3"
    with pytest.raises(OSError, match="synthetic cleanup"):
        run()
    assert current(path, adapter).version == "v2"
    monkeypatch.setattr(collection, "delete", real_delete)
    assert run() == 1


def test_crash_orphan_recovered_before_new_staging(setup_runner):
    adapter, path, run = setup_runner
    run()
    # A separate process dies with a real committed staging handle.
    code = """
import os, sys
from mempalace.sources.lifecycle import SourceLifecycleStore
from mempalace.palace import mine_palace_lock
with mine_palace_lock(sys.argv[1]):
    SourceLifecycleStore(sys.argv[1]).begin(sys.argv[2], sys.argv[3], 'crashed')
    os._exit(0)
"""
    subprocess.run(
        [sys.executable, "-c", code, path, adapter.name, adapter.source_file],
        check=True,
        timeout=15,
    )
    with SourceLifecycleSnapshot(path) as snapshot:
        orphan = snapshot.pending(adapter.name, adapter.source_file)
    assert orphan is not None
    palace.get_collection(path).add(
        ids=["orphan-physical-row"], documents=["crashed hidden text"], metadatas=[orphan.metadata]
    )
    adapter.version, adapter.chunks = "v2", ["recovered"]
    assert run() == 1
    assert contents(path) == {"recovered"}
    with SourceLifecycleSnapshot(path) as snapshot:
        assert snapshot.pending(adapter.name, adapter.source_file) is None


def test_same_source_two_adapters_and_legacy_migration(setup_runner):
    adapter, path, run = setup_runner
    col = palace.get_collection(path)
    col.add(
        ids=["own-old", "peer-old", "unowned"],
        documents=["own obsolete tail", "peer legacy", "unattributed legacy"],
        metadatas=[
            {"adapter_name": adapter.name, "source_file": adapter.source_file},
            {"adapter_name": "peer", "source_file": adapter.source_file},
            {"source_file": adapter.source_file},
        ],
    )
    run()
    assert "own obsolete tail" not in contents(path)
    assert {"peer legacy", "unattributed legacy"} <= contents(path)
    peer = FixtureAdapter()
    peer.name, peer.chunks = "peer", ["peer new"]
    assert run_incremental_adapter(adapter=peer, source_path="/fixture", palace_path=path) == 1
    adapter.version, adapter.chunks = "__deleted__", []
    run()
    assert contents(path) == {"peer new", "unattributed legacy"}


@pytest.mark.parametrize(
    "action,match",
    [
        (
            lambda ctx: ctx.drawer_collection.upsert(ids=["forged"], documents=["bad"]),
            "direct drawer",
        ),
        (lambda ctx: ctx.drawer_collection.delete(where={"source_file": "s"}), "direct drawer"),
        (lambda ctx: ctx.knowledge_graph.add_triple("a", "b", "c"), "KG"),
        (lambda ctx: ctx.closet_collection.upsert(ids=["bad"]), "closet"),
        (lambda ctx: ctx.skip_current_item(), "verified current"),
        (lambda ctx: ctx._open_item(object(), None), "belongs to core"),
    ],
)
def test_direct_facade_writes_and_skip_misuse_poison_item(setup_runner, action, match):
    adapter, path, run = setup_runner
    run()
    old = current(path, adapter)
    adapter.version = "v2"

    def swallowed(ctx):
        try:
            action(ctx)
        except IncrementalProtocolError:
            pass

    adapter.action = swallowed
    with pytest.raises(IncrementalProtocolError, match=match):
        run()
    assert current(path, adapter) == old


@pytest.mark.parametrize(
    "metadata",
    [
        {"source_generation": "forged"},
        {"source_version": "forged"},
        {"source_deleted": False},
        {"adapter_name": "peer"},
        {"source_file": "other"},
        {"chunk_index": True},
        {"adapter_version": "forged"},
    ],
)
def test_forged_provenance_is_rejected(setup_runner, metadata):
    adapter, path, run = setup_runner
    adapter.action = lambda ctx: ctx.upsert_drawer(
        DrawerRecord("bad", adapter.source_file, metadata=metadata)
    )
    with pytest.raises(IncrementalProtocolError, match="provenance"):
        run()
    assert current(path, adapter) is None
    assert contents(path) == set()


def test_duplicate_chunk_index_across_direct_and_yielded_writes(setup_runner):
    adapter, path, run = setup_runner
    adapter.action = lambda ctx: ctx.upsert_drawer(DrawerRecord("direct", adapter.source_file, 0))
    with pytest.raises(IncrementalProtocolError, match="unique nonnegative"):
        run()
    assert contents(path) == set()


def test_source_inode_is_read_for_every_drawer(setup_runner, monkeypatch):
    import mempalace.sources.incremental as facade

    adapter, path, run = setup_runner
    identities = iter(["101", None, "303"])
    monkeypatch.setattr(facade, "source_directory_identity", lambda _source: next(identities))
    run()
    rows = palace.get_collection(path).get()
    by_index = {m["chunk_index"]: m for m in rows.metadatas}
    assert by_index[0]["source_dir_ino"] == "101"
    assert "source_dir_ino" not in by_index[1]
    assert by_index[2]["source_dir_ino"] == "303"


@pytest.mark.parametrize("exists", [False, True])
def test_incremental_dry_run_never_opens_storage(setup_runner, monkeypatch, exists):
    from pathlib import Path

    adapter, path, run = setup_runner
    if exists:
        run()
    before = {str(p): p.read_bytes() for p in Path(path).rglob("*") if p.is_file()}
    for target in ("get_collection", "mine_palace_lock"):
        monkeypatch.setattr(palace, target, lambda *_a, **_kw: pytest.fail("storage opened"))
    monkeypatch.setattr(
        SourceLifecycleStore, "__init__", lambda *_a: pytest.fail("registry opened")
    )
    assert run(dry_run=True) == 3  # no currentness reads, even for existing palaces
    after = {str(p): p.read_bytes() for p in Path(path).rglob("*") if p.is_file()}
    assert after == before
    assert Path(path).exists() == exists


def test_registry_absence_refused_before_backend_open(setup_runner, monkeypatch):
    adapter, path, run = setup_runner
    run()
    from pathlib import Path

    registry = lifecycle_path(path)
    registry.rename(Path(path) / "registry-preserved-for-test")
    monkeypatch.setattr(palace, "get_collection", lambda *_a: pytest.fail("backend opened"))
    with pytest.raises(LifecycleStateError, match="without their registry"):
        run()
    assert not registry.exists()


def test_reader_spans_runner_activation_and_applies_cleanup_backpressure(setup_runner):
    adapter, path, run = setup_runner
    run()
    old = current(path, adapter)
    with SourceLifecycleSnapshot(path) as reader:
        assert reader.visible(old.metadata)
        adapter.version, adapter.chunks = "v2", ["new"]
        assert run() == 1
        assert reader.visible(old.metadata)
        assert reader.current(adapter.name, adapter.source_file) == old
        assert contents(path) == {"new"}
        assert run() == 0  # unchanged currentness needs no physical reclamation
        adapter.version = "v3"
        with pytest.raises(LifecycleConflictError, match="readers still hold"):
            run()
        assert current(path, adapter).version == "v2"
    assert run() == 1


def test_second_process_writer_refused_before_handles(setup_runner, monkeypatch):
    adapter, path, run = setup_runner
    code = """
import sys
from mempalace.palace import mine_palace_lock
with mine_palace_lock(sys.argv[1]):
    print('ready', flush=True)
    sys.stdin.read()
"""
    holder = subprocess.Popen(
        [sys.executable, "-c", code, path],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "ready"
        monkeypatch.setattr(palace, "get_collection", lambda *_a: pytest.fail("backend opened"))
        with pytest.raises(palace.MineAlreadyRunning):
            run()
        assert adapter.context is None
        assert not lifecycle_path(path).exists()
    finally:
        holder.communicate(input="", timeout=10)
        assert holder.returncode == 0


@pytest.mark.parametrize("after_write", [False, True])
def test_backend_write_failure_even_when_caught_cannot_activate(
    setup_runner, monkeypatch, after_write
):
    adapter, path, run = setup_runner
    run()
    old = current(path, adapter)
    col = palace.get_collection(path)
    real_add = col.add

    def broken_add(**kwargs):
        if after_write:
            real_add(**kwargs)
        raise OSError("injected write failure")

    monkeypatch.setattr(col, "add", broken_add)
    monkeypatch.setattr(palace, "get_collection", lambda _path: col)
    adapter.version, adapter.chunks = "v2", []

    def caught(ctx):
        try:
            ctx.upsert_drawer(DrawerRecord("partial", adapter.source_file))
        except OSError:
            pass

    adapter.action = caught
    with pytest.raises(IncrementalProtocolError, match="write failed"):
        run()
    assert current(path, adapter) == old
    assert "partial" not in contents(path)


def test_exception_after_activation_commit_keeps_new_cursor(setup_runner, monkeypatch):
    adapter, path, run = setup_runner
    run()
    real_activate = SourceLifecycleStore.activate

    def committed_then_failed(self, generation):
        real_activate(self, generation)
        raise OSError("after commit")

    monkeypatch.setattr(SourceLifecycleStore, "activate", committed_then_failed)
    adapter.version, adapter.chunks = "v2", ["committed current"]
    with pytest.raises(OSError, match="after commit"):
        run()
    assert current(path, adapter).version == "v2"
    assert contents(path) == {"committed current"}
    monkeypatch.setattr(SourceLifecycleStore, "activate", real_activate)
    assert run() == 0


@pytest.mark.parametrize(
    "kind",
    ["no-item", "object", "wrong-source", "duplicate-item", "deleted-drawer", "skip-then-write"],
)
def test_item_protocol_rejects_invalid_output(setup_runner, kind):
    adapter, path, _run = setup_runner

    class Invalid(FixtureAdapter):
        def ingest(self, *, source, palace):
            if kind == "no-item":
                yield DrawerRecord("bad", self.source_file)
                return
            if kind == "object":
                yield object()
                return
            version = "__deleted__" if kind == "deleted-drawer" else self.version
            yield SourceItemMetadata(self.source_file, version)
            if kind == "wrong-source":
                yield DrawerRecord("bad", "other")
            elif kind == "duplicate-item":
                yield SourceItemMetadata(self.source_file, version)
            elif kind == "skip-then-write":
                # A plugin must not turn the documented observation flag into
                # permission to append to the already active generation.
                palace._skip_requested = False
                yield DrawerRecord("bad", self.source_file, 99)
            else:
                yield DrawerRecord("bad", self.source_file)

    if kind == "skip-then-write":
        _run()
    with pytest.raises(IncrementalProtocolError):
        run_incremental_adapter(adapter=Invalid(), source_path="/fixture", palace_path=path)
    assert "bad" not in contents(path)


@pytest.mark.parametrize("decision", [True, 1, None, "yes"])
def test_is_current_cannot_forge_absent_cursor(setup_runner, decision):
    adapter, path, run = setup_runner
    adapter.is_current = lambda **_kwargs: decision
    with pytest.raises(IncrementalProtocolError, match="generic cursor|boolean"):
        run()
    assert current(path, adapter) is None


@pytest.mark.parametrize(
    "capability", ["supports_kg_triples", "supports_media_assets", "media_references"]
)
def test_unsupported_capabilities_rejected_without_opens(setup_runner, monkeypatch, capability):
    adapter, path, run = setup_runner
    adapter.capabilities = adapter.capabilities | {capability}
    monkeypatch.setattr(palace, "mine_palace_lock", lambda *_a: pytest.fail("lock opened"))
    with pytest.raises(cli.UnsupportedSourceAdapterProtocolError, match="KG/media"):
        run()
    assert adapter.context is None
    assert not lifecycle_path(path).exists()


def test_nonlocal_backend_rejected_before_writes(setup_runner, monkeypatch):
    adapter, path, run = setup_runner
    monkeypatch.setenv("MEMPALACE_BACKEND", "qdrant")
    monkeypatch.setattr(palace, "get_collection", lambda *_a: pytest.fail("backend opened"))
    with pytest.raises(cli.UnsupportedSourceAdapterProtocolError, match="local backend"):
        run()
    assert adapter.context is None
    assert not lifecycle_path(path).exists()


def test_nested_incremental_invocation_in_same_process_refused(setup_runner):
    adapter, path, run = setup_runner

    def nested(_ctx):
        with pytest.raises(palace.MineAlreadyRunning, match="invocation already owns"):
            run_incremental_adapter(
                adapter=FixtureAdapter(), source_path="/fixture", palace_path=path
            )

    adapter.action = nested
    assert run() == 3


def test_restart_process_reads_only_current_rows(setup_runner):
    adapter, path, run = setup_runner
    run()
    adapter.version, adapter.chunks = "v2", ["only visible row"]
    run()
    code = """
import sys
from pathlib import Path
from mempalace.sources.lifecycle import SourceLifecycleSnapshot
from mempalace.sources.read_visibility import open_logical_reader
path = Path(sys.argv[1])
with SourceLifecycleSnapshot(str(path)) as s:
    assert s.current(sys.argv[2], sys.argv[3]).version == 'v2'
exact = (path / 'sqlite_exact.sqlite3').exists()
with open_logical_reader(path / ('sqlite_exact.sqlite3' if exact else 'chroma.sqlite3'),
                         backend='exact' if exact else 'chroma') as r:
    assert r.execute('SELECT COUNT(*) FROM ' + ('documents' if exact else 'embeddings')).fetchone()[0] == 1
"""
    subprocess.run(
        [sys.executable, "-c", code, path, adapter.name, adapter.source_file],
        check=True,
        timeout=15,
    )


@pytest.mark.parametrize("mode", ["default", "explicit", "historical"])
def test_palace_path_independent_of_kg_resolver(setup_runner, monkeypatch, tmp_path, mode):
    from mempalace import paths
    from mempalace.config import MempalaceConfig

    adapter, _path, _run = setup_runner
    path = tmp_path / (".mempalace/chroma" if mode == "historical" else mode + "-palace")
    monkeypatch.setenv("MEMPALACE_PALACE_PATH", str(path))
    monkeypatch.setattr(
        paths, "knowledge_graph_path", lambda *_a, **_kw: pytest.fail("KG resolver called")
    )
    resolved = MempalaceConfig().palace_path if mode == "default" else str(path)
    assert (
        cli.mine_source_adapter(
            source_name=adapter.name,
            source_path="/fixture",
            palace_path=resolved,
            explicit_palace=mode == "explicit",
        )
        == 3
    )
    assert lifecycle_path(resolved).parent == path.resolve()
    assert contents(resolved) == set(adapter.chunks)


def test_skip_after_a_direct_write_is_rejected(setup_runner):
    adapter, path, run = setup_runner

    def action(ctx):
        ctx.upsert_drawer(DrawerRecord("unpublished", adapter.source_file, 99))
        ctx.skip_current_item()

    adapter.action = action
    with pytest.raises(IncrementalProtocolError, match="verified current"):
        run()
    assert contents(path) == set()


def test_undeclared_tombstone_and_media_output_are_rejected(setup_runner, tmp_path):
    from mempalace.media import MediaAsset

    adapter, path, run = setup_runner
    adapter.capabilities = frozenset({"supports_incremental"})
    adapter.version, adapter.chunks = "__deleted__", []
    with pytest.raises(IncrementalProtocolError, match="tombstone capability"):
        run()

    class UndeclaredMedia(FixtureAdapter):
        def ingest(self, *, source, palace):
            yield SourceItemMetadata(self.source_file, "v1")
            yield DrawerRecord("not published", self.source_file)
            yield MediaAsset(
                asset_id="fixture",
                source_id="fixture",
                source_file=str(tmp_path / "fake.png"),
                media_type="image",
                mime_type="image/png",
                title="synthetic",
                project="synthetic",
                inferred_wing="synthetic",
                file_size_bytes=0,
                file_mtime_ns=0,
                file_modified_at="2026-01-01T00:00:00+00:00",
            )

    with pytest.raises(
        IncrementalProtocolError, match="unsupported incremental result type MediaAsset"
    ):
        run_incremental_adapter(adapter=UndeclaredMedia(), source_path="/fixture", palace_path=path)
    assert contents(path) == set()


def test_legacy_backend_reader_spans_first_managed_activation(setup_runner):
    from pathlib import Path

    from mempalace.sources.read_visibility import open_logical_reader

    adapter, path, run = setup_runner
    SourceLifecycleStore(path)
    col = palace.get_collection(path)
    col.add(
        ids=["legacy-own"],
        documents=["old"],
        metadatas=[{"adapter_name": adapter.name, "source_file": adapter.source_file}],
    )
    exact = (Path(path) / "sqlite_exact.sqlite3").exists()
    filename, kind, table = (
        ("sqlite_exact.sqlite3", "exact", "documents")
        if exact
        else ("chroma.sqlite3", "chroma", "embeddings")
    )
    with open_logical_reader(Path(path) / filename, backend=kind) as reader:
        assert reader.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 1
        assert run() == 3
        assert reader.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 1
        assert contents(path) == set(adapter.chunks)
    # The legacy row is hidden, never physically deleted during migration.
    assert col.physical_count() == 4


def test_rust_exact_incremental_lifecycle_uses_logical_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMPALACE_BACKEND", "rust_exact")
    path = str(tmp_path / "rust-palace")
    adapter = FixtureAdapter()

    def run():
        return run_incremental_adapter(adapter=adapter, source_path="/fixture", palace_path=path)

    assert run() == 3
    assert run() == 0
    adapter.version, adapter.chunks = "v2", ["shortened"]
    assert run() == 1
    assert contents(path) == {"shortened"}
    assert palace.get_collection(path).query(query_texts=["shortened"]).documents == [["shortened"]]
