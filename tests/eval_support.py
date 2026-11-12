"""Shared fixtures for the ``commonplace eval`` tests (synthetic data only)."""

from __future__ import annotations

import io
import json
import os
import sqlite3
import sys
from contextlib import closing, redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace

from commonplace.cli import main
from commonplace.control_db import ControlDB
from commonplace.handoff.models import ThreadMessage
from commonplace.workspace_store import open_threads, workspace_path

KEY = "Synthetic_eval-key-0001"


def make_env(tmp_path, *, compact=False):
    """A private data dir with one owner, one personal workspace and its store."""
    old = os.umask(0o077)
    try:
        data = tmp_path / "data"
        with closing(ControlDB(str(data / "control.sqlite3"))) as db:
            owner = db.create_user("owner@example.test", is_admin=True)
            ws = db.create_workspace("Synthetic", owner, personal=True)
            member = db.create_user("member@example.test")
            db.add_member(ws, member, "member", actor=owner)
        directory = workspace_path(data, ws)
        directory.mkdir(parents=True, exist_ok=True)
        if compact:
            (directory / "workspace.json").write_text('{"store":"compact"}')
        with closing(open_threads(directory)):
            pass
    finally:
        os.umask(old)
    return SimpleNamespace(data=data, owner=owner, ws=ws, member=member, directory=directory)


def conversation(text="Hello ☃\r\nline two", n=0):
    """A tool_call/tool_result pair with call IDs and distinct timestamps."""
    return [
        ThreadMessage(
            0, "user", "message", text, json.dumps({"m": text}), at="2026-01-01T00:00:00Z"
        ),
        ThreadMessage(
            1,
            "assistant",
            "tool_call",
            '{"cmd":"ls"}',
            '{"type":"tool_use","id":"c1"}',
            name="bash",
            call_id="c1",
            at="2026-01-01T00:00:01Z",
        ),
        ThreadMessage(
            2,
            "tool",
            "tool_result",
            "out\x00put",
            '{"type":"tool_result","id":"c1"}',
            name="bash",
            call_id="c1",
            at="2026-01-01T00:00:02Z",
        ),
        *[
            ThreadMessage(3 + i, "assistant", "message", f"m{i}", "{}", at=f"t{i}")
            for i in range(n)
        ],
    ]


def store_thread(env, source="t3:synthetic", messages=None, wing="threads", **kwargs):
    with closing(open_threads(env.directory)) as store:
        return store.store(
            env.ws, source, messages or conversation(), actor="synthetic", wing=wing, **kwargs
        )


def run(env, *argv, stdin=b"", by=None):
    """Run ``commonplace eval``; returns ``(exit, stdout, stderr)``."""
    out, err = io.StringIO(), io.StringIO()
    saved = sys.stdin
    sys.stdin = SimpleNamespace(buffer=io.BytesIO(stdin))
    extra = ["--by", by] if by else []
    try:
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = main(["--data", str(env.data), "eval", *argv, *extra])
            except SystemExit as exc:
                code = exc.code
    finally:
        sys.stdin = saved
    return code, out.getvalue(), err.getvalue()


def archive(env, stored_id, key=KEY, artifacts=None, by=None):
    argv = ["archive", "--workspace", env.ws, "--thread", stored_id, "--key", key]
    stdin = b""
    if artifacts is not None:
        argv.append("--artifacts-stdin")
        stdin = json.dumps(artifacts).encode()
    return run(env, *argv, stdin=stdin, by=by)


def archived(env, stored_id, **kwargs):
    code, out, err = archive(env, stored_id, **kwargs)
    assert code == 0, err
    assert eval_db(env).exists(), "an archived run must live in the workspace eval DB"
    return json.loads(out)["run_id"]


def listed(env, by=None):
    code, out, err = run(env, "list", "--workspace", env.ws, by=by)
    assert code == 0, err
    return json.loads(out)


def read_all(env, run_id, obj):
    import base64

    data, offset = b"", 0
    while offset is not None:
        code, out, err = run(
            env, "read", run_id, "--workspace", env.ws, "--object", obj, "--offset", str(offset)
        )
        assert code == 0, err
        page = json.loads(out)
        data += base64.b64decode(page["data_base64"])
        offset = page["next"]
    return data, page


def eval_db(env):
    return env.directory / "evals" / "runs.sqlite3"


def db_rows(env, table="runs"):
    path = eval_db(env)
    if not path.exists():
        return []
    with closing(sqlite3.connect(path)) as conn:
        try:
            return conn.execute(f"SELECT * FROM {table}").fetchall()
        except sqlite3.OperationalError:
            return []


def refused(result, code):
    exit_code, out, err = result
    assert exit_code == 2, (exit_code, out, err)
    assert err.startswith(f"commonplace: {code}"), err
    assert "Traceback" not in err
    return err


def control(env):
    return ControlDB(str(Path(env.data) / "control.sqlite3"))
