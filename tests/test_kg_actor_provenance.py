"""Actor labels survive KG lifecycle writes, reads, retries and migration."""

import json
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from mempalace.knowledge_graph import KnowledgeGraph


CREATOR = "mac:codex:memory"
EDITOR = "windows:claude:memory"
BOUNDARY = "2026-10-01T12:00:00Z"


def test_legacy_migration_retains_original_fields_and_unknown_actors(tmp_path):
    path = str(tmp_path / "legacy.sqlite3")
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE entities (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, type TEXT,
                properties TEXT, created_at TEXT
            );
            CREATE TABLE triples (
                id TEXT PRIMARY KEY, subject TEXT, predicate TEXT, object TEXT,
                valid_from TEXT, valid_to TEXT, confidence REAL,
                source_closet TEXT, source_file TEXT, extracted_at TEXT
            );
            INSERT INTO entities VALUES ('alice', 'Alice', 'person', '{}', '2001-01-01');
            INSERT INTO entities VALUES ('café', 'Café', 'place', '{}', '2001-01-01');
            INSERT INTO triples VALUES (
                'old', 'alice', 'visits', 'café', '2020-01-01', '2021-01-01',
                0.8, 'original closet', 'anotações.md', '2022-01-01'
            );
        """)
        conn.row_factory = sqlite3.Row
        before = dict(conn.execute("SELECT * FROM triples").fetchone())

    for _ in range(2):
        with KnowledgeGraph(path) as graph:
            row = dict(graph._conn().execute("SELECT * FROM triples").fetchone())
            assert {key: row[key] for key in before} == before
            assert row["created_by"] is row["ended_by"] is None
            assert graph.timeline()[0]["created_by"] is None
            assert graph.timeline()[0]["ended_by"] is None


def _legacy_actor_database(path):
    """Current pre-actor schema, including already-migrated source pointers."""
    with sqlite3.connect(path) as connection:
        connection.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE entities (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, type TEXT,
                properties TEXT, created_at TEXT
            );
            CREATE TABLE triples (
                id TEXT PRIMARY KEY, subject TEXT, predicate TEXT, object TEXT,
                valid_from TEXT, valid_to TEXT, confidence REAL,
                source_closet TEXT, source_file TEXT, source_drawer_id TEXT,
                adapter_name TEXT, extracted_at TEXT
            );
            INSERT INTO entities VALUES ('alice', 'Alice', 'person', '{}', '2001-01-01');
            INSERT INTO entities VALUES ('café', 'Café', 'place', '{}', '2001-01-01');
            INSERT INTO triples VALUES (
                'old', 'alice', 'visits', 'café', '2020-01-01', '2021-01-01',
                0.8, 'original closet', 'anotações.md', 'original drawer', 'original adapter',
                '2022-01-01'
            );
        """)
        connection.row_factory = sqlite3.Row
        return dict(connection.execute("SELECT * FROM triples").fetchone())


def test_two_real_constructors_serialize_legacy_actor_migration(tmp_path, monkeypatch):
    path = str(tmp_path / "concurrent.sqlite3")
    before = _legacy_actor_database(path)
    start_migration = threading.Barrier(2)
    original_connect = sqlite3.connect
    original_migrate = KnowledgeGraph._migrate_schema

    class SlowActorAlter:
        def __init__(self, connection):
            self.connection = connection

        @property
        def row_factory(self):
            return self.connection.row_factory

        @row_factory.setter
        def row_factory(self, value):
            self.connection.row_factory = value

        def execute(self, sql, *arguments):
            if sql.startswith("ALTER TABLE triples ADD COLUMN created_by"):
                # Let the other real connection reach schema inspection before
                # the first ALTER on the old implementation. A SQLite write
                # reservation must instead keep that reader outside migration.
                time.sleep(0.1)
            return self.connection.execute(sql, *arguments)

        def __getattr__(self, name):
            return getattr(self.connection, name)

    def migrate_together(self, connection):
        start_migration.wait(timeout=5)
        original_migrate(self, connection)

    monkeypatch.setattr(
        sqlite3, "connect", lambda *a, **k: SlowActorAlter(original_connect(*a, **k))
    )
    monkeypatch.setattr(KnowledgeGraph, "_migrate_schema", migrate_together)

    def initialize(_):
        with KnowledgeGraph(path) as graph:
            row = dict(graph._conn().execute("SELECT * FROM triples").fetchone())
            return row

    with ThreadPoolExecutor(max_workers=2) as pool:
        rows = list(pool.map(initialize, range(2)))
    for row in rows:
        assert {key: row[key] for key in before} == before
        assert row["created_by"] is row["ended_by"] is None


@pytest.mark.parametrize("fail_after_first_column", [False, True])
def test_actor_migration_does_not_commit_caller_owned_transaction(
    tmp_path, fail_after_first_column
):
    path = str(tmp_path / "outer.sqlite3")
    before = _legacy_actor_database(path)

    class Connection(sqlite3.Connection):
        def execute(self, sql, *arguments):
            if fail_after_first_column and sql.startswith(
                "ALTER TABLE triples ADD COLUMN ended_by"
            ):
                raise sqlite3.OperationalError("simulated migration failure")
            return super().execute(sql, *arguments)

    connection = sqlite3.connect(path, factory=Connection)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("BEGIN")
        connection.execute("UPDATE triples SET source_file='pending caller edit' WHERE id='old'")
        if fail_after_first_column:
            with pytest.raises(sqlite3.OperationalError, match="simulated migration failure"):
                KnowledgeGraph._migrate_schema(None, connection)
            columns = {r["name"] for r in connection.execute("PRAGMA table_info(triples)")}
            assert "created_by" not in columns and "ended_by" not in columns
        else:
            KnowledgeGraph._migrate_schema(None, connection)
        assert connection.in_transaction
        assert (
            connection.execute("SELECT source_file FROM triples").fetchone()[0]
            == "pending caller edit"
        )
        connection.rollback()
        assert dict(connection.execute("SELECT * FROM triples").fetchone()) == before
        columns = {r["name"] for r in connection.execute("PRAGMA table_info(triples)")}
        assert "created_by" not in columns and "ended_by" not in columns
    finally:
        connection.close()


def test_upgraded_wal_graph_can_open_and_read_beside_real_writer(tmp_path, monkeypatch):
    path = str(tmp_path / "upgraded.sqlite3")
    with KnowledgeGraph(path) as graph:
        triple = graph.add_triple("Alice", "works_at", "A", created_by=CREATOR)
    original_connect = sqlite3.connect
    writer = original_connect(path)
    try:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("UPDATE triples SET created_by='pending writer' WHERE id=?", (triple,))

        def connect(*args, **kwargs):
            kwargs["timeout"] = 0.15
            return original_connect(*args, **kwargs)

        monkeypatch.setattr(sqlite3, "connect", connect)
        with KnowledgeGraph(path) as reader:
            assert reader._conn().execute("PRAGMA journal_mode").fetchone()[0] == "wal"
            assert reader.timeline()[0]["created_by"] == CREATOR
            assert reader.query_entity("Alice")[0]["created_by"] == CREATOR
            assert not reader._conn().in_transaction
        assert writer.in_transaction
    finally:
        writer.rollback()
        writer.close()


def test_failed_constructor_migration_rolls_back_columns_and_can_retry(tmp_path, monkeypatch):
    path = str(tmp_path / "retry.sqlite3")
    before = _legacy_actor_database(path)
    original_connect = sqlite3.connect
    connections = []

    class FailingMigration(sqlite3.Connection):
        def execute(self, sql, *arguments):
            if sql.startswith("ALTER TABLE triples ADD COLUMN ended_by"):
                raise sqlite3.OperationalError("simulated migration failure")
            return super().execute(sql, *arguments)

    def connect(*arguments, **kwargs):
        connection = original_connect(*arguments, factory=FailingMigration, **kwargs)
        connections.append(connection)
        return connection

    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", connect)
        with pytest.raises(sqlite3.OperationalError, match="simulated migration failure"):
            KnowledgeGraph(path)
        for connection in connections:
            assert not connection.in_transaction
            assert dict(connection.execute("SELECT * FROM triples").fetchone()) == before
            connection.close()
    with KnowledgeGraph(path) as graph:
        row = dict(graph._conn().execute("SELECT * FROM triples").fetchone())
        assert {key: row[key] for key in before} == before
        assert row["created_by"] is row["ended_by"] is None


def test_creation_end_and_supersession_round_trip_all_reads(kg):
    old = kg.add_triple(
        "Dr Alice", "visits", "Café Central", valid_from="2026-01-01", created_by=CREATOR
    )
    new = kg.supersede("Dr Alice", "visits", "Library", "Park", at=BOUNDARY, by=EDITOR)
    # The absent old value degrades to add, with the caller as creator.
    assert (
        kg._conn().execute("SELECT created_by FROM triples WHERE id=?", (new,)).fetchone()[0]
        == EDITOR
    )
    kg.invalidate("Dr Alice", "visits", "Café Central", ended=BOUNDARY, ended_by=EDITOR)
    row = kg._conn().execute("SELECT * FROM triples WHERE id=?", (old,)).fetchone()
    assert (row["created_by"], row["ended_by"]) == (CREATOR, EDITOR)

    views = [
        kg.query_entity("Dr Alice", direction="outgoing"),
        kg.query_entity("Café Central", direction="incoming"),
        kg.query_entity("Alice", direction="outgoing"),
        kg.query_entity("Café", direction="incoming"),
        kg.query_entity("Dr Alice", as_of="2026-09-01", direction="both"),
        kg.query_relationship("visits", as_of="2026-09-01"),
        kg.timeline("Dr Alice"),
        kg.timeline(),
    ]
    for view in views:
        facts = [fact for fact in view if fact["object"] == "Café Central"]
        assert len(facts) == 1
        assert (facts[0]["created_by"], facts[0]["ended_by"]) == (CREATOR, EDITOR)


def test_supersede_attribution_and_boundary_are_atomic(kg):
    kg.add_triple("Alice", "works_at", "A", valid_from="2026-01-01", created_by=CREATOR)
    kg.supersede("Alice", "works_at", "A", "B", at=BOUNDARY, by=EDITOR)
    before = kg.query_entity("Alice", as_of="2026-09-01")
    after = kg.query_entity("Alice", as_of=BOUNDARY)
    assert [(f["object"], f["created_by"], f["ended_by"]) for f in before] == [
        ("A", CREATOR, EDITOR)
    ]
    assert [(f["object"], f["created_by"], f["ended_by"]) for f in after] == [("B", EDITOR, None)]


def test_duplicate_assertion_and_transition_retries_do_not_reassign_authors(kg):
    old = kg.add_triple("Alice", "works_at", "A", created_by=CREATOR)
    assert kg.add_triple("Alice", "works_at", "A", created_by=EDITOR) == old
    new = kg.supersede("Alice", "works_at", "A", "B", at=BOUNDARY, by=EDITOR)
    assert kg.supersede("Alice", "works_at", "A", "B", at=BOUNDARY, by="retry") == new
    kg.invalidate("Alice", "works_at", "A", ended="2026-10-02", ended_by="retry")
    facts = {fact["object"]: fact for fact in kg.timeline()}
    assert (facts["A"]["created_by"], facts["A"]["ended_by"]) == (CREATOR, EDITOR)
    assert facts["A"]["valid_to"] == BOUNDARY
    assert facts["B"]["created_by"] == EDITOR


def test_existing_successor_keeps_its_original_assertor(kg):
    kg.add_triple("Alice", "works_at", "A", created_by=CREATOR)
    successor = kg.add_triple("Alice", "works_at", "B", created_by="third-party")
    assert kg.supersede("Alice", "works_at", "A", "B", at=BOUNDARY, by=EDITOR) == successor
    facts = {fact["object"]: fact for fact in kg.timeline()}
    assert facts["A"]["ended_by"] == EDITOR
    assert facts["B"]["created_by"] == "third-party"


def test_distinct_validity_intervals_keep_distinct_actors(kg):
    first = kg.add_triple("Alice", "works_at", "A", valid_from="2020-01-01", created_by="first")
    kg.invalidate("Alice", "works_at", "A", ended="2021-01-01", ended_by="first-end")
    second = kg.add_triple("Alice", "works_at", "A", valid_from="2022-01-01", created_by="second")
    kg.invalidate("Alice", "works_at", "A", ended="2023-01-01", ended_by="second-end")
    assert first != second
    assert [(f["created_by"], f["ended_by"]) for f in kg.timeline()] == [
        ("first", "first-end"),
        ("second", "second-end"),
    ]


def test_omitted_actor_stays_unknown_on_old_and_new_facts(kg):
    kg.add_triple("Alice", "works_at", "A")
    kg.add_triple("Alice", "works_at", "A", created_by=CREATOR)
    kg.supersede("Alice", "works_at", "A", "B", at=BOUNDARY)
    kg.invalidate("Alice", "works_at", "B", ended="2026-10-02")
    assert all(f["created_by"] is f["ended_by"] is None for f in kg.timeline())


def test_actor_label_round_trip_preserves_exact_unicode_spelling(kg):
    actor = " mac:codex:açúcar "
    kg.add_triple("Alice", "visits", "Café", created_by=actor)
    assert kg.timeline()[0]["created_by"] == actor


@pytest.mark.parametrize(
    "actor", [42, False, "", "  ", "x\0y", "x" * 129, " " * 129 + "a", "x\ud800y"]
)
@pytest.mark.parametrize("operation", ["add", "invalidate", "supersede"])
def test_invalid_actor_rejected_before_any_mutation(kg, actor, operation):
    kg.add_triple("Alice", "works_at", "A", created_by=CREATOR)
    before = kg.dump_rows("triples")
    with pytest.raises(ValueError):
        if operation == "add":
            kg.add_triple("Other", "works_at", "B", created_by=actor)
        elif operation == "invalidate":
            kg.invalidate("Alice", "works_at", "A", ended=BOUNDARY, ended_by=actor)
        else:
            kg.supersede("Alice", "works_at", "A", "B", at=BOUNDARY, by=actor)
    assert kg.dump_rows("triples") == before
    assert kg.stats()["entities"] == 2


def test_supersede_failed_successor_rolls_back_end_actor(kg, monkeypatch):
    kg.add_triple("Alice", "works_at", "A", created_by=CREATOR)
    connection = kg._conn()
    before = kg.dump_rows("triples")

    class FailingConnection:
        def __enter__(self):
            return connection.__enter__()

        def __exit__(self, *args):
            return connection.__exit__(*args)

        def execute(self, sql, *args):
            if sql.lstrip().startswith("INSERT INTO triples"):
                raise sqlite3.OperationalError("simulated write failure")
            return connection.execute(sql, *args)

    monkeypatch.setattr(kg, "_conn", lambda: FailingConnection())
    with pytest.raises(sqlite3.OperationalError):
        kg.supersede("Alice", "works_at", "A", "B", at=BOUNDARY, by=EDITOR)
    monkeypatch.setattr(kg, "_conn", lambda: connection)
    assert kg.dump_rows("triples") == before
    assert kg.stats()["entities"] == 2


def test_rewrite_preserves_author_and_does_not_merge_distinct_authorship(kg):
    original = kg.add_triple("Alice", "old_status", "Ready", created_by=CREATOR)
    other = kg.add_triple("Alice", "status", "Ready", created_by=EDITOR)
    rewritten = kg.rewrite(original, "status", "Ready", at=BOUNDARY)
    assert rewritten != other
    facts = kg.query_relationship("status")
    assert {f["created_by"] for f in facts} == {CREATOR, EDITOR}
    history = kg.timeline()
    assert len(history) == 3
    assert next(f for f in history if f["predicate"] == "old_status")["created_by"] == CREATOR
    assert kg.rewrite(original, "status", "Ready", at=BOUNDARY) is None
    assert kg.timeline() == history


def test_new_replica_round_trip_and_legacy_replay_preserve_actor_labels(kg, tmp_path):
    kg.add_triple("Alice", "works_at", "A", created_by=CREATOR)
    kg.invalidate("Alice", "works_at", "A", ended=BOUNDARY, ended_by=EDITOR)
    with KnowledgeGraph(str(tmp_path / "replica.sqlite3")) as replica:
        for table in ("entities", "triples"):
            for row in kg.dump_rows(table):
                replica.apply_row(table, row)
        assert replica.timeline() == kg.timeline()
        row = kg.dump_rows("triples")[0]
        row.pop("created_by")
        row.pop("ended_by")
        replica.apply_row("triples", row)
        assert replica.timeline() == kg.timeline()


@pytest.mark.parametrize("transport", ["full", "light", "daemon"])
def test_actor_lifecycle_through_mcp_and_daemon(monkeypatch, config, kg, transport):
    from _mcp_server_helpers import _patch_mcp_server
    from mempalace import mcp_light_server, mcp_server
    from mempalace.service import run_mcp_tool

    _patch_mcp_server(monkeypatch, config, kg)
    monkeypatch.setattr(mcp_server, "_apply_server_flags", lambda **_: None)
    monkeypatch.setattr(mcp_server, "_READ_ONLY", False)
    monkeypatch.setattr(mcp_server, "_vector_disabled", False)

    def execute(action, **arguments):
        if transport == "daemon":
            return run_mcp_tool({"name": "mempalace_" + action, "arguments": arguments})
        light = transport == "light"
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "palace_exec" if light else "mempalace_" + action,
                "arguments": {"action": action, **arguments} if light else arguments,
            },
        }
        response = (
            mcp_light_server.handle_light_request(request)
            if light
            else mcp_server.handle_request(request)
        )
        assert "error" not in response, response
        return json.loads(response["result"]["content"][0]["text"])

    assert execute("kg_add", subject="Alice", predicate="works_at", object="A", created_by=CREATOR)[
        "success"
    ]
    assert execute(
        "kg_supersede",
        subject="Alice",
        predicate="works_at",
        old_object="A",
        new_object="B",
        at=BOUNDARY,
        by=EDITOR,
    )["success"]
    assert execute(
        "kg_invalidate",
        subject="Alice",
        predicate="works_at",
        object="B",
        ended="2026-10-02",
        ended_by="end-session",
    )["success"]
    facts = mcp_server.tool_kg_query("Alice")["facts"]
    assert {(f["object"], f["created_by"], f["ended_by"]) for f in facts} == {
        ("A", CREATOR, EDITOR),
        ("B", EDITOR, "end-session"),
    }
    assert mcp_server.tool_kg_timeline("Alice")["timeline"] == kg.timeline("Alice")
    assert mcp_server.tool_kg_query("Alice", as_of=BOUNDARY)["facts"][0]["created_by"] == EDITOR


def test_mcp_actor_schema_and_invalid_argument_envelope(monkeypatch, config, kg):
    from _mcp_server_helpers import _patch_mcp_server
    from mempalace import mcp_light_server, mcp_server

    _patch_mcp_server(monkeypatch, config, kg)
    for tool, parameter in [
        ("kg_add", "created_by"),
        ("kg_invalidate", "ended_by"),
        ("kg_supersede", "by"),
    ]:
        assert parameter in mcp_server.TOOLS["mempalace_" + tool]["input_schema"]["properties"]
        assert (
            parameter in mcp_light_server.LIGHT_TOOLS["palace_exec"]["input_schema"]["properties"]
        )
    result = mcp_server.tool_kg_add("Alice", "works_at", "A", created_by=42)
    assert result["success"] is False
    assert "created_by" in result["error"]
    assert kg.stats()["triples"] == 0


def test_malformed_unicode_actor_rejected_before_wal_or_database_write(monkeypatch, config, kg):
    from _mcp_server_helpers import _patch_mcp_server
    from mempalace import mcp_server

    _patch_mcp_server(monkeypatch, config, kg)
    monkeypatch.setattr(
        mcp_server, "_wal_log", lambda *a, **k: pytest.fail("invalid actor reached WAL")
    )
    result = mcp_server.tool_kg_add("Alice", "works_at", "A", created_by="mac:codex:\ud800")
    assert result == {"success": False, "error": "created_by must be valid UTF-8 text"}
    assert kg.stats()["triples"] == 0
