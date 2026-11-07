"""Inert area preview: topology, frozen experiments and transactional refusals."""

import copy
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from commonplace import extensions
from commonplace.orch import cli, schema, store as orch_store, tools
from tests._commonplace_helpers import hub_fixture  # noqa: F401
from tests.test_orch_surfaces import BOARD, Env, config_text

SENTINEL = "private-preview-body-marker"


def area_doc():
    return {
        "schema": schema.SCHEMA_ID,
        "project": BOARD,
        "roles": {
            "main": {"kind": "orchestrator"},
            "frontend": {
                "kind": "sub-orchestrator",
                "parent": "main",
                "topic": "frontend",
                "area": "frontend",
                "prompt": SENTINEL,
            },
        },
    }


def text(doc):
    """Encode these deliberately simple fixtures without using schema serialization."""
    lines = []
    for key, value in doc.items():
        if isinstance(value, dict):
            if all(not isinstance(item, dict) for item in value.values()):
                lines.append(f"[{key}]")
                lines.extend(f"{name} = {json.dumps(item)}" for name, item in value.items())
            else:
                for name, item in value.items():
                    lines.append(f"[{key}.{name}]")
                    lines.extend(f"{k} = {json.dumps(v)}" for k, v in item.items())
        else:
            lines.append(f"{key} = {json.dumps(value)}")
    return "\n".join(lines) + "\n"


def outcome(call):
    try:
        call()
    except (schema.ConfigError, orch_store.OrchError) as exc:
        return exc.code, exc.path
    return None, None


def assert_area_preview_supported(doc=None):
    doc = area_doc() if doc is None else doc
    result = outcome(lambda: schema.parse(text(doc)))
    assert result == (None, None), f"expected preview code None, got {result}"


def assert_error(doc, code, path):
    assert outcome(lambda: schema.parse(text(doc))) == (code, path)


def without_area(doc):
    doc = copy.deepcopy(doc)
    for role in doc["roles"].values():
        role.pop("area", None)
    return doc


def test_preview_roundtrip_diff(tmp_path):
    doc = area_doc()
    assert_area_preview_supported(doc)
    config = schema.parse(text(doc))
    assert config.to_dict() == doc
    assert schema.validate(config.to_dict()).to_dict() == doc
    assert "area: `frontend`" in schema.render(config)
    old = schema.parse(text(without_area(doc)))
    changes = schema.diff(old, config)
    assert [(c.path, c.op, c.new) for c in changes] == [("roles.frontend.area", "add", "frontend")]
    env_store = orch_store.Store(tmp_path / "workspace")
    from tests.test_orch_store import OWNER

    before = env_store.propose(OWNER, BOARD, text(without_area(doc)), connections=[])
    after = env_store.propose(OWNER, BOARD, text(doc), connections=[])
    assert env_store.diff(BOARD, before.version, after.version) == changes
    doc["roles"]["lead"] = doc["roles"].pop("main")
    doc["roles"]["frontend"]["parent"] = "lead"
    assert_area_preview_supported(doc)
    assert schema.MODEL_ROLES == ("main", "worker")


@pytest.mark.parametrize("field", ["parent", "topic"])
def test_area_required_fields(field):
    doc = area_doc()
    assert_area_preview_supported(doc)
    del doc["roles"]["frontend"][field]
    assert_error(doc, "missing_key", f"roles.frontend.{field}")


def test_area_name():
    doc = area_doc()
    assert_area_preview_supported(doc)
    doc["roles"]["frontend"]["area"] = "Front End"
    assert_error(doc, "invalid_value", "roles.frontend.area")


def test_area_type():
    doc = area_doc()
    assert_area_preview_supported(doc)
    doc["roles"]["frontend"]["area"] = 5
    assert_error(doc, "wrong_type", "roles.frontend.area")


def test_area_count_boundary():
    doc = area_doc()
    child = doc["roles"].pop("frontend")
    for i in range(8):
        doc["roles"][f"a{i}"] = {**child, "area": f"a{i}"}
    assert_area_preview_supported(doc)
    doc["roles"]["ninth"] = {**child, "area": "ninth"}
    assert_error(doc, "too_many", "roles")


def test_area_duplicate():
    doc = area_doc()
    doc["roles"]["backend"] = {**doc["roles"]["frontend"], "area": "backend"}
    assert_area_preview_supported(doc)
    doc["roles"]["backend"]["area"] = "frontend"
    assert_error(doc, "area_duplicate", "roles.backend.area")


@pytest.mark.parametrize("kind", ["builder", "planner", "reviewer", "orchestrator"])
def test_area_kind(kind):
    doc = area_doc()
    assert_area_preview_supported(doc)
    doc["roles"]["frontend"]["kind"] = kind
    if kind == "planner":
        doc["roles"]["frontend"]["prompt"] = "{{include:design_before_code}}"
    assert_error(doc, "area_kind", "roles.frontend.kind")


def test_area_main_parentless():
    doc = area_doc()
    assert_area_preview_supported(doc)
    doc["roles"]["w"] = {"kind": "builder"}
    doc["roles"]["main"]["parent"] = "w"
    assert outcome(lambda: schema.parse(text(without_area(doc)))) == (None, None)
    assert_error(doc, "area_main", "roles")


@pytest.mark.parametrize("parent", ["parentless", "parented", "absent"])
def test_area_main_unique(parent):
    doc = area_doc()
    assert_area_preview_supported(doc)
    if parent == "absent":
        doc["roles"]["main"]["kind"] = "builder"
    else:
        doc["roles"]["lead"] = {"kind": "orchestrator"}
        if parent == "parented":
            doc["roles"]["lead"]["parent"] = "main"
    assert outcome(lambda: schema.parse(text(without_area(doc)))) == (None, None)
    assert_error(doc, "area_main", "roles")


def test_area_parent_unknown():
    doc = area_doc()
    assert_area_preview_supported(doc)
    doc["roles"]["frontend"]["parent"] = "ghost"
    assert_error(doc, "unknown_role", "roles.frontend.parent")


def test_area_parent_indirect():
    doc = area_doc()
    assert_area_preview_supported(doc)
    doc["roles"]["w"] = {"kind": "builder", "parent": "main"}
    doc["roles"]["frontend"]["parent"] = "w"
    assert_error(doc, "area_parent", "roles.frontend.parent")


def test_area_parent_cycle():
    doc = area_doc()
    assert_area_preview_supported(doc)
    doc["roles"]["w"] = {"kind": "builder", "parent": "z"}
    doc["roles"]["z"] = {"kind": "builder", "parent": "w"}
    assert_error(doc, "parent_cycle", "roles.w.parent")


def test_area_runner():
    doc = area_doc()
    assert_area_preview_supported(doc)
    doc["roles"]["frontend"].update(harness="t3", model_role="worker")
    doc["runner"] = {
        "role": "frontend",
        "machine": "mach_0123456789abcdef",
        "project": "demo",
        "runtime_mode": "approval-required",
    }
    doc["limits"] = {"max_agents": 3}
    assert outcome(lambda: schema.parse(text(without_area(doc)))) == (None, None)
    assert_error(doc, "area_runner", "runner.role")


def test_experiment_freeze():
    doc = area_doc()
    assert_area_preview_supported(doc)
    live = schema.parse(text(doc))
    edit = copy.deepcopy(doc)
    edit["roles"]["frontend"]["prompt"] = "New instructions"
    assert schema.experiment_violations(live, schema.parse(text(edit))) == []
    edits = []
    changed = copy.deepcopy(doc)
    changed["roles"]["frontend"]["area"] = "backend"
    edits.append((doc, changed, "area"))
    edits.append((doc, without_area(doc), "area"))
    edits.append((without_area(doc), doc, "area"))
    # No-area configs isolate parent/kind freezing from preview topology checks.
    plain = without_area(doc)
    plain["roles"]["w"] = {"kind": "builder", "parent": "main"}
    for field, value in (("parent", "w"), ("kind", "builder")):
        changed = copy.deepcopy(plain)
        changed["roles"]["frontend"][field] = value
        edits.append((plain, changed, field))
    for before, after, field in edits:
        result = outcome(
            lambda before=before, after=after: schema.check_experiment(
                schema.parse(text(before)), schema.parse(text(after))
            )
        )
        assert result == ("experiment_not_allowed", f"roles.frontend.{field}")


@pytest.fixture
def env(hub):
    return Env(hub)


def snapshot(env):
    """Full rows, including leases, jobs, cursors and versions; no count-only proof."""
    directory = Path(env.hub.gateway.workspace_dir(env.ws))
    result = {}
    for name in ("runner.sqlite3", "boards.sqlite3"):
        with sqlite3.connect(directory / name) as conn:
            tables = [
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            ]
            result[name] = {
                table: sorted(conn.execute(f'SELECT * FROM "{table}"').fetchall(), key=repr)
                for table in tables
            }
    with env.hub.db._lock:
        conn = env.hub.db._conn
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND "
                "(name LIKE 'project_%' OR name LIKE '%token%' OR name LIKE '%subscription%' "
                "OR name IN ('connections', 'runner_connections', 'events'))"
            )
        ]
        result["control"] = {
            table: sorted((tuple(r) for r in conn.execute(f'SELECT * FROM "{table}"')), key=repr)
            for table in tables
        }
    return result


def assert_refusal(call):
    try:
        call()
    except orch_store.OrchError as exc:
        assert (exc.code, exc.path, exc.status) == ("area_not_supported", "roles", 400)
        assert str(exc) == "no: area configs are preview-only (area_not_supported)"
        assert SENTINEL not in str(exc)
    else:
        raise AssertionError("area acceptance must refuse")


@pytest.mark.parametrize(
    "path", ["store_accept", "store_write", "tools_write", "mcp_write", "rest_write", "cli_accept"]
)
@pytest.mark.parametrize("had_runner", [False, True])
def test_acceptance_paths(env, tmp_path, monkeypatch, path, had_runner):
    assert_area_preview_supported()
    st = orch_store.open_store(env.hub.gateway, env.ws)
    baseline = config_text(runner=had_runner)
    previous = st.write(
        env.owner, BOARD, baseline, connections=[], sha256=schema.sha256_text(baseline)
    )
    doc = area_doc()
    # Opposite runner state detects both resetting and dropping a cursor before refusal.
    if not had_runner:
        doc["roles"]["main"].update(harness="t3", topic="orchestrate", model_role="main")
        doc["limits"] = {"max_agents": 3}
        doc["runner"] = {
            "role": "main",
            "machine": "mach_0123456789abcdef",
            "project": "demo",
            "runtime_mode": "approval-required",
        }
    body = text(doc)
    proposal = st.propose(env.a, BOARD, body, connections=[])
    assert proposal.status == "proposed"
    assert SENTINEL in schema.render(schema.parse(proposal.body))
    cursor_calls = []
    for name in ("reset_cursor_in", "drop_cursor_in"):
        original = getattr(orch_store, name)

        def observed(*args, _name=name, _original=original, **kwargs):
            cursor_calls.append(_name)
            return _original(*args, **kwargs)

        monkeypatch.setattr(orch_store, name, observed)
    before = snapshot(env)
    args = {"board": BOARD, "text": body, "sha256": schema.sha256_text(body)}
    if path == "store_accept":

        def call():
            return st.accept(
                env.owner, BOARD, proposal.version, sha256=args["sha256"], connections=[]
            )
    elif path == "store_write":

        def call():
            return st.write(env.owner, connections=[], **args)
    elif path == "tools_write":

        def call():
            return tools.config_write(env.ctx(env.owner), **args)
    elif path == "mcp_write":
        # Exercise the registered MCP handler with an owner principal; PATs are not people.
        handler = extensions.get("orch_config_write").handler
        from commonplace.extensions import ToolError

        try:
            handler(env.ctx(env.owner), args)
        except ToolError as exc:
            assert str(exc) == (
                "no: area configs are preview-only (area_not_supported)\n"
                '{"code": "area_not_supported"}'
            )
        else:
            raise AssertionError("registered MCP write must refuse")
        call = None
    elif path == "rest_write":
        status, payload = env.person(
            "POST", f"boards/{BOARD}/write", {"text": body, "sha256": args["sha256"]}
        )
        assert (status, payload["code"]) == (400, "area_not_supported")
        assert SENTINEL not in str(payload)
        assert payload["error"] == "no: area configs are preview-only (area_not_supported)"
        call = None
    else:
        # Real CLI context, using the data directory and normal local owner lookup.
        cli_args = SimpleNamespace(
            data=str(tmp_path / "data"),
            workspace=env.ws,
            by=None,
            board=BOARD,
            version=proposal.version,
            sha256=args["sha256"],
        )

        def call():
            return cli.cmd_accept(cli_args)

    if call is not None:
        assert_refusal(call)
    assert cursor_calls == []
    assert snapshot(env) == before
    assert st.get(BOARD, proposal.version).status == "proposed"
    assert st.accepted(BOARD).version == previous.version.version
    assert not any(e["kind"] == "orch.config.accepted" for e in env.events())
    assert "area_not_supported" not in schema.CODES


@pytest.mark.parametrize("path", ["accept", "write"])
def test_exp_acceptance(env, path):
    assert_area_preview_supported()
    from tests._orch_helpers import seed_accepted_area

    st = orch_store.open_store(env.hub.gateway, env.ws)
    # Adding an area to a plain live config is a forbidden experiment at proposal time.
    doc = area_doc()
    plain = text(without_area(doc))
    st.write(env.owner, BOARD, plain, connections=[], sha256=schema.sha256_text(plain))
    assert outcome(
        lambda: st.propose(env.owner, BOARD, text(doc), connections=[], channel="exp:add")
    ) == ("experiment_not_allowed", "roles.frontend.area")
    seed_accepted_area(st.directory, BOARD, text(doc), version=3)
    seed_accepted_area(st.directory, BOARD, text(doc), version=4, channel="exp:trial")
    doc["roles"]["frontend"]["prompt"] = SENTINEL + " edited"
    body = text(doc)
    proposed = st.propose(env.owner, BOARD, body, connections=[], channel="exp:trial")
    before = snapshot(env)
    if path == "accept":

        def call():
            return st.accept(
                env.owner, BOARD, proposed.version, sha256=schema.sha256_text(body), connections=[]
            )
    else:

        def call():
            return st.write(
                env.owner,
                BOARD,
                body,
                sha256=schema.sha256_text(body),
                connections=[],
                channel="exp:trial",
            )

    assert_refusal(call)
    assert snapshot(env) == before


def test_preview_boundaries(env):
    assert_area_preview_supported()
    from tests._orch_helpers import seed_accepted_area

    st = orch_store.open_store(env.hub.gateway, env.ws)
    before = snapshot(env)
    body = text(area_doc())
    proposed = tools.config_propose(env.ctx(env.owner), BOARD, body)
    rendered = tools.config_render(env.ctx(env.owner), BOARD, version=proposed["version"])
    assert "area: `frontend`" in rendered["markdown"]
    after = snapshot(env)
    for db in before:
        for table in before[db]:
            if table not in ("orch_configs", "events"):
                assert before[db][table] == after[db][table], table
    assert st.accepted(BOARD).body == config_text()
    for principal in (env.a, env.untrusted):
        for name, args in (
            (
                "orch_config_accept",
                {"board": BOARD, "version": proposed["version"], "sha256": proposed["sha256"]},
            ),
            (
                "orch_config_write",
                {"board": BOARD, "text": body, "sha256": schema.sha256_text(body)},
            ),
        ):
            assert outcome(
                lambda principal=principal, name=name, args=args: tools.dispatch(
                    env.ctx(principal), name, args
                )
            ) == (
                "owner_only",
                "",
            )
    # Proposals retain the existing write-wing boundary.
    from commonplace.extensions import ToolError

    try:
        tools.config_propose(env.ctx(env.reader), BOARD, body)
    except ToolError:
        pass
    else:
        raise AssertionError("read-only principal must not propose")
    for version in (3, 4):
        seeded = seed_accepted_area(st.directory, BOARD, body, version=version)
        assert seeded["sha256"] == schema.sha256_text(body)
    rows = st.versions(BOARD)
    assert sum(r.status == "accepted" and r.channel == "live" for r in rows) == 1
    assert st.get(BOARD, 4).parent_version == 3
    assert st.get(BOARD, 3).status == "superseded"
    for source in (Path(__file__).resolve().parents[1] / "commonplace").rglob("*.py"):
        assert "seed_accepted_area" not in source.read_text(), source
