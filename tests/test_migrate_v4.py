"""
test_migrate_v4.py — the v4 content-pure id migration planner (RFC 004 id purity).

The planner is read-only: it reassembles logical drawers, computes each one's
content-pure v4 id, and reports the alias map, content-collision groups (drawers
that merge), already-v4 drawers, and the inbound references (KG source_drawer_id,
tunnels) that a rewrite would have to remap.
"""

import json
import os

from mempalace.backends.base import GetResult
from mempalace.ids import make_drawer_id_content_pure
from mempalace.migrate_v4 import (
    apply_v4_migration,
    copy_replica_sidecars,
    plan_v4_migration,
)


class _FakeCollection:
    def __init__(self):
        self.rows = {}  # id -> (doc, meta, emb)
        self.unreadable = set()  # ids whose embedding read raises (index damage)

    def get(self, ids=None, where=None, include=None, limit=None, offset=None):
        if ids is not None:
            hits = [(i, *self.rows[i]) for i in ids if i in self.rows]
        else:
            hits = [(i, d, m, e) for i, (d, m, e) in self.rows.items()]
            if offset:
                hits = hits[offset:]
            if limit is not None:
                hits = hits[:limit]
        want_emb = include is not None and "embeddings" in include
        if want_emb and any(h[0] in self.unreadable for h in hits):
            # ChromaDB fails the whole batch when any row's vector is unfindable.
            raise RuntimeError("Error finding id")
        return GetResult(
            ids=[h[0] for h in hits],
            documents=[h[1] for h in hits],
            metadatas=[h[2] for h in hits],
            embeddings=[h[3] for h in hits] if want_emb else None,
        )

    def upsert(self, ids, documents, metadatas, embeddings=None):
        embeddings = embeddings if embeddings is not None else [None] * len(ids)
        for i, doc, meta, emb in zip(ids, documents, metadatas, embeddings):
            self.rows[i] = (doc, meta, emb)


def test_distinct_content_aliases_every_changing_drawer():
    col = _FakeCollection()
    col.upsert(
        ids=["drawer_w_r_aaa", "drawer_w_r_bbb"],
        documents=["alpha content", "beta content"],
        metadatas=[{"wing": "w", "room": "r"}, {"wing": "w", "room": "r"}],
    )
    plan = plan_v4_migration(col)
    assert plan["logical_drawers"] == 2
    assert plan["changing"] == 2
    assert plan["collision_groups"] == 0
    assert plan["_alias"]["drawer_w_r_aaa"] == make_drawer_id_content_pure("alpha content")


def test_identical_content_collides_into_one_v4_drawer():
    col = _FakeCollection()
    # Same content mined into two wings — two v3 ids, one v4 id (dedup).
    col.upsert(
        ids=["drawer_projects_day_x", "drawer_people_day_y"],
        documents=["the exact same note", "the exact same note"],
        metadatas=[{"wing": "projects"}, {"wing": "people"}],
    )
    plan = plan_v4_migration(col)
    assert plan["collision_groups"] == 1
    assert plan["collision_drawers"] == 2
    v4 = make_drawer_id_content_pure("the exact same note")
    assert sorted(plan["_collisions"][v4]) == ["drawer_people_day_y", "drawer_projects_day_x"]


def test_chunked_drawer_hashes_full_reassembled_content():
    col = _FakeCollection()
    col.upsert(
        ids=["drawer_w_r_big_chunk_000000", "drawer_w_r_big_chunk_000001"],
        documents=["part one ", "part two"],
        metadatas=[
            {"chunk_index": 0, "parent_drawer_id": "drawer_w_r_big"},
            {"chunk_index": 1, "parent_drawer_id": "drawer_w_r_big"},
        ],
    )
    plan = plan_v4_migration(col)
    assert plan["logical_drawers"] == 1
    assert plan["_alias"]["drawer_w_r_big"] == make_drawer_id_content_pure("part one part two")


def test_already_v4_drawer_is_not_changing():
    col = _FakeCollection()
    v4_id = make_drawer_id_content_pure("already migrated body")
    col.upsert(
        ids=[v4_id],
        documents=["already migrated body"],
        metadatas=[{"wing": "w", "id_recipe": "v4"}],
    )
    plan = plan_v4_migration(col)
    assert plan["already_v4"] == 1
    assert plan["changing"] == 0


def test_registry_sentinels_and_empty_drawers_excluded():
    col = _FakeCollection()
    col.upsert(
        ids=["_reg_abc", "drawer_w_r_empty", "drawer_w_r_real"],
        documents=["[registry] /f", "", "real body"],
        metadatas=[{"ingest_mode": "registry"}, {"wing": "w"}, {"wing": "w"}],
    )
    plan = plan_v4_migration(col)
    assert plan["registry_skipped"] == 1
    assert plan["empty_drawers"] == 1
    assert plan["empty_sample"] == ["drawer_w_r_empty"]
    assert plan["changing"] == 1  # only the real drawer


def test_inbound_ref_remap_and_dangling_counts():
    col = _FakeCollection()
    col.upsert(
        ids=["drawer_w_r_src"],
        documents=["source of a triple"],
        metadatas=[{"wing": "w"}],
    )
    plan = plan_v4_migration(
        col,
        kg_source_ids={"drawer_w_r_src", "drawer_gone_deleted"},
        tunnel_drawer_ids={"drawer_w_r_src"},
    )
    # The live ref remaps; the ref to a no-longer-present drawer is dangling.
    assert plan["kg_refs_remapped"] == 1
    assert plan["kg_refs_remapped_sample"] == ["drawer_w_r_src"]
    assert plan["kg_refs_dangling"] == 1
    assert plan["tunnel_refs_remapped"] == 1


def test_paged_scan_covers_all_rows():
    col = _FakeCollection()
    for i in range(7):
        col.upsert(ids=[f"drawer_w_r_{i}"], documents=[f"body {i}"], metadatas=[{"wing": "w"}])
    plan = plan_v4_migration(col, batch=3)
    assert plan["logical_drawers"] == 7
    assert plan["changing"] == 7


class TestApplier:
    def test_single_row_rewrite_copies_vector_and_stamps_v4(self):
        src = _FakeCollection()
        src.upsert(
            ids=["drawer_w_r_x"],
            documents=["migrate me"],
            metadatas=[{"wing": "w", "room": "r", "source_file": "/f"}],
            embeddings=[[0.1, 0.2, 0.3]],
        )
        tgt = _FakeCollection()
        stats = apply_v4_migration(src, tgt)
        v4 = make_drawer_id_content_pure("migrate me")
        assert stats["written"] == 1 and stats["merged_away"] == 0
        assert v4 in tgt.rows
        doc, meta, emb = tgt.rows[v4]
        assert doc == "migrate me"
        assert emb == [0.1, 0.2, 0.3]  # vector copied, not re-derived
        assert meta["id_recipe"] == "v4"
        assert meta["wing"] == "w" and meta["source_file"] == "/f"  # provenance kept as metadata
        assert "drawer_w_r_x" not in tgt.rows  # source unchanged; old id not written

    def test_collision_merges_into_one_v4_drawer(self):
        src = _FakeCollection()
        # Identical content in two wings; latest filed_at wins the placement.
        src.upsert(
            ids=["drawer_projects_d_a"],
            documents=["the same body"],
            metadatas=[{"wing": "projects", "room": "d", "filed_at": "2026-01-01"}],
            embeddings=[[1.0]],
        )
        src.upsert(
            ids=["drawer_people_d_b"],
            documents=["the same body"],
            metadatas=[{"wing": "people", "room": "d", "filed_at": "2026-07-01"}],
            embeddings=[[1.0]],
        )
        tgt = _FakeCollection()
        stats = apply_v4_migration(src, tgt)
        v4 = make_drawer_id_content_pure("the same body")
        assert stats["written"] == 1 and stats["merged_away"] == 1
        assert stats["alias"]["drawer_projects_d_a"] == v4
        assert stats["alias"]["drawer_people_d_b"] == v4
        assert len([k for k in tgt.rows if not k.endswith("_chunk")]) == 1
        # Latest filed_at (people) won the placement.
        assert tgt.rows[v4][1]["wing"] == "people"

    def test_chunked_drawer_rewrites_parent_and_chunk_ids(self):
        src = _FakeCollection()
        src.upsert(
            ids=["drawer_w_r_big_chunk_000000", "drawer_w_r_big_chunk_000001"],
            documents=["part one ", "part two"],
            metadatas=[
                {"chunk_index": 0, "parent_drawer_id": "drawer_w_r_big", "wing": "w"},
                {"chunk_index": 1, "parent_drawer_id": "drawer_w_r_big", "wing": "w"},
            ],
            embeddings=[[0.1], [0.2]],
        )
        tgt = _FakeCollection()
        apply_v4_migration(src, tgt)
        v4 = make_drawer_id_content_pure("part one part two")
        assert f"{v4}_chunk_000000" in tgt.rows
        assert f"{v4}_chunk_000001" in tgt.rows
        c0 = tgt.rows[f"{v4}_chunk_000000"]
        assert c0[0] == "part one " and c0[2] == [0.1]  # content + vector preserved
        assert c0[1]["parent_drawer_id"] == v4 and c0[1]["id_recipe"] == "v4"

    def test_dry_run_writes_nothing_but_reports(self):
        src = _FakeCollection()
        src.upsert(
            ids=["drawer_w_r_a", "drawer_w_r_b"],
            documents=["dup", "dup"],
            metadatas=[{"wing": "w"}, {"wing": "w"}],
            embeddings=[[1.0], [1.0]],
        )
        tgt = _FakeCollection()
        stats = apply_v4_migration(src, tgt, dry_run=True)
        assert stats["written"] == 1 and stats["merged_away"] == 1
        assert tgt.rows == {}  # nothing written on a dry run

    def test_pass_one_reads_no_embeddings_and_writes_are_batched(self):
        # The refactor's whole point: the winner-planning pass must not pull the
        # palace's vectors into RAM, and the write pass must upsert in bounded
        # blocks — not one giant all-vectors call, not one call per drawer.
        class _SpyCollection(_FakeCollection):
            def __init__(self):
                super().__init__()
                self.get_includes = []  # include= for every get() (the scans)
                self.upsert_sizes = []  # row count of every upsert (the writes)

            def get(self, ids=None, where=None, include=None, limit=None, offset=None):
                self.get_includes.append(list(include or []))
                return super().get(
                    ids=ids, where=where, include=include, limit=limit, offset=offset
                )

            def upsert(self, ids, documents, metadatas, embeddings=None):
                self.upsert_sizes.append(len(ids))
                super().upsert(ids, documents, metadatas, embeddings=embeddings)

        src = _SpyCollection()
        for i in range(25):  # 25 distinct single-row drawers, each with a vector
            src.upsert(
                ids=[f"drawer_w_r_{i}"],
                documents=[f"body number {i}"],
                metadatas=[{"wing": "w", "room": "r"}],
                embeddings=[[float(i)]],
            )
        tgt = _SpyCollection()
        stats = apply_v4_migration(src, tgt, batch=100, write_batch=10)

        assert stats["written"] == 25 and stats["rows_written"] == 25
        # Pass 1 scanned content+metadata but NEVER embeddings.
        pass_one = [inc for inc in src.get_includes if "embeddings" not in inc]
        assert pass_one, "expected an embedding-free planning scan"
        assert all("documents" in inc and "metadatas" in inc for inc in pass_one)
        # Pass 2 did read embeddings (to copy them).
        assert any("embeddings" in inc for inc in src.get_includes)
        # Writes are batched at write_batch, not one-per-drawer, not one-giant.
        assert max(tgt.upsert_sizes) <= 10
        assert len(tgt.upsert_sizes) == 3  # 25 rows / 10 -> 10 + 10 + 5

    def test_row_missing_vector_is_written_for_target_to_embed(self):
        # A drawer whose vector didn't come back is still migrated — upserted
        # without an embedding so the target derives one, kept out of the
        # vector-carrying batch.
        src = _FakeCollection()
        src.upsert(
            ids=["drawer_w_r_hasvec"],
            documents=["has a vector"],
            metadatas=[{"wing": "w"}],
            embeddings=[[0.5]],
        )
        src.upsert(
            ids=["drawer_w_r_novec"],
            documents=["missing a vector"],
            metadatas=[{"wing": "w"}],
            embeddings=[None],
        )
        tgt = _FakeCollection()
        stats = apply_v4_migration(src, tgt)
        assert stats["rows_written"] == 2
        v_has = make_drawer_id_content_pure("has a vector")
        v_no = make_drawer_id_content_pure("missing a vector")
        assert tgt.rows[v_has][2] == [0.5]  # vector copied
        assert tgt.rows[v_no][2] is None  # left for the target to embed

    def test_unreadable_vector_is_re_embedded_not_fatal(self):
        # A real six-figure palace can have localized vector-index damage: a row
        # whose metadata is present but whose embedding read raises. The
        # migration must migrate that row without its vector (target re-embeds),
        # count it, and keep going — never crash the whole run.
        src = _FakeCollection()
        src.upsert(
            ids=["drawer_w_r_good"],
            documents=["healthy row"],
            metadatas=[{"wing": "w"}],
            embeddings=[[0.9]],
        )
        src.upsert(
            ids=["diary_w_r_damaged_chunk_000000", "diary_w_r_damaged_chunk_000001"],
            documents=["damaged ", "vector"],
            metadatas=[
                {"parent_drawer_id": "diary_w_r_damaged", "chunk_index": 0, "wing": "w"},
                {"parent_drawer_id": "diary_w_r_damaged", "chunk_index": 1, "wing": "w"},
            ],
            embeddings=[[0.1], [0.2]],
        )
        # One of the damaged drawer's chunks can't have its vector read.
        src.unreadable.add("diary_w_r_damaged_chunk_000001")

        tgt = _FakeCollection()
        stats = apply_v4_migration(src, tgt, write_batch=1000)
        assert stats["vectors_unreadable"] == 1
        assert stats["rows_written"] == 3  # every row still migrated
        v_good = make_drawer_id_content_pure("healthy row")
        v_dmg = make_drawer_id_content_pure("damaged vector")
        assert tgt.rows[v_good][2] == [0.9]  # healthy vector copied
        assert tgt.rows[f"{v_dmg}_chunk_000000"][2] == [0.1]  # readable chunk copied
        assert tgt.rows[f"{v_dmg}_chunk_000001"][2] is None  # damaged chunk re-embeds

    def test_migrated_palace_replans_to_zero_changes(self):
        # Idempotency: applying, then planning the target, shows all v4.
        src = _FakeCollection()
        src.upsert(
            ids=["drawer_w_r_x", "drawer_w_r_y"],
            documents=["one", "two"],
            metadatas=[{"wing": "w", "room": "r"}, {"wing": "w", "room": "r"}],
            embeddings=[[0.1], [0.2]],
        )
        tgt = _FakeCollection()
        apply_v4_migration(src, tgt)
        plan = plan_v4_migration(tgt)
        assert plan["logical_drawers"] == 2
        assert plan["already_v4"] == 2
        assert plan["changing"] == 0


class TestReplicaSidecars:
    """The live-swap contract: the migrated target must be the SAME replica.

    ``copy_replica_sidecars`` carries identity + coordination state
    (replica.json, logstream, peers, embedder config) and deliberately never
    carries the op-log (rebuilt by re-promotion in the coordinated fleet
    window) or the derived vector cache.
    """

    def _source(self, tmp_dir):
        src = os.path.join(tmp_dir, "src_palace")
        os.makedirs(src)
        with open(os.path.join(src, "replica.json"), "w", encoding="utf-8") as f:
            json.dump({"replica_id": "rep_aaaabbbbcccc", "minted_at_note": "test"}, f)
        for name in (
            "logstream.sqlite3",
            "logstream.sqlite3-wal",
            "peers.json",
            "mempalace_embedder.json",
            "oplog.sqlite3",
            "vector_cache.sqlite3",
        ):
            with open(os.path.join(src, name), "w", encoding="utf-8") as f:
                f.write(f"payload of {name}")
        return src

    def test_carries_identity_and_state_never_oplog_or_cache(self, tmp_dir):
        src = self._source(tmp_dir)
        tgt = os.path.join(tmp_dir, "tgt_palace")
        result = copy_replica_sidecars(src, tgt)

        assert result["replica_id"] == "rep_aaaabbbbcccc"
        assert "replica.json" in result["carried"]
        assert "logstream.sqlite3" in result["carried"]
        assert "peers.json" in result["carried"]
        assert "mempalace_embedder.json" in result["carried"]
        # The swap-ready target is the same replica with its state...
        with open(os.path.join(tgt, "replica.json"), encoding="utf-8") as f:
            assert json.load(f)["replica_id"] == "rep_aaaabbbbcccc"
        with open(os.path.join(tgt, "logstream.sqlite3"), encoding="utf-8") as f:
            assert f.read() == "payload of logstream.sqlite3"
        # ...but NEVER inherits the old op-log or the derived cache.
        assert not os.path.exists(os.path.join(tgt, "oplog.sqlite3"))
        assert not os.path.exists(os.path.join(tgt, "vector_cache.sqlite3"))
        # And the SOURCE is untouched (copy-first): its op-log is still there.
        assert os.path.exists(os.path.join(src, "oplog.sqlite3"))

    def test_missing_sidecars_tolerated_and_reported(self, tmp_dir):
        # A validation copy may lack most sidecars; only report, never fail.
        src = os.path.join(tmp_dir, "bare_palace")
        os.makedirs(src)
        tgt = os.path.join(tmp_dir, "bare_target")
        result = copy_replica_sidecars(src, tgt)
        assert result["carried"] == []
        assert result["replica_id"] is None
        assert "replica.json" in result["missing"]
