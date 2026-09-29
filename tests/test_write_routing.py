from __future__ import annotations

import json

import pytest

from mempalace.config import MempalaceConfig
from mempalace.write_routing import (
    RoutingPolicyCandidate,
    WriteRoutingError,
    WriteRoutingPolicy,
    WriteRoutingTarget,
    choose_write_route,
    parse_write_routing_policy,
    resolve_write_routing_policy,
)


_ROUTING_ENV_KEYS = (
    "MEMPALACE_WRITE_ROUTING",
    "MEMPALACE_HOOK_WRITE_ROUTING",
    "MEMPALACE_CLI_WRITE_ROUTING",
    "MEMPALACE_HOOKS_DAEMON",
)


@pytest.fixture(autouse=True)
def _clear_routing_env(monkeypatch):
    for key in _ROUTING_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def _write_config(tmp_path, payload):
    (tmp_path / "config.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    "value",
    ["direct", " DIRECT ", WriteRoutingPolicy.DIRECT],
)
def test_parse_direct(value):
    assert parse_write_routing_policy(value) is WriteRoutingPolicy.DIRECT


@pytest.mark.parametrize(
    "value",
    ["prefer", " PREFER ", WriteRoutingPolicy.PREFER],
)
def test_parse_prefer(value):
    assert parse_write_routing_policy(value) is WriteRoutingPolicy.PREFER


@pytest.mark.parametrize(
    "value",
    ["require", " REQUIRE ", WriteRoutingPolicy.REQUIRE],
)
def test_parse_require(value):
    assert parse_write_routing_policy(value) is WriteRoutingPolicy.REQUIRE


@pytest.mark.parametrize(
    "value",
    [True, 1, "true", "yes", "on", "daemon"],
)
def test_legacy_truthy_maps_to_prefer(value):
    assert (
        parse_write_routing_policy(
            value,
            legacy_boolean=True,
        )
        is WriteRoutingPolicy.PREFER
    )


@pytest.mark.parametrize(
    "value",
    [False, 0, "false", "no", "off"],
)
def test_legacy_falsy_maps_to_direct(value):
    assert (
        parse_write_routing_policy(
            value,
            legacy_boolean=True,
        )
        is WriteRoutingPolicy.DIRECT
    )


@pytest.mark.parametrize(
    "value",
    [True, False, 1, 0, "maybe", ""],
)
def test_new_policy_rejects_legacy_or_invalid_values(value):
    with pytest.raises(WriteRoutingError):
        parse_write_routing_policy(value)


def test_resolver_returns_first_configured_candidate():
    resolved = resolve_write_routing_policy(
        [
            RoutingPolicyCandidate("first", None),
            RoutingPolicyCandidate("second", "require"),
            RoutingPolicyCandidate("third", "direct"),
        ]
    )

    assert resolved.policy is WriteRoutingPolicy.REQUIRE
    assert resolved.source == "second"


def test_resolver_names_invalid_source():
    with pytest.raises(
        WriteRoutingError,
        match="MEMPALACE_WRITE_ROUTING",
    ):
        resolve_write_routing_policy(
            [
                RoutingPolicyCandidate(
                    "MEMPALACE_WRITE_ROUTING",
                    "typo",
                )
            ]
        )


@pytest.mark.parametrize(
    (
        "policy",
        "available",
        "can_start",
        "target",
        "auto_start",
    ),
    [
        (
            WriteRoutingPolicy.DIRECT,
            False,
            False,
            WriteRoutingTarget.DIRECT,
            False,
        ),
        (
            WriteRoutingPolicy.DIRECT,
            True,
            True,
            WriteRoutingTarget.DIRECT,
            False,
        ),
        (
            WriteRoutingPolicy.PREFER,
            True,
            False,
            WriteRoutingTarget.DAEMON,
            False,
        ),
        (
            WriteRoutingPolicy.PREFER,
            False,
            True,
            WriteRoutingTarget.DAEMON,
            True,
        ),
        (
            WriteRoutingPolicy.PREFER,
            False,
            False,
            WriteRoutingTarget.DIRECT,
            False,
        ),
        (
            WriteRoutingPolicy.REQUIRE,
            True,
            False,
            WriteRoutingTarget.DAEMON,
            False,
        ),
        (
            WriteRoutingPolicy.REQUIRE,
            False,
            True,
            WriteRoutingTarget.DAEMON,
            True,
        ),
        (
            WriteRoutingPolicy.REQUIRE,
            False,
            False,
            WriteRoutingTarget.BLOCKED,
            False,
        ),
    ],
)
def test_decision_matrix(
    policy,
    available,
    can_start,
    target,
    auto_start,
):
    decision = choose_write_route(
        policy,
        daemon_available=available,
        daemon_can_start=can_start,
    )

    assert decision.target is target
    assert decision.auto_start_daemon is auto_start
    assert decision.use_daemon is (target is WriteRoutingTarget.DAEMON)
    assert decision.blocked is (target is WriteRoutingTarget.BLOCKED)


def test_hook_policy_defaults_to_direct(tmp_path):
    cfg = MempalaceConfig(config_dir=tmp_path)

    resolved = cfg.resolve_write_routing("hooks")

    assert resolved.policy is WriteRoutingPolicy.DIRECT
    assert resolved.source == "default"


def test_cli_policy_defaults_to_direct(tmp_path):
    cfg = MempalaceConfig(config_dir=tmp_path)

    assert cfg.cli_write_routing is WriteRoutingPolicy.DIRECT


def test_scoped_env_beats_global_and_config(
    tmp_path,
    monkeypatch,
):
    _write_config(
        tmp_path,
        {
            "write_routing": {
                "default": "direct",
                "hooks": "prefer",
            }
        },
    )

    monkeypatch.setenv(
        "MEMPALACE_WRITE_ROUTING",
        "prefer",
    )
    monkeypatch.setenv(
        "MEMPALACE_HOOK_WRITE_ROUTING",
        "require",
    )

    resolved = MempalaceConfig(config_dir=tmp_path).resolve_write_routing("hooks")

    assert resolved.policy is WriteRoutingPolicy.REQUIRE
    assert resolved.source == "MEMPALACE_HOOK_WRITE_ROUTING"


def test_global_env_beats_legacy_hook_env_and_config(
    tmp_path,
    monkeypatch,
):
    _write_config(
        tmp_path,
        {
            "write_routing": {
                "hooks": "direct",
            },
            "hooks": {
                "daemon": False,
            },
        },
    )

    monkeypatch.setenv(
        "MEMPALACE_WRITE_ROUTING",
        "require",
    )
    monkeypatch.setenv(
        "MEMPALACE_HOOKS_DAEMON",
        "true",
    )

    cfg = MempalaceConfig(config_dir=tmp_path)

    assert cfg.hook_write_routing is WriteRoutingPolicy.REQUIRE


def test_legacy_hook_env_beats_config(
    tmp_path,
    monkeypatch,
):
    _write_config(
        tmp_path,
        {
            "write_routing": {
                "hooks": "direct",
            },
            "hooks": {
                "daemon": False,
            },
        },
    )

    monkeypatch.setenv(
        "MEMPALACE_HOOKS_DAEMON",
        "true",
    )

    resolved = MempalaceConfig(config_dir=tmp_path).resolve_write_routing("hooks")

    assert resolved.policy is WriteRoutingPolicy.PREFER
    assert resolved.source == "MEMPALACE_HOOKS_DAEMON (legacy)"


def test_scoped_config_beats_global_config(tmp_path):
    _write_config(
        tmp_path,
        {
            "write_routing": {
                "default": "prefer",
                "cli": "require",
            }
        },
    )

    resolved = MempalaceConfig(config_dir=tmp_path).resolve_write_routing("cli")

    assert resolved.policy is WriteRoutingPolicy.REQUIRE
    assert resolved.source == "config write_routing.cli"


def test_global_config_applies_to_both_scopes(tmp_path):
    _write_config(
        tmp_path,
        {
            "write_routing": {
                "default": "prefer",
            }
        },
    )

    cfg = MempalaceConfig(config_dir=tmp_path)

    assert cfg.hook_write_routing is WriteRoutingPolicy.PREFER
    assert cfg.cli_write_routing is WriteRoutingPolicy.PREFER


def test_legacy_hook_config_maps_true_to_prefer(tmp_path):
    _write_config(
        tmp_path,
        {
            "hooks": {
                "daemon": True,
            }
        },
    )

    resolved = MempalaceConfig(config_dir=tmp_path).resolve_write_routing("hooks")

    assert resolved.policy is WriteRoutingPolicy.PREFER
    assert resolved.source == "config hooks.daemon (legacy)"


def test_existing_hook_use_daemon_behavior_is_unchanged(
    tmp_path,
    monkeypatch,
):
    _write_config(
        tmp_path,
        {
            "hooks": {
                "daemon": False,
            }
        },
    )

    monkeypatch.setenv(
        "MEMPALACE_HOOKS_DAEMON",
        "yes",
    )

    assert MempalaceConfig(config_dir=tmp_path).hook_use_daemon is True


def test_invalid_scoped_policy_fails_loudly(tmp_path):
    _write_config(
        tmp_path,
        {
            "write_routing": {
                "hooks": "typo",
            }
        },
    )

    with pytest.raises(
        WriteRoutingError,
        match="config write_routing.hooks",
    ):
        MempalaceConfig(config_dir=tmp_path).resolve_write_routing("hooks")


def test_invalid_routing_object_fails_loudly(tmp_path):
    _write_config(
        tmp_path,
        {
            "write_routing": "require",
        },
    )

    with pytest.raises(
        WriteRoutingError,
        match="must be an object",
    ):
        MempalaceConfig(config_dir=tmp_path).resolve_write_routing("cli")


def test_unknown_scope_is_rejected(tmp_path):
    with pytest.raises(
        WriteRoutingError,
        match="scope",
    ):
        MempalaceConfig(config_dir=tmp_path).resolve_write_routing("mcp")
