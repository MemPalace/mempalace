"""Orchestrator store: versions, owner-only accept, cursor rows and the restore flag (R5)."""

import importlib
import json
import os
import sqlite3
from contextlib import closing

import pytest

from commonplace.backup import RESTORE_MARKER, backup, restore
from commonplace.control_db import ControlDB, Principal
from commonplace.orch import schema, store as orch_store
from commonplace.orch.store import OrchError, Store
from commonplace.policy import Policy

BOARD = "acme-shop"
MACHINE = "mach_0123456789abcdef"
CONNECTIONS = ("planner-acme", "builder-acme")


def principal(
    *, person=True, role="owner", auth="session", runner=None, user="u1", trust="trusted"
):
    return Principal(
        user_id=user,
        user_email=f"{user}@example.test",
        workspace_id="ws_test",
        connection_id="" if person else "conn_00000000000000aa",
        connection="web" if person else "agent-one",
        client="test",
        auth=auth,
        role=role,
        policy=Policy.build(profile="full", read_wings=["*"], trust=trust),
        person=person,
        runner=runner,
    )


OWNER = principal()
AGENT_OWNER = principal(person=False, role="owner", auth="token")
MEMBER = principal(role="member", user="u2")


def cli_principal():
    return Principal(
        user_id="u1",
        user_email="u1@example.test",
        workspace_id="ws_test",
        connection_id="",
        connection="cli",
        client="commonplace",
        auth="local",
        role="owner",
        policy=Policy.build(profile="full", read_wings=["*"], trust="trusted"),
    )


def config_text(
    *,
    board=BOARD,
    runner=True,
    prompt="Build the card.",
    topic="build",
    planner=None,
    max_agents=3,
    harness_extra="",
):
    lines = [
        'schema = "commonplace.orchestrator/1"',
        f'project = "{board}"',
        "[limits]",
        f"max_agents = {max_agents}",
        "[roles.main]",
        'kind = "orchestrator"',
        'harness = "t3"',
        'topic = "orchestrate"',
        'model_role = "main"',
        "prompt = '''Coordinate.'''",
        "[roles.builder]",
        'kind = "builder"',
        'parent = "main"',
        'harness = "t3"',
        f'topic = "{topic}"',
        'model_role = "worker"',
        'workflow = "build-unit"',
        f"prompt = '''{prompt}'''",
    ]
    if planner is not None:
        names = ", ".join(f'"{name}"' for name in planner)
        lines += [
            "[roles.planner]",
            'kind = "planner"',
            'parent = "main"',
            f"connections = [{names}]",
            "prompt = '''{{include:design_before_code}}'''",
        ]
    lines += ["[workflows.build-unit]", 'steps = ["read plan", "implement", "report"]']
    if runner:
        lines += [
            "[runner]",
            'role = "builder"',
            f'machine = "{MACHINE}"',
            'project = "acme"',
            'runtime_mode = "approval-required"',
            "[[triggers]]",
            'on = "card.todo"',
        ]
    return "\n".join(lines) + "\n" + harness_extra


def sha(text):
    return schema.sha256_text(text)


class Clock:
    def __init__(self):
        self.tick = 0

    def __call__(self):
        self.tick += 1
        return f"2026-11-06T10:{self.tick // 60:02d}:{self.tick % 60:02d}Z"


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "ws", now=Clock())


def accept_text(store, text, *, board=BOARD, channel="live", who=OWNER):
    return store.write(who, board, text, connections=CONNECTIONS, sha256=sha(text), channel=channel)


def propose(store, text, *, board=BOARD, channel="live", who=MEMBER):
    return store.propose(who, board, text, connections=CONNECTIONS, channel=channel)


def raw(store, sql, args=()):
    with store.connection() as conn:
        return [tuple(row) for row in conn.execute(sql, args).fetchall()]


# ------------------------------------------------------------------ versions


def test_propose_accept_supersede_and_numbering(store):
    first = propose(store, config_text())
    assert (first.version, first.status, first.parent_version, first.channel) == (
        1,
        "proposed",
        None,
        "live",
    )
    assert first.proposed_by == "user:u2" and first.proposer_trust == "trusted"
    assert first.sha256 == sha(config_text())
    result = store.accept(OWNER, BOARD, 1, sha256=first.sha256, connections=CONNECTIONS)
    assert result.version.status == "accepted" and result.version.decided_by == "user:u1"
    assert result.previous is None and {c.op for c in result.changes} == {"add"}
    second = propose(store, config_text(prompt="Build it well."))
    assert (second.version, second.parent_version) == (2, 1)
    result = store.accept(OWNER, BOARD, 2, sha256=second.sha256, connections=CONNECTIONS)
    assert result.previous.version == 1
    assert [c.path for c in result.changes] == ["roles.builder.prompt"]
    assert store.get(BOARD, 1).status == "superseded"
    assert store.accepted(BOARD).version == 2
    assert [v.version for v in store.versions(BOARD)] == [2, 1]
    assert [v.version for v in store.versions(BOARD, status="superseded")] == [1]
    assert store.diff(BOARD, 1, 2)[0].path == "roles.builder.prompt"
    assert store.resolve(BOARD).version == 2
    public = store.get(BOARD, 2).public()
    assert public["ref"] == "v2" and "body" not in public
    assert store.get(BOARD, 2).public(body=True)["body"] == config_text(prompt="Build it well.")


def test_accept_is_owner_only(store):
    """Agent tokens with role owner and members are refused; person owner and CLI accept."""
    text = config_text()
    version = propose(store, text)
    for who in (AGENT_OWNER, MEMBER, principal(runner="mach_x")):
        with pytest.raises(OrchError) as err:
            store.accept(who, BOARD, version.version, sha256=sha(text), connections=CONNECTIONS)
        assert (err.value.code, err.value.status) == ("owner_only", 403)
        calls = (
            lambda w: store.reject(w, BOARD, 1),
            lambda w: store.retire(w, BOARD, "exp:a"),
            lambda w: store.restore_ack(w, "x"),
            lambda w: accept_text(store, text, who=w),
        )
        for call in calls:
            with pytest.raises(OrchError, match="owner_only"):
                call(who)
    assert store.get(BOARD, 1).status == "proposed"
    store.accept(cli_principal(), BOARD, 1, sha256=sha(text), connections=CONNECTIONS)
    assert store.accepted(BOARD).decided_by == "conn:cli"
    assert orch_store.orch_owner(OWNER) and not orch_store.orch_owner(AGENT_OWNER)


def test_accept_needs_the_approved_sha_and_a_proposed_version(store):
    text = config_text()
    propose(store, text)
    other = sha(config_text(prompt="Something else."))
    with pytest.raises(OrchError) as err:
        store.accept(OWNER, BOARD, 1, sha256=other, connections=CONNECTIONS)
    assert err.value.code == "sha256_mismatch" and err.value.status == 409
    with pytest.raises(OrchError, match="sha256_invalid"):
        store.accept(OWNER, BOARD, 1, sha256="ABC", connections=CONNECTIONS)
    with pytest.raises(OrchError, match="not_found"):
        store.accept(OWNER, BOARD, 9, sha256=sha(text), connections=CONNECTIONS)
    store.accept(OWNER, BOARD, 1, sha256=sha(text), connections=CONNECTIONS)
    with pytest.raises(OrchError, match="not_proposed"):
        store.accept(OWNER, BOARD, 1, sha256=sha(text), connections=CONNECTIONS)
    with pytest.raises(OrchError, match="not_proposed"):
        store.reject(OWNER, BOARD, 1)
    propose(store, text)
    assert store.reject(OWNER, BOARD, 2).status == "rejected"
    with pytest.raises(OrchError, match="sha256_invalid"):
        store.write(OWNER, BOARD, text, connections=CONNECTIONS, sha256=None)


def test_submit_refusals_do_not_echo_config_values(store):
    secret = "sk-ant-" + "a" * 40
    cases = {
        "project_mismatch": config_text(board="other-board"),
        "connection_unknown": config_text(planner=["nobody"]),
        "toml_invalid": "schema = [",
    }
    for code, text in cases.items():
        with pytest.raises(OrchError) as err:
            propose(store, text)
        assert err.value.code == code and err.value.status == 400
    with pytest.raises(OrchError) as err:
        propose(store, config_text(prompt=f"use {secret}"))
    assert err.value.code == "inline_secret" and secret not in str(err.value)
    with pytest.raises(OrchError) as err:
        store.propose(MEMBER, BOARD, config_text(), connections=(), sha256=sha("x"))
    assert err.value.code == "sha256_mismatch"
    for bad in ("Bad Board", "", None):
        with pytest.raises(OrchError, match="invalid_board"):
            propose(store, config_text(), board=bad)
    with pytest.raises(OrchError, match="invalid_channel"):
        propose(store, config_text(), channel="exp:Bad")
    with pytest.raises(OrchError, match="invalid_source_ref"):
        store.propose(MEMBER, BOARD, config_text(), connections=(), source_ref="a b")
    assert store.versions(BOARD) == []


def test_connections_checked_again_at_accept(store):
    """R20f store side: a connection removed after submit is refused at accept."""
    text = config_text(planner=["planner-acme"])
    propose(store, text)
    with pytest.raises(OrchError) as err:
        store.accept(OWNER, BOARD, 1, sha256=sha(text), connections=("builder-acme",))
    assert err.value.code == "connection_unknown"
    assert store.get(BOARD, 1).status == "proposed"


def test_open_proposal_caps(store):
    for index in range(orch_store.MAX_OPEN_PER_PROPOSER):
        propose(store, config_text(prompt=f"Variant {index}."))
    with pytest.raises(OrchError, match="too_many_open"):
        propose(store, config_text(prompt="One more."))
    users = [principal(role="member", user=f"m{i}") for i in range(4)]
    for who in users[:3]:
        for index in range(orch_store.MAX_OPEN_PER_PROPOSER):
            propose(store, config_text(prompt=f"{who.user_id} {index}."), who=who)
    assert len(store.versions(BOARD, status="proposed")) == orch_store.MAX_OPEN_PER_BOARD
    with pytest.raises(OrchError) as err:
        propose(store, config_text(prompt="Board full."), who=users[3])
    assert err.value.code == "too_many_open" and err.value.status == 409


# --------------------------------------------------------------- experiments


def test_experiments_allow_list_resolve_and_retire(store):
    with pytest.raises(OrchError, match="no_live"):
        propose(store, config_text(prompt="Try this."), channel="exp:a")
    accept_text(store, config_text())
    with pytest.raises(OrchError) as err:
        propose(store, config_text(max_agents=5), channel="exp:a")
    assert err.value.code == "experiment_not_allowed" and err.value.path == "limits.max_agents"
    exp = propose(store, config_text(prompt="Try this.", topic="build-x"), channel="exp:a")
    assert exp.parent_version == 1
    store.accept(OWNER, BOARD, exp.version, sha256=exp.sha256, connections=CONNECTIONS)
    assert store.resolve(BOARD, "exp:a").version == exp.version
    assert store.resolve(BOARD, "live").version == 1
    with pytest.raises(OrchError, match="unknown_experiment"):
        store.resolve(BOARD, "exp:b")
    # Live moves on in a way the experiment no longer matches: the run is refused.
    accept_text(store, config_text(max_agents=4))
    with pytest.raises(OrchError) as err:
        store.resolve(BOARD, "exp:a")
    assert err.value.code == "experiment_not_allowed" and err.value.status == 409
    retired = store.retire(OWNER, BOARD, "exp:a")
    assert [(v.version, v.status) for v in retired] == [(exp.version, "superseded")]
    with pytest.raises(OrchError, match="unknown_experiment"):
        store.retire(OWNER, BOARD, "exp:a")
    with pytest.raises(OrchError, match="invalid_channel"):
        store.retire(OWNER, BOARD, "live")


def test_experiment_count_is_capped(store):
    accept_text(store, config_text())
    for index in range(orch_store.MAX_EXPERIMENTS):
        accept_text(store, config_text(prompt=f"Exp {index}."), channel=f"exp:e{index}")
    with pytest.raises(OrchError, match="too_many_experiments"):
        accept_text(store, config_text(prompt="Ninth."), channel="exp:e9")
    accept_text(store, config_text(prompt="Replace."), channel="exp:e0")  # same channel is ok


# ------------------------------------------------------------- cursor rows


def test_cursor_rows_follow_configuration(store):
    """A5 step 1, A7, A11: arm on configuring accepts, keep on reconfig, drop on unconfigure."""
    with store.connection() as conn:
        assert orch_store.cursor_in(conn, BOARD) is None
    result = accept_text(store, config_text(runner=False))
    assert result.cursor is None and store.configured_boards() == []
    result = accept_text(store, config_text())
    assert (result.cursor, result.gen, result.arm_needed) == ("arming", 1, True)
    with store.transaction() as conn:
        assert orch_store.cursor_in(conn, BOARD) == (None, 1)
        conn.execute("UPDATE orch_cursor SET cursor=42 WHERE board=?", (BOARD,))
    assert store.configured_boards() == [BOARD]
    result = accept_text(store, config_text(prompt="Still configured."))
    assert result.cursor is None and not result.arm_needed
    accept_text(store, config_text(prompt="Exp."), channel="exp:a")
    with store.connection() as conn:
        assert orch_store.cursor_in(conn, BOARD) == (42, 1)
    result = accept_text(store, config_text(runner=False, prompt="Off."))
    assert result.cursor == "dropped"
    with store.connection() as conn:
        assert orch_store.cursor_in(conn, BOARD) is None
        assert not orch_store.configured_in(conn, BOARD)
    assert raw(store, "SELECT last_gen FROM orch_cursor_gen WHERE board=?", (BOARD,)) == [(1,)]
    with pytest.raises(OrchError, match="not_configured"):
        store.resolve(BOARD)
    result = accept_text(store, config_text(prompt="Back on."))
    assert (result.cursor, result.gen) == ("arming", 2)
    with store.connection() as conn:
        assert orch_store.cursor_in(conn, BOARD) == (None, 2)


def test_unreadable_live_config_counts_as_configured(store):
    accept_text(store, config_text())
    with store.transaction() as conn:
        conn.execute("UPDATE orch_configs SET body='schema = [' WHERE board=?", (BOARD,))
        assert orch_store.configured_in(conn, BOARD)
    with pytest.raises(OrchError, match="config_invalid"):
        store.resolve(BOARD)
    with store.transaction() as conn:
        conn.execute(
            "UPDATE orch_configs SET body=? WHERE board=?", (config_text(prompt="X."), BOARD)
        )
    with pytest.raises(OrchError, match="config_invalid"):  # body no longer matches its sha256
        store.resolve(BOARD)


def test_store_opens_a_seeded_and_a_pre_orch_runner_db(tmp_path):
    from commonplace.runner.jobs import SCHEMA as JOBS_SCHEMA

    directory = tmp_path / "ws"
    directory.mkdir()
    with closing(sqlite3.connect(directory / "runner.sqlite3")) as conn:
        conn.executescript(JOBS_SCHEMA)
        conn.executescript(
            orch_store.SCHEMA_SQL.read_text().split("CREATE TABLE IF NOT EXISTS orch_state")[0]
        )
        text = config_text()
        conn.execute(
            "INSERT INTO orch_configs VALUES (?,1,NULL,'live','accepted',NULL,?,?,'user:seed',"
            "'trusted','user:seed','2026-11-06T00:00:00Z','2026-11-06T00:00:00Z')",
            (BOARD, sha(text), text),
        )
        conn.execute("INSERT INTO orch_cursor_gen VALUES (?, 1)", (BOARD,))
        conn.execute("INSERT INTO orch_cursor VALUES (?, 7, 1)", (BOARD,))
        conn.commit()
    first = Store(directory)
    assert first.resolve(BOARD).version == 1
    state = first.restore_state()
    assert not state.flagged and len(state.incarnation) == 32
    again = Store(directory)
    assert again.restore_state().incarnation == state.incarnation
    with again.connection() as conn:
        assert orch_store.cursor_in(conn, BOARD) == (7, 1)
        assert conn.execute("SELECT COUNT(*) FROM runner_jobs").fetchone()[0] == 0


# --------------------------------------------------------------- restore R5


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(
        importlib.import_module("commonplace.backup"), "_verify_offline", lambda r: None
    )
    data = tmp_path / "data"
    with closing(ControlDB(str(data / "control.sqlite3"))) as control:
        user = control.create_user("owner@example.test")
        workspace = control.create_workspace("main", user, personal=True)
    directory = data / "workspaces" / workspace
    return data, workspace, directory


def test_r5_restore_blocks_until_exact_ack(data_dir, tmp_path):
    """R5: backup -> work -> restore; reopen twice: blocked, wrong text refused, exact text
    clears, old tokens (bound to the incarnation) dead."""
    data, workspace, directory = data_dir
    original = Store(directory, now=Clock())
    accept_text(original, config_text())
    accept_text(original, config_text(board="quiet-board", runner=False), board="quiet-board")
    with original.transaction() as conn:
        conn.execute("UPDATE orch_cursor SET cursor=5 WHERE board=?", (BOARD,))
    old_incarnation = original.restore_state().incarnation
    snapshot = tmp_path / "snapshot"
    manifest = backup(data, snapshot)
    restored = tmp_path / "restored"
    restore(snapshot, restored)
    marker = restored / "workspaces" / workspace / RESTORE_MARKER
    assert marker.is_file()
    assert RESTORE_MARKER not in {entry["path"].rsplit("/", 1)[-1] for entry in manifest["files"]}
    # The original data directory carries no marker and is not blocked.
    assert not (directory / RESTORE_MARKER).exists() and not original.restore_state().flagged

    first = Store(restored / "workspaces" / workspace)
    state = first.restore_state()
    assert state.flagged and state.incarnation != old_incarnation
    after = orch_store._marker_time(manifest["created_at"])
    assert state.after == after and state.ack_text == f"effects after {after} may be unknown"
    assert not marker.exists()
    with first.connection() as conn, pytest.raises(OrchError) as err:
        orch_store.check_restore_in(conn)
    assert (err.value.code, err.value.status) == ("restore_pending", 409)

    second = Store(restored / "workspaces" / workspace)  # reopen: still blocked, same incarnation
    assert second.restore_state() == state
    with pytest.raises(OrchError, match="owner_only"):
        second.restore_ack(AGENT_OWNER, state.ack_text)
    for wrong in (
        "effects after T may be unknown",
        state.ack_text + " ",
        state.ack_text.upper(),
        None,
    ):
        with pytest.raises(OrchError) as err:
            second.restore_ack(OWNER, wrong)
        assert err.value.code == "restore_text"
    assert second.restore_state().flagged
    with second.connection() as conn:
        assert orch_store.cursor_in(conn, BOARD) == (5, 1)

    assert second.restore_ack(OWNER, state.ack_text) == [(BOARD, 2)]
    cleared = Store(restored / "workspaces" / workspace).restore_state()
    assert not cleared.flagged and cleared.acked_by == "user:u1" and cleared.ack_text is None
    assert cleared.incarnation == state.incarnation != old_incarnation
    with second.connection() as conn:
        orch_store.check_restore_in(conn)
        assert orch_store.cursor_in(conn, BOARD) == (None, 2)
        assert orch_store.cursor_in(conn, "quiet-board") is None
    with pytest.raises(OrchError, match="restore_not_pending"):
        second.restore_ack(OWNER, state.ack_text)


def test_restore_marker_on_a_pre_orch_workspace_creates_flagged_tables(data_dir, tmp_path):
    data, workspace, directory = data_dir
    directory.mkdir(parents=True)
    with closing(sqlite3.connect(directory / "runner.sqlite3")) as conn:
        conn.execute("CREATE TABLE runner_jobs (id TEXT PRIMARY KEY)")
        conn.commit()
    backup(data, tmp_path / "snap")
    restore(tmp_path / "snap", tmp_path / "back")
    state = Store(tmp_path / "back" / "workspaces" / workspace).restore_state()
    assert state.flagged and state.ack_text


def test_marker_conversion_is_idempotent_and_fail_closed(tmp_path):
    directory = tmp_path / "ws"
    directory.mkdir()
    marker = directory / RESTORE_MARKER
    content = json.dumps(
        {"version": 1, "backup_created_at": "2026-11-05T21:30:15.123456+00:00", "id": "ab" * 16}
    )
    marker.write_text(content)
    first = Store(directory).restore_state()
    assert first.flagged and first.after == "2026-11-05T21:30:15Z"
    # Crash after the commit, before the unlink: the same marker comes back.
    marker.write_text(content)
    assert Store(directory).restore_state() == first and not marker.exists()
    owner_store = Store(directory)
    owner_store.restore_ack(OWNER, first.ack_text)
    marker.write_text(content)  # an already acknowledged marker does not re-flag
    assert not Store(directory).restore_state().flagged
    # A new, unreadable marker still flags, with its mtime as T.
    marker.write_text("not json")
    os.utime(marker, (1_700_000_000, 1_700_000_000))
    state = Store(directory).restore_state()
    assert state.flagged and state.after == "2023-11-14T22:13:20Z"
    assert state.incarnation != first.incarnation


def test_restore_writes_no_marker_without_workspaces(data_dir, tmp_path):
    data, _workspace, _directory = data_dir
    backup(data, tmp_path / "snap")
    restore(tmp_path / "snap", tmp_path / "back")
    assert not list((tmp_path / "back").rglob(RESTORE_MARKER))


def test_marker_kept_when_the_flag_commit_fails(tmp_path, monkeypatch):
    """The marker is deleted only after the flag commit, so a failed open retries."""
    directory = tmp_path / "ws"
    Store(directory)
    (directory / RESTORE_MARKER).write_text(json.dumps({"version": 1, "id": "cd" * 16}))

    def broken(self):
        raise sqlite3.OperationalError("database is locked")

    with monkeypatch.context() as patch:
        patch.setattr(Store, "transaction", broken)
        with pytest.raises(sqlite3.OperationalError):
            Store(directory)
    assert (directory / RESTORE_MARKER).exists()
    assert Store(directory).restore_state().flagged
