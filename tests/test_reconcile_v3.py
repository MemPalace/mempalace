"""
test_reconcile_v3.py — RFC 004 write-flip PART B: draining legacy v3-keyed
"ghost" drawers to their content-hash v4 id in place.

Ghosts (id_recipe != 'v4') are REWRITTEN to drawer_<hash(content)>, never
deleted (they are the sole copy of their content) — unless a v4 twin already
holds that content, in which case the ghost is dropped as a duplicate.
"""

from mempalace.backends.base import GetResult
from mempalace.ids import make_drawer_id_content_pure
from mempalace.reconcile_v3 import apply_v3_reconcile, plan_v3_reconcile


class _FakeCollection:
    """Honors get-by-ids, paged get, upsert (with embeddings), delete, and the
    default-style rekey_row (reinsert under new id, delete old)."""

    def __init__(self):
        self.rows = {}  # id -> (doc, meta, emb)

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
        return GetResult(
            ids=[h[0] for h in hits],
            documents=[h[1] for h in hits],
            metadatas=[h[2] for h in hits],
            embeddings=[h[3] for h in hits] if want_emb else None,
        )

    def upsert(self, ids, documents, metadatas=None, embeddings=None):
        embs = embeddings if embeddings is not None else [None] * len(ids)
        metas = metadatas if metadatas is not None else [{}] * len(ids)
        for i, d, m, e in zip(ids, documents, metas, embs):
            self.rows[i] = (d, m, e)

    def delete(self, ids=None, where=None):
        for i in ids or []:
            self.rows.pop(i, None)

    def rekey_row(self, old_id, new_id, *, content, metadata, embedding=None):
        # Mirror BaseCollection's default: reinsert under new id, delete old.
        if new_id == old_id:
            return
        self.upsert(ids=[new_id], documents=[content], metadatas=[metadata], embeddings=[embedding])
        self.delete(ids=[old_id])


def _ghost(col, drawer_id, content, emb=None, **extra_meta):
    col.rows[drawer_id] = (content, {"id_recipe": "v3", "wing": "w", **extra_meta}, emb)


def test_plan_counts_rewrite_and_drop():
    col = _FakeCollection()
    _ghost(col, "drawer_sessions_technical_aaa", "sole copy content")  # -> rewrite
    dup = "already stored under v4"
    _ghost(col, "drawer_sessions_technical_bbb", dup)  # -> drop (twin below)
    v4 = make_drawer_id_content_pure(dup)
    col.rows[v4] = (dup, {"id_recipe": "v4"}, None)  # the v4 twin
    plan = plan_v3_reconcile(col)
    assert plan["ghost_drawers"] == 2
    assert plan["rewrite"] == 1 and plan["drop"] == 1


def test_plan_dedups_duplicate_content_ghosts_matching_apply():
    # Two ghosts with IDENTICAL content, no pre-existing v4 twin. The sequential
    # drain rewrites the first and drops the second (twin now present). The plan
    # must project the same 1 rewrite / 1 drop, not 2 rewrites.
    col = _FakeCollection()
    dup = "same revised content under two v3 ids"
    _ghost(col, "drawer_sessions_technical_g1", dup)
    _ghost(col, "drawer_sessions_technical_g2", dup)
    plan = plan_v3_reconcile(col)
    assert plan["ghost_drawers"] == 2
    assert plan["rewrite"] == 1 and plan["drop"] == 1
    stats = apply_v3_reconcile(col)  # apply agrees with the projection
    assert stats["rewritten"] == 1 and stats["dropped"] == 1


def test_apply_rewrites_single_row_ghost_to_content_hash():
    col = _FakeCollection()
    content = "revised note that only exists under a v3 id"
    _ghost(col, "drawer_sessions_technical_ccc", content, emb=[0.4, 0.5])
    stats = apply_v3_reconcile(col)
    assert stats["rewritten"] == 1 and stats["dropped"] == 0
    v4 = make_drawer_id_content_pure(content)
    assert v4 in col.rows  # rewritten under content-hash id
    assert "drawer_sessions_technical_ccc" not in col.rows  # ghost gone
    doc, meta, emb = col.rows[v4]
    assert doc == content and meta["id_recipe"] == "v4" and emb == [0.4, 0.5]  # embedding carried


def test_apply_drops_ghost_when_v4_twin_present():
    col = _FakeCollection()
    content = "content that already lives under its v4 id"
    v4 = make_drawer_id_content_pure(content)
    col.rows[v4] = (content, {"id_recipe": "v4"}, [1.0])  # the v4 twin
    _ghost(col, "drawer_sessions_technical_ddd", content)  # a stale v3 dup
    stats = apply_v3_reconcile(col)
    assert stats["dropped"] == 1 and stats["rewritten"] == 0
    assert "drawer_sessions_technical_ddd" not in col.rows  # ghost dropped
    assert col.rows[v4][0] == content  # v4 twin untouched


def test_apply_rewrites_chunked_ghost_preserving_chunks():
    col = _FakeCollection()
    parent = "drawer_sessions_technical_eee"
    _ghost(
        col,
        f"{parent}_chunk_000000",
        "part one ",
        emb=[0.1],
        parent_drawer_id=parent,
        chunk_index=0,
    )
    _ghost(
        col, f"{parent}_chunk_000001", "part two", emb=[0.2], parent_drawer_id=parent, chunk_index=1
    )
    apply_v3_reconcile(col)
    v4 = make_drawer_id_content_pure("part one part two")
    assert f"{v4}_chunk_000000" in col.rows and f"{v4}_chunk_000001" in col.rows
    assert not any(k.startswith(parent) for k in col.rows)  # old chunks gone
    c0 = col.rows[f"{v4}_chunk_000000"]
    assert c0[0] == "part one " and c0[2] == [0.1]  # content + embedding preserved
    assert c0[1]["parent_drawer_id"] == v4 and c0[1]["id_recipe"] == "v4"


def test_v4_rows_are_never_touched():
    col = _FakeCollection()
    content = "a normal v4 drawer"
    v4 = make_drawer_id_content_pure(content)
    col.rows[v4] = (content, {"id_recipe": "v4", "wing": "w"}, [0.9])
    stats = apply_v3_reconcile(col)
    assert stats["ghost_drawers"] == 0 and stats["rewritten"] == 0
    assert col.rows[v4] == (content, {"id_recipe": "v4", "wing": "w"}, [0.9])


def test_apply_is_idempotent_and_dry_run_writes_nothing():
    col = _FakeCollection()
    content = "idempotent reconcile content"
    _ghost(col, "drawer_sessions_technical_fff", content, emb=[0.3])
    dry = apply_v3_reconcile(col, dry_run=True)
    assert dry["rewritten"] == 1
    assert "drawer_sessions_technical_fff" in col.rows  # dry run wrote nothing
    apply_v3_reconcile(col)
    again = apply_v3_reconcile(col)  # re-run: nothing left to move
    assert again["ghost_drawers"] == 0


def test_base_default_rekey_row_reinserts_then_deletes_without_clobbering_twin():
    # The abstract default (used by chroma) probes the v4 twin first, then
    # rewrites via upsert(new)+delete(old), carrying the embedding so it is not
    # re-derived. If the twin already exists, it is preserved and only the old
    # ghost is dropped.
    from mempalace.backends.base import BaseCollection

    class _Min(BaseCollection):
        def __init__(self):
            self.rows = {}

        def add(self, **k):
            raise NotImplementedError

        def upsert(self, *, documents, ids, metadatas=None, embeddings=None):
            for i, d, m, e in zip(
                ids, documents, metadatas or [{}] * len(ids), embeddings or [None] * len(ids)
            ):
                self.rows[i] = (d, m, e)

        def query(self, **k):
            raise NotImplementedError

        def get(self, *, ids=None, **k):
            hits = [(i, *self.rows[i]) for i in ids or [] if i in self.rows]
            return GetResult(
                ids=[h[0] for h in hits],
                documents=[h[1] for h in hits],
                metadatas=[h[2] for h in hits],
                embeddings=None,
            )

        def delete(self, *, ids=None, where=None):
            for i in ids or []:
                self.rows.pop(i, None)

        def count(self):
            return len(self.rows)

    c = _Min()
    c.upsert(ids=["old"], documents=["body"], metadatas=[{"id_recipe": "v3"}], embeddings=[[0.7]])
    c.rekey_row("old", "new", content="body", metadata={"id_recipe": "v4"}, embedding=[0.7])
    assert "old" not in c.rows
    assert c.rows["new"] == ("body", {"id_recipe": "v4"}, [0.7])

    c.upsert(ids=["old2"], documents=["body"], metadatas=[{"id_recipe": "v3"}])
    c.upsert(ids=["new"], documents=["body"], metadatas=[{"id_recipe": "v4", "fresh": True}])
    c.rekey_row("old2", "new", content="body", metadata={"id_recipe": "v4", "stale": True})
    assert "old2" not in c.rows
    assert c.rows["new"] == ("body", {"id_recipe": "v4", "fresh": True}, None)
