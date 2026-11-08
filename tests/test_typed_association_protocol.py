"""Metadata-only association snapshots and failure behavior."""

# ruff: noqa: F811

from pathlib import Path

import pytest

from commonplace.handoff.mirror_access import reader
from commonplace.handoff.recall import Recall
from commonplace.rules import checks
from mempalace import mcp_server as mcp
from tests.test_typed_association_reads import (  # noqa: F401
    case,
    citation,
    fact,
    functional_worker_transport,
    hub_fixture,
    invoke,
    seed,
    target,
)


@pytest.mark.parametrize("role", ["member", "owner"])
@pytest.mark.parametrize("kind", ["drawer", "closet"])
@pytest.mark.parametrize("fault", ["missing", "roomless", "lookup", "changed", "revoked"])
def test_association_unproved(case, monkeypatch, role, kind, fault):
    c = case
    field = "source_drawer_id" if kind == "drawer" else "source_closet"
    col = target(c, kind, c.primary.id)
    seed(c, field, "opaque-local-id")
    control = fact(c, role)
    assert citation(control, kind)[field] == "opaque-local-id"
    original = c.h.gateway.mediator.workers.call_tool

    def call(principal, name, args):
        if name == "read_associations":
            if fault == "lookup":
                raise OSError("unavailable")
            if "expect" in args:
                if fault == "changed":
                    col.update(ids=["opaque-local-id"], metadatas=[{"room": c.hidden.id}])
                elif fault == "revoked":
                    current = Recall.can_read
                    monkeypatch.setattr(
                        Recall,
                        "can_read",
                        lambda self, p, room, **kw: (
                            False if room == c.primary.id else current(self, p, room, **kw)
                        ),
                    )
        return original(principal, name, args)

    if fault == "missing":
        col.delete(ids=["opaque-local-id"])
    elif fault == "roomless":
        col.update(ids=["opaque-local-id"], metadatas=[{"room": ""}])
    monkeypatch.setattr(c.h.gateway.mediator.workers, "call_tool", call)
    # A fact with an ordinary readable source isolates citation proof from D1.
    if fault == "revoked":
        with c.kg._lock:
            c.kg._conn().execute("UPDATE fact_sources SET source_thread='ordinary'")
            c.kg._conn().execute("UPDATE triples SET source_thread='ordinary'")
            c.kg._conn().commit()
    row = invoke(c, "kg_query", role, entity="TypedFact")["facts"][0]
    assert row["object"] == "Exact  original words"
    assert field not in citation(row, kind)
    if fault != "revoked":
        for item in [control, *control["sources"]]:
            item.pop(field, None)
        assert row == control


@pytest.mark.parametrize("role", ["member", "owner"])
@pytest.mark.parametrize("parent_key", ["parent_drawer_id", "parent_entry_id"])
def test_logical_all_chunks(case, role, parent_key):
    c = case
    c.col.add(
        ids=["chunk-b", "chunk-a"],
        documents=["b", "a"],
        metadatas=[
            {"wing": "shared", "room": c.hidden.id, parent_key: "logical", "chunk_index": 1},
            {"wing": "shared", "room": c.primary.id, parent_key: "logical", "chunk_index": 0},
        ],
    )
    seed(c, "source_drawer_id", "logical")
    row = fact(c, role)
    assert "source_drawer_id" not in row["sources"][0]
    c.col.update(ids=["chunk-b"], metadatas=[{"room": c.primary.id}])
    assert fact(c, role)["sources"][0]["source_drawer_id"] == "logical"


def test_optional_absent_and_no_writes(case, monkeypatch):
    c = case
    from mempalace import palace
    from mempalace.backends.base import CollectionNotInitializedError

    original = palace.get_closets_collection
    opened = []

    def optional(*args, **kwargs):
        opened.append(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(palace, "get_closets_collection", optional)
    with pytest.raises(CollectionNotInitializedError):
        original(c.config.palace_path, create=False)
    seed(c, "source_closet", "missing")
    before = c.kg._conn().total_changes
    wal = Path(c.config.palace_path) / "chroma.sqlite3-wal"
    old = wal.read_bytes() if wal.exists() else None
    assert "source_closet" not in fact(c)
    assert c.kg._conn().total_changes == before
    assert (wal.read_bytes() if wal.exists() else None) == old
    assert not opened
    with pytest.raises(CollectionNotInitializedError):
        original(c.config.palace_path, create=False)


def test_metadata_only_pages_and_alignment(case, monkeypatch):
    c = case
    for rid, room in [("a", c.primary.id), ("b", c.hidden.id)]:
        c.col.add(
            ids=[rid],
            documents=["Never hydrate"],
            metadatas=[{"wing": "shared", "room": room, "parent_entry_id": "logical"}],
        )
    from mempalace.backends import chroma_sqlite

    original = chroma_sqlite._decoded_rows
    calls = []

    def decode(conn, ids, **kwargs):
        assert kwargs == {"metadata_only": True}
        statements = []
        conn.set_trace_callback(statements.append)
        result = original(conn, ids, **kwargs)
        calls.extend(statements)
        return result

    monkeypatch.setattr(chroma_sqlite, "_decoded_rows", decode)
    result = mcp.read_associations("drawer", "logical")
    assert [(r["id"], r["room"]) for r in result["associations"]] == [
        ("a", c.primary.id),
        ("b", c.hidden.id),
    ]
    assert any("substr(key, 1, 7) != 'chroma:'" in sql for sql in calls)
    monkeypatch.setattr(chroma_sqlite, "_decoded_rows", original)
    # Exercise public transport as well as the private metadata seam.
    invoke(c, "get_drawer", drawer_id="a")


def test_expired_deadline_never_probes():
    from types import SimpleNamespace

    calls = []

    def call_tool(*args):
        calls.append(args)
        return {"associations": None}

    access = reader(
        SimpleNamespace(mediator=SimpleNamespace(workers=SimpleNamespace(call_tool=call_tool))),
        SimpleNamespace(),
    )
    token = checks._deadline.set(checks.monotonic() - 1)
    try:
        with pytest.raises(TimeoutError, match="rules deadline expired"):
            checks.remaining(0)
        assert not access.association("drawer", "opaque")
        assert calls == []
    finally:
        checks._deadline.reset(token)


def test_logical_metadata_pages_no_truncation(case):
    c = case
    ids = [f"piece-{i:04}" for i in range(501)]
    c.col.add(
        ids=ids,
        documents=["Never hydrate"] * 501,
        metadatas=[
            {
                "wing": "shared",
                "room": c.primary.id if i < 500 else c.hidden.id,
                "parent_entry_id": "logical",
                "chunk_index": i,
            }
            for i in range(501)
        ],
    )
    wal = Path(c.config.palace_path) / "chroma.sqlite3-wal"
    before = wal.read_bytes() if wal.exists() else None
    descriptors = mcp.read_associations("drawer", "logical")["associations"]
    assert len(descriptors) == 501
    assert (wal.read_bytes() if wal.exists() else None) == before
    seed(c, "source_drawer_id", "logical")
    assert "source_drawer_id" not in fact(c)["sources"][0]


@pytest.mark.parametrize("value", [42, True])
def test_malformed_raw_file_keeps_drawer(case, value):
    c = case
    c.col.add(
        ids=["ordinary"],
        documents=["Exact drawer words"],
        metadatas=[{"wing": "shared", "room": c.primary.id, "source_file": value}],
    )
    row = invoke(c, "get_drawer", drawer_id="ordinary")
    assert row["content"] == "Exact drawer words"
    assert "source_file" not in row["metadata"]


@pytest.mark.parametrize(
    "fault", ["missing-wing", "missing-room", "empty-room", "lookup", "readable"]
)
def test_closet_mutation_probe_unavailable(config, kg, monkeypatch, fault):
    from tests._mcp_server_helpers import _patch_mcp_server

    _patch_mcp_server(monkeypatch, config, kg)

    calls = []

    def unavailable(*args, **kwargs):
        calls.append((args, kwargs))
        if fault == "lookup":
            raise ValueError("incomplete metadata")
        row = {"wing": "shared", "room": "notes"}
        if fault == "missing-wing":
            row.pop("wing")
        elif fault == "missing-room":
            row.pop("room")
        elif fault == "empty-room":
            row["room"] = ""
        return {"associations": [row]}

    monkeypatch.setattr(mcp, "read_associations", unavailable)
    result = mcp.tool_kg_add(
        subject="A",
        predicate="keeps",
        object="B",
        wing="shared",
        sensitivity="public",
        source_closet="opaque",
        thread_scope={"probe": True, "closet": "opaque"},
    )
    assert calls == [(("closet", "opaque"), {})]
    assert result["cited"] == {"closet": ["shared", "notes"] if fault == "readable" else None}
    assert kg.query_entity("A") == []


@pytest.mark.parametrize("role", ["member", "owner"])
def test_capped_backend_logical_citation_fails_closed(case, monkeypatch, role):
    from mempalace import palace

    c = case
    calls = []

    class CappedCollection:
        def get(self, *, ids=None, include, where=None, limit=None, offset=0):
            assert include == ["metadatas"]
            calls.append((ids, where, limit, offset))
            if ids is not None:
                return {"ids": [], "metadatas": []}
            stop = min(offset + min(limit, 100), 601)
            return {
                "ids": [f"chunk-{i:04}" for i in range(offset, stop)],
                "metadatas": [
                    {
                        "wing": "shared",
                        "room": c.hidden.id if i == 550 else c.primary.id,
                        "parent_entry_id": "logical",
                        "chunk_index": i,
                    }
                    for i in range(offset, stop)
                ],
            }

    backend = CappedCollection()
    assert len(backend.get(include=["metadatas"], where={}, limit=500)["ids"]) == 100
    assert (
        backend.get(include=["metadatas"], where={}, limit=500, offset=550)["metadatas"][0]["room"]
        == c.hidden.id
    )
    calls.clear()
    monkeypatch.setattr(mcp, "_is_chroma_backend", lambda: False)
    monkeypatch.setattr(palace, "get_collection", lambda *a, **kw: backend)
    seed(c, "source_drawer_id", "logical")
    assert (
        c.kg._conn().execute("SELECT source_drawer_id FROM fact_sources").fetchone()[0] == "logical"
    )
    row = fact(c, role)
    assert "source_drawer_id" not in row["sources"][0], "incomplete backend proof exposed citation"
    assert calls and calls[0][0] == ["logical"]
