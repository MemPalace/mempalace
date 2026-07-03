"""
test_oppromote.py — The local-capture promotion pass (RFC 004 step 2a).

Store rows that predate the op-log become drawer.add ops under the
replica's identity: remote-stamped copies and registry sentinels are
excluded, dual-write-era drawers are recognized as already op-carried,
reruns emit nothing, chunk groups reassemble to the verifier's sha256,
and promoted ops fold cleanly on a peer that lacked the content.
"""

import hashlib
import os

import pytest

from mempalace.backends.base import GetResult
from mempalace.oplog import OPLOG_DB_FILENAME, OpLog
from mempalace.opfold import fold_ops
from mempalace.oplog_verify import fold_expected, read_logical_drawer
from mempalace.oppromote import promote_local_rows, scan_logical_drawers


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class _FakeCollection:
    """Store fake with the promotion pass's contract: paged full scans
    (limit/offset) plus get-by-ids, returning real GetResult objects."""

    def __init__(self):
        self.rows = {}  # id -> (document, metadata)

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

    def delete(self, ids):
        for i in ids:
            self.rows.pop(i, None)


@pytest.fixture
def oplog(tmp_path):
    log = OpLog(os.path.join(tmp_path, "palace", OPLOG_DB_FILENAME), replica_id="rep_local001")
    yield log
    log.close()


def _meta(wing="projects", room="day", **extra):
    return {
        "wing": wing,
        "room": room,
        "source_file": extra.pop("source_file", "/src/a.md"),
        "added_by": extra.pop("added_by", "miner"),
        "filed_at": extra.pop("filed_at", "2026-04-18T10:00:00"),
        "id_recipe": extra.pop("id_recipe", "v3"),
        "chunk_index": extra.pop("chunk_index", 0),
        **extra,
    }


class TestScan:
    def test_groups_chunked_rows_under_parent(self):
        col = _FakeCollection()
        col.upsert(
            ids=["d_solo", "d_big_chunk_000000", "d_big_chunk_000001"],
            documents=["solo", "part one ", "part two"],
            metadatas=[
                _meta(),
                _meta(chunk_index=0, parent_drawer_id="d_big"),
                _meta(chunk_index=1, parent_drawer_id="d_big"),
            ],
        )
        scan = scan_logical_drawers(col)
        assert set(scan["drawers"]) == {"d_solo", "d_big"}
        assert scan["drawers"]["d_solo"]["direct_row"] == "d_solo"
        assert [r for _, r in sorted(scan["drawers"]["d_big"]["rows"])] == [
            "d_big_chunk_000000",
            "d_big_chunk_000001",
        ]

    def test_registry_sentinels_and_remote_stamps_flagged(self):
        col = _FakeCollection()
        col.upsert(
            ids=["_reg_abc", "d_remote"],
            documents=["[registry] /f.md", "remote content"],
            metadatas=[
                _meta(room="_registry", ingest_mode="registry"),
                _meta(replica_origin="rep_other0001", op_hlc="0001-000000-rep_other0001"),
            ],
        )
        scan = scan_logical_drawers(col)
        assert scan["registry_skipped"] == 1
        assert "_reg_abc" not in scan["drawers"]
        assert scan["drawers"]["d_remote"]["stamped"] is True

    def test_paged_scan_covers_every_row(self):
        col = _FakeCollection()
        for i in range(7):
            col.upsert(ids=[f"d_{i}"], documents=[f"c{i}"], metadatas=[_meta()])
        scan = scan_logical_drawers(col, batch=3)
        assert scan["rows_scanned"] == 7
        assert len(scan["drawers"]) == 7


class TestPromote:
    def test_promotes_only_unstamped_op_less_drawers(self, oplog):
        col = _FakeCollection()
        col.upsert(
            ids=["d_old", "d_dual", "d_remote", "_reg_x"],
            documents=["april content", "dual-written", "folded copy", "[registry] f"],
            metadatas=[
                _meta(),
                _meta(added_by="mac-claude"),
                _meta(replica_origin="rep_other0001"),
                _meta(room="_registry", ingest_mode="registry"),
            ],
        )
        # d_dual already went through the dual-write path.
        oplog.append(
            "drawer.add",
            {
                "drawer_id": "d_dual",
                "content": "dual-written",
                "content_sha256": _sha("dual-written"),
            },
        )

        stats = promote_local_rows(oplog, col)
        assert stats["promoted"] == 1
        assert stats["already_op_carried"] == 1
        assert stats["remote_skipped"] == 1
        assert stats["registry_skipped"] == 1
        assert stats["errors"] == 0

        ops = oplog.list_all(kinds=["drawer.add"])
        promoted = [op for op in ops if op["payload"].get("promoted")]
        assert len(promoted) == 1
        payload = promoted[0]["payload"]
        assert payload["drawer_id"] == "d_old"
        assert payload["content"] == "april content"
        assert payload["content_sha256"] == _sha("april content")
        assert payload["wing"] == "projects"
        assert payload["filed_at"] == "2026-04-18T10:00:00"
        assert promoted[0]["author_agent"] == "miner"
        assert promoted[0]["origin_replica"] == "rep_local001"

    def test_chunked_drawer_reassembles_to_verifier_sha(self, oplog):
        col = _FakeCollection()
        col.upsert(
            ids=["d_big_chunk_000000", "d_big_chunk_000001"],
            documents=["part one ", "part two"],
            metadatas=[
                _meta(chunk_index=0, parent_drawer_id="d_big"),
                _meta(chunk_index=1, parent_drawer_id="d_big"),
            ],
        )
        stats = promote_local_rows(oplog, col)
        assert stats["promoted"] == 1
        op = oplog.list_all(kinds=["drawer.add"])[0]
        assert op["payload"]["content"] == "part one part two"
        assert op["payload"]["chunks"] == 2
        # The verifier must replay this op to a clean match.
        record = read_logical_drawer(col, "d_big")
        assert _sha(record["content"]) == op["payload"]["content_sha256"]

    def test_rerun_emits_nothing(self, oplog):
        col = _FakeCollection()
        col.upsert(ids=["d_a", "d_b"], documents=["aa", "bb"], metadatas=[_meta(), _meta()])
        first = promote_local_rows(oplog, col)
        assert first["promoted"] == 2
        second = promote_local_rows(oplog, col)
        assert second["promoted"] == 0
        assert second["already_op_carried"] == 2
        assert oplog.count() == 2

    def test_limit_is_resumable(self, oplog):
        col = _FakeCollection()
        for i in range(5):
            col.upsert(ids=[f"d_{i}"], documents=[f"c{i}"], metadatas=[_meta()])
        first = promote_local_rows(oplog, col, limit=2)
        assert first["promoted"] == 2
        assert first["remaining"] == 3
        second = promote_local_rows(oplog, col)
        assert second["promoted"] == 3
        assert oplog.count() == 5

    def test_dry_run_emits_nothing(self, oplog):
        col = _FakeCollection()
        col.upsert(ids=["d_a"], documents=["aa"], metadatas=[_meta()])
        stats = promote_local_rows(oplog, col, dry_run=True)
        assert stats["promoted"] == 1
        assert stats["dry_run"] is True
        assert oplog.count() == 0

    def test_dry_run_reads_no_content(self, oplog):
        # Perf contract: a dry run counts candidates from the scan alone and
        # must NOT issue a get-by-id per drawer — that one-read-per-drawer
        # cost turned a 156k dry run into a 40-minute grind. Here get(ids=)
        # would raise, so any content read fails the test.
        class _NoReadCollection(_FakeCollection):
            def get(self, ids=None, where=None, include=None, limit=None, offset=None):
                if ids is not None:
                    raise AssertionError("dry run must not read content by id")
                return super().get(where=where, include=include, limit=limit, offset=offset)

        col = _NoReadCollection()
        for i in range(50):
            col.upsert(ids=[f"d_{i}"], documents=[f"c{i}"], metadatas=[_meta()])
        stats = promote_local_rows(oplog, col, dry_run=True)
        assert stats["promoted"] == 50
        assert oplog.count() == 0

    def test_empty_content_skipped_not_emitted(self, oplog):
        col = _FakeCollection()
        col.upsert(ids=["d_empty"], documents=[""], metadatas=[_meta()])
        stats = promote_local_rows(oplog, col)
        assert stats["promoted"] == 0
        assert stats["skipped_empty"] == 1
        assert stats["empty_sample"] == ["d_empty"]
        assert oplog.count() == 0

    def test_missing_added_by_falls_back(self, oplog):
        col = _FakeCollection()
        meta = _meta()
        meta.pop("added_by")
        col.upsert(ids=["d_x"], documents=["xx"], metadatas=[meta])
        promote_local_rows(oplog, col)
        assert oplog.list_all()[0]["author_agent"] == "promotion"


class TestPromotedOpsDownstream:
    def test_promoted_ops_fold_on_a_peer_without_the_content(self, tmp_path):
        with (
            OpLog(os.path.join(tmp_path, "a", OPLOG_DB_FILENAME), replica_id="rep_aaaa0001") as a,
            OpLog(os.path.join(tmp_path, "b", OPLOG_DB_FILENAME), replica_id="rep_bbbb0001") as b,
        ):
            col_a = _FakeCollection()
            col_a.upsert(ids=["d_hist"], documents=["april history"], metadatas=[_meta()])
            promote_local_rows(a, col_a)

            for op in a.list_all():
                b.apply_remote_op(op)
            col_b = _FakeCollection()
            stats = fold_ops(b, col_b, kg=None)
            assert stats["applied_adds"] == 1
            record = read_logical_drawer(col_b, "d_hist")
            assert record["content"] == "april history"
            meta = col_b.rows["d_hist"][1]
            assert meta["replica_origin"] == "rep_aaaa0001"

    def test_promoted_ops_skip_existing_on_a_peer_with_the_content(self, tmp_path):
        with (
            OpLog(os.path.join(tmp_path, "a", OPLOG_DB_FILENAME), replica_id="rep_aaaa0001") as a,
            OpLog(os.path.join(tmp_path, "b", OPLOG_DB_FILENAME), replica_id="rep_bbbb0001") as b,
        ):
            col_a = _FakeCollection()
            col_a.upsert(ids=["d_hist"], documents=["april history"], metadatas=[_meta()])
            promote_local_rows(a, col_a)

            # B already holds the content via a step-1 snapshot pull.
            col_b = _FakeCollection()
            col_b.upsert(
                ids=["d_hist"],
                documents=["april history"],
                metadatas=[_meta(replica_origin="rep_aaaa0001")],
            )
            for op in a.list_all():
                b.apply_remote_op(op)
            stats = fold_ops(b, col_b, kg=None)
            assert stats["applied_adds"] == 0
            assert stats["skipped_existing"] == 1

    def test_verifier_expected_state_includes_promoted_drawers(self, oplog):
        col = _FakeCollection()
        col.upsert(ids=["d_v"], documents=["verify me"], metadatas=[_meta()])
        promote_local_rows(oplog, col)
        expected = fold_expected(oplog.list_all())
        assert expected["drawers"]["d_v"] == {
            "content_sha256": _sha("verify me"),
            "tombstoned": False,
        }
