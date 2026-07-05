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

    def test_unversioned_local_drawer_held_as_safety_net(self, rig):
        # Post write-flip the guard is a SAFETY NET: a drawer carrying NO version
        # at all (no replica_origin AND no op_hlc — a pre-flip legacy row or a
        # local write whose op emission failed) has no basis for LWW, so a remote
        # revise/tombstone is held rather than clobbering it blindly.
        rig["col"].rows["d_local"] = ("precious", {"wing": "w", "room": "r"})
        _ship(rig, "drawer.revise", {"drawer_id": "d_local", "content": "overwrite"})
        _ship(rig, "drawer.tombstone", {"drawer_id": "d_local"})
        stats = _fold(rig)
        assert stats["conflicts"] == 2
        assert rig["col"].rows["d_local"][0] == "precious"
        # Held conflicts do not wedge the fold: cursor is past them.
        assert rig["b"].fold_cursor(FOLD_CONSUMER) == rig["b"].count()

    def test_flip_stamped_local_drawer_accepts_newer_remote_revise(self, rig):
        # The write-flip's point: a locally-authored drawer is stamped with
        # op_hlc at author time, so a NEWER cross-replica revise now APPLIES via
        # LWW — exactly what the shadow guard used to block.
        rig["col"].rows["d_local"] = (
            "v1",
            {"wing": "w", "room": "r", "op_hlc": "0000000000001-000000-rep_local"},
        )
        _ship(rig, "drawer.revise", {"drawer_id": "d_local", "content": "v2"})  # real, newer hlc
        stats = _fold(rig)
        assert stats["applied_revises"] == 1 and stats["conflicts"] == 0
        assert rig["col"].rows["d_local"][0] == "v2"

    def test_flip_stamped_local_drawer_rejects_stale_remote_revise(self, rig):
        # A stamped local drawer whose op_hlc is NEWER than the remote revise:
        # the stale remote loses (LWW), content unchanged, not a conflict.
        rig["col"].rows["d_local"] = ("current", {"op_hlc": "9999999999999-000000-rep_local"})
        _ship(rig, "drawer.revise", {"drawer_id": "d_local", "content": "ancient"})
        stats = _fold(rig)
        assert stats["skipped_stale"] == 1 and stats["conflicts"] == 0
        assert rig["col"].rows["d_local"][0] == "current"

    def test_write_flip_remaps_legacy_v3_keyed_op_to_content_hash(self, rig):
        # A legacy v3-keyed drawer.revise (drawer_id is wing/room-shaped, does
        # not match hash(content)) is re-keyed to the content-pure v4 id and
        # applied THERE — not folded as a v3 "ghost" under the stale id.
        from mempalace.ids import make_drawer_id_content_pure

        content = "revised!"  # <= chunk_size (10) so it stays a single row
        v4_id = make_drawer_id_content_pure(content)
        _ship(
            rig,
            "drawer.revise",
            {
                "drawer_id": "drawer_sessions_technical_abc123def456",
                "content": content,
                "content_sha256": _sha(content),
            },
        )
        stats = _fold(rig)
        assert stats["remapped_v3_ids"] == 1
        assert v4_id in rig["col"].rows  # applied under the content-hash id
        assert "drawer_sessions_technical_abc123def456" not in rig["col"].rows  # no ghost
        assert rig["col"].rows[v4_id][0] == content

    def test_write_flip_v4_keyed_op_is_not_remapped(self, rig):
        # The normal path: a v4-keyed op already equals its content hash, so the
        # remap is a no-op — zero behavior change, no counter bump.
        from mempalace.ids import make_drawer_id_content_pure

        content = "v4 keyed"  # <= chunk_size, single row under its own v4 id
        v4_id = make_drawer_id_content_pure(content)
        _ship(
            rig,
            "drawer.add",
            {"drawer_id": v4_id, "content": content, "content_sha256": _sha(content)},
        )
        stats = _fold(rig)
        assert stats["remapped_v3_ids"] == 0
        assert v4_id in rig["col"].rows and rig["col"].rows[v4_id][0] == content

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


class _ScanBanCollection(_FakeCollection):
    """A store that forbids the parent_drawer_id where-scan. get-by-ids is an
    indexed primary-key lookup; a where-filter scans + metadata-decodes every
    row, which drove fold drain to hours on large palaces. The fold must reach
    a chunked drawer's rows through the index alone."""

    def __init__(self):
        super().__init__()
        self.id_gets = 0

    def get(self, ids=None, where=None, include=None):
        if where is not None:
            raise AssertionError(f"fold triggered a full-collection where-scan: {where}")
        if ids is not None:
            self.id_gets += 1
        return super().get(ids=ids, where=where, include=include)


class TestFoldNeverScans:
    """Regression pin for the fold-throughput fix (RFC 004 2a): resolving a
    chunked drawer's presence, provenance, and physical rows must never fall
    back to a parent_drawer_id where-scan — one scan per fold op is O(N) per op
    and collapsed fold to a ~13h drain on a 156k-drawer palace."""

    @pytest.fixture
    def rig(self, tmp_dir):
        paths = {n: os.path.join(tmp_dir, f"palace_{n}") for n in ("a", "b")}
        for p in paths.values():
            os.makedirs(p)
        a = OpLog(db_path=os.path.join(paths["a"], OPLOG_DB_FILENAME))
        b = OpLog(db_path=os.path.join(paths["b"], OPLOG_DB_FILENAME))
        kg = KnowledgeGraph(db_path=os.path.join(paths["b"], "kg.sqlite3"))
        yield {"a": a, "b": b, "kg": kg, "col": _ScanBanCollection(), "paths": paths}
        a.close()
        b.close()
        kg.close()

    def test_chunked_add_revise_tombstone_uses_only_indexed_gets(self, rig):
        # A large (multi-chunk) drawer through its whole lifecycle: add, then a
        # revise that rewrites the chunk layout, then a tombstone. Every step
        # must resolve rows by id — the _ScanBanCollection raises on any where.
        big = "0123456789" * 4  # chunk_size=10 -> 4 chunks
        _ship(rig, "drawer.add", {"drawer_id": "big", "content": big})
        assert _fold(rig)["applied_adds"] == 1
        assert rig["col"].logical_content("big") == big

        _ship(rig, "drawer.revise", {"drawer_id": "big", "content": "0123456789" * 6})
        assert _fold(rig)["applied_revises"] == 1
        assert rig["col"].logical_content("big") == "0123456789" * 6
        # The revise shrank/grew the chunk set; no stale chunk rows survive.
        live = {i for i in rig["col"].rows if i.startswith("big_chunk_")}
        assert live == {_chunk_id_str("big", i) for i in range(6)}

        _ship(rig, "drawer.tombstone", {"drawer_id": "big"})
        assert _fold(rig)["applied_tombstones"] == 1
        assert rig["col"].logical_content("big") is None
        assert not any(i.startswith("big") for i in rig["col"].rows)

    def test_single_row_drawer_resolves_in_one_get(self, rig):
        # A short (unchunked) add-skip is the fold's hot path: presence must
        # cost exactly one indexed get, never a probe for chunks it lacks.
        _ship(rig, "drawer.add", {"drawer_id": "d1", "content": "short"})
        _fold(rig)
        before = rig["col"].id_gets
        # A duplicate add for the same drawer (a fresh op, new op_id) the fold
        # must process and skip as already-present.
        _ship(rig, "drawer.add", {"drawer_id": "d1", "content": "short"})
        stats = _fold(rig)
        assert stats["skipped_existing"] == 1
        assert rig["col"].id_gets - before == 1  # single presence get, no more


def _chunk_id_str(drawer_id: str, index: int) -> str:
    return f"{drawer_id}_chunk_{index:06d}"


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
