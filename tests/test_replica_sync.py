"""
Tests for RFC 004 step 1 — memory read replicas (snapshot pull + local fold).

Origin palace served by the production HTTP server; replica palace folds
via replica_sync. Uses the sqlite_exact backend with a fake embedder (the
same pattern as test_sqlite_exact_backend.py) so no model download happens:
the point is fact replication, and vectors are derived locally by design.
"""

import http.client
import json
import math
import os
import threading

import pytest

from mempalace.knowledge_graph import KnowledgeGraph
from mempalace.replica_sync import REPLICA_ORIGIN_KEY, pull_memory


@pytest.fixture
def fake_embedder(monkeypatch):
    import mempalace.backends.embedding_wrapper as embedding_wrapper

    def fake_embed(texts):
        return [[1.0, 0.0] if "canary" in t else [0.5, math.sqrt(0.75)] for t in texts]

    monkeypatch.setenv("MEMPALACE_BACKEND_EXPLICIT", "sqlite_exact")
    monkeypatch.setattr(embedding_wrapper, "_embed_texts", fake_embed)


@pytest.fixture
def origin_server(monkeypatch, fake_embedder, config, palace_path):
    """Origin palace with seeded drawers + KG, served over real HTTP."""
    from mempalace import mcp_server as mcp
    from mempalace.palace import get_collection

    monkeypatch.setattr(mcp, "_config", config)
    monkeypatch.setattr(mcp, "_logstream_by_path", {})
    monkeypatch.setattr(mcp, "_collection_cache", None)
    monkeypatch.setattr(mcp, "_client_cache", None)

    col = get_collection(palace_path, create=True)
    col.upsert(
        ids=["drawer_w_r_aaa", "drawer_w_r_bbb", "drawer_w2_r2_ccc"],
        documents=[
            "the search canary drawer",
            "alembic migrations run on postgres",
            "sprint planning notes for q3",
        ],
        metadatas=[
            {"wing": "w", "room": "r", "source_file": "a.md", "filed_at": "2026-07-01T00:00:00"},
            {"wing": "w", "room": "r", "source_file": "b.md", "filed_at": "2026-07-01T00:00:01"},
            {"wing": "w2", "room": "r2", "added_by": "mcp", "filed_at": "2026-07-01T00:00:02"},
        ],
    )
    kg = KnowledgeGraph(db_path=os.path.join(palace_path, "knowledge_graph.sqlite3"))
    kg.add_entity("Alice", entity_type="person")
    kg.add_triple("Alice", "works_on", "MemPalace", valid_from="2026-01-01")
    kg.close()

    monkeypatch.setattr(
        mcp,
        "_get_kg",
        lambda *a, **kw: KnowledgeGraph(
            db_path=os.path.join(palace_path, "knowledge_graph.sqlite3")
        ),
    )

    httpd = mcp._build_http_server("127.0.0.1", 0)
    port = httpd.server_address[1]
    thread = threading.Thread(
        target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}", palace_path, mcp
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
        for ls in mcp._logstream_by_path.values():
            ls.close()


@pytest.fixture
def replica_palace(tmp_dir):
    p = os.path.join(tmp_dir, "replica_palace")
    os.makedirs(p)
    return p


def _get(url, path):
    host, port = url.replace("http://", "").split(":")
    conn = http.client.HTTPConnection(host, int(port), timeout=5)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        return resp.status, json.loads(resp.read() or b"{}")
    finally:
        conn.close()


class _ConfigProxy:
    """Config stand-in pointing the served palace at a different directory."""

    def __init__(self, palace_path, base):
        self.palace_path = palace_path
        self.collection_name = base.collection_name

    def __getattr__(self, name):
        raise AttributeError(name)


class TestSnapshotEndpoints:
    def test_manifest_counts(self, origin_server):
        url, _, _ = origin_server
        status, manifest = _get(url, "/snapshot/manifest")
        assert status == 200
        assert manifest["drawers"] == 3
        assert manifest["kg"]["triples"] == 1
        assert manifest["replica_id"].startswith("rep_")

    def test_drawers_paginate(self, origin_server):
        url, _, _ = origin_server
        status, page1 = _get(url, "/snapshot/drawers?offset=0&limit=2")
        status2, page2 = _get(url, "/snapshot/drawers?offset=2&limit=2")
        assert status == status2 == 200
        ids = [i["id"] for i in page1["items"]] + [i["id"] for i in page2["items"]]
        assert sorted(ids) == ["drawer_w2_r2_ccc", "drawer_w_r_aaa", "drawer_w_r_bbb"]
        assert all("document" in i and "metadata" in i for i in page1["items"])

    def test_ids_endpoint(self, origin_server):
        url, _, _ = origin_server
        status, page = _get(url, "/snapshot/ids?offset=0&limit=10")
        assert status == 200
        assert len(page["ids"]) == 3

    def test_kg_pages_and_rejects_bad_table(self, origin_server):
        url, _, _ = origin_server
        status, triples = _get(url, "/snapshot/kg?table=triples&after=0&limit=10")
        assert status == 200
        assert triples["rows"][0]["subject"] == "alice"
        status, bad = _get(url, "/snapshot/kg?table=users&after=0")
        assert status == 400

    def test_bad_pagination_is_400(self, origin_server):
        url, _, _ = origin_server
        status, _ = _get(url, "/snapshot/drawers?offset=nope")
        assert status == 400


class TestReplicaPull:
    def test_full_pull_folds_facts_and_derives_locally(
        self, origin_server, replica_palace, fake_embedder
    ):
        url, _, _ = origin_server
        stats = pull_memory(replica_palace, url)
        assert stats["drawers_upserted"] == 3
        assert stats["drawers_deleted"] == 0
        assert stats["kg_triples"] == 1
        assert stats["kg_entities"] >= 1

        # Facts are verbatim and searchable locally.
        from mempalace.searcher import search_memories

        result = search_memories("search canary", replica_palace, n_results=1)
        assert result["results"][0]["text"] == "the search canary drawer"

        # Provenance stamp present on the copy.
        from mempalace.palace import get_collection

        col = get_collection(replica_palace)
        copy = col.get(ids=["drawer_w_r_aaa"], include=["metadatas"])
        assert copy["metadatas"][0][REPLICA_ORIGIN_KEY].startswith("rep_")

        # KG fact replicated with temporal fields intact.
        kg = KnowledgeGraph(db_path=os.path.join(replica_palace, "knowledge_graph.sqlite3"))
        try:
            facts = kg.query_entity("Alice")
            assert any(f["object"] == "MemPalace" for f in facts)
        finally:
            kg.close()

    def test_pull_is_idempotent(self, origin_server, replica_palace, fake_embedder):
        url, _, _ = origin_server
        pull_memory(replica_palace, url)
        stats = pull_memory(replica_palace, url)
        assert stats["drawers_upserted"] == 3  # upserts, not duplicates
        from mempalace.palace import get_collection

        assert get_collection(replica_palace).count() == 3

    def test_reconcile_deletes_only_origin_copies(
        self, origin_server, replica_palace, fake_embedder
    ):
        url, origin_palace, _ = origin_server
        pull_memory(replica_palace, url)

        # A locally-authored drawer must survive reconciliation forever.
        from mempalace.palace import get_collection

        replica_col = get_collection(replica_palace)
        replica_col.upsert(
            ids=["drawer_local_diary"],
            documents=["local diary entry, authored on the replica"],
            metadatas=[{"wing": "local", "room": "diary"}],
        )

        # Upstream deletes one drawer; the replica's copy reconciles away.
        origin_col = get_collection(origin_palace)
        origin_col.delete(ids=["drawer_w_r_bbb"])

        stats = pull_memory(replica_palace, url)
        assert stats["drawers_deleted"] == 1
        remaining = set(replica_col.get(limit=100)["ids"])
        assert "drawer_w_r_bbb" not in remaining
        assert "drawer_local_diary" in remaining

    def test_kg_invalidation_converges_on_repull(
        self, origin_server, replica_palace, fake_embedder
    ):
        url, origin_palace, _ = origin_server
        pull_memory(replica_palace, url)

        origin_kg = KnowledgeGraph(db_path=os.path.join(origin_palace, "knowledge_graph.sqlite3"))
        origin_kg.invalidate("Alice", "works_on", "MemPalace", ended="2026-07-01")
        origin_kg.close()

        pull_memory(replica_palace, url)
        kg = KnowledgeGraph(db_path=os.path.join(replica_palace, "knowledge_graph.sqlite3"))
        try:
            live_now = kg.query_entity("Alice", as_of="2026-07-02")
            assert not any(f["object"] == "MemPalace" for f in live_now)
        finally:
            kg.close()

    def test_bidirectional_two_origin_pull_has_no_echo(
        self, origin_server, replica_palace, fake_embedder, monkeypatch
    ):
        """Option C from the fleet: each machine authors its own history and
        pulls the other's. Snapshots serve authored-only content, so a pull
        pair must never echo a replica's own drawers back re-stamped."""
        url, origin_palace, mcp = origin_server
        pull_memory(replica_palace, url)  # replica now holds origin's 3 drawers

        # Replica authors net-new local content (the Windows mining case).
        from mempalace.palace import get_collection

        replica_col = get_collection(replica_palace)
        replica_col.upsert(
            ids=["drawer_win_convo_001"],
            documents=["windows conversation canary from the second origin"],
            metadatas=[{"wing": "claude_conversations_windows", "room": "technical"}],
        )

        # Reverse direction: serve the REPLICA palace, pull into the origin.
        monkeypatch.setattr(mcp, "_config", _ConfigProxy(replica_palace, mcp._config))
        monkeypatch.setattr(mcp, "_collection_cache", None)
        monkeypatch.setattr(mcp, "_client_cache", None)
        monkeypatch.setattr(mcp, "_logstream_by_path", {})
        import threading

        httpd = mcp._build_http_server("127.0.0.1", 0)
        port = httpd.server_address[1]
        thread = threading.Thread(
            target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        thread.start()
        try:
            stats = pull_memory(origin_palace, f"http://127.0.0.1:{port}", pull_kg=False)
            # Only the replica-AUTHORED drawer crosses; the origin's own 3
            # drawers are not echoed back.
            assert stats["drawers_upserted"] == 1
            origin_col = get_collection(origin_palace)
            copy = origin_col.get(ids=["drawer_win_convo_001"], include=["documents", "metadatas"])
            assert copy["documents"][0] == "windows conversation canary from the second origin"
            assert copy["metadatas"][0][REPLICA_ORIGIN_KEY].startswith("rep_")
            # Origin's own drawers keep clean provenance (no stamp).
            own = origin_col.get(ids=["drawer_w_r_aaa"], include=["metadatas"])
            assert REPLICA_ORIGIN_KEY not in (own["metadatas"][0] or {})
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)

    def test_cli_replica_pull(self, origin_server, replica_palace, fake_embedder, capsys):
        url, _, _ = origin_server
        from types import SimpleNamespace

        from mempalace.cli import cmd_replica

        cmd_replica(
            SimpleNamespace(
                palace=replica_palace,
                replica_action="pull",
                peer=url,
                token=None,
                no_reconcile=False,
                json=True,
            )
        )
        results = json.loads(capsys.readouterr().out)
        assert results[0]["drawers_upserted"] == 3
