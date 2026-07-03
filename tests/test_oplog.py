"""
test_oplog.py — Tests for the RFC 004 step-2a canonical memory op-log.

Covers the durable SQLite core in mempalace/oplog.py (envelope stamping,
HLC monotonicity, version vectors, idempotent remote apply), the
KnowledgeGraph dual-write shadow emission (including the dedup and
zero-match cases that must NOT emit), and the shadow divergence verifier
in mempalace/oplog_verify.py.
"""

import hashlib
import os
import threading

import pytest

from mempalace.hlc import parse as hlc_parse
from mempalace.knowledge_graph import KnowledgeGraph
from mempalace.oplog import (
    MAX_PAYLOAD_BYTES,
    OPLOG_DB_FILENAME,
    OpLog,
    emit_op,
    shadow_append,
)
from mempalace.oplog_verify import fold_expected, read_logical_drawer, verify_shadow


@pytest.fixture
def oplog(palace_path):
    """An isolated OpLog inside an empty palace dir."""
    log = OpLog(db_path=os.path.join(palace_path, OPLOG_DB_FILENAME))
    yield log
    log.close()


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _remote_op(origin="rep_aaaaaaaaaaaa", seq=1, kind="drawer.add", **payload):
    return {
        "op_id": f"op_{origin}_{seq}",
        "origin_replica": origin,
        "origin_seq": seq,
        "hlc": f"{1783000000000 + seq:013d}-000000-{origin}",
        "author_agent": "windows-claude",
        "authored_at": "2026-07-02T20:00:00Z",
        "kind": kind,
        "payload": payload or {"drawer_id": f"drawer_{seq}"},
    }


class TestOpLogCore:
    def test_append_stamps_envelope(self, oplog):
        op = oplog.append("drawer.add", {"drawer_id": "d1", "content": "hello"})
        assert op["op_id"] == f"op_{oplog.replica_id}_{op['origin_seq']}"
        assert op["origin_replica"] == oplog.replica_id
        assert op["origin_seq"] == op["seq"]
        assert op["kind"] == "drawer.add"
        assert op["payload"] == {"drawer_id": "d1", "content": "hello"}
        ms, counter, replica = hlc_parse(op["hlc"])
        assert replica == oplog.replica_id
        assert op["authored_at"].endswith("Z")

    def test_hlc_strictly_increases(self, oplog):
        stamps = [oplog.append("drawer.add", {"drawer_id": f"d{i}"})["hlc"] for i in range(20)]
        assert stamps == sorted(stamps)
        assert len(set(stamps)) == len(stamps)

    def test_hlc_survives_reopen(self, palace_path, oplog):
        first = oplog.append("drawer.add", {"drawer_id": "d1"})["hlc"]
        oplog.close()
        reopened = OpLog(db_path=os.path.join(palace_path, OPLOG_DB_FILENAME))
        try:
            assert reopened.append("drawer.add", {"drawer_id": "d2"})["hlc"] > first
        finally:
            reopened.close()

    def test_unknown_kind_rejected_locally(self, oplog):
        with pytest.raises(ValueError, match="kind="):
            oplog.append("drawer.explode", {"drawer_id": "d1"})

    def test_payload_must_be_object(self, oplog):
        with pytest.raises(ValueError, match="payload must be an object"):
            oplog.append("drawer.add", "not a dict")

    def test_payload_size_capped(self, oplog):
        with pytest.raises(ValueError, match="maximum size"):
            oplog.append("drawer.add", {"content": "x" * (MAX_PAYLOAD_BYTES + 1)})

    def test_author_agent_validated(self, oplog):
        with pytest.raises(ValueError, match="author_agent"):
            oplog.append("drawer.add", {"drawer_id": "d1"}, author_agent="")

    def test_version_vector_and_list_ops(self, oplog):
        for i in range(3):
            oplog.append("drawer.add", {"drawer_id": f"d{i}"})
        vector = oplog.version_vector()
        assert vector == {oplog.replica_id: 3}
        ops = oplog.list_ops(oplog.replica_id, after_seq=1)
        assert [op["origin_seq"] for op in ops] == [2, 3]

    def test_list_all_kind_filter_and_cursor(self, oplog):
        oplog.append("drawer.add", {"drawer_id": "d1"})
        oplog.append("kg.assert", {"triple_id": "t1"})
        oplog.append("drawer.tombstone", {"drawer_id": "d1"})
        drawer_ops = oplog.list_all(kinds=["drawer.add", "drawer.tombstone"])
        assert [op["kind"] for op in drawer_ops] == ["drawer.add", "drawer.tombstone"]
        tail = oplog.list_all(after_seq=drawer_ops[0]["seq"])
        assert [op["kind"] for op in tail] == ["kg.assert", "drawer.tombstone"]

    def test_concurrent_appends_get_unique_ids(self, oplog):
        results = []
        errors = []

        def worker(n):
            try:
                for i in range(20):
                    results.append(oplog.append("drawer.add", {"drawer_id": f"w{n}_{i}"}))
            except Exception as exc:  # pragma: no cover - failure surface
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        op_ids = [op["op_id"] for op in results]
        assert len(set(op_ids)) == len(op_ids) == 80


class TestLegacySchemaMigration:
    def test_opens_pre_subject_id_oplog(self, palace_path):
        """A production op-log created before the fold consumer (no
        subject_id column, no fold_state table) must open and migrate.

        Regression: the canonical init script created the subject_id index
        BEFORE the migration could add the column, so every pre-existing
        op-log failed at open — and the abandoned half-open constructor
        leaked one fd per sync round until the hub exhausted its table.
        Fresh test databases always had the column, which is why 3,513
        green tests missed it.
        """
        import sqlite3 as sqlite3_mod

        db_path = os.path.join(palace_path, OPLOG_DB_FILENAME)
        legacy = sqlite3_mod.connect(db_path)
        legacy.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE ops (
                op_id TEXT NOT NULL UNIQUE,
                origin_replica TEXT NOT NULL,
                origin_seq INTEGER,
                hlc TEXT NOT NULL,
                author_agent TEXT NOT NULL,
                authored_at TEXT NOT NULL,
                kind TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE UNIQUE INDEX ops_origin_seq_idx ON ops(origin_replica, origin_seq);
            CREATE INDEX ops_kind_idx ON ops(kind);
            CREATE INDEX ops_hlc_idx ON ops(hlc);
        """)
        legacy.execute(
            "INSERT INTO ops (op_id, origin_replica, origin_seq, hlc, author_agent,"
            " authored_at, kind, payload) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "op_rep_aaaaaaaaaaaa_1",
                "rep_aaaaaaaaaaaa",
                1,
                "1783000000000-000000-rep_aaaaaaaaaaaa",
                "mcp",
                "2026-07-02T20:00:00Z",
                "drawer.add",
                '{"drawer_id": "d_legacy", "content": "x"}',
            ),
        )
        legacy.commit()
        legacy.close()

        log = OpLog(db_path=db_path)
        try:
            # Migration backfilled the merge key and the log is fully usable.
            assert log.subject_head_hlc("d_legacy") == "1783000000000-000000-rep_aaaaaaaaaaaa"
            assert log.fold_cursor() == 0
            fresh = log.append("drawer.add", {"drawer_id": "d_new"})
            assert fresh["origin_seq"] == 2
        finally:
            log.close()

    def test_failed_init_does_not_leak_or_poison(self, palace_path, monkeypatch):
        """If init fails, the connection closes and the next attempt retries
        cleanly (no cached half-open instance, no fd left behind)."""
        db_path = os.path.join(palace_path, OPLOG_DB_FILENAME)
        closed = []
        original_close = OpLog.close

        def tracking_close(self):
            closed.append(True)
            original_close(self)

        monkeypatch.setattr(OpLog, "close", tracking_close)
        monkeypatch.setattr(
            OpLog, "_migrate_subject_column", lambda self, conn: 1 / 0, raising=True
        )
        with pytest.raises(ZeroDivisionError):
            OpLog(db_path=db_path)
        assert closed  # the failing constructor released its connection

        monkeypatch.undo()
        log = OpLog(db_path=db_path)  # clean retry succeeds
        try:
            assert log.count() == 0
        finally:
            log.close()


class TestApplyRemoteOp:
    def test_apply_and_idempotency(self, oplog):
        op = _remote_op(seq=1)
        assert oplog.apply_remote_op(op) is True
        assert oplog.apply_remote_op(op) is False
        assert oplog.version_vector()["rep_aaaaaaaaaaaa"] == 1

    def test_own_origin_echo_is_noop(self, oplog):
        local = oplog.append("drawer.add", {"drawer_id": "d1"})
        assert oplog.apply_remote_op(local) is False

    def test_remote_hlc_observed(self, oplog):
        remote = _remote_op(seq=1)
        remote["hlc"] = "9999999999999-000000-rep_aaaaaaaaaaaa"
        oplog.apply_remote_op(remote)
        nxt = oplog.append("drawer.add", {"drawer_id": "d2"})
        assert nxt["hlc"] > remote["hlc"]

    def test_missing_field_rejected(self, oplog):
        op = _remote_op(seq=1)
        del op["hlc"]
        with pytest.raises(ValueError, match="hlc"):
            oplog.apply_remote_op(op)

    def test_unknown_remote_kind_accepted_verbatim(self, oplog):
        op = _remote_op(seq=1, kind="drawer.future_thing")
        assert oplog.apply_remote_op(op) is True
        assert oplog.list_ops("rep_aaaaaaaaaaaa")[0]["kind"] == "drawer.future_thing"

    def test_malformed_remote_kind_rejected(self, oplog):
        op = _remote_op(seq=1)
        op["kind"] = "NOT A KIND"
        with pytest.raises(ValueError, match="kind"):
            oplog.apply_remote_op(op)

    def test_arrival_order_is_local_cursor(self, oplog):
        oplog.apply_remote_op(_remote_op(seq=1))
        local = oplog.append("drawer.add", {"drawer_id": "mine"})
        oplog.apply_remote_op(_remote_op(seq=2))
        seqs = [op["seq"] for op in oplog.list_all()]
        assert seqs == sorted(seqs)
        assert local["origin_seq"] == local["seq"]


class TestShadowEmission:
    def test_emit_op_shadow_never_raises(self, tmp_dir):
        # Nonexistent parent is created; a garbage path degrades to None.
        assert emit_op("", "drawer.add", {"drawer_id": "d"}) is None
        assert emit_op(tmp_dir, "not.a.kind", {"drawer_id": "d"}) is None

    def test_emit_op_required_raises(self, tmp_dir):
        with pytest.raises(ValueError):
            emit_op(tmp_dir, "not.a.kind", {"drawer_id": "d"}, required=True)

    def test_shadow_append_swallows_errors(self, oplog):
        assert shadow_append(oplog, "not.a.kind", {"x": 1}) is None
        assert shadow_append(None, "drawer.add", {"x": 1}) is None
        assert shadow_append(oplog, "drawer.add", {"x": 1})["kind"] == "drawer.add"


class TestKnowledgeGraphShadow:
    @pytest.fixture
    def kg(self, tmp_dir, oplog):
        graph = KnowledgeGraph(db_path=os.path.join(tmp_dir, "kg.sqlite3"), oplog=oplog)
        yield graph
        graph.close()

    def test_add_triple_emits_assert_once(self, kg, oplog):
        triple_id = kg.add_triple("Igor", "works_on", "MemPalace", author_agent="mac-claude")
        ops = oplog.list_all(kinds=["kg.assert"])
        assert len(ops) == 1
        payload = ops[0]["payload"]
        assert payload["triple_id"] == triple_id
        assert payload["subject_name"] == "Igor"
        assert payload["predicate"] == "works_on"
        assert payload["extracted_at"]
        assert ops[0]["author_agent"] == "mac-claude"
        # Dedup short-circuit: identical open triple changes nothing, emits nothing.
        assert kg.add_triple("Igor", "works_on", "MemPalace") == triple_id
        assert len(oplog.list_all(kinds=["kg.assert"])) == 1

    def test_invalidate_emits_close_with_ids(self, kg, oplog):
        triple_id = kg.add_triple("Igor", "uses", "Chroma")
        kg.invalidate("Igor", "uses", "Chroma", ended="2026-07-02")
        ops = oplog.list_all(kinds=["kg.close"])
        assert len(ops) == 1
        assert ops[0]["payload"]["closed_triple_ids"] == [triple_id]
        assert ops[0]["payload"]["ended"] == "2026-07-02"

    def test_zero_match_invalidate_emits_nothing(self, kg, oplog):
        kg.invalidate("Nobody", "did", "nothing")
        assert oplog.list_all(kinds=["kg.close"]) == []

    def test_add_entity_emits_upsert(self, kg, oplog):
        kg.add_entity("Igor", "person", {"role": "maintainer"})
        ops = oplog.list_all(kinds=["kg.entity.upsert"])
        assert len(ops) == 1
        assert ops[0]["payload"] == {
            "entity_id": "igor",
            "name": "Igor",
            "type": "person",
            "properties": {"role": "maintainer"},
        }

    def test_kg_without_oplog_stays_silent(self, tmp_dir):
        graph = KnowledgeGraph(db_path=os.path.join(tmp_dir, "kg_plain.sqlite3"))
        try:
            graph.add_triple("A", "knows", "B")
            graph.invalidate("A", "knows", "B")
        finally:
            graph.close()

    def test_getters(self, kg):
        triple_id = kg.add_triple("Igor", "works_on", "MemPalace")
        assert kg.get_triple(triple_id)["predicate"] == "works_on"
        assert kg.get_triple("missing") is None
        assert kg.get_entity("igor")["name"] == "Igor"
        assert kg.get_entity("missing") is None


class _FakeCollection:
    """Stand-in for a palace collection: id + parent group reads.

    Returns real ``GetResult`` instances — NOT plain dicts — because that is
    what backend collections return, and a dict-shaped fake hid exactly the
    bug the first production verify run hit (an ``isinstance(result, dict)``
    guard dropping every live result).
    """

    def __init__(self):
        self.rows = {}  # id -> (document, metadata)

    def put(self, doc_id, document, metadata=None):
        self.rows[doc_id] = (document, metadata or {})

    def get(self, ids=None, where=None, include=None):
        from mempalace.backends.base import GetResult

        if ids is not None:
            hits = [(i, *self.rows[i]) for i in ids if i in self.rows]
        elif where:
            key, value = next(iter(where.items()))
            hits = [
                (i, doc, meta) for i, (doc, meta) in self.rows.items() if meta.get(key) == value
            ]
        else:
            hits = [(i, doc, meta) for i, (doc, meta) in self.rows.items()]
        return GetResult(
            ids=[h[0] for h in hits],
            documents=[h[1] for h in hits],
            metadatas=[h[2] for h in hits],
            embeddings=None,
        )


class TestShadowVerify:
    def test_read_logical_drawer_direct_and_chunked(self):
        col = _FakeCollection()
        col.put("d1", "hello")
        col.put("d2_chunk_000001", "world", {"parent_drawer_id": "d2", "chunk_index": 1})
        col.put("d2_chunk_000000", "hello ", {"parent_drawer_id": "d2", "chunk_index": 0})
        assert read_logical_drawer(col, "d1")["content"] == "hello"
        assert read_logical_drawer(col, "d2")["content"] == "hello world"
        assert read_logical_drawer(col, "missing") is None

    def test_fold_last_writer_wins_and_tombstones(self, oplog):
        oplog.append("drawer.add", {"drawer_id": "d1", "content_sha256": _sha("v1")})
        oplog.append("drawer.revise", {"drawer_id": "d1", "content_sha256": _sha("v2")})
        oplog.append("drawer.add", {"drawer_id": "d2", "content_sha256": _sha("gone")})
        oplog.append("drawer.tombstone", {"drawer_id": "d2", "content_sha256": _sha("gone")})
        expected = fold_expected(oplog.list_all())
        assert expected["drawers"]["d1"] == {"content_sha256": _sha("v2"), "tombstoned": False}
        assert expected["drawers"]["d2"]["tombstoned"] is True

    def test_verify_clean_and_divergent(self, palace_path, tmp_dir, oplog):
        kg = KnowledgeGraph(db_path=os.path.join(tmp_dir, "kg.sqlite3"), oplog=oplog)
        col = _FakeCollection()
        try:
            oplog.append("drawer.add", {"drawer_id": "d_ok", "content_sha256": _sha("fine")})
            oplog.append("drawer.add", {"drawer_id": "d_gone", "content_sha256": _sha("x")})
            oplog.append("drawer.add", {"drawer_id": "d_drift", "content_sha256": _sha("a")})
            oplog.append("drawer.add", {"drawer_id": "d_dead", "content_sha256": _sha("rip")})
            oplog.append("drawer.tombstone", {"drawer_id": "d_dead", "content_sha256": _sha("rip")})
            kg.add_triple("Igor", "works_on", "MemPalace")

            col.put("d_ok", "fine")
            col.put("d_drift", "b")  # store disagrees with the op

            report = verify_shadow(palace_path, collection=col, kg=kg)
            assert report["ops"] == 6
            assert report["clean"] is False
            assert report["drawers"]["ok"] == 2  # d_ok + tombstoned-absent d_dead
            assert report["drawers"]["missing"] == ["d_gone"]
            assert report["drawers"]["diverged"] == ["d_drift"]
            assert report["kg"]["ok"] == report["kg"]["checked"] == 1

            col.put("d_gone", "x")
            col.put("d_drift", "a")
            report = verify_shadow(palace_path, collection=col, kg=kg)
            assert report["clean"] is True
        finally:
            kg.close()

    def test_verify_flags_untombstoned_and_closed_kg(self, palace_path, tmp_dir, oplog):
        kg = KnowledgeGraph(db_path=os.path.join(tmp_dir, "kg.sqlite3"), oplog=oplog)
        col = _FakeCollection()
        try:
            oplog.append("drawer.add", {"drawer_id": "d1", "content_sha256": _sha("v")})
            oplog.append("drawer.tombstone", {"drawer_id": "d1", "content_sha256": _sha("v")})
            col.put("d1", "v")  # still present although tombstoned

            triple_id = kg.add_triple("Igor", "uses", "Chroma")
            kg.invalidate("Igor", "uses", "Chroma")
            # Sabotage: reopen the interval behind the op-log's back.
            with kg._lock:
                with kg._conn() as conn:
                    conn.execute("UPDATE triples SET valid_to = NULL WHERE id = ?", (triple_id,))

            report = verify_shadow(palace_path, collection=col, kg=kg)
            assert report["clean"] is False
            assert report["drawers"]["not_tombstoned"] == ["d1"]
            assert report["kg"]["diverged"] == [triple_id]
        finally:
            kg.close()

    def test_verify_empty_oplog_is_clean(self, palace_path):
        report = verify_shadow(palace_path)
        assert report["ops"] == 0
        assert report["clean"] is True
