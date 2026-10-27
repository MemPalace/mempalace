"""Production logical-read acceptance gates before incremental runner enablement.

Real Chroma/sqlite_exact stores and production readers, not copies of filtering
implementations. All former strict xfails now execute normally. Synthetic local
embeddings only; this is not full end-to-end ingestion acceptance.
"""

import pytest

from mempalace.palace import get_collection
from mempalace.sources.lifecycle import SourceLifecycleStore

_NAME = "mempalace_drawers"
_VISIBLE = {"current", "peer", "legacy"}


@pytest.fixture(params=["chroma", "sqlite_exact"])
def generated_palace(tmp_path, request):
    path = str(tmp_path / request.param)
    col = get_collection(path, backend=request.param)
    store = SourceLifecycleStore(path)
    previous = store.begin("alpha", "shared", "v1")
    for i in range(2):
        col.upsert(
            ids=[f"old-{i}"],
            documents=[f"quartz retired text {i}"],
            metadatas=[{**previous.metadata, "wing": "retired", "room": "notes"}],
        )
    store.activate(previous)
    current = store.begin("alpha", "shared", "v2")
    col.upsert(
        ids=["current"],
        documents=["quartz active text"],
        metadatas=[{**current.metadata, "wing": "visible", "room": "notes"}],
    )
    store.activate(current)
    peer = store.begin("beta", "shared", "peer-v1")
    col.upsert(
        ids=["peer"],
        documents=["quartz other producer"],
        metadatas=[{**peer.metadata, "wing": "visible", "room": "notes"}],
    )
    store.activate(peer)
    pending = store.begin("alpha", "shared", "v3")
    col.upsert(
        ids=["pending"],
        documents=["quartz staging text"],
        metadatas=[{**pending.metadata, "wing": "staging", "room": "notes"}],
    )
    col.upsert(
        ids=["legacy"],
        documents=["quartz legacy text"],
        metadatas=[{"source_file": "shared", "wing": "visible", "room": "notes"}],
    )
    return path, col, store, request.param


def test_predicate_preserves_last_good_shortened_generation_and_other_producers(generated_palace):
    _path, col, store, _backend = generated_palace
    rows = col.get(include=["documents", "metadatas"])
    with store.snapshot() as reader:
        visible = {rid for rid, meta in zip(rows.ids, rows.metadatas) if reader.visible(meta)}
    assert visible == _VISIBLE
    assert col.count() == 3
    assert col.physical_count() == 6  # Explicit physical maintenance contract.


def test_public_collection_get_hides_staging_and_retired(generated_palace):
    _path, col, _store, _backend = generated_palace
    assert set(col.get().ids) == _VISIBLE


def test_public_collection_projection_still_enforces_visibility(generated_palace):
    _path, col, _store, _backend = generated_palace
    assert set(col.get(include=["documents"]).ids) == _VISIBLE


def test_public_vector_query_hides_staging_and_retired(generated_palace):
    _path, col, _store, _backend = generated_palace
    assert set(col.query(query_texts=["quartz"], n_results=6).ids[0]) == _VISIBLE


def test_public_lexical_query_hides_staging_and_retired(generated_palace):
    _path, col, _store, _backend = generated_palace
    assert {hit.id for hit in col.lexical_search(query="quartz", n_results=6).hits} == _VISIBLE


def test_fast_taxonomy_counts_only_visible_drawers(generated_palace):
    path, _col, _store, backend = generated_palace
    if backend == "chroma":
        from mempalace.backends.chroma_sqlite import _sqlite_wing_room_counts as counts
    else:
        from mempalace.backends.sqlite_exact import sqlite_wing_room_counts as counts
    assert counts(path, _NAME) == (3, {"visible": {"notes": 3}})


def test_fast_graph_grouping_hides_staging_and_retired(generated_palace):
    path, _col, _store, backend = generated_palace
    if backend == "chroma":
        from mempalace.backends.chroma_sqlite import sqlite_room_wing_hall_counts as counts
    else:
        from mempalace.backends.sqlite_exact import sqlite_room_wing_hall_counts as counts
    assert {row[1] for row in counts(path, _NAME)} == {"visible"}


def test_export_does_not_publish_staging_or_retired_text(generated_palace, tmp_path):
    from mempalace.exporter import export_palace

    path, _col, _store, _backend = generated_palace
    output = tmp_path / "export"
    stats = export_palace(path, str(output))
    assert stats["drawers"] == 3
    text = "\n".join(p.read_text() for p in output.rglob("*.md"))
    assert "staging text" not in text
    assert "retired text" not in text


def test_raw_sqlite_bm25_does_not_return_staging(generated_palace):
    path, _col, _store, backend = generated_palace
    if backend != "chroma":
        pytest.skip("Chroma-only raw fallback; sqlite_exact uses backend lexical search")
    from mempalace.searcher import _bm25_only_via_sqlite

    result = _bm25_only_via_sqlite("quartz", path, n_results=10)
    assert {hit["drawer_id"] for hit in result["results"]} == _VISIBLE


def test_raw_metadata_stream_hides_staging_even_when_projecting_keys(generated_palace):
    path, _col, _store, backend = generated_palace
    if backend != "chroma":
        pytest.skip("Chroma-only metadata stream")
    from mempalace.backends.chroma_sqlite import sqlite_iter_metadata

    rows = list(sqlite_iter_metadata(path, _NAME, keys=["wing"]))
    assert len(rows) == 3
    assert {meta["wing"] for meta in rows} == {"visible"}


def test_raw_drawer_preview_hides_staging(generated_palace):
    path, _col, _store, backend = generated_palace
    if backend != "chroma":
        pytest.skip("Chroma-only raw preview reader")
    from mempalace.backends.chroma_sqlite import sqlite_documents_for_ids

    assert sqlite_documents_for_ids(path, _NAME, ["pending"]) == {}


@pytest.mark.parametrize("include", [[], ["documents"], ["metadatas"], ["embeddings"]])
def test_logical_offset_and_projection(generated_palace, include):
    _path, col, _store, _backend = generated_palace
    all_ids = col.get(include=[]).ids
    page = col.get(limit=1, offset=1, include=include)
    assert page.ids == all_ids[1:2]
    assert len(page.documents) == (1 if "documents" in include else 0)
    assert len(page.metadatas) == (1 if "metadatas" in include else 0)
    assert (page.embeddings is not None) == ("embeddings" in include)


def test_bulk_recent_facets_and_logical_count(generated_palace):
    _path, col, _store, _backend = generated_palace
    assert set(col.get_all_rows(include=["documents"]).ids) == _VISIBLE
    assert len(col.get_all_metadata()) == 3
    assert set(col.get_recent(limit=20).ids) == _VISIBLE
    assert col.facet_counts("wing") == {"visible": 3}
    assert col.count() == 3
    assert col.physical_count() == 6


@pytest.mark.parametrize(
    "bad", ["missing_version", "forged_version", "missing_generation", "unknown"]
)
def test_production_reads_reject_partial_and_forged_stamps(generated_palace, bad):
    from mempalace.sources.lifecycle import LifecycleStateError

    path, col, store, backend = generated_palace
    with store.snapshot() as snapshot:
        meta = snapshot.current("beta", "shared").metadata
    if bad == "missing_version":
        del meta["source_version"]
    elif bad == "missing_generation":
        del meta["source_generation"]
    elif bad == "unknown":
        meta["source_generation"] = "unknown"
    else:
        meta["source_version"] = "forged"
    col.upsert(ids=["forged"], documents=["quartz forged"], metadatas=[meta])
    with pytest.raises(LifecycleStateError):
        col.get(ids=["forged"], include=["documents"])
    with pytest.raises(LifecycleStateError):
        col.count()
    if backend == "chroma":
        from mempalace.backends.chroma_sqlite import sqlite_documents_for_ids

        with pytest.raises(LifecycleStateError):
            sqlite_documents_for_ids(path, _NAME, ["forged"])


def test_reader_lifetime_blocks_gc_and_backpressure_preserves_active(generated_palace):
    from mempalace.sources.lifecycle import LifecycleConflictError
    from mempalace.sources.read_visibility import iter_drawer_batches

    path, col, store, _backend = generated_palace
    rows = iter_drawer_batches(col, path, batch_size=1)
    first = next(rows)
    assert set(first.ids) <= _VISIBLE
    with pytest.raises(LifecycleConflictError, match="readers"):
        store.reclaim(col, "alpha", "shared")
    with store.snapshot() as snapshot:
        pending = snapshot.pending("alpha", "shared")
    with pytest.raises(LifecycleConflictError, match="retired"):
        store.activate(pending)
    assert set(col.get().ids) == _VISIBLE
    rows.close()
    store.reclaim(col, "alpha", "shared")
    assert col.physical_count() == 4
    store.activate(pending)
    assert set(col.get().ids) == {"pending", "peer", "legacy"}


def test_gc_failure_keeps_registry_and_other_producers(generated_palace, monkeypatch):
    path, col, store, _backend = generated_palace
    original = col.delete

    def fail(**kwargs):
        raise OSError("physical delete failure")

    monkeypatch.setattr(col, "delete", fail)
    with pytest.raises(OSError, match="physical delete"):
        store.reclaim(col, "alpha", "shared")
    assert col.physical_count() == 6
    assert set(col.get().ids) == _VISIBLE
    monkeypatch.setattr(col, "delete", original)
    store.reclaim(col, "alpha", "shared")
    assert col.physical_count() == 4
    assert set(col.get().ids) == _VISIBLE


def test_registry_only_activation_invalidates_known_wings(generated_palace):
    from mempalace.search_util import chroma_known_wings, known_wings

    path, col, store, backend = generated_palace
    read = (lambda: chroma_known_wings(path)) if backend == "chroma" else lambda: known_wings(col)
    assert read() == {"visible"}
    # Physical write precedes the first cache fill above; only registry changes.
    store.reclaim(col, "alpha", "shared")
    with store.snapshot() as snapshot:
        pending = snapshot.pending("alpha", "shared")
    # Fill again after physical cleanup, ensuring activation alone invalidates.
    assert read() == {"visible"}
    store.activate(pending)
    assert read() == {"visible", "staging"}


def test_missing_registry_restore_never_resurrects_managed_rows(generated_palace):
    from mempalace.sources.lifecycle import LifecycleStateError

    path, col, store, backend = generated_palace
    store.path.unlink()
    with pytest.raises(LifecycleStateError):
        col.get(include=["documents"])
    if backend == "chroma":
        from mempalace.backends.chroma_sqlite import sqlite_documents_for_ids

        with pytest.raises(LifecycleStateError):
            sqlite_documents_for_ids(path, _NAME, ["current"])
    assert not store.path.exists()


def test_closets_are_not_lifecycle_drawers(generated_palace):
    from mempalace.palace import get_closets_collection

    path, _col, _store, backend = generated_palace
    closets = get_closets_collection(path, backend=backend)
    closets.add(
        ids=["unmanaged"],
        documents=["closet pointer"],
        metadatas=[{"source_generation": "not-a-drawer-generation"}],
    )
    assert closets.get().ids == ["unmanaged"]


def test_export_uses_one_native_selection_cursor(generated_palace, monkeypatch):
    from mempalace.sources.read_visibility import iter_drawer_batches

    path, col, _store, backend = generated_palace

    def no_pages(**kwargs):
        pytest.fail("bulk operation paged the public collection")

    monkeypatch.setattr(col, "get", no_pages)
    batches = list(iter_drawer_batches(col, path, batch_size=1))
    assert {rid for batch in batches for rid in batch.ids} == _VISIBLE


def test_chroma_ann_refills_bounded_and_discloses_truncation(tmp_path, monkeypatch):
    from mempalace.palace import get_collection

    col = get_collection(str(tmp_path), backend="chroma")
    store = SourceLifecycleStore(str(tmp_path))
    staged = store.begin("a", "hidden", "v")
    col.add(
        ids=[f"hidden-{i}" for i in range(40)],
        documents=["quartz"] * 40,
        metadatas=[staged.metadata] * 40,
    )
    col.add(ids=["visible"], documents=["distant memory"], metadatas=[{"wing": "legacy"}])
    physical = col._inner
    original = physical._query_physical
    requested = []

    def trace(**kwargs):
        requested.append(kwargs["n_results"])
        return original(**kwargs)

    monkeypatch.setattr(physical, "_query_physical", trace)
    with pytest.warns(RuntimeWarning, match="truncated"):
        result = col.query(query_texts=["quartz"], n_results=1)
    assert result.ids == [[]]
    assert max(requested) <= 16
    assert physical.physical_count() == 41


def test_registry_only_activation_invalidates_taxonomy_metadata_graph(
    generated_palace, monkeypatch
):
    from _mcp_server_helpers import _patch_mcp_server

    from mempalace import mcp_server as mcp, palace_graph
    from mempalace.config import MempalaceConfig

    path, col, store, backend = generated_palace
    config = MempalaceConfig(palace_path=path)
    _patch_mcp_server(monkeypatch, config, None)
    monkeypatch.setattr(mcp, "_is_chroma_backend", lambda: backend == "chroma")
    monkeypatch.setattr(mcp, "_get_collection", lambda *a, **kw: col)
    monkeypatch.setenv("MEMPALACE_BACKEND_EXPLICIT", backend)
    mcp._invalidate_overview_caches()
    palace_graph.invalidate_graph_cache()
    store.reclaim(col, "alpha", "shared")
    assert len(mcp._get_cached_metadata(col)) == 3
    assert mcp._sqlite_taxonomy()[0] == 3
    before = palace_graph.build_graph(config=config)[0]
    assert before["notes"]["wings"] == ["visible"]
    with store.snapshot() as snap:
        pending = snap.pending("alpha", "shared")
    store.activate(pending)
    assert {m["wing"] for m in mcp._get_cached_metadata(col)} == {"visible", "staging"}
    assert mcp._sqlite_taxonomy()[0] == 3
    after = palace_graph.build_graph(config=config)[0]
    assert after["notes"]["wings"] == ["staging", "visible"]


def test_configured_drawer_collection_applies_visibility(tmp_path, monkeypatch):
    import mempalace.config as config
    from mempalace.backends.chroma_sqlite import sqlite_documents_for_ids

    name = "custom_drawers"
    monkeypatch.setattr(config, "get_configured_collection_name", lambda: name)
    col = get_collection(str(tmp_path), collection_name=name, backend="chroma")
    store = SourceLifecycleStore(str(tmp_path))
    pending = store.begin("a", "s", "v")
    col.add(ids=["hidden"], documents=["quartz hidden"], metadatas=[pending.metadata])
    assert col.get().ids == []
    assert sqlite_documents_for_ids(str(tmp_path), name, ["hidden"]) == {}
    store.activate(pending)
    assert col.get().ids == ["hidden"]


def test_filtered_generation_unicode_and_nonstring_fields(generated_palace):
    _path, col, _store, _backend = generated_palace
    col.add(
        ids=["typed"],
        documents=["verbatim"],
        metadatas=[{"flag": True, "wing": "\x01mpesc:literal"}],
    )
    assert col.get(where={"flag": True}).ids == ["typed"]
    assert col.get(where={"wing": "\x01mpesc:literal"}).ids == ["typed"]


def test_chroma_query_preserves_array_metadata(generated_palace):
    _path, col, _store, backend = generated_palace
    if backend != "chroma":
        pytest.skip("Chroma array storage encoding")
    col.add(ids=["arrays"], documents=["quartz array"], metadatas=[{"tags": ["a", "日本語"]}])
    result = col.query(query_texts=["quartz array"], n_results=10)
    assert dict(zip(result.ids[0], result.metadatas[0]))["arrays"]["tags"] == ["a", "日本語"]


def test_initialization_refuses_tagged_vector_only_restore(generated_palace):
    from mempalace.sources.lifecycle import LifecycleStateError

    path, col, store, _backend = generated_palace
    store.path.unlink()
    with pytest.raises(LifecycleStateError, match="restore"):
        SourceLifecycleStore(path)
    assert not store.path.exists()
    assert col.physical_count() == 6


def test_chroma_metadata_projection_does_not_hydrate_verbatim(generated_palace, monkeypatch):
    import mempalace.sources.read_visibility as reads

    path, col, _store, backend = generated_palace
    if backend != "chroma":
        pytest.skip("Chroma SQL projection trace")
    original = reads.open_logical_reader
    statements = []

    def traced(*args, **kwargs):
        reader = original(*args, **kwargs)
        reader.conn.set_trace_callback(statements.append)
        return reader

    monkeypatch.setattr(reads, "open_logical_reader", traced)
    assert col.get(limit=1, include=["metadatas"]).ids
    hydration = [s for s in statements if s.startswith("SELECT id, key,")]
    assert hydration
    assert all("key != 'chroma:document'" in sql for sql in hydration)


def test_raw_diary_singular_and_source_groups_hide_inactive(generated_palace):
    path, col, store, backend = generated_palace
    with store.snapshot() as snap:
        visible = snap.current("beta", "shared")
        pending = snap.pending("alpha", "shared")
    col.add(
        ids=["diary-visible", "diary-hidden"],
        documents=["visible diary", "hidden diary"],
        metadatas=[
            {**visible.metadata, "room": "diary", "agent": "fixture", "filed_at": "2026-10-26"},
            {**pending.metadata, "room": "diary", "agent": "fixture", "filed_at": "2026-10-27"},
        ],
    )
    if backend == "chroma":
        from mempalace.backends.chroma_sqlite import sqlite_diary_rows, sqlite_drawer_rows

        total, rows = sqlite_diary_rows(path, _NAME, agent_name="fixture")
        assert total == 1
        assert [r[0] for r in rows] == ["diary-visible"]
        assert sqlite_drawer_rows(path, _NAME, drawer_id="diary-hidden") is None
        assert col.get(ids=["diary-hidden"]).ids == []
    else:
        assert col.get(where={"room": "diary"}).ids == ["diary-visible"]


def test_gc_prune_failure_is_retryable_without_resurrecting_rows(generated_palace):
    import contextlib
    import sqlite3

    path, col, store, _backend = generated_palace
    with contextlib.closing(sqlite3.connect(store.path)) as conn, conn:
        conn.execute(
            "CREATE TRIGGER fail_prune BEFORE DELETE ON source_generations "
            "BEGIN SELECT RAISE(ABORT, 'prune failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="prune failure"):
        store.reclaim(col, "alpha", "shared")
    assert col.physical_count() == 4  # physical deletion succeeded first
    assert set(col.get().ids) == _VISIBLE
    with contextlib.closing(sqlite3.connect(store.path)) as conn, conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM source_generations WHERE state='retired'"
            ).fetchone()[0]
            == 1
        )
        conn.execute("DROP TRIGGER fail_prune")
    store.reclaim(col, "alpha", "shared")
    assert set(col.get().ids) == _VISIBLE


def test_exact_lexical_managed_query_is_bounded(tmp_path):
    col = get_collection(str(tmp_path), backend="sqlite_exact")
    SourceLifecycleStore(str(tmp_path))
    col.add(
        ids=[f"r{i}" for i in range(50)],
        documents=["needle"] * 50,
        metadatas=[{"wing": "other"}] * 50,
    )
    with pytest.warns(RuntimeWarning, match="truncated"):
        result = col.lexical_search(query="needle", where={"wing": "absent"}, n_results=1)
    assert result.hits == []


def test_array_only_lifecycle_stamp_cannot_downgrade_to_legacy(tmp_path):
    from mempalace.sources.lifecycle import LifecycleStateError

    col = get_collection(str(tmp_path), backend="chroma")
    col.add(
        ids=["bad-array"],
        documents=["never expose"],
        metadatas=[{"source_version": ["not", "scalar"]}],
    )
    with pytest.raises(LifecycleStateError, match="array"):
        col.get(include=["documents"])


def test_array_only_restore_refuses_registry_initialization(tmp_path):
    from mempalace.sources.lifecycle import LifecycleStateError, lifecycle_path

    col = get_collection(str(tmp_path), backend="chroma")
    col.add(ids=["bad"], documents=["managed?"], metadatas=[{"source_generation": ["g"]}])
    with pytest.raises(LifecycleStateError):
        SourceLifecycleStore(str(tmp_path))
    assert not lifecycle_path(str(tmp_path)).exists()
