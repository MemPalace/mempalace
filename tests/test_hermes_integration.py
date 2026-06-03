"""Tests for the MemPalace ↔ Hermes integration provider.

The provider lives outside the importable ``mempalace`` package (it sits in
``integrations/hermes/`` so it can be copied into ``~/.hermes/plugins/`` at
install time). These tests load it the same way: by file path.

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
    path = Path(__file__).resolve().parent.parent / "integrations" / "hermes" / "__init__.py"
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
        "collection_name",
    }


def test_tool_schemas_module_constant_has_eight_tools(integration_module):
    # Documented in integrations/hermes/README.md as "8 tools exposed".
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


def test_on_pre_compress_returns_string_hint(provider):
    # Bypass full initialize() — we only need _cron_skipped=False and the queue.
    provider._cron_skipped = False
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
