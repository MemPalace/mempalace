"""Head-only unit contract for the new shared eligibility helper."""

import sqlite3
from types import SimpleNamespace

import pytest

from commonplace.boards import task_types
from commonplace.extensions import ToolError
from tests.test_commonplace_boards import env as env, hub_fixture as hub_fixture


@pytest.fixture
def policy_db():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE connection_task_types(connection_id TEXT, task_type TEXT)")
    conn.execute("INSERT INTO connection_task_types VALUES ('worker', 'code')")
    yield conn
    conn.close()


@pytest.mark.parametrize(
    "label,person,cid,expected",
    [
        ("", False, "absent", True),
        ("code", False, "worker", True),
        ("review", False, "worker", False),
        ("code", False, "absent", False),
        ("code", True, None, True),
        ("Invalid", True, None, False),
        (None, False, "worker", False),
        ("code", False, "parent", False),
    ],
)
def test_shared_eligibility(policy_db, label, person, cid, expected):
    holder = SimpleNamespace(person=person, connection_id=cid)
    assert task_types.eligible(policy_db, label, holder) is expected


def test_no_holder_corrupt_mapping_and_read_failure(policy_db, tmp_path):
    assert not task_types.eligible(policy_db, "code", None)
    worker = SimpleNamespace(person=False, connection_id="worker")
    policy_db.execute("INSERT INTO connection_task_types VALUES ('worker', 'Invalid')")
    assert not task_types.eligible(policy_db, "code", worker)
    policy_db.execute("DROP TABLE connection_task_types")
    assert not task_types.eligible(policy_db, "code", worker)
    assert not task_types.read_eligible(tmp_path, "code", worker)
    assert task_types.read_eligible(tmp_path, "", worker)


def test_exact_name_resolution_and_member_caller(hub, env, monkeypatch):
    from dataclasses import replace

    _, agent, person, _, owner = env
    assert owner._type_holder(agent.actor).connection_id == agent.connection_id
    assert owner._type_holder("conn:" + agent.connection_id) is None
    rows = hub.db.connections(hub.workspace)
    with monkeypatch.context() as patch:
        patch.setattr(hub.db, "connections", lambda workspace: rows + rows)
        assert owner._type_holder(agent.actor) is None
    with monkeypatch.context() as patch:
        patch.setattr(hub.db, "authenticate_connection", lambda cid: None)
        assert owner._type_holder(agent.actor) is None
    with monkeypatch.context() as patch:
        patch.setattr(
            hub.db, "authenticate_connection", lambda cid: replace(agent, workspace_id="foreign")
        )
        assert owner._type_holder(agent.actor) is None
    # A member caller's person exemption must never transfer to the connection holder.
    owner.principal = replace(person, role="member")
    holder = owner._type_holder(agent.actor)
    with owner.store.connection() as conn:
        assert not task_types.eligible(conn, "code", holder)
    assert owner._type_holder(person.actor).person


def test_person_owner_field_gate(env):
    from dataclasses import replace

    _, agent, person, _, _ = env
    for caller in (agent, replace(person, role="member")):
        for value in (None, "", "code"):
            with pytest.raises(ToolError, match="owner"):
                task_types.owner_value(caller, value)
    assert task_types.owner_value(person, "") == ""
    with pytest.raises(ToolError, match="slug"):
        task_types.owner_value(person, None)
