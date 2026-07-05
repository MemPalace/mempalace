"""
test_op_emit.py — shared op emission for the ingest + bulk-delete paths (RFC 004 2a).

Covers emit_miner_writes (revise per chunk, tombstone dropped chunks),
emit_bulk_tombstones, and read_logical_drawers_where (chunk-group rejoin).
"""

import hashlib
import os

import pytest

from mempalace.backends.base import GetResult
from mempalace.oplog import OPLOG_DB_FILENAME, OpLog
from mempalace.op_emit import (
    emit_bulk_tombstones,
    emit_miner_writes,
    read_logical_drawers_where,
    stamp_op_hlc,
)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class _FakeCollection:
    def __init__(self):
        self.rows = {}

    def get(self, ids=None, where=None, include=None, limit=None, offset=None):
        if ids is not None:
            hits = [(i, *self.rows[i]) for i in ids if i in self.rows]
        elif where:
            key, value = next(iter(where.items()))
            hits = [(i, d, m) for i, (d, m) in self.rows.items() if (m or {}).get(key) == value]
        else:
            hits = [(i, d, m) for i, (d, m) in self.rows.items()]
        if offset:
            hits = hits[offset:]
        if limit is not None:
            hits = hits[:limit]
        return GetResult(
            ids=[h[0] for h in hits],
            documents=[h[1] for h in hits],
            metadatas=[h[2] for h in hits],
            embeddings=None,
        )

    def upsert(self, ids, documents, metadatas):
        for i, doc, meta in zip(ids, documents, metadatas):
            self.rows[i] = (doc, meta)

    def update(self, *, ids, documents=None, metadatas=None, embeddings=None):
        # Metadata-merge, like the real backend (no re-embed).
        for i, m in zip(ids, metadatas or []):
            if i in self.rows:
                doc, meta = self.rows[i]
                self.rows[i] = (doc, {**(meta or {}), **(m or {})})

    def delete(self, ids=None, where=None):
        for i in ids or []:
            self.rows.pop(i, None)


@pytest.fixture
def oplog(tmp_path):
    log = OpLog(os.path.join(tmp_path, "palace", OPLOG_DB_FILENAME), replica_id="rep_local001")
    yield log
    log.close()


class TestMinerWrites:
    def test_fresh_mine_emits_revise_per_chunk(self, oplog):
        new = [
            ("d_0", "chunk zero", {"wing": "w", "room": "r", "filed_at": "2026-07-03"}),
            ("d_1", "chunk one", {"wing": "w", "room": "r", "filed_at": "2026-07-03"}),
        ]
        emit_miner_writes(oplog, "/src/f.md", {}, new, author_agent="miner")
        ops = oplog.list_all()
        assert [o["kind"] for o in ops] == ["drawer.revise", "drawer.revise"]
        assert {o["payload"]["drawer_id"] for o in ops} == {"d_0", "d_1"}
        assert ops[0]["payload"]["content_sha256"] == _sha("chunk zero")
        assert ops[0]["author_agent"] == "miner"

    def test_remine_shrink_tombstones_dropped_chunks(self, oplog):
        prior = {
            "d_0": ("old zero", {"wing": "w", "room": "r"}),
            "d_1": ("old one", {"wing": "w", "room": "r"}),
            "d_2": ("old two", {"wing": "w", "room": "r"}),
        }
        # Re-mine now produces only 2 chunks — d_2 must be tombstoned.
        new = [
            ("d_0", "new zero", {"wing": "w", "room": "r"}),
            ("d_1", "new one", {"wing": "w", "room": "r"}),
        ]
        emit_miner_writes(oplog, "/src/f.md", prior, new, author_agent="miner")
        by_kind = {}
        for o in oplog.list_all():
            by_kind.setdefault(o["kind"], []).append(o["payload"]["drawer_id"])
        assert sorted(by_kind["drawer.revise"]) == ["d_0", "d_1"]
        assert by_kind["drawer.tombstone"] == ["d_2"]
        tomb = next(o for o in oplog.list_all() if o["kind"] == "drawer.tombstone")
        assert tomb["payload"]["content"] == "old two"  # verbatim content preserved

    def test_none_oplog_is_a_noop(self):
        # Never raises when emission is disabled.
        emit_miner_writes(None, "/src/f.md", {"d": ("x", {})}, [("d2", "y", {})])

    def test_empty_content_chunk_skipped(self, oplog):
        emit_miner_writes(oplog, "/src/f.md", {}, [("d_0", "", {}), ("d_1", "real", {})])
        ops = oplog.list_all()
        assert [o["payload"]["drawer_id"] for o in ops] == ["d_1"]


class TestBulkTombstones:
    def test_emits_one_tombstone_per_drawer(self, oplog):
        drawers = [
            ("d_a", "content a", {"wing": "w", "room": "r"}),
            ("d_b", "content b", {"wing": "w", "room": "r"}),
        ]
        n = emit_bulk_tombstones(oplog, drawers, author_agent="mac-claude", reason="source_purge")
        assert n == 2
        ops = oplog.list_all()
        assert all(o["kind"] == "drawer.tombstone" for o in ops)
        assert {o["payload"]["drawer_id"] for o in ops} == {"d_a", "d_b"}
        assert ops[0]["payload"]["reason"] == "source_purge"
        assert ops[0]["payload"]["content_sha256"] == _sha("content a")

    def test_none_oplog_returns_zero(self):
        assert emit_bulk_tombstones(None, [("d", "x", {})]) == 0


class TestDedupEmitsTombstones:
    def test_dedup_delete_emits_tombstone(self, oplog):
        from mempalace.dedup import dedup_source_group

        col = _FakeCollection()
        # A too-short drawer (< 20 chars) is deleted outright — no query needed.
        col.upsert(
            ids=["keep", "drop"],
            documents=["a long enough drawer body to be kept as canonical", "tiny"],
            metadatas=[{"wing": "w", "room": "r"}, {"wing": "w", "room": "r"}],
        )
        kept, deleted = dedup_source_group(col, ["keep", "drop"], dry_run=False, oplog=oplog)
        assert deleted == ["drop"] and "drop" not in col.rows
        ops = oplog.list_all()
        assert [o["kind"] for o in ops] == ["drawer.tombstone"]
        assert ops[0]["payload"]["drawer_id"] == "drop"
        assert ops[0]["payload"]["content"] == "tiny"
        assert ops[0]["payload"]["reason"] == "dedup"
        assert ops[0]["author_agent"] == "dedup"

    def test_dedup_dry_run_emits_nothing(self, oplog):
        from mempalace.dedup import dedup_source_group

        col = _FakeCollection()
        col.upsert(
            ids=["keep", "drop"],
            documents=["a long enough drawer body to be kept as canonical", "tiny"],
            metadatas=[{"wing": "w"}, {"wing": "w"}],
        )
        dedup_source_group(col, ["keep", "drop"], dry_run=True, oplog=oplog)
        assert oplog.count() == 0
        assert "drop" in col.rows  # dry run deletes nothing


class TestReadLogicalDrawersWhere:
    def test_rejoins_chunk_groups(self):
        col = _FakeCollection()
        col.upsert(
            ids=["d_solo", "d_big_chunk_000000", "d_big_chunk_000001"],
            documents=["solo body", "part one ", "part two"],
            metadatas=[
                {"source_file": "/f", "wing": "w", "room": "r"},
                {"source_file": "/f", "chunk_index": 0, "parent_drawer_id": "d_big"},
                {"source_file": "/f", "chunk_index": 1, "parent_drawer_id": "d_big"},
            ],
        )
        got = dict(
            (d_id, content)
            for d_id, content, _m in read_logical_drawers_where(col, {"source_file": "/f"})
        )
        assert got == {"d_solo": "solo body", "d_big": "part one part two"}

    def test_end_to_end_bulk_delete_tombstones_fold_on_peer(self, tmp_path):
        # A bulk-delete's tombstones, folded on a peer, remove the peer's copy.
        from mempalace.opfold import fold_ops

        col = _FakeCollection()
        col.upsert(
            ids=["d_x_chunk_000000", "d_x_chunk_000001"],
            documents=["hello ", "world"],
            metadatas=[
                {"source_file": "/junk", "chunk_index": 0, "parent_drawer_id": "d_x"},
                {"source_file": "/junk", "chunk_index": 1, "parent_drawer_id": "d_x"},
            ],
        )
        with (
            OpLog(os.path.join(tmp_path, "a", OPLOG_DB_FILENAME), replica_id="rep_aaaa0001") as a,
            OpLog(os.path.join(tmp_path, "b", OPLOG_DB_FILENAME), replica_id="rep_bbbb0001") as b,
        ):
            drawers = read_logical_drawers_where(col, {"source_file": "/junk"})
            assert drawers == [("d_x", "hello world", drawers[0][2])]
            emit_bulk_tombstones(a, drawers, author_agent="mac-claude", reason="source_purge")
            for op in a.list_all():
                b.apply_remote_op(op)
            # Peer already holds the drawer (stamped remote copy); the tombstone deletes it.
            peer_col = _FakeCollection()
            peer_col.upsert(
                ids=["d_x"],
                documents=["hello world"],
                metadatas=[{"replica_origin": "rep_aaaa0001", "op_hlc": "0-0-x"}],
            )
            stats = fold_ops(b, peer_col, kg=None)
            assert stats["applied_tombstones"] == 1
            assert "d_x" not in peer_col.rows


class TestStampOpHlc:
    """The write-flip stamps op_hlc (the LWW head) on locally-authored drawers
    so a later cross-replica revise resolves by HLC instead of being held."""

    def test_stamp_adds_op_hlc_only_not_replica_origin(self):
        col = _FakeCollection()
        col.rows["d1"] = ("body", {"wing": "w", "room": "r"})
        stamp_op_hlc(col, ["d1"], {"hlc": "1783250000000-000000-rep_local001"})
        _doc, meta = col.rows["d1"]
        assert meta["op_hlc"] == "1783250000000-000000-rep_local001"
        # replica_origin stays ABSENT — "no stamp = locally authored" is
        # load-bearing in vector_cache / oppromote / replica_sync.
        assert "replica_origin" not in meta
        assert meta["wing"] == "w"  # existing metadata preserved (merge, not replace)

    def test_stamp_all_chunk_rows(self):
        col = _FakeCollection()
        col.rows["d_chunk_000000"] = ("a", {"parent_drawer_id": "d"})
        col.rows["d_chunk_000001"] = ("b", {"parent_drawer_id": "d"})
        stamp_op_hlc(col, ["d_chunk_000000", "d_chunk_000001"], {"hlc": "h1"})
        assert col.rows["d_chunk_000000"][1]["op_hlc"] == "h1"
        assert col.rows["d_chunk_000001"][1]["op_hlc"] == "h1"

    def test_stamp_noops_on_missing_op_or_ids(self):
        col = _FakeCollection()
        col.rows["d1"] = ("body", {})
        stamp_op_hlc(col, ["d1"], None)  # emit failed -> op is None
        stamp_op_hlc(col, ["d1"], {})  # no hlc
        stamp_op_hlc(col, [], {"hlc": "h"})  # no ids
        assert "op_hlc" not in col.rows["d1"][1]

    def test_emit_miner_writes_returns_ops_for_stamping(self, oplog):
        new = [("d_0", "one", {"wing": "w"}), ("d_1", "two", {"wing": "w"})]
        stamped = emit_miner_writes(oplog, "/src/f.md", {}, new, author_agent="miner")
        assert {did for did, _op in stamped} == {"d_0", "d_1"}
        assert all(op.get("hlc") for _did, op in stamped)
        # And stamping through them versions every mined drawer.
        col = _FakeCollection()
        col.rows["d_0"] = ("one", {"wing": "w"})
        col.rows["d_1"] = ("two", {"wing": "w"})
        for did, op in stamped:
            stamp_op_hlc(col, [did], op)
        assert col.rows["d_0"][1]["op_hlc"] and col.rows["d_1"][1]["op_hlc"]
