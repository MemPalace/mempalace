"""The registry boundary is independent of Chroma, KG location and adapter cursors."""

import contextlib
import dataclasses
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from mempalace import paths
from mempalace.sources.lifecycle import (
    LifecycleConflictError,
    LifecycleStateError,
    SourceLifecycleSnapshot,
    SourceLifecycleStore,
    lifecycle_path,
)


def test_legacy_read_and_missing_registry_create_nothing(tmp_path):
    palace = tmp_path / "absent"
    with SourceLifecycleSnapshot(str(palace)) as reader:
        assert reader.visible(None)
        assert reader.visible({"adapter_name": "old", "source_file": "same"})
        with pytest.raises(LifecycleStateError, match="restore the matching registry"):
            reader.visible(
                {
                    "adapter_name": "a",
                    "source_file": "s",
                    "source_generation": "g",
                    "source_version": "v",
                }
            )
    assert not palace.exists()


@pytest.mark.parametrize("generation", [None, "", 0, [], {}])
def test_invalid_generation_is_not_legacy(tmp_path, generation):
    with SourceLifecycleSnapshot(str(tmp_path)) as reader:
        with pytest.raises(LifecycleStateError, match="invalid ownership"):
            reader.visible({"source_generation": generation})
    assert list(tmp_path.iterdir()) == []


def test_unknown_generation_fails_closed(tmp_path):
    SourceLifecycleStore(str(tmp_path))
    with SourceLifecycleSnapshot(str(tmp_path)) as reader:
        with pytest.raises(LifecycleStateError, match="no activation state"):
            reader.visible(
                {
                    "adapter_name": "a",
                    "source_file": "s",
                    "source_generation": "g",
                    "source_version": "v",
                }
            )


def test_staging_replacement_abandon_and_restart(tmp_path):
    store = SourceLifecycleStore(str(tmp_path))
    first = store.begin("a", "s", "v1")
    with store.snapshot() as reader:
        assert reader.current("a", "s") is None
        assert not reader.visible(first.metadata)
    store.activate(first)
    second = store.begin("a", "s", "v2")
    with store.snapshot() as reader:
        assert reader.current("a", "s") == first
        assert reader.visible(first.metadata)
        assert not reader.visible(second.metadata)
    store.abandon(second)
    # A new instance does not reactivate failed extraction or change ownership.
    store = SourceLifecycleStore(str(tmp_path))
    store.reclaim(_EmptyPhysicalCollection(), "a", "s")
    third = store.begin("a", "s", "v3")
    store.activate(third)
    with store.snapshot() as reader:
        assert reader.current("a", "s") == third
        assert not reader.visible(first.metadata)
        with pytest.raises(LifecycleStateError):
            reader.visible(second.metadata)
        assert reader.visible(third.metadata)


def test_same_source_two_adapters_and_legacy_remain_independent(tmp_path):
    store = SourceLifecycleStore(str(tmp_path))
    left = store.begin("a", "shared", "left")
    right = store.begin("b", "shared", "right")
    store.activate(left)
    store.activate(right)
    deleted = store.begin("a", "shared", "__deleted__", deleted=True)
    store.activate(deleted)
    with store.snapshot() as reader:
        assert not reader.visible(left.metadata)
        assert not reader.visible(deleted.metadata)
        assert reader.visible(right.metadata)
        assert reader.visible({"source_file": "shared"})
        assert reader.current("b", "shared") == right


def test_empty_delete_recreate_uses_one_generic_cursor(tmp_path):
    store = SourceLifecycleStore(str(tmp_path))
    empty = store.begin("a", "s", "empty-v1")
    store.activate(empty)  # No drawer is required to retain the version.
    with store.snapshot() as reader:
        cursor = reader.current("a", "s").cursor
        assert cursor["source_version"] == "empty-v1"
        assert cursor["source_deleted"] is False
        assert "openclaw_session_version" not in cursor
        assert cursor["source_version"] != "nonempty-v2"
    nonempty = store.begin("a", "s", "nonempty-v2")
    store.activate(nonempty)
    deleted = store.begin("a", "s", "__deleted__", deleted=True)
    store.reclaim(_EmptyPhysicalCollection(), "a", "s")
    store.activate(deleted)
    with store.snapshot() as reader:
        assert reader.current("a", "s").cursor["source_deleted"] is True
    recreated = store.begin("a", "s", "nonempty-v2")
    store.reclaim(_EmptyPhysicalCollection(), "a", "s")
    store.activate(recreated)
    with store.snapshot() as reader:
        assert reader.current("a", "s").cursor["source_deleted"] is False
        assert reader.visible(recreated.metadata)
        with pytest.raises(LifecycleStateError):
            reader.visible(nonempty.metadata)


def test_reader_spans_atomic_activation_without_changing_snapshot(tmp_path):
    store = SourceLifecycleStore(str(tmp_path))
    first = store.begin("a", "s", "v1")
    store.activate(first)
    second = store.begin("a", "s", "v2")
    with store.snapshot() as old_reader:
        assert old_reader.current("a", "s") == first
        # Activation genuinely executes on another thread/connection while the
        # read transaction is held, rather than testing a cached Python bool.
        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(store.activate, second).result(timeout=5)
        assert old_reader.current("a", "s") == first
        assert old_reader.visible(first.metadata)
        assert not old_reader.visible(second.metadata)
        with store.snapshot() as new_reader:
            assert new_reader.current("a", "s") == second
            assert not new_reader.visible(first.metadata)
            assert new_reader.visible(second.metadata)


def test_concurrent_staging_refused_without_overwriting_pending(tmp_path):
    store = SourceLifecycleStore(str(tmp_path))
    pending = store.begin("a", "s", "v1")
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(store.begin, "a", "s", "v2")
        with pytest.raises(LifecycleConflictError, match="already has a staged"):
            future.result(timeout=5)
    store.activate(pending)
    with store.snapshot() as reader:
        assert reader.current("a", "s") == pending


def test_activation_failure_rolls_back_retirement(tmp_path):
    store = SourceLifecycleStore(str(tmp_path))
    first = store.begin("a", "s", "v1")
    store.activate(first)
    second = store.begin("a", "s", "v2")
    # Inject a storage failure AFTER retirement, at the activation SQL statement.
    with contextlib.closing(sqlite3.connect(store.path)) as conn, conn:
        conn.execute(
            "CREATE TRIGGER fail_activation BEFORE UPDATE ON source_generations "
            "WHEN NEW.state='active' BEGIN SELECT RAISE(ABORT, 'activation failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="activation failure"):
        store.activate(second)
    with store.snapshot() as reader:
        assert reader.current("a", "s") == first
        assert reader.visible(first.metadata)
        assert not reader.visible(second.metadata)
    store.abandon(second)


@pytest.mark.parametrize("operation", ["activate", "abandon"])
@pytest.mark.parametrize(
    "field,value",
    [
        ("adapter_name", "other"),
        ("source_file", "other"),
        ("version", "forged"),
        ("previous", "forged"),
        ("deleted", True),
    ],
)
def test_forged_generation_cannot_transition(tmp_path, operation, field, value):
    store = SourceLifecycleStore(str(tmp_path))
    pending = store.begin("a", "s", "v1")
    forged = dataclasses.replace(pending, **{field: value})
    with pytest.raises(LifecycleConflictError):
        getattr(store, operation)(forged)
    store.activate(pending)
    with store.snapshot() as reader:
        assert reader.current("a", "s") == pending


def test_cannot_abandon_or_activate_active_generation(tmp_path):
    store = SourceLifecycleStore(str(tmp_path))
    generation = store.begin("a", "s", "v1")
    store.activate(generation)
    for method in (store.activate, store.abandon):
        with pytest.raises(LifecycleConflictError):
            method(generation)
    with store.snapshot() as reader:
        assert reader.visible(generation.metadata)


def test_disappeared_registry_never_recreated_by_reader_or_transition(tmp_path):
    store = SourceLifecycleStore(str(tmp_path))
    pending = store.begin("a", "s", "v1")
    reader = store.snapshot()  # Lazy, deliberately before unlink.
    store.path.unlink()
    with reader, pytest.raises(LifecycleStateError, match="missing or invalid"):
        reader.visible(pending.metadata)
    with pytest.raises(sqlite3.OperationalError):
        store.activate(pending)
    assert not store.path.exists()


@pytest.mark.parametrize("invalid", ["bytes", "schema", "tables"])
def test_invalid_registry_reports_diagnostic_without_repair(tmp_path, invalid):
    path = lifecycle_path(str(tmp_path))
    if invalid == "bytes":
        path.write_bytes(b"not a sqlite database")
    else:
        with contextlib.closing(sqlite3.connect(path)) as conn, conn:
            conn.execute(f"PRAGMA user_version={99 if invalid == 'schema' else 1}")
    before = path.read_bytes()
    with pytest.raises(LifecycleStateError, match="registry"):
        with SourceLifecycleSnapshot(str(tmp_path)) as reader:
            reader.visible(
                {
                    "adapter_name": "a",
                    "source_file": "s",
                    "source_generation": "g",
                    "source_version": "v",
                }
            )
    assert path.read_bytes() == before


def test_registry_location_does_not_follow_kg_facts(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    palace = paths.default_palace_path()
    legacy = paths.legacy_knowledge_graph_path()
    from mempalace.knowledge_graph import KnowledgeGraph

    with contextlib.closing(KnowledgeGraph(db_path=legacy)) as kg:
        kg.add_triple("legacy", "has", "facts")
    expected = lifecycle_path(palace)
    assert paths.knowledge_graph_path(palace) == legacy
    with contextlib.closing(
        KnowledgeGraph(db_path=str(expected.parent / paths.KNOWLEDGE_GRAPH_FILENAME))
    ) as kg:
        kg.add_triple("local", "has", "facts")
    assert paths.knowledge_graph_path(palace) != legacy
    assert lifecycle_path(palace) == expected
    assert lifecycle_path(str(expected.parent / ".")) == expected
    assert expected.name == "source_lifecycle.sqlite3"
    assert not expected.exists()


def test_closed_reader_cannot_be_reused(tmp_path):
    reader = SourceLifecycleSnapshot(str(tmp_path))
    reader.close()
    reader.close()
    with pytest.raises(LifecycleStateError, match="closed"):
        reader.visible({})


def test_write_open_does_not_reinitialize_foreign_database(tmp_path):
    path = lifecycle_path(str(tmp_path))
    with contextlib.closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("CREATE TABLE unrelated(value)")
    with pytest.raises(LifecycleStateError, match="unrecognized"):
        SourceLifecycleStore(str(tmp_path))
    with contextlib.closing(sqlite3.connect(path)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
        assert conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == [
            ("unrelated",)
        ]


def test_crash_orphan_can_be_recovered_without_losing_active_cursor(tmp_path):
    store = SourceLifecycleStore(str(tmp_path))
    good = store.begin("a", "s", "v1")
    store.activate(good)
    orphan = store.begin("a", "s", "v2")
    # Lose both writer-side handles, as in a process exit after partial writes.
    del store, orphan
    reopened = SourceLifecycleStore(str(tmp_path))
    with reopened.snapshot() as reader:
        recovered = reader.pending("a", "s")
        assert recovered.version == "v2"
        assert reader.pending("other", "s") is None
        assert not reader.visible(recovered.metadata)
        assert reader.current("a", "s") == good
    reopened.abandon(recovered)
    reopened.reclaim(_EmptyPhysicalCollection(), "a", "s")
    replacement = reopened.begin("a", "s", "v3")
    reopened.activate(replacement)
    with reopened.snapshot() as reader:
        assert reader.pending("a", "s") is None
        assert reader.current("a", "s") == replacement


def test_corrupt_duplicate_active_state_fails_closed(tmp_path):
    store = SourceLifecycleStore(str(tmp_path))
    good = store.begin("a", "s", "v1")
    store.activate(good)
    store.begin("a", "s", "v2")
    with contextlib.closing(sqlite3.connect(store.path)) as conn, conn:
        conn.execute("DROP INDEX one_active_source_generation")
        conn.execute("UPDATE source_generations SET state='active'")
    with pytest.raises(LifecycleStateError, match="registry"):
        with store.snapshot() as reader:
            reader.visible(good.metadata)


@pytest.mark.parametrize(
    "adapter,source,version,deleted",
    [
        ("", "s", "v", False),
        (None, "s", "v", False),
        ("a", "", "v", False),
        ("a", None, "v", False),
        ("a", "s", None, False),
        ("a", "s", "v", "yes"),
    ],
)
def test_invalid_begin_is_rejected_without_a_staged_row(
    tmp_path, adapter, source, version, deleted
):
    store = SourceLifecycleStore(str(tmp_path))
    with pytest.raises(ValueError):
        store.begin(adapter, source, version, deleted=deleted)
    with store.snapshot() as reader:
        assert reader.pending("a", "s") is None


def test_failed_abandon_leaves_last_good_generation_visible(tmp_path):
    store = SourceLifecycleStore(str(tmp_path))
    good = store.begin("a", "s", "v1")
    store.activate(good)
    failed = store.begin("a", "s", "v2")
    with contextlib.closing(sqlite3.connect(store.path)) as conn, conn:
        conn.execute(
            "CREATE TRIGGER fail_abandon BEFORE UPDATE ON source_generations "
            "WHEN NEW.state='abandoned' BEGIN SELECT RAISE(ABORT, 'abandon failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="abandon failure"):
        store.abandon(failed)
    with store.snapshot() as reader:
        assert reader.current("a", "s") == good
        assert reader.visible(good.metadata)
        assert not reader.visible(failed.metadata)


def test_obsolete_activation_cursor_is_refused(tmp_path):
    store = SourceLifecycleStore(str(tmp_path))
    good = store.begin("a", "s", "v1")
    store.activate(good)
    pending = store.begin("a", "s", "v2")
    # Simulate state replacement by an unsupported restore while extracting.
    with contextlib.closing(sqlite3.connect(store.path)) as conn, conn:
        conn.execute("UPDATE source_generations SET state='retired' WHERE state='active'")
    with pytest.raises(LifecycleConflictError, match="changed during extraction"):
        store.activate(pending)
    with store.snapshot() as reader:
        assert reader.current("a", "s") is None
        assert reader.pending("a", "s") == pending
        assert not reader.visible(pending.metadata)


def test_corrupt_cursor_fails_closed(tmp_path):
    store = SourceLifecycleStore(str(tmp_path))
    generation = store.begin("a", "s", "v1")
    store.activate(generation)
    with contextlib.closing(sqlite3.connect(store.path)) as conn, conn:
        conn.execute("UPDATE source_generations SET previous=''")
    with store.snapshot() as reader:
        with pytest.raises(LifecycleStateError, match="invalid source generation cursor"):
            reader.current("a", "s")
        with pytest.raises(LifecycleStateError, match="invalid source generation cursor"):
            reader.visible(generation.metadata)


def test_unknown_registry_version_refused_by_writer(tmp_path):
    store = SourceLifecycleStore(str(tmp_path))
    with contextlib.closing(sqlite3.connect(store.path)) as conn, conn:
        conn.execute("PRAGMA user_version=99")
    with pytest.raises(LifecycleStateError, match="unsupported"):
        SourceLifecycleStore(str(tmp_path))


def test_predicate_cache_is_bounded_without_losing_snapshot_semantics(tmp_path, monkeypatch):
    from mempalace.sources import lifecycle

    monkeypatch.setattr(lifecycle, "_STATE_CACHE_LIMIT", 2)
    store = SourceLifecycleStore(str(tmp_path))
    generations = [store.begin("a", str(i), "v1") for i in range(3)]
    for generation in generations:
        store.activate(generation)
    with store.snapshot() as reader:
        for generation in generations:
            assert reader.visible(generation.metadata)
        assert len(reader._states) == 2
        assert reader.visible(generations[0].metadata)
        assert len(reader._states) == 2


def test_cached_predicate_queries_once_per_generation_not_per_chunk(tmp_path):
    store = SourceLifecycleStore(str(tmp_path))
    record = store.begin("a", "s", "v")
    store.activate(record)
    with store.snapshot() as reader:
        assert reader.current("a", "s") == record  # Open the snapshot before tracing.
        statements = []
        reader._conn.set_trace_callback(statements.append)
        for _ in range(1000):
            assert reader.visible(record.metadata)
        assert len(statements) == 2  # State and unambiguous-active checks, both indexed.


class _EmptyPhysicalCollection:
    # Registry-only tests have no drawers. Reclamation still exercises real SQL.
    def delete(self, **kwargs):
        pass


@pytest.mark.parametrize(
    "key", ["adapter_name", "source_file", "source_generation", "source_version"]
)
def test_missing_managed_stamp_fails_closed(tmp_path, key):
    store = SourceLifecycleStore(str(tmp_path))
    record = store.begin("a", "s", "v")
    store.activate(record)
    meta = record.metadata
    del meta[key]
    with store.snapshot() as reader, pytest.raises(LifecycleStateError):
        reader.visible(meta)


@pytest.mark.parametrize(
    "key", ["adapter_name", "source_file", "source_generation", "source_version"]
)
@pytest.mark.parametrize("value", [None, 42, True, [], {}, "forged"])
def test_invalid_or_copied_managed_stamp(tmp_path, key, value):
    store = SourceLifecycleStore(str(tmp_path))
    record = store.begin("a", "s", "v")
    store.activate(record)
    with store.snapshot() as reader, pytest.raises(LifecycleStateError):
        reader.visible({**record.metadata, key: value})


@pytest.mark.parametrize("index", ["one_active_source_generation", "one_staging_source_generation"])
@pytest.mark.parametrize("operation", ["reopen", "begin", "snapshot"])
def test_missing_unique_index_before_or_between_operations(tmp_path, index, operation):
    store = SourceLifecycleStore(str(tmp_path))
    with contextlib.closing(sqlite3.connect(store.path)) as conn, conn:
        conn.execute(f"DROP INDEX {index}")
    with pytest.raises(LifecycleStateError):
        if operation == "reopen":
            SourceLifecycleStore(str(tmp_path))
        elif operation == "begin":
            store.begin("a", "s", "v")
        else:
            with store.snapshot():
                pass


def test_changed_partial_index_predicate_is_rejected(tmp_path):
    store = SourceLifecycleStore(str(tmp_path))
    with contextlib.closing(sqlite3.connect(store.path)) as conn, conn:
        conn.execute("DROP INDEX one_staging_source_generation")
        conn.execute(
            "CREATE UNIQUE INDEX one_staging_source_generation "
            "ON source_generations(adapter, source) WHERE state='retired'"
        )
    with pytest.raises(LifecycleStateError):
        store.begin("a", "s", "v")


@pytest.mark.parametrize(
    "ddl",
    [
        "DROP TABLE source_generations",
        "ALTER TABLE source_generations ADD COLUMN unexpected TEXT",
        "DROP TABLE source_lifecycle_state",
        "DELETE FROM source_lifecycle_state",
    ],
)
def test_schema_structure_damage_after_store_open_fails_closed(tmp_path, ddl):
    store = SourceLifecycleStore(str(tmp_path))
    with contextlib.closing(sqlite3.connect(store.path)) as conn, conn:
        conn.execute(ddl)
    with pytest.raises((LifecycleStateError, sqlite3.Error)):
        store.begin("a", "s", "v")


def test_current_schema_version_without_tables_is_not_accepted(tmp_path):
    from mempalace.sources.lifecycle import _SCHEMA_VERSION

    with contextlib.closing(sqlite3.connect(lifecycle_path(str(tmp_path)))) as conn, conn:
        conn.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
    with pytest.raises(LifecycleStateError):
        SourceLifecycleStore(str(tmp_path))
