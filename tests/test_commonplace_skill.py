"""Keep the Commonplace agent instructions usable and within their budgets."""

import os
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "commonplace/agent-skill/SKILL.md"
SNIPPET = ROOT / "docs/mvp/AGENTS-SNIPPET.md"
INSTALLER = ROOT / "commonplace/agent-skill/install.sh"
CONTRACT = ROOT / "docs/mvp/CONTRACT.md"


def test_frontmatter_and_size():
    text = SKILL.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    front, body = text[4:].split("\n---\n", 1)
    fields = dict(line.split(": ", 1) for line in front.splitlines())
    assert fields["name"] == "commonplace"
    description = fields["description"].strip('"')
    assert 0 < len(description) < 300
    assert len(text.splitlines()) < 120
    for phrase in ("picking up work", "reporting status", "remembering", "asking the user"):
        assert phrase in description
    assert body.strip()


def _contract_tools() -> set:
    contract = CONTRACT.read_text(encoding="utf-8")
    sections = contract.split("## Boards (W1)", 1)[1].split("## Webhooks (W3)", 1)[0]
    rules = (ROOT / "docs/design/RULES.md").read_text(encoding="utf-8")
    return set(re.findall(r"^\| `([a-z][a-z0-9_]*)` \|", sections + rules, re.MULTILINE))


_TOOLISH = re.compile(r"\b(?:task|board|memory|needs|rules|session|ask|decide)(?:_[a-z]+)*\b")
_ARGS = {"status_line", "rules_seq"}


def test_tool_references_match_contract():
    tools = _contract_tools()
    assert {"task_take_next", "memory_save", "ask", "needs_me"} <= tools
    for path in (SKILL, SNIPPET):
        text = path.read_text(encoding="utf-8")
        named = {w for w in _TOOLISH.findall(text) if "_" in w}
        assert named - _ARGS <= tools, named - _ARGS - tools
    skill = SKILL.read_text(encoding="utf-8")
    for tool in (
        "task_list",
        "task_take_next",
        "task_update",
        "session_start",
        "memory_search",
        "memory_save",
        "rules_publish",
        "ask",
    ):
        assert f"`{tool}" in skill, tool


def test_snippet_budgets_and_slim_tools():
    text = SNIPPET.read_text(encoding="utf-8")
    blocks = re.findall(r"```text\n(.*?)\n```", text, re.DOTALL)
    assert len(blocks) == 2
    assert len(blocks[0].splitlines()) <= 15
    assert len(blocks[1].splitlines()) <= 10
    grok = {w for w in _TOOLISH.findall(blocks[1]) if "_" in w or w == "ask"}
    assert grok <= {
        "memory_search",
        "memory_save",
        "session_start",
        "rules_publish",
        "task_add",
        "task_heartbeat",
        "task_approve",
        "ask",
    }, grok


def test_no_user_facing_jargon():
    for path in (SKILL, SNIPPET, INSTALLER):
        assert not re.search(r"\b(?:palace|wing|room|drawer|closet)s?\b", path.read_text(), re.I)


@pytest.fixture
def installer_env(tmp_path):
    home = tmp_path / "test home"
    home.mkdir()
    commands = tmp_path / "bin"
    commands.mkdir()
    cli = commands / "commonplace"
    cli.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$@" > "$ONBOARD_TEST_LOG"\nexit "${ONBOARD_TEST_EXIT:-0}"\n',
        encoding="utf-8",
    )
    cli.chmod(0o755)
    log = tmp_path / "arguments"
    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": str(commands) + os.pathsep + os.environ.get("PATH", ""),
        "COMMONPLACE_TOKEN": "secret-test-token",
        "ONBOARD_TEST_LOG": str(log),
    }
    env.pop("COMMONPLACE_URL", None)
    return home, log, env


@pytest.mark.skipif(os.name == "nt", reason="POSIX installer")
def test_installer_dry_run_is_non_mutating_and_safe(installer_env):
    home, log, env = installer_env
    result = subprocess.run(
        [str(INSTALLER), "--dry-run"], env=env, text=True, capture_output=True, check=True
    )
    assert list(home.iterdir()) == []
    assert log.read_text().splitlines() == ["onboard", "--dry-run"]
    for root in (".claude", ".codex", ".agents"):
        assert f"{root}/skills/commonplace/SKILL.md" in result.stdout
    assert "https://app.cmmn.dev" in result.stdout
    assert "environment reference" in result.stdout
    assert "native hook trust" in result.stdout
    assert "secret-test-token" not in result.stdout
    bad = subprocess.run([str(INSTALLER), "--unknown"], env=env, capture_output=True)
    assert bad.returncode != 0
    assert list(home.iterdir()) == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX installer")
def test_installer_runs_default_onboarding_and_preserves_arguments(installer_env):
    home, log, env = installer_env
    args = ["--url", "http://localhost:8765", "--token-env", "TEST_TOKEN", "--no-project"]
    result = subprocess.run(
        [str(INSTALLER), *args], env=env, text=True, capture_output=True, check=True
    )
    assert log.read_text().splitlines() == ["onboard", *args]
    for root in (".claude", ".codex", ".agents"):
        assert (home / root / "skills/commonplace/SKILL.md").read_bytes() == SKILL.read_bytes()
    assert "secret-test-token" not in result.stdout + result.stderr


@pytest.mark.skipif(os.name == "nt", reason="POSIX installer")
def test_installer_dry_run_home_override_and_invalid_scope(installer_env):
    home, log, env = installer_env
    selected = home.parent / "selected home"
    result = subprocess.run(
        [str(INSTALLER), "--dry-run", "--home", str(selected)],
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    assert str(selected / ".agents/skills/commonplace/SKILL.md") in result.stdout
    assert not selected.exists()
    assert list(home.iterdir()) == []
    log.unlink()
    for args in (["--url"], ["--project", "example", "--no-project"]):
        bad = subprocess.run([str(INSTALLER), *args], env=env, capture_output=True)
        assert bad.returncode != 0
        assert not log.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX installer")
def test_installer_reports_onboarding_failure(installer_env):
    home, _log, env = installer_env
    result = subprocess.run(
        [str(INSTALLER)], env={**env, "ONBOARD_TEST_EXIT": "2"}, capture_output=True
    )
    assert result.returncode == 2
    assert list(home.iterdir()) == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX installer")
def test_installer_continues_independent_skill_setup_after_file_refusals(installer_env):
    home, _log, env = installer_env
    result = subprocess.run(
        [str(INSTALLER)], env={**env, "ONBOARD_TEST_EXIT": "1"}, capture_output=True
    )
    assert result.returncode == 1
    for root in (".claude", ".codex", ".agents"):
        assert (home / root / "skills/commonplace/SKILL.md").read_bytes() == SKILL.read_bytes()


def test_documented_url_defaults_overrides_and_oauth():
    skill = SKILL.read_text(encoding="utf-8")
    snippet = SNIPPET.read_text(encoding="utf-8")
    assert "$COMMONPLACE_URL/mcp" in skill
    assert "https://app.cmmn.dev" in skill
    assert "`https://app.cmmn.dev/mcp` as a custom connector" in snippet
    assert "OAuth" in snippet
    guide = (ROOT / "docs/agent-onboarding.md").read_text(encoding="utf-8")
    for option in ("COMMONPLACE_URL", "--url", "--token-env", "localhost", "--no-project"):
        assert option in guide


def test_rules_session_loop():
    text = SKILL.read_text(encoding="utf-8").split("## Rules\n", 1)[1].split("\n## ", 1)[0]
    for phrase in (
        "rules_changed since=<last seq>",
        "rules_get",
        "text",
        "owner edits win",
        "retired",
        "rules_publish",
        "replace=true",
    ):
        assert phrase in text


def test_session_save_guidance_and_import_approval():
    text = SKILL.read_text(encoding="utf-8")
    for phrase in (
        "`session_start` automatically",
        "after compaction",
        "context is incomplete",
        "owner approval",
        "agent='*'",
        "natural-language",
        "`commonplace check`",
    ):
        assert phrase in text
    for tool in ("session_start", "rules_publish"):
        assert tool in _contract_tools()
    assert "agent?: self" in CONTRACT.read_text(encoding="utf-8")


@pytest.fixture
def client_commands(monkeypatch):
    from commonplace import cli, client, onboarding

    def server_access(*_args, **_kwargs):
        pytest.fail("client command attempted to open or lock server data")

    monkeypatch.setattr(cli, "data_lock", server_access)
    monkeypatch.setattr(cli, "_db", server_access)
    remote = SimpleNamespace(
        remember=Mock(return_value="saved m-1"),
        publish_rule=Mock(return_value="rule-key proposed 1\nseq 1"),
    )
    factory = Mock(return_value=remote)
    monkeypatch.setattr(client, "Client", factory)
    original_onboard = onboarding.onboard
    migrate = Mock(return_value={"files": [], "warnings": []})
    monkeypatch.setattr(onboarding, "onboard", migrate)
    installer = ModuleType("commonplace.client_install")
    installer.install_client_config = Mock(return_value={"files": [], "warnings": []})
    monkeypatch.setitem(sys.modules, "commonplace.client_install", installer)
    return SimpleNamespace(
        main=cli.main,
        remote=remote,
        factory=factory,
        migrate=migrate,
        original_onboard=original_onboard,
        install=installer.install_client_config,
    )


def test_cli_remember_and_rule_preserve_exact_text_without_server_data(
    client_commands, tmp_path, capsys
):
    commands = client_commands
    data = tmp_path / "unused server data"
    text = '  Exact café.\n"Keep spacing."  '
    args = ["--data", str(data)]
    assert commands.main(args + ["remember", text, "--url", "http://localhost:8765"]) == 0
    commands.factory.assert_called_with(url="http://localhost:8765", token_env="COMMONPLACE_TOKEN")
    commands.remote.remember.assert_called_once_with(text)
    assert commands.main(args + ["rule", text, "--agent", "*", "--kind", "limit"]) == 0
    commands.remote.publish_rule.assert_called_once_with(text, agent="*", kind="limit")
    assert "owner approval" in capsys.readouterr().out
    assert not data.exists()


def test_cli_rule_default_is_self(client_commands):
    assert client_commands.main(["rule", "Exact rule.", "--token-env", "TEST_TOKEN"]) == 0
    client_commands.factory.assert_called_once_with(url=None, token_env="TEST_TOKEN")
    client_commands.remote.publish_rule.assert_called_once_with(
        "Exact rule.", agent="self", kind="instruction"
    )


def test_cli_onboard_uses_default_home_project_and_orders_migration_first(
    client_commands, monkeypatch, tmp_path
):
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.chdir(project)
    order = Mock()
    order.attach_mock(client_commands.migrate, "migrate")
    order.attach_mock(client_commands.install, "install")
    assert client_commands.main(["onboard"]) == 0
    client_commands.migrate.assert_called_once_with(
        client_commands.remote, home=home, project=project, dry_run=False
    )
    client_commands.install.assert_called_once_with(
        home=home, project=project, url=None, token_env="COMMONPLACE_TOKEN", dry_run=False
    )
    assert [call[0] for call in order.mock_calls] == ["migrate", "install"]


def test_cli_onboard_explicit_paths_and_project_opt_out(client_commands, tmp_path):
    home, project = tmp_path / "selected home", tmp_path / "selected project"
    assert (
        client_commands.main(
            [
                "onboard",
                "--home",
                str(home),
                "--project",
                str(project),
                "--url",
                "https://example.test",
            ]
        )
        == 0
    )
    assert client_commands.migrate.call_args.kwargs["project"] == project
    assert client_commands.install.call_args.kwargs["url"] == "https://example.test"
    assert client_commands.main(["onboard", "--home", str(home), "--no-project"]) == 0
    assert client_commands.migrate.call_args.kwargs["project"] is None
    assert client_commands.install.call_args.kwargs["project"] is None


def test_cli_dry_run_needs_no_token_and_changes_nothing(client_commands, monkeypatch, tmp_path):
    from commonplace import client, onboarding

    # Exercise the real preflight instead of the migration stub.
    monkeypatch.setattr(onboarding, "onboard", client_commands.original_onboard)
    monkeypatch.setattr(client, "Client", Mock(side_effect=AssertionError("dry-run client access")))
    monkeypatch.delenv("COMMONPLACE_TOKEN", raising=False)
    home = tmp_path / "empty home"
    assert client_commands.main(["onboard", "--dry-run", "--no-project", "--home", str(home)]) == 0
    client_commands.install.assert_called_once_with(
        home=home, project=None, url=None, token_env="COMMONPLACE_TOKEN", dry_run=True
    )
    assert not home.exists()


def test_cli_onboarding_refusal_continues_client_setup_and_reports_warning(client_commands, capsys):
    client_commands.migrate.return_value = {
        "files": [{"path": "CLAUDE.md", "status": "refused", "reason": "complete rule too large"}],
        "warnings": ["CLAUDE.md: complete rule too large"],
    }
    assert client_commands.main(["onboard", "--no-project"]) == 1
    client_commands.install.assert_called_once()
    assert "CLAUDE.md: complete rule too large" in capsys.readouterr().err


@pytest.mark.parametrize("command", ["onboard", "remember", "rule"])
@pytest.mark.parametrize("error", [OSError, ValueError, "client"])
def test_cli_errors_never_echo_source_or_credentials(client_commands, command, error, capsys):
    if error == "client":
        from commonplace.client import ClientError

        error = ClientError
    client_commands.factory.side_effect = error("secret-test-token private-source-text")
    args = [command] + (["private-source-text"] if command != "onboard" else [])
    assert client_commands.main(args) == 2
    output = capsys.readouterr()
    assert "failed" in output.err
    assert "secret-test-token" not in output.out + output.err
    assert "private-source-text" not in output.out + output.err


def test_cli_check_warns_without_uploading_or_printing_content(client_commands, tmp_path, capsys):
    from commonplace.onboarding import POINTER

    home = tmp_path / "home"
    managed = home / ".codex/AGENTS.md"
    managed.parent.mkdir(parents=True)
    managed.write_text(POINTER, encoding="utf-8")
    backup = home / ".commonplace/backups/AGENTS.md"
    backup.parent.mkdir(parents=True)
    backup.write_text("private-source-text", encoding="utf-8")
    args = ["check", "--home", str(home), "--no-project"]
    assert client_commands.main(args) == 0
    capsys.readouterr()
    managed.write_text("private-source-text", encoding="utf-8")
    assert client_commands.main(args) == 1
    output = capsys.readouterr()
    assert "AGENTS.md: local-only" in output.err
    assert "memory_save" in output.err and "rules_publish" in output.err
    assert "private-source-text" not in output.out + output.err
    client_commands.factory.assert_not_called()
    assert managed.read_text(encoding="utf-8") == "private-source-text"


def test_cli_check_project_opt_out_and_defaults(client_commands, monkeypatch, tmp_path):
    home, project = tmp_path / "home", tmp_path / "project"
    project.mkdir()
    (project / "MEMORY.md").write_text("local memory", encoding="utf-8")
    monkeypatch.chdir(project)
    assert client_commands.main(["check", "--home", str(home), "--no-project"]) == 0
    assert client_commands.main(["check", "--home", str(home)]) == 1


def test_cli_rejects_conflicting_project_options_and_other_agent_targets(client_commands):
    for args in (
        ["onboard", "--project", "example", "--no-project"],
        ["check", "--project", "example", "--no-project"],
        ["rule", "Exact rule.", "--agent", "someone-else"],
    ):
        with pytest.raises(SystemExit) as caught:
            client_commands.main(args)
        assert caught.value.code == 2
    client_commands.factory.assert_not_called()
