"""Manual handoff workspace selection is discoverable without launching T3."""

import json

import pytest

from commonplace import cli
from commonplace.control_db import ControlDB
from commonplace.handoff import manual
from commonplace.handoff.db import HandoffDB
from commonplace.handoff.models import SettingRow
from commonplace.handoff.t3 import SourceThread


@pytest.fixture
def workspace_cli(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in (
        "COMMONPLACE_WORKSPACE",
        "COMMONPLACE_HANDOFF",
        "COMMONPLACE_AGENT",
        "COMMONPLACE_TASK",
    ):
        monkeypatch.delenv(name, raising=False)
    db = ControlDB(str(tmp_path / "control.sqlite3"))
    owner = db.create_user("owner@example.test", is_admin=True)
    workspace = db.create_workspace("my space", owner, personal=True)
    cli._settings(db, HandoffDB(db)).set(
        SettingRow(workspace, "workspace", "", enabled=True), actor=owner, is_owner=True
    )
    source = SourceThread(
        "mcp:source",
        str(tmp_path / "project" / "checkout"),
        "codex",
        "model",
        "messages",
        (),
        "project",
        workspace_root=str(tmp_path / "project"),
    )
    monkeypatch.setattr(cli, "_db", lambda args: db)
    monkeypatch.setattr(manual.T3, "resolve", lambda self, thread: source)

    def run(*flags):
        return cli.main(
            [
                "--data",
                str(tmp_path),
                "handoff",
                "start",
                "--thread",
                source.thread_id,
                "--to",
                "claude-code/claude-opus-5-5",
                "--dry-run",
                *flags,
            ]
        )

    yield db, workspace, source, run
    db.close()


@pytest.mark.parametrize("selection", ["missing", "unknown", "ambiguous"])
def test_workspace_errors_show_usable_ids(workspace_cli, capsys, selection):
    db, workspace, source, run = workspace_cli
    flags = []
    if selection == "unknown":
        flags = ["--workspace", "personal"]
    elif selection == "ambiguous":
        db.create_workspace("my space", db.users()[0]["id"])
        flags = ["--workspace", "my space"]
    assert run(*flags) == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert workspace in output.err and "my space" in output.err
    assert "--workspace" in output.err and "workspace list" in output.err
    assert not HandoffDB(db).list(workspace)


@pytest.mark.parametrize("selection", ["id", "name", "environment", "checkout", "root"])
def test_workspace_selection_routes_to_the_selected_workspace(
    workspace_cli, tmp_path, monkeypatch, capsys, selection
):
    db, workspace, source, run = workspace_cli
    flags = []
    if selection in ("id", "name"):
        # An explicit argument wins over the environment and checkout mapping.
        monkeypatch.setenv("COMMONPLACE_WORKSPACE", "missing")
        flags = ["--workspace", workspace if selection == "id" else "my space"]
    elif selection == "environment":
        monkeypatch.setenv("COMMONPLACE_WORKSPACE", workspace)
    else:
        root = source.cwd if selection == "checkout" else source.workspace_root
        config = tmp_path / ".commonplace" / "handoff-client.json"
        config.parent.mkdir()
        config.write_text(json.dumps({"workspaces": {root: workspace}}))
    assert run(*flags) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["dry_run"] and result["cwd"] == source.cwd
    assert not HandoffDB(db).list(workspace)


def test_workspace_help_explains_discovery(capsys):
    with pytest.raises(SystemExit) as error:
        cli.build_parser().parse_args(["handoff", "start", "--help"])
    assert error.value.code == 0
    output = capsys.readouterr().out
    assert "workspace list" in output and "name or ID" in output
