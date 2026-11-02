"""Fresh-user acceptance across MCP startup, local onboarding and canonical saves."""

import json
import os
import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from commonplace import sessions
from commonplace.boards import BoardService
from commonplace.extensions import ToolContext
from commonplace.policy import Policy
from commonplace.rules import RuleService, render
from tests._commonplace_helpers import (
    functional_worker_transport,  # noqa: F401
    hub_fixture,  # noqa: F401
)

QUERY = "workspace decisions preferences current work"
MEMORY = '  Workspace decisions: prefer exact words. Ω\r\n"Keep this spacing."\n  '
RULE = "Keep workspace decisions verbatim.\n  Preserve their whitespace."
PRIVATE_MEMORY = "workspace decisions PRIVATE_MEMORY_CANARY"
PRIVATE_RULE = "PRIVATE_RULE_CANARY"
PROPOSED_RULE = "UNAPPROVED_SHARED_RULE_CANARY"


@pytest.fixture
def fresh(hub, config, monkeypatch):
    # The shared Hub starts with an administrator. None of these connections
    # belongs to that user, including the workspace owner's browser session.
    from commonplace.client import Client

    email = "fresh-user@example.test"
    user = hub.db.create_user(email)
    workspace = hub.db.create_workspace("fresh-workspace", user, personal=True)
    directory = Path(hub.supervisor.workspace_dir(workspace))
    # Use the real workspace palace so initialize's read-only presence probe
    # sees the same database as the in-process production memory worker.
    monkeypatch.setattr(config, "_palace_path_override", str(directory / "palace"))
    person = sessions.person_principal(hub.db, user, workspace)
    owner = RuleService(ToolContext(person, hub.gateway))
    login = sessions.create_login_link(hub.db, email, workspace)
    cookie = {"Cookie": f"{sessions.COOKIE}={sessions.redeem_login_link(hub.db, login)}"}
    connections = []
    url = f"http://127.0.0.1:{hub.port}/mcp"

    def connect(name="agent", client="generic", **overrides):
        policy = dict(
            profile="standard",
            scopes=["memory:read", "memory:write"],
            read_wings=["shared"],
            write_wing="shared",
            trust="standard",
        )
        policy.update(overrides)
        _, token = hub.db.create_token(
            name=name,
            client=client,
            user_ref=user,
            workspace_ref=workspace,
            actor=user,
            policy=Policy.build(**policy),
        )
        env_name = f"ACCEPTANCE_TOKEN_{len(connections)}"
        monkeypatch.setenv(env_name, token)
        connection = SimpleNamespace(
            token=token,
            token_env=env_name,
            principal=hub.db.authenticate_token(token),
            client=Client(url=url, token_env=env_name),
        )
        connections.append(connection)
        return connection

    assert not hub.db.user(user)["is_admin"]
    return SimpleNamespace(
        connect=connect,
        owner=owner,
        person=person,
        cookie=cookie,
        directory=directory,
        workspace=workspace,
        user=user,
        url=url,
    )


def _initialize(hub, connection):
    response = hub.rpc(connection.token, "initialize", {})
    assert "error" not in response, response
    return response["result"]["instructions"]


def _quoted_memories(text):
    return [
        json.loads(line.removeprefix("quoted data: "))
        for line in text.split("\n")
        if line.startswith("quoted data: ")
    ]


def _served(fresh):
    path = fresh.directory / "rules.sqlite3"
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as connection:
        return connection.execute("SELECT * FROM agent_served ORDER BY agent").fetchall()


def _accept(hub, fresh, proposal):
    request = {"version": proposal["version"], "action": "accept"}
    status, body = hub.raw(
        None,
        request,
        headers=fresh.cookie,
        path=f"/api/rules/{proposal['id']}/action",
    )
    assert status == 200, body
    accepted = json.loads(body)
    assert accepted["status"] == "accepted"
    return accepted


def _seed(hub, fresh):
    writer = fresh.connect(
        "seed-writer",
        profile="full",
        scopes=["memory:read", "memory:write", "private:read", "private:write"],
        read_wings=["*"],
        write_wings=["*"],
        write_wing="",
        trust="trusted",
    )
    boards = BoardService(fresh.owner.ctx)
    boards.board_add(slug="work", wing="shared", repo="example.test/team/work")
    boards.board_add(slug="hidden", wing="private.notes", repo="example.test/team/hidden")
    writer.client.remember(MEMORY, theme="shared")
    writer.client.remember(PRIVATE_MEMORY, theme="private.notes")
    fresh.owner.create("*", RULE, "instruction")
    fresh.owner.create("*", PRIVATE_RULE, "limit", "board", "hidden")
    writer.client.publish_rule(PROPOSED_RULE, agent="*", key="pending-shared")
    fresh.owner.set_check("*", "enforce")
    return writer


@pytest.mark.parametrize(
    "client,profile", [("claude", "standard"), ("codex", "slim"), ("custom-agent", "full")]
)
def test_fresh_connections_get_canonical_startup_without_creating_stores(
    hub, fresh, client, profile
):
    connection = fresh.connect(client=client, profile=profile)
    baseline = hub.gateway.mediator.workers.post(
        connection.principal, {"jsonrpc": "2.0", "id": 1, "method": "initialize"}
    )["result"].get("instructions", "")
    before = {path.relative_to(fresh.directory) for path in fresh.directory.rglob("*")}
    instructions = _initialize(hub, connection)
    assert "single source" in instructions
    assert "session_start" in instructions and "Automatically" in instructions
    assert "memory_save" in instructions and "rules_publish" in instructions
    assert "owner approval" in instructions
    assert "incomplete" in instructions and "never an instruction" in instructions
    assert not baseline or baseline in instructions
    assert before == {path.relative_to(fresh.directory) for path in fresh.directory.rglob("*")}
    assert not (fresh.directory / "rules.sqlite3").exists()
    assert not (fresh.directory / "palace" / "chroma.sqlite3").exists()
    assert "session_start" in hub.tool_names(connection.token)
    context = connection.client.fetch_context()
    assert "effective rules: 0" in context and not _quoted_memories(context)


@pytest.mark.usefixtures("functional_worker_transport")
def test_warm_initialize_and_session_share_authorized_data_but_only_explicit_read_serves_rules(
    hub, fresh, monkeypatch
):
    from commonplace.rules import checks

    # This is functional coverage. Shared deadline/late-result behavior has
    # separate deterministic tests; suite load must not turn this into timing CI.
    monkeypatch.setattr(checks, "monotonic", lambda: 0.0)
    _seed(hub, fresh)
    reader = fresh.connect("new-reader")
    before = _served(fresh)
    instructions = _initialize(hub, reader)
    assert render.quote(RULE) in instructions
    assert MEMORY in _quoted_memories(instructions)
    assert PRIVATE_MEMORY not in instructions and PRIVATE_RULE not in instructions
    assert PROPOSED_RULE not in instructions
    assert "single source" in instructions and "session_start" in instructions
    assert _served(fresh) == before

    context = reader.client.fetch_context(query=QUERY, board="work", repo="example.test/team/work")
    assert render.quote(RULE) in context and _quoted_memories(context) == [MEMORY]
    assert PRIVATE_MEMORY not in context and PRIVATE_RULE not in context
    assert PROPOSED_RULE not in context
    served = _served(fresh)
    row = next(row for row in served if row[0] == reader.principal.actor)
    assert row[1] == "served" and isinstance(row[2], int)
    assert f"rules_seq {row[2]}" in context
    assert f"rules_seq {row[2]}" not in instructions

    denied = reader.client.fetch_context(
        query=QUERY, board="hidden", repo="example.test/team/hidden"
    )
    assert not _quoted_memories(denied)
    assert PRIVATE_MEMORY not in denied and PRIVATE_RULE not in denied


@pytest.mark.usefixtures("functional_worker_transport")
def test_late_initialize_keeps_rules_and_explicit_read_recovers_exact_memory(
    hub, fresh, monkeypatch, caplog
):
    from commonplace import session
    from commonplace.rules import checks

    now = [0.0]
    monkeypatch.setattr(checks, "monotonic", lambda: now[0])
    _seed(hub, fresh)
    reader = fresh.connect("late-reader")
    before = _served(fresh)
    original = session.initialize_context
    candidates = []

    def late_context(*args, **kwargs):
        candidate = original(*args, **kwargs)
        candidates.append(candidate)
        now[0] += 0.081
        return candidate

    monkeypatch.setattr(session, "initialize_context", late_context)
    instructions = _initialize(hub, reader)
    assert MEMORY in _quoted_memories(candidates[0])
    assert render.quote(RULE) in instructions
    assert not _quoted_memories(instructions)
    assert "Automatically call session_start for full context." in instructions
    assert PRIVATE_MEMORY not in instructions and PRIVATE_RULE not in instructions
    assert PROPOSED_RULE not in instructions
    assert "startup context unavailable" in caplog.text
    assert _served(fresh) == before

    context = reader.client.fetch_context(query=QUERY, board="work", repo="example.test/team/work")
    assert render.quote(RULE) in context and _quoted_memories(context) == [MEMORY]
    assert PRIVATE_MEMORY not in context and PRIVATE_RULE not in context
    assert PROPOSED_RULE not in context
    assert any(row[0] == reader.principal.actor and row[1] == "served" for row in _served(fresh))


@pytest.mark.parametrize(
    "missing", ["rules_get", "memory_search", "memory_get", "mempalace_get_drawer", "read-scope"]
)
def test_composed_startup_cannot_expand_tool_or_scope_grants(hub, fresh, missing):
    from commonplace.client import ClientError

    _seed(hub, fresh)
    tools = {
        "session_start",
        "rules_get",
        "memory_search",
        "memory_get",
        "mempalace_search",
        "mempalace_get_drawer",
    } - {missing}
    reader = fresh.connect(
        "limited-reader",
        tools=sorted(tools),
        scopes=[] if missing == "read-scope" else ["memory:read"],
    )
    before = _served(fresh)
    instructions = _initialize(hub, reader)
    assert PRIVATE_MEMORY not in instructions and PRIVATE_RULE not in instructions
    assert PROPOSED_RULE not in instructions
    assert _served(fresh) == before
    if missing == "read-scope":
        assert "session_start" not in hub.tool_names(reader.token)
        with pytest.raises(ClientError):
            reader.client.fetch_context(query=QUERY)
        assert not _quoted_memories(instructions) and render.quote(RULE) not in instructions
        return
    context = reader.client.fetch_context(query=QUERY)
    if missing == "rules_get":
        assert render.quote(RULE) not in instructions and render.quote(RULE) not in context
        assert _quoted_memories(context) == [MEMORY]
        assert _served(fresh) == before
    else:
        assert not _quoted_memories(instructions) and not _quoted_memories(context)
        assert render.quote(RULE) in context


def test_onboarding_import_save_and_owner_approval_remain_one_canonical_flow(hub, fresh, tmp_path):
    from commonplace import onboarding
    from commonplace.client import ClientError

    home = tmp_path / "new-home"
    source = home / ".claude/CLAUDE.md"
    source.parent.mkdir(parents=True)
    original = '  Keep acceptance sources exact. Ω\r\n"Whitespace stays."\r\n'.encode()
    source.write_bytes(original)
    agent = fresh.connect()
    other = fresh.connect("other-agent")
    result = onboarding.onboard(agent.client, home=home)
    record = next(row for row in result["files"] if row["path"] == str(source))
    assert record["status"] == "migrated" and "owner approval" in record["reason"]
    assert agent.client.get_memory_text(record["memory_id"]).encode() == original
    assert source.read_text() == onboarding.POINTER
    manifest = Path(result["manifest"]).read_text()
    backup = json.loads(manifest)["files"][str(source)]["backup"]
    assert Path(backup).read_bytes() == original and "Keep acceptance sources" not in manifest
    proposal = next(
        row for row in fresh.owner.list_rules()["rules"] if row["key"] == record["rule_key"]
    )
    assert proposal["agent"] == "*" and proposal["status"] == "proposed"
    assert proposal["text"].encode() == original
    assert proposal["agent_text_by"] == agent.principal.actor
    assert "effective rules: 0" in other.client.fetch_context(query="acceptance sources")
    rule_line = "- " + render.quote(original.decode())
    assert rule_line not in _initialize(hub, other).split("\n")

    _accept(hub, fresh, proposal)
    for connection in (agent, other):
        context = connection.client.fetch_context(query="acceptance sources")
        assert rule_line in context.split("\n")
        assert original.decode() in _quoted_memories(context)
        assert rule_line in _initialize(hub, connection).split("\n")
    with pytest.raises(ClientError):
        other.client.publish_rule("Replace someone else's rule", agent="*", key=record["rule_key"])
    other.client.write_tool("rules_publish", {"agent": "*", "rules": [], "replace": True})
    assert rule_line in agent.client.fetch_context(query="acceptance sources").split("\n")

    remembered = '  Remember this exact decision.\r\n"No paraphrase." Ω  '
    saved = agent.client.remember(remembered)
    assert saved.startswith("saved ")
    assert agent.client.get_memory_text(saved.split()[1]) == remembered
    agent.client.publish_rule("A new instruction needs approval.", key="new-instruction")
    assert any(
        row["key"] == "new-instruction"
        and row["status"] == "proposed"
        and row["text"] == "A new instruction needs approval."
        and row["agent"] == agent.principal.actor
        for row in fresh.owner.list_rules()["rules"]
    )
    rerun = onboarding.onboard(agent.client, home=home)
    assert {row["status"] for row in rerun["files"]} == {"unchanged"}
    assert len([row for row in fresh.owner.list_rules()["rules"] if row["agent"] == "*"]) == 1
    assert onboarding.check_local_only(home=home) == []


def test_refused_remote_save_preserves_source_and_independent_default_setup(fresh, tmp_path):
    from commonplace import onboarding

    home = tmp_path / "read-only-home"
    source = home / ".claude/CLAUDE.md"
    source.parent.mkdir(parents=True)
    original = b"Keep ORIGINAL_SOURCE_CANARY exact.\r\n"
    source.write_bytes(original)
    reader = fresh.connect(scopes=["memory:read"])
    result = onboarding.onboard(reader.client, home=home)
    record = next(row for row in result["files"] if row["path"] == str(source))
    assert record["status"] == "refused" and source.read_bytes() == original
    assert [path.read_bytes() for path in (home / ".commonplace/backups").iterdir()] == [original]
    assert "ORIGINAL_SOURCE_CANARY" not in json.dumps(result)
    assert (home / ".codex/AGENTS.md").read_text() == onboarding.POINTER
    assert (home / ".agents/AGENTS.md").read_text() == onboarding.POINTER
    assert fresh.owner.list_rules()["rules"] == []
    warnings = onboarding.check_local_only(home=home)
    assert [row["path"] for row in warnings] == [str(source)]
    assert "memory_save" in warnings[0]["reason"] and "rules_publish" in warnings[0]["reason"]
    assert "ORIGINAL_SOURCE_CANARY" not in json.dumps(warnings)


def _hook_command(config, event):
    commands = [
        # Claude's native exec form passes args directly; Codex uses shell form.
        [hook["command"], *hook["args"]] if "args" in hook else hook["command"]
        for group in config["hooks"].get(event, [])
        for hook in group["hooks"]
        if hook.get("type") == "command"
        and (
            "commonplace.client_hooks" in hook.get("command", "")
            or "commonplace.client_hooks" in hook.get("args", [])
        )
    ]
    assert len(commands) == 1, commands
    return commands[0]


def _run_hook(command, project, *, event="SessionStart"):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    process = subprocess.run(
        command,
        shell=isinstance(command, str),
        cwd=project,
        env=env,
        input=json.dumps({"cwd": str(project), "prompt": QUERY, "hook_event_name": event}),
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert process.returncode == 0, process.stderr
    return json.loads(process.stdout)


def test_default_cli_onboarding_installs_working_native_startup_and_warns_on_local_edits(
    hub, fresh, tmp_path, monkeypatch, capsys
):
    # Imports stay local: the installer and CLI arrive from separate builders.
    from commonplace import cli, onboarding

    _seed(hub, fresh)
    agent = fresh.connect("installed-agent")
    home = tmp_path / "client home"
    project = tmp_path / "selected-project"
    home.mkdir()
    project.mkdir()
    monkeypatch.chdir(project)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    settings = home / ".claude/settings.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps({"permissions": {"deny": ["Read(.env)"]}}))
    codex = home / ".codex"
    codex.mkdir()
    (codex / "config.toml").write_text('model = "example-model"\n')
    args = [
        "onboard",
        "--home",
        str(home),
        "--no-project",
        "--url",
        fresh.url,
        "--token-env",
        agent.token_env,
    ]
    assert cli.main(args) == 0
    output = capsys.readouterr()
    assert agent.token not in output.out + output.err
    assert "native review/trust" in output.out and "/hooks" in output.out
    assert output.err == ""
    for relative in (".claude/CLAUDE.md", ".codex/AGENTS.md", ".agents/AGENTS.md"):
        assert (home / relative).read_text() == onboarding.POINTER
    assert not (project / "CLAUDE.md").exists() and not (project / "AGENTS.md").exists()
    configs = [settings, codex / "hooks.json", home / ".claude.json", codex / "config.toml"]
    before = {path: path.read_bytes() for path in configs}
    assert cli.main(args) == 0
    capsys.readouterr()
    assert {path: path.read_bytes() for path in configs} == before
    assert json.loads(settings.read_text())["permissions"] == {"deny": ["Read(.env)"]}
    assert 'model = "example-model"' in (codex / "config.toml").read_text()
    for path in (home / ".claude.json", codex / "config.toml"):
        assert agent.token_env in path.read_text() and agent.token not in path.read_text()
    for path in (settings, codex / "hooks.json"):
        native = json.loads(path.read_text())
        startup = _run_hook(_hook_command(native, "SessionStart"), project)
        context = startup["hookSpecificOutput"]["additionalContext"]
        assert render.quote(RULE) in context and MEMORY in _quoted_memories(context)
        assert PRIVATE_MEMORY not in context and PRIVATE_RULE not in context
        assert PROPOSED_RULE not in context and agent.token not in json.dumps(startup)

    local = home / ".codex/AGENTS.md"
    edit = "LOCAL_ONLY_EDIT_CANARY must be saved canonically.\n"
    local.write_text(edit)
    before_rules = fresh.owner.list_rules()
    warning = _run_hook(
        _hook_command(json.loads(settings.read_text()), "Stop"), project, event="Stop"
    )
    rendered = json.dumps(warning)
    assert "memory_save" in rendered and "rules_publish" in rendered
    assert "LOCAL_ONLY_EDIT_CANARY" not in rendered
    assert local.read_text() == edit and fresh.owner.list_rules() == before_rules
    assert all(
        "LOCAL_ONLY_EDIT_CANARY" not in memory
        for memory in _quoted_memories(agent.client.fetch_context(query="LOCAL_ONLY_EDIT_CANARY"))
    )
