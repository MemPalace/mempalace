from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mempalace import hooks_cli
from mempalace.write_routing import (
    ResolvedWriteRoutingPolicy,
    WriteRoutingError,
    WriteRoutingPolicy,
    WriteRoutingTarget,
)


class _HookConfig:
    def __init__(
        self,
        policy: WriteRoutingPolicy = WriteRoutingPolicy.DIRECT,
        *,
        source: str = "test",
        routing_error: Exception | None = None,
        palace_path: str = "/tmp/palace",
    ):
        self._policy = policy
        self._source = source
        self._routing_error = routing_error
        self.palace_path = palace_path
        self.hooks_auto_save = True
        self.hook_silent_save = True
        self.hook_desktop_toast = False

    def resolve_write_routing(self, scope: str) -> ResolvedWriteRoutingPolicy:
        assert scope == "hooks"
        if self._routing_error is not None:
            raise self._routing_error
        return ResolvedWriteRoutingPolicy(
            policy=self._policy,
            source=self._source,
        )


@pytest.fixture(autouse=True)
def _clear_hook_routing_context(monkeypatch):
    token = hooks_cli._HOOK_WRITE_ROUTING_CONTEXT.set(None)

    for key in (
        "MEMPALACE_WRITE_ROUTING",
        "MEMPALACE_HOOK_WRITE_ROUTING",
        "MEMPALACE_HOOKS_DAEMON",
    ):
        monkeypatch.delenv(key, raising=False)

    try:
        yield
    finally:
        hooks_cli._HOOK_WRITE_ROUTING_CONTEXT.reset(token)


def _write_transcript(path: Path, count: int = 3) -> None:
    path.write_text(
        "".join(
            json.dumps(
                {
                    "message": {
                        "role": "user",
                        "content": f"message {index}",
                    }
                }
            )
            + "\n"
            for index in range(count)
        ),
        encoding="utf-8",
    )


def _capture_output(callable_):
    captured = []

    with patch(
        "mempalace.hooks_cli._output",
        side_effect=captured.append,
    ):
        callable_()

    assert captured
    return captured[-1]


def test_direct_policy_does_not_probe_daemon():
    config = _HookConfig(WriteRoutingPolicy.DIRECT)

    with (
        patch(
            "mempalace.hooks_cli.MempalaceConfig",
            return_value=config,
        ),
        patch(
            "mempalace.hooks_cli._daemon_available",
            side_effect=AssertionError("direct must not probe daemon"),
        ),
    ):
        routing = hooks_cli._compute_hook_write_routing()

    assert routing.decision is not None
    assert routing.decision.target is WriteRoutingTarget.DIRECT
    assert routing.blocked is False
    assert routing.use_daemon is False


@pytest.mark.parametrize(
    ("policy", "available", "target"),
    [
        (
            WriteRoutingPolicy.PREFER,
            True,
            WriteRoutingTarget.DAEMON,
        ),
        (
            WriteRoutingPolicy.PREFER,
            False,
            WriteRoutingTarget.DIRECT,
        ),
        (
            WriteRoutingPolicy.REQUIRE,
            True,
            WriteRoutingTarget.DAEMON,
        ),
        (
            WriteRoutingPolicy.REQUIRE,
            False,
            WriteRoutingTarget.BLOCKED,
        ),
    ],
)
def test_hook_route_decision_matrix(policy, available, target):
    config = _HookConfig(policy)

    with (
        patch(
            "mempalace.hooks_cli.MempalaceConfig",
            return_value=config,
        ),
        patch(
            "mempalace.hooks_cli._daemon_available",
            return_value=available,
        ),
    ):
        routing = hooks_cli._compute_hook_write_routing()

    assert routing.decision is not None
    assert routing.decision.target is target
    assert routing.use_daemon is (target is WriteRoutingTarget.DAEMON)
    assert routing.blocked is (target is WriteRoutingTarget.BLOCKED)


def test_invalid_policy_blocks_instead_of_falling_back_direct():
    config = _HookConfig(
        routing_error=WriteRoutingError("bad hook policy"),
    )

    with (
        patch(
            "mempalace.hooks_cli.MempalaceConfig",
            return_value=config,
        ),
        patch(
            "mempalace.hooks_cli._daemon_available",
        ) as probe,
        patch("mempalace.hooks_cli._log"),
    ):
        routing = hooks_cli._compute_hook_write_routing()

    probe.assert_not_called()
    assert routing.blocked is True
    assert routing.decision is None
    assert "invalid" in routing.notice
    assert "No direct ChromaDB fallback" in routing.notice


def test_unrelated_config_failure_preserves_historical_direct_fallback():
    with (
        patch(
            "mempalace.hooks_cli.MempalaceConfig",
            side_effect=RuntimeError("config unreadable"),
        ),
        patch(
            "mempalace.hooks_cli._daemon_available",
            side_effect=AssertionError("direct fallback must not probe daemon"),
        ),
        patch("mempalace.hooks_cli._log") as log,
    ):
        routing = hooks_cli._compute_hook_write_routing()

    assert routing.decision is not None
    assert routing.decision.target is WriteRoutingTarget.DIRECT
    assert routing.source == "config-unavailable fallback"
    assert routing.blocked is False
    assert "defaulting to direct" in log.call_args.args[0]


def test_context_reuses_one_daemon_probe_for_whole_hook_fire():
    config = _HookConfig(WriteRoutingPolicy.PREFER)

    with (
        patch(
            "mempalace.hooks_cli.MempalaceConfig",
            return_value=config,
        ),
        patch(
            "mempalace.hooks_cli._daemon_available",
            return_value=True,
        ) as probe,
        patch("mempalace.hooks_cli._log"),
    ):
        with hooks_cli._hook_write_routing_context() as routing:
            assert hooks_cli._current_hook_write_routing() is routing
            assert hooks_cli._current_hook_write_routing() is routing
            assert routing.use_daemon is True

    probe.assert_called_once_with()


@pytest.mark.parametrize(
    ("policy", "available", "expected"),
    [
        (WriteRoutingPolicy.DIRECT, False, "direct"),
        (WriteRoutingPolicy.PREFER, False, "direct"),
        (WriteRoutingPolicy.PREFER, True, "daemon"),
        (WriteRoutingPolicy.REQUIRE, False, "blocked"),
        (WriteRoutingPolicy.REQUIRE, True, "daemon"),
    ],
)
def test_project_auto_ingest_applies_policy(
    policy,
    available,
    expected,
):
    config = _HookConfig(policy)

    with (
        patch(
            "mempalace.hooks_cli.MempalaceConfig",
            return_value=config,
        ),
        patch(
            "mempalace.hooks_cli._get_mine_targets",
            return_value=[("/project", "projects")],
        ),
        patch(
            "mempalace.hooks_cli._daemon_available",
            return_value=available,
        ),
        patch(
            "mempalace.hooks_cli._submit_daemon_job",
        ) as submit,
        patch(
            "mempalace.hooks_cli._spawn_mine",
        ) as spawn,
        patch("mempalace.hooks_cli._log"),
    ):
        hooks_cli._maybe_auto_ingest()

    if expected == "daemon":
        submit.assert_called_once()
        spawn.assert_not_called()
    elif expected == "direct":
        submit.assert_not_called()
        spawn.assert_called_once()
    else:
        submit.assert_not_called()
        spawn.assert_not_called()


def test_require_unavailable_blocks_every_direct_hook_write_path(
    tmp_path,
):
    config = _HookConfig(WriteRoutingPolicy.REQUIRE)
    transcript = tmp_path / "session.jsonl"
    _write_transcript(transcript)

    fake_mcp = types.ModuleType("mempalace.mcp_server")
    fake_mcp.tool_diary_write = MagicMock()

    with (
        patch(
            "mempalace.hooks_cli.MempalaceConfig",
            return_value=config,
        ),
        patch("mempalace.hooks_cli.STATE_DIR", tmp_path),
        patch(
            "mempalace.hooks_cli._get_mine_targets",
            return_value=[("/project", "projects")],
        ),
        patch(
            "mempalace.hooks_cli._daemon_available",
            return_value=False,
        ) as probe,
        patch(
            "mempalace.hooks_cli._submit_daemon_job",
        ) as submit,
        patch(
            "mempalace.hooks_cli._spawn_mine",
        ) as spawn,
        patch(
            "mempalace.hooks_cli.subprocess.run",
        ) as sync_run,
        patch("mempalace.hooks_cli._log"),
        patch.dict(
            sys.modules,
            {"mempalace.mcp_server": fake_mcp},
        ),
    ):
        with hooks_cli._hook_write_routing_context():
            hooks_cli._maybe_auto_ingest()
            hooks_cli._mine_sync()

            result = hooks_cli._save_diary_direct(
                str(transcript),
                "session",
                agent_name="claude",
            )

            hooks_cli._ingest_transcript(str(transcript))

    probe.assert_called_once_with()
    submit.assert_not_called()
    spawn.assert_not_called()
    sync_run.assert_not_called()
    fake_mcp.tool_diary_write.assert_not_called()

    assert result["count"] == 0
    assert result["routing_blocked"] is True


def test_daemon_submission_failure_never_falls_back_to_direct():
    config = _HookConfig(WriteRoutingPolicy.REQUIRE)

    with (
        patch(
            "mempalace.hooks_cli.MempalaceConfig",
            return_value=config,
        ),
        patch(
            "mempalace.hooks_cli._get_mine_targets",
            return_value=[("/project", "projects")],
        ),
        patch(
            "mempalace.hooks_cli._daemon_available",
            return_value=True,
        ),
        patch(
            "mempalace.hooks_cli._submit_daemon_job",
            side_effect=RuntimeError("daemon disappeared"),
        ) as submit,
        patch(
            "mempalace.hooks_cli._spawn_mine",
        ) as spawn,
        patch("mempalace.hooks_cli._log"),
    ):
        hooks_cli._maybe_auto_ingest()

    submit.assert_called_once()
    spawn.assert_not_called()


def test_session_start_warns_when_required_daemon_unavailable(
    tmp_path,
):
    config = _HookConfig(WriteRoutingPolicy.REQUIRE)

    with (
        patch(
            "mempalace.hooks_cli._palace_root_exists",
            return_value=True,
        ),
        patch(
            "mempalace.hooks_cli.MempalaceConfig",
            return_value=config,
        ),
        patch("mempalace.hooks_cli.STATE_DIR", tmp_path),
        patch(
            "mempalace.hooks_cli._daemon_available",
            return_value=False,
        ),
        patch("mempalace.hooks_cli._log"),
    ):
        output = _capture_output(
            lambda: hooks_cli.hook_session_start(
                {"session_id": "s1"},
                "claude-code",
            )
        )

    assert "systemMessage" in output
    assert "require" in output["systemMessage"]
    assert "no direct ChromaDB fallback" in output["systemMessage"]


def test_stop_require_unavailable_warns_and_does_not_advance_marker(
    tmp_path,
):
    config = _HookConfig(WriteRoutingPolicy.REQUIRE)
    transcript = tmp_path / "session.jsonl"
    _write_transcript(transcript, hooks_cli.SAVE_INTERVAL)

    with (
        patch(
            "mempalace.hooks_cli._palace_root_exists",
            return_value=True,
        ),
        patch(
            "mempalace.hooks_cli.MempalaceConfig",
            return_value=config,
        ),
        patch("mempalace.hooks_cli.STATE_DIR", tmp_path),
        patch(
            "mempalace.hooks_cli._daemon_available",
            return_value=False,
        ),
        patch(
            "mempalace.hooks_cli._save_diary_direct",
        ) as diary,
        patch(
            "mempalace.hooks_cli._ingest_transcript",
        ) as ingest,
        patch(
            "mempalace.hooks_cli._maybe_auto_ingest",
        ) as auto_ingest,
        patch("mempalace.hooks_cli._log"),
    ):
        output = _capture_output(
            lambda: hooks_cli.hook_stop(
                {
                    "session_id": "s1",
                    "stop_hook_active": False,
                    "transcript_path": str(transcript),
                },
                "claude-code",
            )
        )

    diary.assert_not_called()
    ingest.assert_not_called()
    auto_ingest.assert_not_called()

    assert not (tmp_path / "s1_last_save").exists()
    assert "systemMessage" in output
    assert "require" in output["systemMessage"]


def test_precompact_require_unavailable_skips_all_writes(
    tmp_path,
):
    config = _HookConfig(WriteRoutingPolicy.REQUIRE)

    with (
        patch(
            "mempalace.hooks_cli._palace_root_exists",
            return_value=True,
        ),
        patch(
            "mempalace.hooks_cli.MempalaceConfig",
            return_value=config,
        ),
        patch(
            "mempalace.hooks_cli._daemon_available",
            return_value=False,
        ),
        patch(
            "mempalace.hooks_cli._ingest_transcript",
        ) as ingest,
        patch(
            "mempalace.hooks_cli._mine_sync",
        ) as mine_sync,
        patch("mempalace.hooks_cli._log"),
    ):
        output = _capture_output(
            lambda: hooks_cli.hook_precompact(
                {
                    "session_id": "s1",
                    "transcript_path": str(tmp_path / "session.jsonl"),
                },
                "claude-code",
            )
        )

    ingest.assert_not_called()
    mine_sync.assert_not_called()
    assert "systemMessage" in output


def test_session_end_require_unavailable_skips_all_writes_and_cleans_marker(
    tmp_path,
):
    config = _HookConfig(WriteRoutingPolicy.REQUIRE)
    transcript = tmp_path / "session.jsonl"
    _write_transcript(transcript)

    marker = tmp_path / "s1_last_save"
    marker.write_text("15", encoding="utf-8")

    with (
        patch(
            "mempalace.hooks_cli._palace_root_exists",
            return_value=True,
        ),
        patch(
            "mempalace.hooks_cli.MempalaceConfig",
            return_value=config,
        ),
        patch("mempalace.hooks_cli.STATE_DIR", tmp_path),
        patch(
            "mempalace.hooks_cli._daemon_available",
            return_value=False,
        ),
        patch(
            "mempalace.hooks_cli._save_diary_direct",
        ) as diary,
        patch(
            "mempalace.hooks_cli._ingest_transcript",
        ) as ingest,
        patch(
            "mempalace.hooks_cli._maybe_auto_ingest",
        ) as auto_ingest,
        patch("mempalace.hooks_cli._log"),
    ):
        output = _capture_output(
            lambda: hooks_cli.hook_session_end(
                {
                    "session_id": "s1",
                    "transcript_path": str(transcript),
                },
                "claude-code",
            )
        )

    diary.assert_not_called()
    ingest.assert_not_called()
    auto_ingest.assert_not_called()

    assert not marker.exists()
    assert "systemMessage" in output


def test_stop_uses_one_daemon_probe_for_all_write_helpers(
    tmp_path,
):
    config = _HookConfig(WriteRoutingPolicy.PREFER)
    transcript = tmp_path / "session.jsonl"
    _write_transcript(transcript, hooks_cli.SAVE_INTERVAL)

    def save(*args, **kwargs):
        assert hooks_cli._current_hook_write_routing().use_daemon is True
        return {
            "count": hooks_cli.SAVE_INTERVAL,
            "themes": [],
        }

    def use_current_route(*args, **kwargs):
        assert hooks_cli._current_hook_write_routing().use_daemon is True

    with (
        patch(
            "mempalace.hooks_cli._palace_root_exists",
            return_value=True,
        ),
        patch(
            "mempalace.hooks_cli.MempalaceConfig",
            return_value=config,
        ),
        patch("mempalace.hooks_cli.STATE_DIR", tmp_path),
        patch(
            "mempalace.hooks_cli._daemon_available",
            return_value=True,
        ) as probe,
        patch(
            "mempalace.hooks_cli._save_diary_direct",
            side_effect=save,
        ),
        patch(
            "mempalace.hooks_cli._ingest_transcript",
            side_effect=use_current_route,
        ),
        patch(
            "mempalace.hooks_cli._maybe_auto_ingest",
            side_effect=use_current_route,
        ),
        patch("mempalace.hooks_cli._log"),
    ):
        output = _capture_output(
            lambda: hooks_cli.hook_stop(
                {
                    "session_id": "s1",
                    "stop_hook_active": False,
                    "transcript_path": str(transcript),
                },
                "claude-code",
            )
        )

    probe.assert_called_once_with()
    assert "memories woven" in output["systemMessage"]
