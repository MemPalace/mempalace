"""Tests for the ``mempalace hermes install`` CLI command helpers.

The end-to-end install flow shells out to ``pip`` and writes into the user's
Hermes home — out of scope for unit tests. These tests cover the pure helpers
that resolve paths and edit ``config.yaml``, since those carried the riskiest
review findings on PR #1684 (palace_path divergence, YAML edit brittleness,
Windows path).
"""

from __future__ import annotations

import types
from pathlib import Path

import pytest
import yaml

from mempalace.cli import (
    _atomic_write_text,
    _resolve_hermes_home,
    _resolve_install_palace_path,
    _resolve_install_python,
    _update_hermes_config_yaml,
)


# ---------------------------------------------------------------------------
# _resolve_hermes_home
# ---------------------------------------------------------------------------


def _args(hermes_home=None):
    return types.SimpleNamespace(hermes_home=hermes_home)


def test_resolve_hermes_home_uses_explicit_arg(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_HOME", raising=False)
    explicit = tmp_path / "custom-home"
    assert _resolve_hermes_home(_args(str(explicit))) == explicit.resolve()


def test_resolve_hermes_home_uses_env_var(tmp_path, monkeypatch):
    target = tmp_path / "env-home"
    monkeypatch.setenv("HERMES_HOME", str(target))
    assert _resolve_hermes_home(_args(None)) == target


def test_resolve_hermes_home_defaults_to_dot_hermes(monkeypatch):
    monkeypatch.delenv("HERMES_HOME", raising=False)
    assert _resolve_hermes_home(_args(None)) == Path("~/.hermes").expanduser()


def test_resolve_hermes_home_refuses_cwd(monkeypatch):
    monkeypatch.delenv("HERMES_HOME", raising=False)
    # ``.`` resolves to CWD — would silently install plugin files there.
    with pytest.raises(SystemExit) as excinfo:
        _resolve_hermes_home(_args("."))
    assert excinfo.value.code == 2


def test_resolve_hermes_home_refuses_root(monkeypatch):
    monkeypatch.delenv("HERMES_HOME", raising=False)
    with pytest.raises(SystemExit) as excinfo:
        _resolve_hermes_home(_args("/"))
    assert excinfo.value.code == 2


# ---------------------------------------------------------------------------
# _resolve_install_python
# ---------------------------------------------------------------------------


def test_resolve_install_python_prefers_posix_venv(tmp_path):
    venv_python = tmp_path / "hermes-agent" / "venv" / "bin" / "python3"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("#!/bin/sh\n")
    assert _resolve_install_python(tmp_path) == str(venv_python)


def test_resolve_install_python_falls_back_to_python_no_3(tmp_path):
    # Some venvs only ship `python` not `python3` (Windows-style scaffolds).
    p = tmp_path / "hermes-agent" / "venv" / "bin" / "python"
    p.parent.mkdir(parents=True)
    p.write_text("#!/bin/sh\n")
    assert _resolve_install_python(tmp_path) == str(p)


def test_resolve_install_python_finds_windows_layout(tmp_path):
    p = tmp_path / "hermes-agent" / "venv" / "Scripts" / "python.exe"
    p.parent.mkdir(parents=True)
    p.write_text("#!/bin/sh\n")
    assert _resolve_install_python(tmp_path) == str(p)


def test_resolve_install_python_falls_back_to_sys_executable(tmp_path):
    import sys

    # No venv anywhere under hermes_home → fall back gracefully.
    assert _resolve_install_python(tmp_path) == sys.executable


# ---------------------------------------------------------------------------
# _resolve_install_palace_path
# ---------------------------------------------------------------------------


def test_palace_path_honors_env_var(tmp_path, monkeypatch):
    target = tmp_path / "user-chosen-palace"
    monkeypatch.setenv("MEMPALACE_PALACE_PATH", str(target))
    assert _resolve_install_palace_path() == target


def test_palace_path_default_when_env_unset(monkeypatch):
    monkeypatch.delenv("MEMPALACE_PALACE_PATH", raising=False)
    assert _resolve_install_palace_path() == Path("~/.mempalace/palace").expanduser()


def test_palace_path_ignores_empty_env_var(monkeypatch):
    # ``export MEMPALACE_PALACE_PATH=`` is intent to unset, not to use "".
    monkeypatch.setenv("MEMPALACE_PALACE_PATH", "")
    assert _resolve_install_palace_path() == Path("~/.mempalace/palace").expanduser()


# ---------------------------------------------------------------------------
# _atomic_write_text
# ---------------------------------------------------------------------------


def test_atomic_write_text_creates_parent_dirs(tmp_path):
    target = tmp_path / "deep" / "nested" / "file.yaml"
    _atomic_write_text(target, "hello\n")
    assert target.read_text() == "hello\n"


def test_atomic_write_text_overwrites_existing(tmp_path):
    target = tmp_path / "f.txt"
    target.write_text("original\n")
    _atomic_write_text(target, "replaced\n")
    assert target.read_text() == "replaced\n"
    # No stray .tmp left behind.
    assert not (tmp_path / "f.txt.tmp").exists()


# ---------------------------------------------------------------------------
# _update_hermes_config_yaml
# ---------------------------------------------------------------------------


def test_yaml_update_adds_memory_section_when_missing(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("model: claude-opus\n")
    status, msg = _update_hermes_config_yaml(config, "mempalace")
    assert status == "updated"
    data = yaml.safe_load(config.read_text())
    assert data["memory"]["provider"] == "mempalace"
    assert data["model"] == "claude-opus"
    assert "Updated" in msg


def test_yaml_update_replaces_existing_provider(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("memory:\n  provider: honcho\n")
    status, _ = _update_hermes_config_yaml(config, "mempalace")
    assert status == "updated"
    assert yaml.safe_load(config.read_text())["memory"]["provider"] == "mempalace"


def test_yaml_update_handles_scalar_memory_value(tmp_path):
    # Original line-based heuristic produced duplicate `memory:` keys for this.
    config = tmp_path / "config.yaml"
    config.write_text("memory: ~\nmodel: x\n")
    status, _ = _update_hermes_config_yaml(config, "mempalace")
    assert status == "updated"
    data = yaml.safe_load(config.read_text())
    assert data["memory"]["provider"] == "mempalace"
    assert data["model"] == "x"
    # Exactly one top-level ``memory:`` key. The earlier heuristic produced
    # two when the original value was a scalar — yaml round-tripping rules
    # that out structurally, but assert it via the actual count to catch any
    # future regression at the serialiser level.
    text = config.read_text()
    top_level_memory = sum(
        1 for line in text.splitlines() if line.startswith("memory:") or line.rstrip() == "memory:"
    )
    assert top_level_memory == 1


def test_yaml_update_creates_file_when_missing(tmp_path):
    config = tmp_path / "config.yaml"
    assert not config.exists()
    status, _ = _update_hermes_config_yaml(config, "mempalace")
    assert status == "updated"
    assert config.exists()
    assert yaml.safe_load(config.read_text())["memory"]["provider"] == "mempalace"


def test_yaml_update_noop_when_already_set(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("memory:\n  provider: mempalace\n")
    status, msg = _update_hermes_config_yaml(config, "mempalace")
    # Distinct from "error" — caller should NOT fail the install for this.
    assert status == "noop"
    assert "already has" in msg


def test_yaml_update_rejects_non_mapping(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("- not\n- a\n- mapping\n")
    status, msg = _update_hermes_config_yaml(config, "mempalace")
    # Distinct from "noop" — install command should exit non-zero.
    assert status == "error"
    assert "not a YAML mapping" in msg


def test_yaml_update_handles_malformed_yaml(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("{ this is: not: parseable\n")
    status, msg = _update_hermes_config_yaml(config, "mempalace")
    assert status == "error"
    assert "Could not parse" in msg


def test_yaml_update_is_atomic(tmp_path):
    # The write happens via _atomic_write_text — no .tmp leaks on success.
    config = tmp_path / "config.yaml"
    config.write_text("model: x\n")
    _update_hermes_config_yaml(config, "mempalace")
    assert not (tmp_path / "config.yaml.tmp").exists()


# ---------------------------------------------------------------------------
# Backfill / live-provider routing parity.
# ---------------------------------------------------------------------------


def _load_backfill_module():
    """Load backfill.py by path — the same way the install command does.

    Tests must use this loading mode (not a package import) so they catch
    anything that only breaks under ``spec_from_file_location``.
    """
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
    return backfill


def test_backfill_classify_wing_delegates_to_live_provider():
    """Backfill must route wings exactly like live writes.

    ``classify_wing`` delegates to the provider's
    ``_match_wing_by_keywords`` — checks the delegation survives the
    by-path loading mode, so nobody reintroduces a drifting copy.
    """
    backfill = _load_backfill_module()

    wing_config = {"wing_ai": {"keywords": ["ai"]}}
    # Word-boundary matching: substring matching would route "said" /
    # "available" into the ai wing.
    assert backfill.classify_wing("She said rain is available", wing_config) == "wing_general"
    assert backfill.classify_wing("write some ai bindings", wing_config) == "wing_ai"


def test_backfill_rerun_does_not_duplicate(tmp_path):
    """Re-running backfill over the same sessions must not re-file drawers.

    Exchange drawer ids are salted with ``filed_at``, so without the
    ``file_already_mined`` guard every re-run of ``mempalace hermes
    install`` would re-file every exchange under fresh ids — the exact
    duplicated-dedup failure flagged in the PR #1684 review.
    """
    import json

    backfill = _load_backfill_module()

    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "sess1.json").write_text(
        json.dumps(
            [
                {"role": "user", "content": "let's plan the Q3 roadmap in detail"},
                {"role": "assistant", "content": "here is the detailed plan"},
            ]
        )
    )
    palace = tmp_path / "palace"
    palace.mkdir()

    assert backfill.backfill(sessions, str(palace)) == 1

    from mempalace.backends.chroma import ChromaBackend

    col = ChromaBackend().get_or_create_collection(str(palace), "mempalace_drawers")
    count_after_first = col.count()
    assert count_after_first == 1

    assert backfill.backfill(sessions, str(palace)) == 0
    assert col.count() == count_after_first


def test_messages_to_exchanges_keeps_final_answer_on_tool_turns():
    """A tool-calling turn is user → assistant(tool_use) → user(tool_result)
    → assistant(final text). Pairing user with the immediately-next
    assistant message grabbed the tool_use stub and DROPPED the real
    answer — the common case for a coding agent, and a verbatim
    violation. Segmentation must fold everything up to the next real
    user message into the assistant side (via the live provider's
    _segment_turns — one implementation, not two).
    """
    backfill = _load_backfill_module()

    exchanges = backfill._messages_to_exchanges(
        [
            {"role": "user", "content": "check the weather and tell me if I need an umbrella"},
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "1", "name": "get_weather", "input": {}}],
            },
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "1", "content": "rainy"}],
            },
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "Yes, bring an umbrella - it's rainy."}],
            },
        ]
    )
    assert len(exchanges) == 1
    assert exchanges[0]["user"] == "check the weather and tell me if I need an umbrella"
    assert "Yes, bring an umbrella - it's rainy." in exchanges[0]["assistant"]
    # Tool traffic is verbatim history too — the markers must survive.
    assert "[tool_use: get_weather]" in exchanges[0]["assistant"]
    assert "rainy" in exchanges[0]["assistant"]


def test_parse_session_file_unwraps_hermes_export_jsonl(tmp_path):
    """`hermes sessions export` writes one SESSION object per line
    ({**session, "messages": [...]}); treating each line as a message
    silently produced zero exchanges. The .json dict branch already
    unwraps this shape — .jsonl must too.
    """
    import json

    backfill = _load_backfill_module()

    f = tmp_path / "export.jsonl"
    f.write_text(
        json.dumps(
            {
                "id": "sess-123",
                "messages": [
                    {"role": "user", "content": "let's plan the Q3 roadmap"},
                    {"role": "assistant", "content": "here is the plan"},
                ],
            }
        )
        + "\n"
        + json.dumps(
            {
                "id": "sess-124",
                "messages": [
                    {"role": "user", "content": "and the Q4 one"},
                    {"role": "assistant", "content": "here too"},
                ],
            }
        )
        + "\n"
    )
    exchanges = backfill.parse_session_file(f)
    assert len(exchanges) == 2
    assert exchanges[0]["user"] == "let's plan the Q3 roadmap"
    assert exchanges[1]["assistant"] == "here too"


def test_parse_session_file_still_reads_message_per_line_jsonl(tmp_path):
    import json

    backfill = _load_backfill_module()

    f = tmp_path / "raw.jsonl"
    f.write_text(
        json.dumps({"role": "user", "content": "plain question"})
        + "\n"
        + json.dumps({"role": "assistant", "content": "plain answer"})
        + "\n"
    )
    exchanges = backfill.parse_session_file(f)
    assert exchanges == [{"user": "plain question", "assistant": "plain answer"}]


def test_file_exchange_files_assistant_only_preamble_with_live_composition(tmp_path):
    """Anchorless preamble segments (a transcript starting mid-turn) carry
    assistant-side content only; dropping them violates verbatim. The
    guard rejects only both-empty exchanges, and the drawer text must be
    composed by the live provider's _compose_exchange_text — the exact
    composition the dedup safety net compares against.
    """
    backfill = _load_backfill_module()

    from mempalace.backends.chroma import ChromaBackend
    from mempalace.integrations.hermes import _compose_exchange_text

    palace = tmp_path / "palace"
    palace.mkdir()
    col = ChromaBackend().get_or_create_collection(str(palace), "mempalace_drawers")
    src = tmp_path / "sess.json"
    src.write_text("[]")
    ok = backfill.file_exchange(
        {"user": "", "assistant": "orphaned assistant text"},
        "wing_general",
        str(src),
        collection=col,
    )
    assert ok is True
    docs = col.get(include=["documents"]).get("documents") or []
    assert docs == [_compose_exchange_text("", "orphaned assistant text")]
