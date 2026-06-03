"""Tests for the MemPalace ↔ Hermes integration provider.

The provider lives outside the importable ``mempalace`` package (it sits in
``mempalace/integrations/hermes/`` so it can be copied into
``~/.hermes/plugins/`` at install time). These tests load it the same way:
by file path.

They also stub ``agent.memory_provider`` to mirror the runtime contract — the
plugin is only ever imported with Hermes on the import path.
"""

from __future__ import annotations

import importlib.util
import json
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


# ---------------------------------------------------------------------------
# Shape: name, schemas, config, availability
# ---------------------------------------------------------------------------


def test_name_matches_plugin_yaml(provider):
    assert provider.name == "mempalace"


def test_is_available_imports_mempalace(provider):
    # The repo's own dev install satisfies this. Failure means a broken venv.
    assert provider.is_available() is True


def test_tool_schemas_hidden_before_initialize(provider):
    # Required by ABC: callers must not see schemas while initialize() hasn't
    # been called — half-initialized state would otherwise advertise tools the
    # provider cannot actually service.
    assert provider.get_tool_schemas() == []


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


def test_tool_schemas_module_constant_has_eight_tools(integration_module):
    # Documented in mempalace/integrations/hermes/README.md as "8 tools exposed".
    schemas = integration_module.TOOL_SCHEMAS
    names = [s["name"] for s in schemas]
    assert names == [
        "mempalace_search",
        "mempalace_status",
        "mempalace_list_wings",
        "mempalace_list_rooms",
        "mempalace_kg_query",
        "mempalace_kg_add",
        "mempalace_diary_write",
        "mempalace_diary_read",
    ]


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


def test_backfill_classify_wing_matches_live_provider():
    # Import backfill the same way the install command does.
    import importlib.util

    backfill_path = (
        Path(__file__).resolve().parent.parent
        / "mempalace"
        / "integrations"
        / "hermes"
        / "backfill.py"
    )
    spec = importlib.util.spec_from_file_location("hermes_backfill", backfill_path)
    assert spec is not None and spec.loader is not None
    backfill = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(backfill)

    wing_config = {"wing_ai": {"keywords": ["ai"]}}
    # Both the live provider and backfill must reject substring matches —
    # otherwise backfilled drawers route differently than live writes.
    assert backfill.classify_wing("She said rain is available", wing_config) == "wing_general"
    assert backfill.classify_wing("write some ai bindings", wing_config) == "wing_ai"


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
# on_pre_compress: contract is (a) file the discarded messages, (b) return a
# string to inject into the compression summary prompt.
# ---------------------------------------------------------------------------


def test_on_pre_compress_returns_string_hint_only_when_ready(provider):
    # Before initialize: no hint (provider can't actually persist anything).
    provider._cron_skipped = False
    assert provider.on_pre_compress([{"role": "user", "content": "hi"}]) == ""

    # Simulate post-initialize ready state. We don't need a real backend for
    # this assertion — just the readiness flag the hint gates on.
    provider._initialized = True
    hint = provider.on_pre_compress([{"role": "user", "content": "hi"}])
    assert isinstance(hint, str) and "mempalace_search" in hint


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
    yield provider
    provider.shutdown()


def test_initialize_opens_chroma_via_backend(initialized_provider):
    """The dim-mismatch fix: collection access goes through ChromaBackend."""
    from mempalace.backends.chroma import ChromaBackend

    assert initialized_provider._initialized is True
    assert initialized_provider._collection is not None
    assert isinstance(initialized_provider._backend, ChromaBackend)


def test_get_tool_schemas_returns_eight_after_initialize(initialized_provider):
    schemas = initialized_provider.get_tool_schemas()
    names = [s["name"] for s in schemas]
    assert len(schemas) == 8
    assert "mempalace_search" in names
    assert "mempalace_kg_query" in names


def test_sync_turn_persists_through_worker(initialized_provider):
    initialized_provider.sync_turn("what's the plan?", "ship the PR")
    initialized_provider._worker_queue.join()  # block until worker drains the task

    col = initialized_provider._collection
    assert col.count() >= 1
    metas = col.get(include=["metadatas"]).get("metadatas") or []
    assert any(m.get("source") == "hermes" for m in metas)


def test_sync_turn_routes_to_configured_wing(initialized_provider):
    initialized_provider._wing_config = {"wing_dev": {"keywords": ["pytest"]}}
    initialized_provider.sync_turn("running pytest -q", "all passed")
    initialized_provider._worker_queue.join()

    metas = initialized_provider._collection.get(include=["metadatas"]).get("metadatas") or []
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


def test_collection_name_is_not_user_configurable(provider, tmp_dir, palace_path):
    # Exposing ``collection_name`` would let the provider write to a collection
    # that ``search_memories`` (which reads from mempalace's own config) does
    # not read — making the provider silently appear mute. The field is
    # intentionally absent from the schema and ignored in config files.
    (Path(tmp_dir) / "mempalace.json").write_text(
        json.dumps({"palace_path": palace_path, "collection_name": "custom_drawers"})
    )
    provider.initialize("s1", hermes_home=str(tmp_dir))
    try:
        assert provider._collection_name == provider.DEFAULT_COLLECTION_NAME
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
