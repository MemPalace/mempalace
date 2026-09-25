from __future__ import annotations

import argparse
import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from mempalace import cli, service
from mempalace.cli_write_routing import (
    CliWriteRouting,
    _cli_routing_scope,
    add_cli_write_routing_flags,
    resolve_cli_write_routing,
)
from mempalace.write_routing import (
    ResolvedWriteRoutingPolicy,
    WriteRoutingError,
    WriteRoutingPolicy,
    WriteRoutingTarget,
    choose_write_route,
)


_SERVICE_ENV_KEYS = (
    "MEMPALACE_PALACE_PATH",
    "MEMPAL_PALACE_PATH",
    "MEMPALACE_BACKEND",
    "MEMPALACE_BACKEND_EXPLICIT",
)

# Capture the real pre-suite values once. Direct service helpers mutate these
# process-global variables, whereas production daemon jobs normally run through
# execute_job(), which snapshots and restores them.
_SERVICE_ENV_SNAPSHOT = {key: os.environ.get(key) for key in _SERVICE_ENV_KEYS}


@pytest.fixture(autouse=True)
def _isolate_service_environment():
    """Restore service process globals before and after every focused test."""

    for key, value in _SERVICE_ENV_SNAPSHOT.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value

    yield

    for key, value in _SERVICE_ENV_SNAPSHOT.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


class _RoutingConfig:
    def __init__(
        self,
        policy: WriteRoutingPolicy,
        *,
        source: str = "test",
        palace_path: str = "/tmp/palace",
    ):
        self._policy = policy
        self._source = source
        self.palace_path = palace_path

    def resolve_write_routing(
        self,
        scope: str,
    ) -> ResolvedWriteRoutingPolicy:
        assert scope == "cli"
        return ResolvedWriteRoutingPolicy(
            policy=self._policy,
            source=self._source,
        )


def _args(**overrides):
    values = {
        "palace": None,
        "backend": None,
        "global_backend": None,
        "daemon": False,
        "direct": False,
        "background": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def _route(
    policy: WriteRoutingPolicy,
    *,
    source: str = "test",
) -> CliWriteRouting:
    decision = choose_write_route(
        policy,
        daemon_available=False,
        daemon_can_start=True,
    )
    return CliWriteRouting(
        decision=decision,
        source=source,
        explicit=False,
    )


@pytest.mark.parametrize(
    "policy",
    [
        WriteRoutingPolicy.PREFER,
        WriteRoutingPolicy.REQUIRE,
    ],
)
def test_cli_prefer_and_require_select_startable_daemon(
    policy,
):
    with patch(
        "mempalace.cli_write_routing.MempalaceConfig",
        return_value=_RoutingConfig(policy),
    ):
        routing = resolve_cli_write_routing(
            _args(),
            operation="mine",
        )

    assert routing.use_daemon is True
    assert routing.decision.target is WriteRoutingTarget.DAEMON
    assert routing.decision.auto_start_daemon is True


def test_cli_direct_policy_selects_direct():
    with patch(
        "mempalace.cli_write_routing.MempalaceConfig",
        return_value=_RoutingConfig(WriteRoutingPolicy.DIRECT),
    ):
        routing = resolve_cli_write_routing(
            _args(),
            operation="mine",
        )

    assert routing.use_direct is True
    assert routing.use_daemon is False


def test_explicit_direct_overrides_require_policy():
    with patch(
        "mempalace.cli_write_routing.MempalaceConfig",
    ) as config:
        routing = resolve_cli_write_routing(
            _args(direct=True),
            operation="mine",
        )

    config.assert_not_called()
    assert routing.use_direct is True
    assert routing.source == "--direct"
    assert routing.explicit is True


def test_explicit_daemon_overrides_direct_policy():
    with patch(
        "mempalace.cli_write_routing.MempalaceConfig",
    ) as config:
        routing = resolve_cli_write_routing(
            _args(daemon=True),
            operation="mine",
        )

    config.assert_not_called()
    assert routing.use_daemon is True
    assert routing.source == "--daemon"
    assert routing.explicit is True


def test_background_is_rejected_for_direct_route():
    with patch(
        "mempalace.cli_write_routing.MempalaceConfig",
        return_value=_RoutingConfig(WriteRoutingPolicy.DIRECT),
    ):
        with pytest.raises(
            WriteRoutingError,
            match="--background requires a daemon route",
        ):
            resolve_cli_write_routing(
                _args(background=True),
                operation="mine",
            )


def test_background_is_allowed_for_prefer_route():
    with patch(
        "mempalace.cli_write_routing.MempalaceConfig",
        return_value=_RoutingConfig(WriteRoutingPolicy.PREFER),
    ):
        routing = resolve_cli_write_routing(
            _args(background=True),
            operation="mine",
        )

    assert routing.use_daemon is True


def test_parser_flags_are_mutually_exclusive():
    parser = argparse.ArgumentParser()
    add_cli_write_routing_flags(parser)

    with pytest.raises(SystemExit):
        parser.parse_args(["--daemon", "--direct"])


class _ScopeRecordingConfig:
    """Records which write-routing scope was requested, unlike
    ``_RoutingConfig`` above which hard-asserts ``scope == "cli"``."""

    def __init__(self, policy: WriteRoutingPolicy):
        self._policy = policy
        self.requested_scopes: list[str] = []
        self.palace_path = "/tmp/palace"

    def resolve_write_routing(self, scope: str) -> ResolvedWriteRoutingPolicy:
        self.requested_scopes.append(scope)
        return ResolvedWriteRoutingPolicy(policy=self._policy, source="test")


class TestCliRoutingScopeSelection:
    """Shell hooks shell out to ``mempalace mine`` instead of writing through
    ``mempalace.hooks_cli``, so ``resolve_cli_write_routing`` must resolve the
    caller-selected scope, not a hardcoded ``"cli"``, or a hooks policy is
    silently ignored for every shell-hook-triggered mine."""

    def test_defaults_to_cli_scope_when_env_unset(self, monkeypatch):
        monkeypatch.delenv("MEMPALACE_CLI_ROUTING_SCOPE", raising=False)
        assert _cli_routing_scope() == "cli"

    def test_reads_hooks_scope_from_env(self, monkeypatch):
        monkeypatch.setenv("MEMPALACE_CLI_ROUTING_SCOPE", "hooks")
        assert _cli_routing_scope() == "hooks"

    def test_is_case_insensitive(self, monkeypatch):
        monkeypatch.setenv("MEMPALACE_CLI_ROUTING_SCOPE", "HOOKS")
        assert _cli_routing_scope() == "hooks"

    def test_unrecognized_value_fails_closed(self, monkeypatch):
        monkeypatch.setenv("MEMPALACE_CLI_ROUTING_SCOPE", "bogus")
        with pytest.raises(WriteRoutingError):
            _cli_routing_scope()

    def test_resolve_cli_write_routing_queries_cli_scope_by_default(self, monkeypatch):
        monkeypatch.delenv("MEMPALACE_CLI_ROUTING_SCOPE", raising=False)
        config = _ScopeRecordingConfig(WriteRoutingPolicy.DIRECT)
        with patch(
            "mempalace.cli_write_routing.MempalaceConfig",
            return_value=config,
        ):
            resolve_cli_write_routing(_args(), operation="mine")

        assert config.requested_scopes == ["cli"]

    def test_resolve_cli_write_routing_queries_hooks_scope_for_shell_hooks(self, monkeypatch):
        monkeypatch.setenv("MEMPALACE_CLI_ROUTING_SCOPE", "hooks")
        config = _ScopeRecordingConfig(WriteRoutingPolicy.DIRECT)
        with patch(
            "mempalace.cli_write_routing.MempalaceConfig",
            return_value=config,
        ):
            resolve_cli_write_routing(_args(), operation="mine")

        assert config.requested_scopes == ["hooks"]

    def test_hooks_require_does_not_cold_start(self, monkeypatch):
        monkeypatch.setenv("MEMPALACE_CLI_ROUTING_SCOPE", "hooks")
        config = _ScopeRecordingConfig(WriteRoutingPolicy.REQUIRE)
        with (
            patch("mempalace.cli_write_routing.MempalaceConfig", return_value=config),
            patch("mempalace.cli_write_routing._daemon_already_running", return_value=False),
        ):
            with pytest.raises(WriteRoutingError):
                resolve_cli_write_routing(_args(), operation="mine")

    def test_hooks_scope_does_not_reroute_other_commands(self, monkeypatch):
        monkeypatch.setenv("MEMPALACE_CLI_ROUTING_SCOPE", "hooks")
        config = _ScopeRecordingConfig(WriteRoutingPolicy.DIRECT)
        with patch(
            "mempalace.cli_write_routing.MempalaceConfig",
            return_value=config,
        ):
            resolve_cli_write_routing(_args(), operation="sweep")

        assert config.requested_scopes == ["cli"]

    def test_explicit_daemon_flag_bypasses_scope_selection_entirely(self, monkeypatch):
        """--daemon/--direct short-circuit before any config lookup, so the
        routing scope must never matter when an explicit flag is present."""
        monkeypatch.setenv("MEMPALACE_CLI_ROUTING_SCOPE", "hooks")
        with patch(
            "mempalace.cli_write_routing.MempalaceConfig",
        ) as config:
            routing = resolve_cli_write_routing(
                _args(daemon=True),
                operation="mine",
            )

        config.assert_not_called()
        assert routing.use_daemon is True


def _mine_args(tmp_path, **overrides):
    values = {
        "palace": str(tmp_path / "palace"),
        "backend": None,
        "global_backend": None,
        "dir": str(tmp_path / "project"),
        "mode": "projects",
        "wing": None,
        "agent": "mempalace",
        "limit": 0,
        "dry_run": False,
        "extract": "exchange",
        "no_gitignore": False,
        "include_ignored": [],
        "max_chunks_per_file": None,
        "redetect_origin": False,
        "daemon": False,
        "direct": False,
        "background": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_cmd_mine_prefer_submits_daemon_job(tmp_path):
    args = _mine_args(tmp_path)

    with (
        patch(
            "mempalace.cli._resolve_cli_write_routing_or_exit",
            return_value=_route(WriteRoutingPolicy.PREFER),
        ),
        patch(
            "mempalace.cli._submit_daemon_cli_job",
        ) as submit,
        patch(
            "mempalace.miner.mine",
        ) as direct_mine,
    ):
        cli.cmd_mine(args)

    direct_mine.assert_not_called()
    submit.assert_called_once()

    kind, payload, submitted_args = submit.call_args.args
    assert kind == "mine"
    assert submitted_args is args
    assert payload["source"] == args.dir
    assert payload["mode"] == "projects"
    assert submit.call_args.kwargs == {
        "background": False,
        "auto_start": True,
    }


def test_cmd_mine_direct_preserves_direct_path(tmp_path):
    args = _mine_args(tmp_path)

    with (
        patch(
            "mempalace.cli._resolve_cli_write_routing_or_exit",
            return_value=_route(WriteRoutingPolicy.DIRECT),
        ),
        patch(
            "mempalace.cli._submit_daemon_cli_job",
        ) as submit,
        patch(
            "mempalace.miner.mine",
        ) as direct_mine,
    ):
        cli.cmd_mine(args)

    submit.assert_not_called()
    direct_mine.assert_called_once()


def test_daemon_submission_failure_never_falls_back_direct(
    tmp_path,
):
    args = _mine_args(tmp_path)

    with (
        patch(
            "mempalace.cli._resolve_cli_write_routing_or_exit",
            return_value=_route(WriteRoutingPolicy.REQUIRE),
        ),
        patch(
            "mempalace.cli._submit_daemon_cli_job",
            side_effect=SystemExit(1),
        ),
        patch(
            "mempalace.miner.mine",
        ) as direct_mine,
    ):
        with pytest.raises(SystemExit):
            cli.cmd_mine(args)

    direct_mine.assert_not_called()


def test_cmd_sync_prefer_submits_daemon_job(tmp_path):
    args = _args(
        palace=str(tmp_path / "palace"),
        dir=str(tmp_path / "project"),
        root=[],
        wing=None,
        dry_run=False,
    )

    with (
        patch(
            "mempalace.cli._resolve_cli_write_routing_or_exit",
            return_value=_route(WriteRoutingPolicy.PREFER),
        ),
        patch(
            "mempalace.cli._submit_daemon_cli_job",
        ) as submit,
        patch(
            "mempalace.sync.sync_palace",
        ) as direct_sync,
    ):
        cli.cmd_sync(args)

    direct_sync.assert_not_called()
    submit.assert_called_once()

    assert submit.call_args.args[0] == "sync"
    assert submit.call_args.args[1] == {
        "dir": args.dir,
        "root": [],
        "wing": None,
        "dry_run": False,
    }


def test_cmd_sweep_prefer_submits_daemon_job(tmp_path):
    args = _args(
        palace=str(tmp_path / "palace"),
        target=str(tmp_path / "session.jsonl"),
    )

    with (
        patch(
            "mempalace.cli._resolve_cli_write_routing_or_exit",
            return_value=_route(WriteRoutingPolicy.PREFER),
        ),
        patch(
            "mempalace.cli._submit_daemon_cli_job",
        ) as submit,
        patch(
            "mempalace.sweeper.sweep",
        ) as direct_sweep,
    ):
        cli.cmd_sweep(args)

    direct_sweep.assert_not_called()
    submit.assert_called_once()

    assert submit.call_args.args[0] == "sweep"
    assert submit.call_args.args[1] == {
        "target": str(tmp_path / "session.jsonl"),
    }


def test_init_auto_mine_daemon_preserves_prescan(
    tmp_path,
):
    project = tmp_path / "project"
    palace = tmp_path / "palace"
    project.mkdir()

    first = project / "a.md"
    second = project / "b.md"
    first.write_text("a", encoding="utf-8")
    second.write_text("b", encoding="utf-8")

    args = _args(
        palace=str(palace),
        dir=str(project),
        auto_mine=True,
    )
    config = SimpleNamespace(palace_path=str(palace))

    with (
        patch(
            "mempalace.miner.scan_project",
            return_value=[first, second],
        ) as scan,
        patch(
            "mempalace.cli._resolve_cli_write_routing_or_exit",
            return_value=_route(WriteRoutingPolicy.PREFER),
        ),
        patch(
            "mempalace.cli._submit_daemon_cli_job",
        ) as submit,
        patch(
            "mempalace.miner.mine",
        ) as direct_mine,
    ):
        cli._maybe_run_mine_after_init(args, config)

    scan.assert_called_once_with(str(project))
    direct_mine.assert_not_called()
    submit.assert_called_once()

    payload = submit.call_args.args[1]
    assert payload["source"] == str(project)
    assert payload["files"] == [
        str(first),
        str(second),
    ]


def test_init_auto_mine_direct_reuses_prescan(
    tmp_path,
):
    project = tmp_path / "project"
    palace = tmp_path / "palace"
    project.mkdir()

    source = project / "a.md"
    source.write_text("a", encoding="utf-8")

    args = _args(
        palace=str(palace),
        dir=str(project),
        auto_mine=True,
    )
    config = SimpleNamespace(palace_path=str(palace))

    with (
        patch(
            "mempalace.miner.scan_project",
            return_value=[source],
        ),
        patch(
            "mempalace.cli._resolve_cli_write_routing_or_exit",
            return_value=_route(WriteRoutingPolicy.DIRECT),
        ),
        patch(
            "mempalace.cli._submit_daemon_cli_job",
        ) as submit,
        patch(
            "mempalace.miner.mine",
        ) as direct_mine,
    ):
        cli._maybe_run_mine_after_init(args, config)

    submit.assert_not_called()
    direct_mine.assert_called_once_with(
        project_dir=str(project),
        palace_path=str(palace),
        files=[source],
    )


def test_service_run_sweep_file(tmp_path):
    palace = tmp_path / "palace"
    target = tmp_path / "session.jsonl"
    target.write_text("{}\n", encoding="utf-8")

    sweep_result = {
        "drawers_added": 2,
        "drawers_already_present": 1,
        "drawers_skipped": 3,
    }

    with patch(
        "mempalace.sweeper.sweep",
        return_value=sweep_result,
    ) as sweep:
        result = service.run_sweep(
            {
                "palace_path": str(palace),
                "target": str(target),
            }
        )

    sweep.assert_called_once_with(
        str(target),
        str(palace.resolve()),
    )
    assert result["success"] is True
    assert result["exit_code"] == 0
    assert result["result"] == sweep_result


def test_service_run_sweep_directory_partial_failure(
    tmp_path,
):
    palace = tmp_path / "palace"
    target = tmp_path / "sessions"
    target.mkdir()

    sweep_result = {
        "files_succeeded": 1,
        "files_attempted": 2,
        "drawers_added": 2,
        "drawers_already_present": 0,
        "drawers_skipped": 0,
        "failures": [{"path": "bad.jsonl"}],
    }

    with patch(
        "mempalace.sweeper.sweep_directory",
        return_value=sweep_result,
    ):
        result = service.run_sweep(
            {
                "palace_path": str(palace),
                "target": str(target),
            }
        )

    assert result["success"] is False
    assert result["exit_code"] == 2


def test_execute_job_dispatches_sweep():
    with patch(
        "mempalace.service.run_sweep",
        return_value={
            "success": True,
            "exit_code": 0,
        },
    ) as run_sweep:
        result = service.execute_job(
            "sweep",
            {"target": "session.jsonl"},
        )

    run_sweep.assert_called_once_with({"target": "session.jsonl"})
    assert result["success"] is True


def test_service_run_mine_forwards_valid_prescanned_files(
    tmp_path,
):
    project = tmp_path / "project"
    nested = project / "nested"
    palace = tmp_path / "palace"

    nested.mkdir(parents=True)

    first = project / "a.md"
    second = nested / "b.md"

    first.write_text("a", encoding="utf-8")
    second.write_text("b", encoding="utf-8")

    with patch("mempalace.miner.mine") as mine:
        result = service.run_mine(
            {
                "palace_path": str(palace),
                "source": str(project),
                "mode": "projects",
                "files": [
                    str(first),
                    "nested/b.md",
                ],
                "dry_run": True,
            }
        )

    assert result["success"] is True

    mine.assert_called_once()
    assert mine.call_args.kwargs["files"] == [
        first.resolve(),
        second.resolve(),
    ]


def test_service_run_mine_rejects_prescanned_path_outside_project(
    tmp_path,
):
    project = tmp_path / "project"
    outside = tmp_path / "outside.md"

    project.mkdir()
    outside.write_text("outside", encoding="utf-8")

    with patch("mempalace.miner.mine") as mine:
        result = service.run_mine(
            {
                "palace_path": str(tmp_path / "palace"),
                "source": str(project),
                "mode": "projects",
                "files": [str(outside)],
                "dry_run": True,
            }
        )

    mine.assert_not_called()
    assert result["success"] is False
    assert result["exit_code"] == 2
    assert "outside the project root" in result["error"]


def test_service_run_mine_rejects_non_list_files_payload(
    tmp_path,
):
    project = tmp_path / "project"
    project.mkdir()

    with patch("mempalace.miner.mine") as mine:
        result = service.run_mine(
            {
                "palace_path": str(tmp_path / "palace"),
                "source": str(project),
                "mode": "projects",
                "files": "a.md",
                "dry_run": True,
            }
        )

    mine.assert_not_called()
    assert result == {
        "success": False,
        "error": "mine files payload must be a list",
        "exit_code": 2,
    }
