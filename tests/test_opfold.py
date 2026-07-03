"""
test_opfold.py — The fold consumer (RFC 004 step 2a).

Remote ops → local store: insert-if-absent adds with provenance stamps,
LWW-by-HLC revises and tombstones, the shadow guard protecting
locally-authored drawers, KG merge appliers, cursor durability, and the
end-to-end path through opsync (author on A → sync to B → fold on B).
"""

import hashlib
import os

import pytest

from mempalace.backends.base import GetResult
from mempalace.knowledge_graph import KnowledgeGraph
from mempalace.oplog import OPLOG_DB_FILENAME, OpLog
from mempalace.opfold import FOLD_CONSUMER, fold_ops
from mempalace.opsync import sync_memops_with_peer


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class _FakeCollection:
    """Store fake with the fold's contract: get by ids / parent-group
    where-filter, upsert, delete. Returns real GetResult objects."""

    def __init__(self):
        self.rows = {}  # id -> (document, metadata)

    def get(self, ids=None, where=None, include=None):
        if ids is not None:
            hits = [(i, *self.rows[i]) for i in ids if i in self.rows]
        elif where:
            key, value = next(iter(where.items()))
            hits = [(i, d, m) for i, (d, m) in self.rows.items() if (m or {}).get(key) == value]
        else:
            hits = [(i, d, m) for i, (d, m) in self.rows.items()]
        return GetResult(
            ids=[h[0] for h in hits],
            documents=[h[1] for h in hits],
            metadatas=[h[2] for h in hits],
            embeddings=None,
        )

    def upsert(self, ids, documents, metadatas):
        for i, doc, meta in zip(ids, documents, metadatas):
            self.rows[i] = (doc, meta)

    def delete(self, ids):
        for i in ids:
            self.rows.pop(i, None)

    def logical_content(self, drawer_id):
        if drawer_id in self.rows:
            return self.rows[drawer_id][0]
        chunks = sorted(
            (
                (meta.get("chunk_index", 0), doc)
                for doc, meta in (
                    self.rows[i] for i in self.rows if i.startswith(f"{drawer_id}_chunk_")
                )
            ),
        )
        return "".join(doc for _, doc in chunks) if chunks else None


@pytest.fixture
def rig(tmp_dir):
    """Two op-logs (a=remote author, b=local folder), b's store + KG."""
    paths = {n: os.path.join(tmp_dir, f"palace_{n}") for n in ("a", "b")}
    for p in paths.values():
        os.makedirs(p)
    a = OpLog(db_path=os.path.join(paths["a"], OPLOG_DB_FILENAME))
    b = OpLog(db_path=os.path.join(paths["b"], OPLOG_DB_FILENAME))
    kg = KnowledgeGraph(db_path=os.path.join(paths["b"], "kg.sqlite3"))
    yield {"a": a, "b": b, "kg": kg, "col": _FakeCollection(), "paths": paths}
    a.close()
    b.close()
    kg.close()


def _ship(rig, kind, payload, author="windows-claude"):
    """Author an op on A and apply it remotely into B's op-log."""
    op = rig["a"].append(kind, payload, author_agent=author)
    assert rig["b"].apply_remote_op(op)
    return op


def _fold(rig, **kwargs):
    return fold_ops(rig["b"], rig["col"], rig["kg"], chunk_size=10, **kwargs)


class TestDrawerFold:
    def test_add_folds_with_provenance_stamps(self, rig):
        op = _ship(
            rig,
            "drawer.add",
            {
                "drawer_id": "d1",
                "wing": "w",
                "room": "r",
                "content": "short",
                "content_sha256": _sha("short"),
                "filed_at": "2026-07-03T00:00:00",
            },
        )
        stats = _fold(rig)
        assert stats["applied_adds"] == 1 and stats["errors"] == 0
        doc, meta = rig["col"].rows["d1"]
        assert doc == "short"
        assert meta["replica_origin"] == rig["a"].replica_id
        assert meta["op_hlc"] == op["hlc"]
        assert meta["added_by"] == "windows-claude"
        assert meta["wing"] == "w" and meta["room"] == "r"

    def test_add_chunks_oversized_content(self, rig):
        content = "0123456789" * 3  # chunk_size=10 -> 3 chunks
        _ship(rig, "drawer.add", {"drawer_id": "big", "content": content})
        _fold(rig)
        assert "big" not in rig["col"].rows
        assert rig["col"].logical_content("big") == content
        assert rig["col"].rows["big_chunk_000000"][1]["parent_drawer_id"] == "big"

    def test_own_origin_ops_are_skipped(self, rig):
        rig["b"].append("drawer.add", {"drawer_id": "mine", "content": "local"})
        stats = _fold(rig)
        assert stats["skipped_own"] == 1
        assert rig["col"].rows == {}
        # Cursor advanced past it — the next fold round is a no-op.
        assert rig["b"].fold_cursor(FOLD_CONSUMER) == rig["b"].count()

    def test_max_ops_bounds_the_drain_and_resumes(self, rig):
        # The hub sync loop folds in bounded batches so a large backlog can't
        # monopolize the request lock. fold_ops(max_ops=N) must process at
        # most N ops, advance the durable cursor, and resume where it left
        # off — draining the same final state as one unbounded fold.
        for i in range(5):
            _ship(rig, "drawer.add", {"drawer_id": f"d{i}", "content": f"c{i}"})
        rounds = 0
        while rig["b"].fold_cursor(FOLD_CONSUMER) < rig["b"].count():
            before = rig["b"].fold_cursor(FOLD_CONSUMER)
            _fold(rig, max_ops=2)
            after = rig["b"].fold_cursor(FOLD_CONSUMER)
            assert 0 < after - before <= 2  # bounded, and always progresses
            rounds += 1
            assert rounds <= 5  # 5 ops / 2 per round -> 3 rounds, never spins
        assert rounds == 3
        assert {f"d{i}" for i in range(5)} <= set(rig["col"].rows)

    def test_refold_is_idempotent(self, rig):
        _ship(rig, "drawer.add", {"drawer_id": "d1", "content": "x"})
        assert _fold(rig)["applied_adds"] == 1
        again = _fold(rig)
        assert again["applied_adds"] == 0 and again["skipped_existing"] == 0

    def test_revise_lww_by_hlc(self, rig):
        _ship(rig, "drawer.add", {"drawer_id": "d1", "content": "v1"})
        v2 = _ship(rig, "drawer.revise", {"drawer_id": "d1", "content": "v2"})
        _fold(rig)
        assert rig["col"].rows["d1"][0] == "v2"
        assert rig["col"].rows["d1"][1]["op_hlc"] == v2["hlc"]

        # A stale revise (older hlc, arriving late through another path)
        # must not clobber the newer state.
        stale = dict(v2)
        stale["op_id"] = "op_rep_cccccccccccc_9"
        stale["origin_replica"] = "rep_cccccccccccc"
        stale["origin_seq"] = 9
        stale["hlc"] = "0000000000001-000000-rep_cccccccccccc"
        stale["payload"] = {"drawer_id": "d1", "content": "ancient"}
        assert rig["b"].apply_remote_op(stale)
        stats = _fold(rig)
        assert stats["skipped_stale"] == 1
        assert rig["col"].rows["d1"][0] == "v2"

    def test_revise_replaces_chunk_layout(self, rig):
        _ship(rig, "drawer.add", {"drawer_id": "d1", "content": "0123456789" * 2})
        _fold(rig)
        assert rig["col"].logical_content("d1") == "0123456789" * 2
        _ship(rig, "drawer.revise", {"drawer_id": "d1", "content": "tiny"})
        _fold(rig)
        assert rig["col"].rows["d1"][0] == "tiny"
        assert rig["col"].logical_content("d1") == "tiny"
        assert not any(i.startswith("d1_chunk_") for i in rig["col"].rows)

    def test_tombstone_deletes_folded_copy(self, rig):
        _ship(rig, "drawer.add", {"drawer_id": "d1", "content": "0123456789" * 2})
        _fold(rig)
        _ship(rig, "drawer.tombstone", {"drawer_id": "d1"})
        stats = _fold(rig)
        assert stats["applied_tombstones"] == 1
        assert rig["col"].logical_content("d1") is None

    def test_shadow_guard_holds_local_drawers(self, rig):
        # A locally-authored (unstamped) drawer must never be mutated by a
        # remote op during the shadow.
        rig["col"].rows["d_local"] = ("precious", {"wing": "w", "room": "r"})
        _ship(rig, "drawer.revise", {"drawer_id": "d_local", "content": "overwrite"})
        _ship(rig, "drawer.tombstone", {"drawer_id": "d_local"})
        stats = _fold(rig)
        assert stats["conflicts"] == 2
        assert rig["col"].rows["d_local"][0] == "precious"
        # Held conflicts do not wedge the fold: cursor is past them.
        assert rig["b"].fold_cursor(FOLD_CONSUMER) == rig["b"].count()

    def test_sha_mismatch_stops_fold_for_retry(self, rig):
        _ship(
            rig,
            "drawer.add",
            {"drawer_id": "bad", "content": "actual", "content_sha256": _sha("claimed")},
        )
        _ship(rig, "drawer.add", {"drawer_id": "after", "content": "later"})
        stats = _fold(rig)
        assert stats["errors"] == 1
        assert "bad" not in rig["col"].rows
        assert "after" not in rig["col"].rows  # fold stopped AT the error
        assert rig["b"].fold_cursor(FOLD_CONSUMER) < rig["b"].count()


class TestKgFold:
    def test_assert_close_entity_flow(self, rig):
        _ship(
            rig,
            "kg.assert",
            {
                "triple_id": "t1",
                "subject": "igor",
                "subject_name": "Igor",
                "predicate": "works_on",
                "object": "mempalace",
                "object_name": "MemPalace",
                "valid_from": "2026-07-01",
                "valid_to": None,
                "confidence": 1.0,
                "extracted_at": "2026-07-03 00:00:00",
            },
        )
        _ship(
            rig,
            "kg.close",
            {
                "subject": "igor",
                "predicate": "works_on",
                "object": "mempalace",
                "ended": "2026-07-02",
                "closed_triple_ids": ["t1"],
            },
        )
        _ship(rig, "kg.entity.upsert", {"entity_id": "igor", "name": "Igor", "type": "person"})
        stats = _fold(rig)
        assert stats["applied_kg"] == 3 and stats["errors"] == 0
        triple = rig["kg"].get_triple("t1")
        assert triple["valid_to"] == "2026-07-02"
        assert rig["kg"].get_entity("igor")["type"] == "person"
        assert rig["kg"].get_entity("mempalace")["name"] == "MemPalace"

    def test_close_min_wins_and_assert_gset(self, rig):
        payload = {
            "triple_id": "t1",
            "subject": "a",
            "subject_name": "A",
            "predicate": "p",
            "object": "b",
            "object_name": "B",
            "valid_to": None,
        }
        _ship(rig, "kg.assert", payload)
        _fold(rig)
        # Re-assert of an existing id is a no-op (G-set).
        assert rig["kg"].apply_assert_op(payload) is False
        # Later close, then an EARLIER close: min valid_to wins.
        assert rig["kg"].apply_close_op({"ended": "2026-07-05", "closed_triple_ids": ["t1"]}) == 1
        assert rig["kg"].apply_close_op({"ended": "2026-07-01", "closed_triple_ids": ["t1"]}) == 1
        assert rig["kg"].apply_close_op({"ended": "2026-07-04", "closed_triple_ids": ["t1"]}) == 0
        assert rig["kg"].get_triple("t1")["valid_to"] == "2026-07-01"


class TestEndToEnd:
    def test_author_sync_fold_roundtrip(self, rig):
        from tests.test_opsync import _OpLogTransport

        content = "the fact travels as an op"
        rig["a"].append(
            "drawer.add",
            {"drawer_id": "d_e2e", "content": content, "content_sha256": _sha(content)},
            author_agent="windows-claude",
        )
        sync_memops_with_peer(
            rig["b"], _OpLogTransport({"a": rig["a"]}), {"name": "a", "url": "fake://a"}
        )
        stats = _fold(rig)
        assert stats["applied_adds"] == 1
        # 25 chars at chunk_size=10 folds as a chunk group — read logically.
        assert rig["col"].logical_content("d_e2e") == content
        meta = rig["col"].rows["d_e2e_chunk_000000"][1]
        assert meta["replica_origin"] == rig["a"].replica_id
        assert meta["added_by"] == "windows-claude"

    def test_cursor_survives_reopen(self, rig, tmp_dir):
        _ship(rig, "drawer.add", {"drawer_id": "d1", "content": "x"})
        _fold(rig)
        cursor = rig["b"].fold_cursor(FOLD_CONSUMER)
        assert cursor == rig["b"].count()
        db_path = rig["b"].db_path
        rig["b"].close()
        reopened = OpLog(db_path=db_path)
        try:
            assert reopened.fold_cursor(FOLD_CONSUMER) == cursor
        finally:
            reopened.close()
