"""Head-only C4 and U2 checks; excluded from paired acceptance evidence."""

import pytest

from commonplace.control_db import ControlDB
from commonplace.policy import Policy
from commonplace.runner import agent


@pytest.mark.parametrize("kind", ["machine", "worker"])
@pytest.mark.parametrize("invalid", ["disabled", "membership_removed"])
def test_c4_runner_principal_requires_active_member(tmp_path, kind, invalid):
    db = ControlDB(str(tmp_path / "control.sqlite3"))
    row = {
        "id": "example-connection",
        "user_id": "example-owner",
        "user_email": "a@example.test",
        "workspace_id": "demo",
        "name": "example-machine",
        "client": "commonplace-runner",
        "auth": "pat",
        "role": "owner",
        "runner_kind": kind,
        "policy": Policy.build(profile="slim", scopes=[], trust="standard").to_json(),
        "parent_id": None,
        "revoked_at": None,
        "expires_at": None,
        "user_disabled": 0,
    }
    try:
        principal = db._connection_principal(row)
        assert principal is not None
        assert principal.runner == kind and principal.role == "member"
        row["user_disabled" if invalid == "disabled" else "role"] = (
            1 if invalid == "disabled" else None
        )
        assert db._connection_principal(row) is None
    finally:
        db.close()


@pytest.mark.parametrize(
    "override",
    [
        {"job_id": "job_XYZ"},
        {"title_marker": "unexpected-marker"},
        {"task_number": 0},
        {"task_number": "7"},
    ],
    ids=["invalid-id", "wrong-marker", "zero-task", "string-task"],
)
def test_u2_invalid_job_fails_without_spawn(tmp_path, monkeypatch, override):
    job = {
        "job_id": "job_0123456789abcdef",
        "title_marker": "commonplace-job:job_0123456789abcdef",
        "task_number": 7,
        "lease_token": "synthetic-lease",
    }
    agent.validate_job(job)
    job.update(override)
    with pytest.raises(ValueError, match="job_invalid"):
        agent.validate_job(job)

    calls = []

    class FakeHub:
        def post(self, route, body):
            calls.append((route, body))

    def refuse_spawn(*args, **kwargs):
        pytest.fail("invalid job reached spawn")

    monkeypatch.setattr(agent, "_spawn", refuse_spawn)
    agent._start(FakeHub(), {}, job, tmp_path, {}, ())
    assert calls == [
        (
            "result",
            {"lease_token": "synthetic-lease", "outcome": "failed", "code": "job_invalid"},
        )
    ]
    assert list(tmp_path.iterdir()) == []
