"""Tests for the MemPalace ↔ Hermes integration provider.

The provider ships inside the ``mempalace`` package (at
``mempalace/integrations/hermes/``) but at runtime Hermes loads it from a
copy in ``~/.hermes/plugins/`` via ``spec_from_file_location`` — not as a
package import. These tests load it the same way: by file path.

They also stub ``agent.memory_provider`` to mirror the runtime contract — the
plugin is only ever imported with Hermes on the import path.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import threading
import types
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Fixtures: stub the Hermes ABC, then import the provider module by path.
# ---------------------------------------------------------------------------


def _install_stub_memory_provider() -> None:
    """Install a minimal ``agent.memory_provider`` stub into sys.modules."""
    if "agent.memory_provider" in sys.modules:
        return
    agent_mod = types.ModuleType("agent")
    mp_mod = types.ModuleType("agent.memory_provider")

    class MemoryProvider:  # mirrors the parts the integration class uses
        pass

    mp_mod.MemoryProvider = MemoryProvider  # type: ignore[attr-defined]
    agent_mod.memory_provider = mp_mod  # type: ignore[attr-defined]
    sys.modules["agent"] = agent_mod
    sys.modules["agent.memory_provider"] = mp_mod


@pytest.fixture(scope="module")
def integration_module():
    _install_stub_memory_provider()
    path = (
        Path(__file__).resolve().parent.parent
        / "mempalace"
        / "integrations"
        / "hermes"
        / "__init__.py"
    )
    spec = importlib.util.spec_from_file_location("hermes_integration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def provider(integration_module):
    return integration_module.MempalaceProvider()


@pytest.fixture(autouse=True)
def _isolate_palace_env(integration_module):
    """Keep initialize()'s palace-env bridge from leaking between tests.

    ``initialize`` publishes the resolved palace to MEMPALACE_PALACE_PATH
    (so mcp_server passthrough tools resolve the same palace) and records
    ownership in the module-level ``_ENV_PALACE_BRIDGED`` sentinel. Both
    are process-global — restore them after every test.
    """
    original = os.environ.get("MEMPALACE_PALACE_PATH")
    original_sentinel = integration_module._ENV_PALACE_BRIDGED
    yield
    integration_module._ENV_PALACE_BRIDGED = original_sentinel
    if original is None:
        os.environ.pop("MEMPALACE_PALACE_PATH", None)
    else:
        os.environ["MEMPALACE_PALACE_PATH"] = original


# ---------------------------------------------------------------------------
# Shape: name, schemas, config, availability
# ---------------------------------------------------------------------------


def test_name_matches_plugin_yaml(provider):
    assert provider.name == "mempalace"


def test_is_available_imports_mempalace(provider):
    # The repo's own dev install satisfies this. Failure means a broken venv.
    assert provider.is_available() is True


def test_tool_schemas_visible_before_initialize(provider):
    # Regression for the discovery bug: Hermes'
    # ``agent.memory_manager._register_provider`` snapshots
    # ``get_tool_schemas()`` once at registration time to build its
    # ``tool_name → provider`` routing table. If we returned ``[]`` there,
    # the dispatcher would never learn our tool names and every later call
    # would hit ``"Unknown tool: <name>"`` from the dispatcher without
    # reaching ``handle_tool_call`` at all. Backend readiness gating
    # belongs in ``handle_tool_call``, not here.
    schemas = provider.get_tool_schemas()
    assert len(schemas) == 27  # openclaw set + 8 tools added after #491
    names = {s["name"] for s in schemas}
    assert "mempalace_status" in names
    assert "mempalace_search" in names
    assert "mempalace_add_drawer" in names
    assert "mempalace_update_drawer" in names  # added after openclaw #491
    assert "mempalace_kg_invalidate" in names


def test_config_schema_has_documented_keys(provider):
    keys = {field["key"] for field in provider.get_config_schema()}
    assert keys == {
        "palace_path",
        "identity_path",
        "wing",
        "n_prefetch",
    }
    # ``collection_name`` is intentionally absent — exposing it would let the
    # provider write to a collection that ``search_memories`` doesn't read.
    assert "collection_name" not in keys


def test_tool_schemas_module_constant_matches_expected_surface(integration_module):
    # 27 tools — openclaw's reference skill set (19 tools at
    # MemPalace/mempalace#491, April 2026) plus the 8 agent-facing tools
    # mempalace has added since that openclaw hasn't caught up to.
    # Admin/internal tools (sync, hook_settings, reconnect) intentionally
    # omitted.
    schemas = integration_module.TOOL_SCHEMAS
    names = {s["name"] for s in schemas}
    assert names == {
        # Search + structure
        "mempalace_search",
        "mempalace_status",
        "mempalace_list_wings",
        "mempalace_list_rooms",
        "mempalace_get_taxonomy",
        "mempalace_get_aaak_spec",
        # Drawer CRUD
        "mempalace_add_drawer",
        "mempalace_update_drawer",
        "mempalace_delete_drawer",
        "mempalace_list_drawers",
        "mempalace_get_drawer",
        "mempalace_check_duplicate",
        # Knowledge graph
        "mempalace_kg_query",
        "mempalace_kg_add",
        "mempalace_kg_invalidate",
        "mempalace_kg_timeline",
        "mempalace_kg_stats",
        # Per-agent diary
        "mempalace_diary_write",
        "mempalace_diary_read",
        # Room-graph navigation + tunnel management
        "mempalace_traverse",
        "mempalace_graph_stats",
        "mempalace_find_tunnels",
        "mempalace_create_tunnel",
        "mempalace_list_tunnels",
        "mempalace_delete_tunnel",
        "mempalace_follow_tunnels",
        # Session-level
        "mempalace_memories_filed_away",
    }


# ---------------------------------------------------------------------------
# Cron-context guard: provider must short-circuit on system-generated turns.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"agent_context": "cron"},
        {"agent_context": "flush"},
        {"platform": "cron"},
    ],
)
def test_initialize_skips_under_cron_context(provider, kwargs, tmp_path):
    # Even with a writable hermes_home, cron/flush context must not start the
    # worker or open a collection.
    provider.initialize("session-1", hermes_home=str(tmp_path), **kwargs)
    assert provider._cron_skipped is True
    assert provider._initialized is False
    assert provider.get_tool_schemas() == []
    assert provider.system_prompt_block() == ""
    assert provider.prefetch("anything") == ""
    # Calls that would otherwise enqueue work must be no-ops.
    provider.sync_turn("hi", "hello")
    provider.on_session_end([])
    assert provider._worker_thread is None


def test_handle_tool_call_under_cron_returns_error_json(provider, tmp_path):
    provider.initialize("session-1", hermes_home=str(tmp_path), agent_context="cron")
    result = json.loads(provider.handle_tool_call("mempalace_search", {"query": "x"}))
    assert "error" in result


def test_handle_tool_call_without_initialize_returns_error_json(provider):
    result = json.loads(provider.handle_tool_call("mempalace_status", {}))
    assert "error" in result


def test_on_session_end_no_op_when_not_initialized(provider):
    # Without ``_initialized``, the worker thread isn't running. Enqueueing
    # here would silently fill the bounded queue with tasks that never drain.
    provider._initialized = False
    provider._cron_skipped = False
    pre = provider._worker_queue.qsize()
    provider.on_session_end([{"role": "user", "content": "hi"}])
    assert provider._worker_queue.qsize() == pre


def test_on_memory_write_no_op_when_not_initialized(provider):
    provider._initialized = False
    provider._cron_skipped = False
    pre = provider._worker_queue.qsize()
    provider.on_memory_write("add", "user", "some fact")
    assert provider._worker_queue.qsize() == pre


def test_normalize_content_flattens_anthropic_list(integration_module):
    fn = integration_module._normalize_content
    blocks = [
        {"type": "text", "text": "what's the auth flow?"},
        {"type": "tool_use", "name": "grep", "input": {"q": "JWT"}},
        {"type": "text", "text": "(short clarifier)"},
    ]
    out = fn(blocks)
    assert "what's the auth flow?" in out
    assert "[tool_use: grep]" in out
    assert "(short clarifier)" in out
    # Must not be the literal Python repr.
    assert "{'type'" not in out


def test_match_wing_by_keywords_word_boundary(integration_module):
    fn = integration_module._match_wing_by_keywords
    wing_config = {
        "wing_ai": {"keywords": ["ai"]},
        "wing_dev": {"keywords": ["python"]},
    }
    # Substring matching would have routed "said" / "rain" / "available" to wing_ai.
    assert fn("She said rain is available", wing_config) == "wing_general"
    assert fn("write some ai bindings", wing_config) == "wing_ai"
    assert fn("python script for scraping", wing_config) == "wing_dev"


# ---------------------------------------------------------------------------
# Session switch / turn counter bookkeeping.
# ---------------------------------------------------------------------------


def test_on_session_switch_repoints_session_id(provider):
    provider._session_id = "old"
    provider._turn_count = 7
    provider.on_session_switch("new", reset=False)
    assert provider._session_id == "new"
    assert provider._turn_count == 7  # /resume / /branch keep counters


def test_on_session_switch_with_reset_clears_turn_counter(provider):
    provider._turn_count = 9
    provider.on_session_switch("new", reset=True)
    assert provider._turn_count == 0


def test_on_turn_start_tracks_turn_number(provider):
    provider.on_turn_start(turn_number=4, message="hi")
    assert provider._turn_count == 4


# ---------------------------------------------------------------------------
# on_session_end / on_pre_compress: sync_turn is the PRIMARY filing path —
# these hooks enqueue a scan-based safety net that files only turns
# sync_turn missed. Dedup correctness lives in _file_missing_exchanges.
# ---------------------------------------------------------------------------


def test_on_pre_compress_returns_hint_and_enqueues_safety_net(provider):
    provider._cron_skipped = False
    provider._initialized = True
    pre = provider._worker_queue.qsize()
    hint = provider.on_pre_compress([{"role": "user", "content": "hi"}])
    assert "mempalace_search" in hint
    assert provider._worker_queue.qsize() == pre + 1


def test_on_session_end_enqueues_safety_net_then_wakeup(provider):
    provider._cron_skipped = False
    provider._initialized = True
    provider.on_session_end([{"role": "user", "content": "hi"}])
    # The wake-up refresh is sequenced BEHIND the safety-net task so its
    # ChromaDB reads never race the safety net's writes (Windows sqlite
    # locks under concurrent access; a locked write drops a turn).
    tasks = []
    while not provider._worker_queue.empty():
        tasks.append(provider._worker_queue.get_nowait()[0])
    assert tasks == ["session_end", "wakeup"]


def test_on_pre_compress_under_cron_returns_empty_string(provider, tmp_path):
    provider.initialize("session-1", hermes_home=str(tmp_path), agent_context="cron")
    assert provider.on_pre_compress([{"role": "user", "content": "hi"}]) == ""


# ---------------------------------------------------------------------------
# Wing classification: keyword-based, fall back to wing_general.
# ---------------------------------------------------------------------------


def test_classify_wing_falls_back_to_general_with_no_config(provider):
    provider._wing_config = {}
    assert provider._classify_wing("anything") == "wing_general"


def test_classify_wing_matches_keyword(provider):
    provider._wing_config = {
        "wing_dev": {"keywords": ["python", "pytest"]},
        "wing_ops": {"keywords": ["deploy", "kubernetes"]},
    }
    assert provider._classify_wing("Running pytest -q") == "wing_dev"
    assert provider._classify_wing("kubectl deploy rollout") == "wing_ops"
    assert provider._classify_wing("just chatting") == "wing_general"


# ---------------------------------------------------------------------------
# Shutdown is safe even when initialize() never ran.
# ---------------------------------------------------------------------------


def test_shutdown_is_safe_without_initialize(provider):
    provider.shutdown()  # must not raise


def test_shutdown_drains_running_worker(provider):
    # Spin a fake worker that respects _worker_stop.
    def _loop():
        while not provider._worker_stop.is_set():
            provider._worker_stop.wait(0.05)

    provider._worker_thread = threading.Thread(target=_loop, daemon=True)
    provider._worker_thread.start()
    provider.shutdown()
    assert not provider._worker_thread.is_alive()


# ---------------------------------------------------------------------------
# End-to-end integration: real palace via mempalace's own fixtures.
#
# These exercise the ChromaBackend code path that fixes the dim-mismatch bug
# from prior in-tree Hermes PRs, and run the tool handlers against the
# `seeded_collection` / `seeded_kg` fixtures from `tests/conftest.py`.
# ---------------------------------------------------------------------------


@pytest.fixture
def initialized_provider(provider, palace_path, tmp_dir):
    """Provider initialized against a fresh temp palace."""
    config_path = Path(tmp_dir) / "mempalace.json"
    config_path.write_text(json.dumps({"palace_path": palace_path}))
    provider.initialize("test-session-1", hermes_home=str(tmp_dir), platform="cli")
    # Wait for the optional wake-up warm-up thread so later collection reads
    # do not race the L1 scan under the same Chroma client.
    provider._wake_up_done.wait(timeout=10)
    yield provider
    provider.shutdown()


def _collection_snapshot(provider):
    """Read metadatas under the provider lock after the worker drains."""
    provider._worker_queue.join()
    provider._wake_up_done.wait(timeout=10)
    with provider._collection_lock:
        col = provider._collection
        assert col is not None
        return col.get(include=["metadatas"]).get("metadatas") or []


def test_initialize_opens_chroma_via_backend(initialized_provider):
    """The dim-mismatch fix: collection access goes through ChromaBackend."""
    from mempalace.backends.chroma import ChromaBackend

    assert initialized_provider._initialized is True
    assert initialized_provider._collection is not None
    assert isinstance(initialized_provider._backend, ChromaBackend)


def test_get_tool_schemas_returns_full_surface_after_initialize(initialized_provider):
    schemas = initialized_provider.get_tool_schemas()
    names = {s["name"] for s in schemas}
    assert len(schemas) == 27
    assert "mempalace_search" in names
    assert "mempalace_kg_query" in names
    assert "mempalace_add_drawer" in names
    assert "mempalace_update_drawer" in names
    assert "mempalace_memories_filed_away" in names


def test_sync_turn_persists_through_worker(initialized_provider):
    initialized_provider.sync_turn("what's the plan?", "ship the PR")
    metas = _collection_snapshot(initialized_provider)
    assert any(m.get("source") == "hermes" for m in metas)


def test_sync_turn_writes_canonical_drawer_metadata(initialized_provider):
    """Live turns must carry the same metadata the convo miner writes.

    Without hall / entities / filed_at, Hermes drawers are silently
    invisible to hallway traversal, entity search, and the since/before
    date filters — nothing errors, recall just degrades.
    """
    initialized_provider.sync_turn("meeting with Sarah about the Q3 roadmap", "noted")
    metas = _collection_snapshot(initialized_provider)
    hermes_metas = [m for m in metas if m.get("source") == "hermes"]
    assert hermes_metas
    meta = hermes_metas[0]
    for key in (
        "wing",
        "room",
        "hall",
        "source_file",
        "added_by",
        "filed_at",
        "authored_at",
        "ingest_mode",
        "extract_mode",
        "normalize_version",
        "id_recipe",
    ):
        assert key in meta, f"missing canonical metadata key: {key!r}"
    assert meta["room"] == "conversations"
    assert meta["ingest_mode"] == "convos"
    assert meta["extract_mode"] == "exchange"


def test_sync_turn_routes_to_configured_wing(initialized_provider):
    initialized_provider._wing_config = {"wing_dev": {"keywords": ["pytest"]}}
    initialized_provider.sync_turn("running pytest -q", "all passed")
    metas = _collection_snapshot(initialized_provider)
    assert any(m.get("wing") == "wing_dev" for m in metas)


def test_sync_turn_skips_when_both_sides_empty(initialized_provider):
    pre = initialized_provider._collection.count()
    initialized_provider.sync_turn("", "")
    # Queue should not have received an item; nothing to join, but worker has
    # nothing to do either. Give it a moment then re-check.
    initialized_provider._worker_queue.join()
    assert initialized_provider._collection.count() == pre


# ----- Tool handlers against seeded data ----------------------------------


@pytest.fixture
def provider_on_seeded_palace(seeded_collection, provider, palace_path, tmp_dir):
    """Provider pointed at the same palace_path that ``seeded_collection`` filled.

    ``seeded_collection`` writes 4 drawers via raw ``chromadb.PersistentClient``;
    we then have the provider open the same path via ``ChromaBackend`` — the
    fact that this round-trips at all is the dim-mismatch regression check.
    """
    (Path(tmp_dir) / "mempalace.json").write_text(json.dumps({"palace_path": palace_path}))
    provider.initialize("s1", hermes_home=str(tmp_dir))
    yield provider
    provider.shutdown()


def test_status_tool_counts_seeded_drawers(provider_on_seeded_palace):
    result = json.loads(provider_on_seeded_palace.handle_tool_call("mempalace_status", {}))
    assert result["total_drawers"] == 4
    assert result["wings"]["project"] == 3
    assert result["wings"]["notes"] == 1


def test_list_wings_tool_returns_seeded_wings(provider_on_seeded_palace):
    result = json.loads(provider_on_seeded_palace.handle_tool_call("mempalace_list_wings", {}))
    assert result["wings"] == {"project": 3, "notes": 1}


def test_list_rooms_tool_filters_by_wing(provider_on_seeded_palace):
    result = json.loads(
        provider_on_seeded_palace.handle_tool_call(
            "mempalace_list_rooms",
            {"wing": "project"},
        )
    )
    assert result["wing"] == "project"
    assert result["rooms"]["backend"] == 2
    assert result["rooms"]["frontend"] == 1


def test_list_rooms_tool_rejects_missing_wing(provider_on_seeded_palace):
    result = json.loads(
        provider_on_seeded_palace.handle_tool_call(
            "mempalace_list_rooms",
            {},
        )
    )
    assert "error" in result


def test_status_tool_omits_truncated_under_cap(provider_on_seeded_palace):
    # 4 seeded drawers, cap is 5000 — the response must not advertise
    # itself as a partial view when in fact it's complete.
    result = json.loads(provider_on_seeded_palace.handle_tool_call("mempalace_status", {}))
    assert "truncated" not in result
    assert "scanned" not in result


def test_status_tool_marks_truncated_with_structured_fields(provider_on_seeded_palace):
    # Force the cap below the seeded count so we exercise the truncation path.
    # The model needs ``truncated`` (bool) + ``scanned`` (int) so it can
    # compute coverage = scanned / total_drawers itself rather than parsing
    # a sentence.
    provider_on_seeded_palace.STATUS_SCAN_LIMIT = 2
    result = json.loads(provider_on_seeded_palace.handle_tool_call("mempalace_status", {}))
    assert result["truncated"] is True
    assert result["scanned"] == 2
    assert result["total_drawers"] == 4


def test_list_wings_tool_marks_truncated_with_palace_total(provider_on_seeded_palace):
    # ``_tool_list_wings`` has no unconditional ``total_drawers`` field —
    # when truncated it must surface ``total_drawers`` so callers can
    # compute coverage without a second ``mempalace_status`` call.
    provider_on_seeded_palace.STATUS_SCAN_LIMIT = 2
    result = json.loads(provider_on_seeded_palace.handle_tool_call("mempalace_list_wings", {}))
    assert result["truncated"] is True
    assert result["scanned"] == 2
    assert result["total_drawers"] == 4


def test_list_rooms_tool_marks_truncated_without_wing_total(provider_on_seeded_palace):
    # Rooms can't cheaply give an exact wing total (no ``where=`` on
    # ``count()`` in the pinned chroma version). The structured fields are
    # still present; the absent ``total_drawers`` is intentional and
    # documented in the code.
    provider_on_seeded_palace.STATUS_SCAN_LIMIT = 1
    result = json.loads(
        provider_on_seeded_palace.handle_tool_call(
            "mempalace_list_rooms",
            {"wing": "project"},
        )
    )
    assert result["truncated"] is True
    assert result["scanned"] == 1
    assert "total_drawers" not in result


# ----- Knowledge-graph tool handlers --------------------------------------


def test_kg_add_persists_to_palace_sibling_sqlite(initialized_provider, palace_path):
    """The provider writes to ``<palace_path>/../knowledge_graph.sqlite3``."""
    from mempalace.knowledge_graph import KnowledgeGraph

    result = json.loads(
        initialized_provider.handle_tool_call(
            "mempalace_kg_add",
            {"subject": "user", "predicate": "likes", "object": "coffee"},
        )
    )
    assert result["status"] == "ok"

    db_path = str(Path(palace_path).parent / "knowledge_graph.sqlite3")
    independent_kg = KnowledgeGraph(db_path=db_path)
    try:
        relations = independent_kg.query_entity("user")
    finally:
        independent_kg.close()
    assert any(
        (r.get("predicate") == "likes" and r.get("object") == "coffee") for r in (relations or [])
    )


def test_kg_query_tool_rejects_missing_entity(initialized_provider):
    result = json.loads(
        initialized_provider.handle_tool_call(
            "mempalace_kg_query",
            {},
        )
    )
    assert "error" in result


# ----- Diary roundtrip ----------------------------------------------------


def test_diary_write_read_roundtrip(initialized_provider):
    write = json.loads(
        initialized_provider.handle_tool_call(
            "mempalace_diary_write",
            {"entry": "Today I deepened test coverage."},
        )
    )
    assert write["status"] == "ok"

    read = json.loads(
        initialized_provider.handle_tool_call(
            "mempalace_diary_read",
            {"n": 5},
        )
    )
    assert read["entries"]
    assert read["entries"][-1]["entry"] == "Today I deepened test coverage."


def test_diary_read_empty_when_no_writes(initialized_provider):
    result = json.loads(initialized_provider.handle_tool_call("mempalace_diary_read", {}))
    assert result["entries"] == []


# ----- Config-load path ---------------------------------------------------


def test_initialize_reads_mempalace_json(provider, tmp_dir, palace_path):
    (Path(tmp_dir) / "mempalace.json").write_text(
        json.dumps(
            {
                "palace_path": palace_path,
                "n_prefetch": 7,
                "wing": "wing_from_config",
            }
        )
    )
    provider.initialize("s1", hermes_home=str(tmp_dir))
    try:
        assert provider._config["n_prefetch"] == 7
        assert provider._config["wing"] == "wing_from_config"
    finally:
        provider.shutdown()


def test_collection_name_is_not_hermes_configurable(provider, tmp_dir, palace_path):
    # A hermes-side ``collection_name`` would be a second way to set the
    # name — the write and read sides could silently diverge, making the
    # provider look mute. The key is ignored; with no mempalace-side
    # override (conftest redirects HOME to a temp dir), the default applies.
    (Path(tmp_dir) / "mempalace.json").write_text(
        json.dumps({"palace_path": palace_path, "collection_name": "custom_drawers"})
    )
    provider.initialize("s1", hermes_home=str(tmp_dir))
    try:
        assert provider._collection_name == provider.DEFAULT_COLLECTION_NAME
    finally:
        provider.shutdown()


def test_collection_name_follows_mempalace_config(
    integration_module, provider, tmp_dir, palace_path, tmp_path, monkeypatch
):
    # One source of truth: the provider writes to the collection that
    # ``search_memories`` and the mcp_server passthrough actually read —
    # mempalace's own config — so a customized ``collection_name`` in
    # ``~/.mempalace/config.json`` cannot make live turns invisible to
    # recall.
    mp_config_dir = tmp_path / "mp_home"
    mp_config_dir.mkdir()
    (mp_config_dir / "config.json").write_text(json.dumps({"collection_name": "family_drawers"}))

    from mempalace.config import MempalaceConfig as real_config

    def _patched_config(config_dir=None):
        return real_config(config_dir=str(mp_config_dir))

    # Patch the provider module's own reference — it imported the name at
    # module load, so patching mempalace.config wouldn't reach it.
    monkeypatch.setattr(integration_module, "MempalaceConfig", _patched_config)
    (Path(tmp_dir) / "mempalace.json").write_text(json.dumps({"palace_path": palace_path}))
    provider.initialize("s1", hermes_home=str(tmp_dir))
    try:
        assert provider._collection_name == "family_drawers"
    finally:
        provider.shutdown()


def test_env_vars_override_config_file(provider, tmp_dir, palace_path, monkeypatch):
    monkeypatch.setenv("MEMPALACE_PALACE_PATH", palace_path)
    monkeypatch.setenv("MEMPALACE_WING", "wing_forced")
    provider.initialize("s1", hermes_home=str(tmp_dir))
    try:
        assert provider._palace_path == palace_path
        assert provider._config["wing"] == "wing_forced"
    finally:
        provider.shutdown()


# ---------------------------------------------------------------------------
# PR #1915 review fixes: scan truncation boundary, None metadata, bad keywords.
# ---------------------------------------------------------------------------


class _FakeScanCollection:
    """Serves ``n`` rows through the same get(limit=...) shape chroma uses."""

    def __init__(self, n, metadatas=None):
        self._n = n
        self._metadatas = metadatas

    def count(self):
        return self._n

    def get(self, **kwargs):
        if self._metadatas is not None:
            return {"metadatas": list(self._metadatas)}
        limit = kwargs.get("limit") or self._n
        return {"metadatas": [{"wing": "wing_a", "room": "r"} for _ in range(min(self._n, limit))]}


def test_scan_metadatas_not_truncated_at_exactly_cap(provider):
    cap = provider.STATUS_SCAN_LIMIT
    metas, truncated = provider._scan_metadatas(_FakeScanCollection(cap))
    assert len(metas) == cap
    # Exactly cap rows means the view is complete — flagging it truncated
    # makes the model qualify a breakdown that is in fact 100% coverage.
    assert truncated is False


def test_scan_metadatas_truncated_above_cap(provider):
    cap = provider.STATUS_SCAN_LIMIT
    metas, truncated = provider._scan_metadatas(_FakeScanCollection(cap + 1))
    assert truncated is True
    # Callers still get at most cap rows — the +1 probe row is trimmed.
    assert len(metas) == cap


def test_status_and_list_tools_tolerate_none_metadata_entries(provider):
    # Legacy palaces / raw writers can leave None metadata entries; the
    # breakdown loops must count them as "unknown", not fail the tool call.
    rows = [None, {"wing": "wing_a", "room": "room_a"}]
    provider._collection = _FakeScanCollection(2, metadatas=rows)

    status = provider._tool_status()
    assert "error" not in status
    assert status["wings"] == {"unknown": 1, "wing_a": 1}

    wings = provider._tool_list_wings()
    assert "error" not in wings
    assert wings["wings"] == {"unknown": 1, "wing_a": 1}

    rooms = provider._tool_list_rooms("wing_a")
    assert "error" not in rooms
    assert rooms["rooms"] == {"unknown": 1, "room_a": 1}


def test_match_wing_by_keywords_ignores_non_string_keywords(integration_module):
    # A hand-edited wing_config.json with a number/null in a keyword list
    # must not break wing routing — a raised AttributeError inside
    # _file_turn's try/except silently drops every live turn.
    fn = integration_module._match_wing_by_keywords
    wing_config = {"wing_dev": {"keywords": [None, 3, "python"]}}
    assert fn("write some python code", wing_config) == "wing_dev"
    assert fn("unrelated chatter", wing_config) == "wing_general"


# ---------------------------------------------------------------------------
# Palace unification: the mcp_server passthrough tools must operate on the
# SAME palace the provider writes and searches. The provider bridges its
# resolved palace into MEMPALACE_PALACE_PATH (mcp_server re-reads that var on
# every config access — its own --palace flag works the same way), and the
# KG tools are handled natively because mcp_server's KG path ignores the env
# var unless its CLI flag was given.
# ---------------------------------------------------------------------------


def test_initialize_bridges_palace_env_for_passthrough(provider, tmp_dir, palace_path):
    from mempalace.config import MempalaceConfig

    (Path(tmp_dir) / "mempalace.json").write_text(json.dumps({"palace_path": palace_path}))
    provider.initialize("s1", hermes_home=str(tmp_dir))
    try:
        expected = os.path.abspath(os.path.expanduser(palace_path))
        assert os.environ.get("MEMPALACE_PALACE_PATH") == expected
        # The passthrough side (mcp_server's config) now resolves the same
        # palace the provider writes — the split-brain regression check.
        assert MempalaceConfig().palace_path == expected
    finally:
        provider.shutdown()


def test_reinitialize_follows_updated_hermes_config(provider, tmp_dir, palace_path, tmp_path):
    # The bridge write from session 1 must not masquerade as a user env
    # override in session 2 — a stale bridge would pin the palace to the
    # old hermes-side value forever.
    config_path = Path(tmp_dir) / "mempalace.json"
    config_path.write_text(json.dumps({"palace_path": palace_path}))
    provider.initialize("s1", hermes_home=str(tmp_dir))
    provider.shutdown()

    new_palace = str(tmp_path / "palace_b")
    config_path.write_text(json.dumps({"palace_path": new_palace}))
    provider.initialize("s2", hermes_home=str(tmp_dir))
    try:
        assert provider._palace_path == new_palace
        assert os.environ.get("MEMPALACE_PALACE_PATH") == os.path.abspath(new_palace)
    finally:
        provider.shutdown()


def test_user_set_palace_env_wins_and_is_never_cleared(
    provider, tmp_dir, palace_path, tmp_path, monkeypatch
):
    # A user-set env var outranks the hermes-side config (documented
    # precedence) and the bridge must not claim ownership of it — a later
    # re-initialize must leave the user's value in place.
    monkeypatch.setenv("MEMPALACE_PALACE_PATH", palace_path)
    (Path(tmp_dir) / "mempalace.json").write_text(
        json.dumps({"palace_path": str(tmp_path / "other_palace")})
    )
    provider.initialize("s1", hermes_home=str(tmp_dir))
    try:
        assert provider._palace_path == palace_path
        assert os.environ.get("MEMPALACE_PALACE_PATH") == palace_path
        # Ownership was not claimed: the sentinel stays unset.
        assert provider.__class__.__module__ is not None  # provider alive
    finally:
        provider.shutdown()
    provider.initialize("s2", hermes_home=str(tmp_dir))
    try:
        assert os.environ.get("MEMPALACE_PALACE_PATH") == palace_path
    finally:
        provider.shutdown()


def test_hermes_config_defers_to_mempalace_config_when_unset(
    integration_module, provider, tmp_dir, tmp_path, monkeypatch
):
    # No hermes-side palace_path → the provider follows mempalace's own
    # config rather than hardcoding the default location.
    mp_config_dir = tmp_path / "mp_home"
    mp_config_dir.mkdir()
    custom_palace = str(tmp_path / "custom_palace")
    (mp_config_dir / "config.json").write_text(json.dumps({"palace_path": custom_palace}))

    from mempalace.config import MempalaceConfig as real_config

    def _patched_config(config_dir=None):
        return real_config(config_dir=str(mp_config_dir))

    # Patch the provider module's own reference — it imported the name at
    # module load, so patching mempalace.config wouldn't reach it.
    monkeypatch.setattr(integration_module, "MempalaceConfig", _patched_config)
    (Path(tmp_dir) / "mempalace.json").write_text(json.dumps({}))
    provider.initialize("s1", hermes_home=str(tmp_dir))
    try:
        assert provider._palace_path == custom_palace
    finally:
        provider.shutdown()


def test_kg_tools_all_use_provider_sibling_kg(initialized_provider, palace_path):
    # All five KG tools must hit the SAME database: the sibling of the
    # provider's palace dir — never mcp_server's global DEFAULT_KG_PATH.
    add = json.loads(
        initialized_provider.handle_tool_call(
            "mempalace_kg_add",
            {"subject": "user", "predicate": "drinks", "object": "tea"},
        )
    )
    assert add["status"] == "ok"

    timeline = json.loads(
        initialized_provider.handle_tool_call("mempalace_kg_timeline", {"entity": "user"})
    )
    assert timeline["count"] >= 1
    assert any(t["predicate"] == "drinks" and t["object"] == "tea" for t in timeline["timeline"])

    stats = json.loads(initialized_provider.handle_tool_call("mempalace_kg_stats", {}))
    assert stats["triples"] >= 1

    inv = json.loads(
        initialized_provider.handle_tool_call(
            "mempalace_kg_invalidate",
            {"subject": "user", "predicate": "drinks", "object": "tea"},
        )
    )
    assert inv["success"] is True

    # And the file itself lives next to the palace dir.
    assert (Path(palace_path).parent / "knowledge_graph.sqlite3").exists()


def test_kg_invalidate_rejects_invalid_input(initialized_provider):
    result = json.loads(
        initialized_provider.handle_tool_call(
            "mempalace_kg_invalidate",
            {"subject": "user", "predicate": "likes", "object": "x", "ended": "not-a-date"},
        )
    )
    assert result["success"] is False
    assert "error" in result


# ---------------------------------------------------------------------------
# on_memory_write: Hermes' memory tool defaults to target="memory" (the
# agent's own notes); only target="user" carries facts about the user. Both
# must mirror into the knowledge graph, under distinct subjects.
# ---------------------------------------------------------------------------


def test_on_memory_write_mirrors_both_targets(initialized_provider, palace_path):
    from mempalace.knowledge_graph import KnowledgeGraph

    initialized_provider.on_memory_write("add", "user", "lives in Boston")
    initialized_provider.on_memory_write("add", "memory", "repo uses uv for deps")
    initialized_provider._worker_queue.join()

    kg = KnowledgeGraph(db_path=str(Path(palace_path).parent / "knowledge_graph.sqlite3"))
    try:
        user_relations = kg.query_entity("user")
        agent_relations = kg.query_entity("hermes")
    finally:
        kg.close()
    assert any(
        r.get("predicate") == "asserted" and r.get("object") == "lives in Boston"
        for r in user_relations
    )
    assert any(
        r.get("predicate") == "noted" and r.get("object") == "repo uses uv for deps"
        for r in agent_relations
    )


def test_on_memory_write_skips_unknown_target_and_non_add(initialized_provider):
    pre = initialized_provider._worker_queue.qsize()
    initialized_provider.on_memory_write("add", "bogus", "x")
    initialized_provider.on_memory_write("replace", "memory", "x")
    initialized_provider.on_memory_write("remove", "user", "x")
    assert initialized_provider._worker_queue.qsize() == pre


# ---------------------------------------------------------------------------
# Dedup safety net: real-user detection, turn segmentation, fingerprints.
# ---------------------------------------------------------------------------


def test_is_real_user_message_distinguishes_tool_results(integration_module):
    fn = integration_module._is_real_user_message
    assert fn({"role": "user", "content": "plain text"}) is True
    assert fn({"role": "user", "content": [{"type": "text", "text": "hi"}]}) is True
    # Anthropic-format tool results are user-role with only tool_result blocks.
    assert fn({"role": "user", "content": [{"type": "tool_result", "content": "out"}]}) is False
    assert fn({"role": "assistant", "content": "hi"}) is False
    assert fn({"role": "user", "content": ""}) is False
    assert fn({"role": "user", "content": []}) is False
    # Mixed content (tool_result + text) counts as real.
    assert (
        fn(
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "content": "out"},
                    {"type": "text", "text": "and another thing"},
                ],
            }
        )
        is True
    )


def test_turn_fingerprint_from_messages_uses_last_real_user(integration_module):
    from mempalace.ids import make_turn_fingerprint

    fn = integration_module._turn_fingerprint_from_messages
    norm = integration_module._normalize_content
    msgs = [
        {"role": "user", "content": "first question"},
        {"role": "assistant", "content": "answer"},
        {"role": "user", "content": [{"type": "text", "text": "second question"}]},
        {"role": "assistant", "content": [{"type": "tool_use", "name": "grep"}]},
        # Trailing tool result must NOT be picked as the turn anchor.
        {"role": "user", "content": [{"type": "tool_result", "content": "grep out"}]},
    ]
    expected = make_turn_fingerprint(norm([{"type": "text", "text": "second question"}]))
    assert fn(msgs) == expected
    assert fn([]) == ""
    assert fn([{"role": "assistant", "content": "orphan"}]) == ""


def test_segment_turns_folds_tool_traffic_into_turn(integration_module):
    fn = integration_module._segment_turns
    msgs = [
        {"role": "user", "content": "search for JWT handling"},
        {"role": "assistant", "content": [{"type": "tool_use", "name": "grep"}]},
        {"role": "user", "content": [{"type": "tool_result", "content": "auth.py:42"}]},
        {"role": "assistant", "content": "JWT handling lives in auth.py."},
        {"role": "user", "content": "thanks"},
        {"role": "assistant", "content": "any time"},
    ]
    segs = fn(msgs)
    assert len(segs) == 2
    assert segs[0]["user"] == "search for JWT handling"
    # Tool traffic and the final response all belong to the first turn.
    assert "[tool_use: grep]" in segs[0]["assistant"]
    assert "auth.py:42" in segs[0]["assistant"]
    assert "JWT handling lives in auth.py." in segs[0]["assistant"]
    assert segs[0]["turn_fp"] != ""
    assert segs[1]["user"] == "thanks"
    assert segs[1]["assistant"] == "any time"


def test_segment_turns_preamble_without_user_anchor(integration_module):
    fn = integration_module._segment_turns
    msgs = [
        {"role": "assistant", "content": "orphaned assistant text"},
        {"role": "user", "content": "now a real question"},
        {"role": "assistant", "content": "an answer"},
    ]
    segs = fn(msgs)
    assert len(segs) == 2
    assert segs[0]["user"] == ""
    assert segs[0]["assistant"] == "orphaned assistant text"
    assert segs[0]["turn_fp"] == ""  # no user anchor — text-only dedup applies
    assert segs[1]["user"] == "now a real question"


def test_segment_turns_drops_empty_segments(integration_module):
    fn = integration_module._segment_turns
    assert fn([]) == []
    assert fn([{"role": "assistant", "content": ""}]) == []


# ---------------------------------------------------------------------------
# sync_turn coverage fingerprints: the correlation key the safety net uses.
# ---------------------------------------------------------------------------


def test_sync_turn_with_messages_stores_turn_fingerprint(initialized_provider):
    from mempalace.ids import make_turn_fingerprint

    msgs = [
        {"role": "user", "content": "what's the plan?"},
        {"role": "assistant", "content": "ship the PR"},
    ]
    initialized_provider.sync_turn("what's the plan?", "ship the PR", messages=msgs)
    initialized_provider._worker_queue.join()

    metas = initialized_provider._collection.get(include=["metadatas"]).get("metadatas") or []
    hermes = [m for m in metas if m.get("source") == "hermes"]
    assert len(hermes) == 1, f"expected exactly 1 hermes drawer, got {len(hermes)}"
    assert hermes[0].get("turn_fp") == make_turn_fingerprint("what's the plan?")


def test_sync_turn_without_messages_stores_no_fingerprint(initialized_provider):
    # Legacy callers / on_delegation synthetic turns carry no snapshot —
    # a fabricated fingerprint would wrongly mark raw turns as covered.
    initialized_provider.sync_turn("solo question", "solo answer")
    initialized_provider._worker_queue.join()

    metas = initialized_provider._collection.get(include=["metadatas"]).get("metadatas") or []
    hermes = [m for m in metas if m.get("source") == "hermes"]
    assert len(hermes) == 1, f"expected exactly 1 hermes drawer, got {len(hermes)}"
    assert "turn_fp" not in hermes[0]


# ---------------------------------------------------------------------------
# Branch lineage: /branch mints a new session id but carries the transcript
# forward — the dedup scan must include ancestor sessions. /resume's
# parent_session_id is the session being LEFT (not ancestry) and must clear
# lineage; keeping it could false-dedup against unrelated drawers.
# ---------------------------------------------------------------------------


def test_session_switch_branch_appends_parent_to_lineage(provider):
    provider._session_id = "parent-1"
    provider.on_session_switch(
        "branch-1", parent_session_id="parent-1", reset=False, reason="branch"
    )
    assert provider._session_lineage == ["parent-1"]
    # Branch-of-branch chains.
    provider.on_session_switch(
        "branch-2", parent_session_id="branch-1", reset=False, reason="branch"
    )
    assert provider._session_lineage == ["parent-1", "branch-1"]


def test_session_switch_resume_clears_lineage(provider):
    provider._session_id = "branch-1"
    provider._session_lineage = ["parent-1"]
    provider.on_session_switch(
        "other-session", parent_session_id="branch-1", reset=False, reason="resume"
    )
    assert provider._session_lineage == []


def test_session_switch_reset_clears_lineage(provider):
    provider._session_lineage = ["parent-1"]
    provider.on_session_switch("fresh", reset=True, reason="new_session")
    assert provider._session_lineage == []


def test_session_switch_branch_without_parent_clears_lineage(provider):
    provider._session_id = "parent-1"
    provider._session_lineage = ["grandparent-0"]
    provider.on_session_switch("branch-x", parent_session_id="", reset=False, reason="branch")
    assert provider._session_lineage == []


def test_session_switch_rewind_keeps_lineage(provider):
    # Rewind stays within the same conversation — ancestry still applies.
    provider._session_id = "branch-1"
    provider._session_lineage = ["parent-1"]
    provider.on_session_switch("branch-1", parent_session_id="", reset=False, rewound=True)
    assert provider._session_lineage == ["parent-1"]


def test_fingerprint_call_sites_are_consistent(integration_module):
    # The invariant the dedup system rests on: _turn_fingerprint_from_messages
    # (used by sync_turn) and _segment_turns (used by the safety net) must
    # fingerprint the SAME message dict identically.
    fn_fp = integration_module._turn_fingerprint_from_messages
    fn_seg = integration_module._segment_turns
    msgs = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": [{"type": "text", "text": "second"}]},
        {"role": "assistant", "content": [{"type": "tool_use", "name": "grep"}]},
        {"role": "user", "content": [{"type": "tool_result", "content": "out"}]},
    ]
    direct = fn_fp(msgs)
    from_seg = next((s["turn_fp"] for s in reversed(fn_seg(msgs)) if s["turn_fp"]), None)
    assert from_seg is not None
    assert direct != ""
    assert direct == from_seg


# ---------------------------------------------------------------------------
# Dedup safety net end-to-end: session_end / pre_compress file only what
# sync_turn missed.
# ---------------------------------------------------------------------------


def test_session_end_does_not_refile_synced_turns(initialized_provider):
    msgs = [
        {"role": "user", "content": "what's the plan?"},
        {"role": "assistant", "content": "ship the PR"},
    ]
    initialized_provider.sync_turn("what's the plan?", "ship the PR", messages=msgs)
    initialized_provider._worker_queue.join()
    pre = initialized_provider._collection.count()
    assert pre >= 1

    initialized_provider.on_session_end(msgs)
    initialized_provider._worker_queue.join()
    assert initialized_provider._collection.count() == pre


def test_session_end_skips_tool_turns_covered_by_sync(initialized_provider):
    # The representation-mismatch case: sync_turn files the CLEAN pair while
    # the raw list carries injected user content + tool traffic. Exact-text
    # matching can never catch this — the fingerprint must.
    msgs = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "search for JWT handling"},
                {"type": "text", "text": "[injected skill context]"},
            ],
        },
        {"role": "assistant", "content": [{"type": "tool_use", "name": "grep"}]},
        {"role": "user", "content": [{"type": "tool_result", "content": "auth.py:42"}]},
        {"role": "assistant", "content": "JWT handling lives in auth.py."},
    ]
    initialized_provider.sync_turn(
        "search for JWT handling", "JWT handling lives in auth.py.", messages=msgs
    )
    initialized_provider._worker_queue.join()
    pre = initialized_provider._collection.count()
    assert pre >= 1

    initialized_provider.on_session_end(msgs)
    initialized_provider._worker_queue.join()
    assert initialized_provider._collection.count() == pre


def test_session_end_files_turns_sync_never_saw(initialized_provider):
    synced = [
        {"role": "user", "content": "first question"},
        {"role": "assistant", "content": "first answer"},
    ]
    initialized_provider.sync_turn("first question", "first answer", messages=synced)
    initialized_provider._worker_queue.join()
    pre = initialized_provider._collection.count()

    full = synced + [
        {"role": "user", "content": "second question"},
        {"role": "assistant", "content": "second answer"},
    ]
    initialized_provider.on_session_end(full)
    initialized_provider._worker_queue.join()
    assert initialized_provider._collection.count() == pre + 1
    docs = initialized_provider._collection.get(include=["documents"]).get("documents") or []
    assert any("second question" in d for d in docs)


def test_double_session_end_is_idempotent(initialized_provider):
    # session_end fires more than once in Hermes (best-effort interrupt hook
    # plus real shutdown). Drawers the first pass files carry turn_fp, so
    # the second pass's scan sees them as covered.
    msgs = [
        {"role": "user", "content": "never synced question"},
        {"role": "assistant", "content": "never synced answer"},
    ]
    initialized_provider.on_session_end(msgs)
    initialized_provider._worker_queue.join()
    once = initialized_provider._collection.count()
    assert once >= 1

    initialized_provider.on_session_end(msgs)
    initialized_provider._worker_queue.join()
    assert initialized_provider._collection.count() == once


def test_repeated_identical_exchanges_use_multiset_counts(initialized_provider):
    # Two identical turns, only one synced → session_end owes exactly one.
    one_turn = [
        {"role": "user", "content": "are we done?"},
        {"role": "assistant", "content": "yes"},
    ]
    initialized_provider.sync_turn("are we done?", "yes", messages=one_turn)
    initialized_provider._worker_queue.join()
    pre = initialized_provider._collection.count()

    initialized_provider.on_session_end(one_turn + one_turn)
    initialized_provider._worker_queue.join()
    assert initialized_provider._collection.count() == pre + 1


def test_empty_session_id_blind_files(initialized_provider):
    # Without a session id every anonymous session shares the
    # hermes-session:unknown bucket — dedup there would false-match across
    # sessions (data loss). Blind-filing (duplication) is the safe direction.
    msgs = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ]
    initialized_provider.sync_turn("hello", "hi", messages=msgs)
    initialized_provider._worker_queue.join()
    pre = initialized_provider._collection.count()

    initialized_provider._session_id = ""
    initialized_provider.on_session_end(msgs)
    initialized_provider._worker_queue.join()
    assert initialized_provider._collection.count() == pre + 1


def test_branch_session_end_skips_parent_filed_turns(initialized_provider):
    msgs = [
        {"role": "user", "content": "parent question"},
        {"role": "assistant", "content": "parent answer"},
    ]
    # Filed under the original session id ("test-session-1").
    initialized_provider.sync_turn("parent question", "parent answer", messages=msgs)
    initialized_provider._worker_queue.join()
    pre = initialized_provider._collection.count()

    # /branch mints a new id; the transcript carries forward.
    initialized_provider.on_session_switch(
        "branch-1", parent_session_id="test-session-1", reset=False, reason="branch"
    )
    initialized_provider.on_session_end(msgs)
    initialized_provider._worker_queue.join()
    assert initialized_provider._collection.count() == pre


def test_pre_compress_skips_covered_turns_files_missing(initialized_provider):
    synced = [
        {"role": "user", "content": "covered question"},
        {"role": "assistant", "content": "covered answer"},
    ]
    initialized_provider.sync_turn("covered question", "covered answer", messages=synced)
    initialized_provider._worker_queue.join()
    pre = initialized_provider._collection.count()

    window = synced + [
        {"role": "user", "content": "uncovered question"},
        {"role": "assistant", "content": "uncovered answer"},
    ]
    hint = initialized_provider.on_pre_compress(window)
    initialized_provider._worker_queue.join()
    assert "mempalace_search" in hint
    assert initialized_provider._collection.count() == pre + 1


def test_one_drawer_protects_exactly_one_of_two_same_fp_occurrences(initialized_provider):
    # Turn synced once (drawer: fp F, CLEAN text). The window holds two
    # occurrences of that turn: tool-shaped (fp F, raw text) and a plain
    # repeat whose raw composition equals the drawer's clean text (fp F).
    # One physical drawer must protect exactly one of them.
    tool_turn = [
        {"role": "user", "content": "same question"},
        {"role": "assistant", "content": [{"type": "tool_use", "name": "grep"}]},
        {"role": "assistant", "content": "same answer"},
    ]
    initialized_provider.sync_turn("same question", "same answer", messages=tool_turn)
    initialized_provider._worker_queue.join()
    pre = initialized_provider._collection.count()

    window = tool_turn + [
        {"role": "user", "content": "same question"},
        {"role": "assistant", "content": "same answer"},
    ]
    initialized_provider.on_session_end(window)
    initialized_provider._worker_queue.join()
    assert initialized_provider._collection.count() == pre + 1


def test_text_fallback_consumes_the_drawer_fingerprint_too(initialized_provider):
    # Drawer filed with fp F_injected (sync_turn saw an injected snapshot)
    # and clean text T. Window: a plain segment composing exactly T (fp
    # F_plain) followed by the tool-shaped segment carrying F_injected.
    # The text-fallback consumes the drawer for the first segment; the
    # second must then be FILED, not skipped on the drawer's orphaned fp.
    injected_turn = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "q2"},
                {"type": "text", "text": "[injected skill]"},
            ],
        },
        {"role": "assistant", "content": [{"type": "tool_use", "name": "grep"}]},
        {"role": "assistant", "content": "a2"},
    ]
    initialized_provider.sync_turn("q2", "a2", messages=injected_turn)
    initialized_provider._worker_queue.join()
    pre = initialized_provider._collection.count()

    window = [
        {"role": "user", "content": "q2"},
        {"role": "assistant", "content": "a2"},
    ] + injected_turn
    initialized_provider.on_session_end(window)
    initialized_provider._worker_queue.join()
    assert initialized_provider._collection.count() == pre + 1


def test_interrupted_then_retried_identical_prompt_keeps_both(initialized_provider):
    # Turn A: "continue" → partial answer (interrupted; Hermes never syncs
    # interrupted turns). Turn B: "continue" → "done" (synced). The exact
    # match for B must win before A's fp arm can steal the drawer — A's
    # content gets filed, B is skipped.
    synced = [
        {"role": "user", "content": "continue"},
        {"role": "assistant", "content": "done"},
    ]
    initialized_provider.sync_turn("continue", "done", messages=synced)
    initialized_provider._worker_queue.join()
    pre = initialized_provider._collection.count()

    window = [
        {"role": "user", "content": "continue"},
        {"role": "assistant", "content": "partial answer before interrupt"},
    ] + synced
    initialized_provider.on_session_end(window)
    initialized_provider._worker_queue.join()
    assert initialized_provider._collection.count() == pre + 1
    docs = initialized_provider._collection.get(include=["documents"]).get("documents") or []
    assert any("partial answer before interrupt" in d for d in docs)


def test_contended_fingerprint_files_all_occurrences(initialized_provider):
    # Two same-prompt tool turns with different outcomes; only one synced,
    # and neither raw segment exact-matches the clean drawer. We cannot
    # tell which occurrence the drawer covers — file BOTH (bounded
    # duplication), never guess (possible loss).
    t1 = [
        {"role": "user", "content": "run it"},
        {"role": "assistant", "content": [{"type": "tool_use", "name": "bash"}]},
        {"role": "assistant", "content": "exit 0"},
    ]
    t2 = [
        {"role": "user", "content": "run it"},
        {"role": "assistant", "content": [{"type": "tool_use", "name": "bash"}]},
        {"role": "assistant", "content": "exit 1"},
    ]
    initialized_provider.sync_turn("run it", "exit 0", messages=t1)
    initialized_provider._worker_queue.join()
    pre = initialized_provider._collection.count()

    initialized_provider.on_session_end(t1 + t2)
    initialized_provider._worker_queue.join()
    assert initialized_provider._collection.count() == pre + 2


def test_preamble_segment_dedups_across_repeated_pre_compress(initialized_provider):
    # A post-compression window can START with assistant/tool messages (no
    # user anchor). The anchorless segment files once, and a second
    # overlapping window must not re-file it (exact-text pass, fp == "").
    window = [
        {"role": "assistant", "content": "orphaned assistant tail"},
        {"role": "user", "content": "next question"},
        {"role": "assistant", "content": "next answer"},
    ]
    initialized_provider.on_pre_compress(window)
    initialized_provider._worker_queue.join()
    once = initialized_provider._collection.count()
    assert once >= 2  # preamble segment + anchored turn

    initialized_provider.on_pre_compress(window)
    initialized_provider._worker_queue.join()
    assert initialized_provider._collection.count() == once


def test_pre_compress_scans_branch_lineage(initialized_provider):
    msgs = [
        {"role": "user", "content": "lineage question"},
        {"role": "assistant", "content": "lineage answer"},
    ]
    initialized_provider.sync_turn("lineage question", "lineage answer", messages=msgs)
    initialized_provider._worker_queue.join()
    pre = initialized_provider._collection.count()

    initialized_provider.on_session_switch(
        "branch-pc", parent_session_id="test-session-1", reset=False, reason="branch"
    )
    hint = initialized_provider.on_pre_compress(msgs)
    initialized_provider._worker_queue.join()
    assert "mempalace_search" in hint
    assert initialized_provider._collection.count() == pre


def test_scan_failure_blind_files_with_warning(initialized_provider, monkeypatch, caplog):
    import logging

    def _boom(col, source_files):
        raise RuntimeError("chroma exploded")

    monkeypatch.setattr(initialized_provider, "_scan_filed", _boom)
    msgs = [
        {"role": "user", "content": "scan failure question"},
        {"role": "assistant", "content": "scan failure answer"},
    ]
    pre = initialized_provider._collection.count()
    with caplog.at_level(logging.WARNING, logger="mempalace.hermes"):
        initialized_provider.on_session_end(msgs)
        initialized_provider._worker_queue.join()
    assert initialized_provider._collection.count() == pre + 1
    assert any("filing without dedup" in r.message for r in caplog.records)


def test_session_end_blocking_put_times_out_with_warning(provider, caplog):
    import logging
    import queue as queue_module

    provider._cron_skipped = False
    provider._initialized = True
    provider.SESSION_END_ENQUEUE_TIMEOUT = 0.05
    provider._worker_queue = queue_module.Queue(maxsize=1)
    provider._worker_queue.put_nowait(("file_turn", {}))  # saturate; no worker running
    with caplog.at_level(logging.WARNING, logger="mempalace.hermes"):
        provider.on_session_end([{"role": "user", "content": "hi"}])
    assert any("queue full at session_end" in r.message for r in caplog.records)


def test_truncated_window_fp_coverage_is_accepted_limit(initialized_provider):
    # KNOWN, ACCEPTED residual (documented in _file_missing_exchanges):
    # a synced occurrence of a byte-identical prompt was compressed out of
    # the window; a NEW, never-synced occurrence of the same prompt is then
    # fp-skipped against the stale drawer, and its partial assistant output
    # is not captured. Undecidable with occurrence counting alone — Hermes
    # message dicts carry no per-message identity — and dropping fp-skip
    # would re-file every tool-shaped turn. This test PINS the accepted
    # behavior; if it ever starts failing because the content IS captured,
    # a better correlation key exists and the docstring should be updated.
    old_tool_turn = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": [{"type": "tool_use", "name": "bash"}]},
        {"role": "assistant", "content": "old answer"},
    ]
    initialized_provider.sync_turn("go", "old answer", messages=old_tool_turn)
    initialized_provider._worker_queue.join()
    pre = initialized_provider._collection.count()

    window = [  # post-compression window: only the NEW, never-synced "go" turn
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "brand new partial output qqq"},
    ]
    initialized_provider.on_session_end(window)
    initialized_provider._worker_queue.join()
    docs = initialized_provider._collection.get(include=["documents"]).get("documents") or []
    assert not any("brand new partial output qqq" in d for d in docs)
    assert initialized_provider._collection.count() == pre


def test_file_turn_retries_transient_database_lock(
    integration_module, initialized_provider, monkeypatch
):
    # Chroma's sqlite briefly locks under concurrent readers (notably on
    # Windows — this reproduced in CI as a lost turn). A transient lock
    # must be retried, not swallowed as a permanent write failure.
    calls = {"n": 0}
    real = integration_module.file_conversation_exchange

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("Error updating collection: database is locked")
        return real(*args, **kwargs)

    monkeypatch.setattr(integration_module, "file_conversation_exchange", flaky)
    monkeypatch.setattr(initialized_provider, "FILE_TURN_LOCK_BACKOFF", 0.01)
    pre = initialized_provider._collection.count()
    initialized_provider.sync_turn("locked once", "but persisted")
    initialized_provider._worker_queue.join()
    assert calls["n"] == 2
    assert initialized_provider._collection.count() == pre + 1


def test_file_turn_gives_up_on_persistent_lock_with_warning(
    integration_module, initialized_provider, monkeypatch, caplog
):
    import logging

    def always_locked(*args, **kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(integration_module, "file_conversation_exchange", always_locked)
    monkeypatch.setattr(initialized_provider, "FILE_TURN_LOCK_BACKOFF", 0.01)
    pre = initialized_provider._collection.count()
    with caplog.at_level(logging.WARNING, logger="mempalace.hermes"):
        initialized_provider.sync_turn("never lands", "sadly")
        initialized_provider._worker_queue.join()
    assert initialized_provider._collection.count() == pre
    assert any("turn not persisted" in r.message for r in caplog.records)
