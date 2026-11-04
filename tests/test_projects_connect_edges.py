"""Connect edge cases: shared-wing spellings, replace guard, bad bodies, failed coordinator set."""

import pytest

from commonplace.policy import Policy
from tests._projects_helpers import projects_hub_fixture  # noqa: F401


@pytest.mark.parametrize("wing", ["Shared", "wing_shared"])
def test_shared_wing_spellings_need_consent(projects_hub, wing):
    h = projects_hub
    created = h.create_board(h.owner, "alt", wing=wing)
    status, body = h.connect(h.owner, "alt")
    assert status == 409 and body.get("error") == "shared_wing"  # RED
    assert created[0] == 201
    assert h.connect(h.owner, "alt", {"allow_shared": True})[0] == 201


def test_manual_coordinator_with_seat_name_needs_replace(projects_hub):
    h = projects_hub
    h.db.create_token(
        workspace_ref=h.workspace,
        user_ref=h.admin,
        actor=h.admin,
        name="orchestrator:work",
        client="test",
        policy=Policy.build(
            profile="standard",
            scopes=["memory:read", "memory:write"],
            read_wings=["work"],
            write_wing="work",
            trust="standard",
        ),
    )
    patched = h.request(
        h.owner, "PATCH", "/api/boards/work", {"coordinator": "conn:orchestrator:work"}
    )
    status, body = h.connect(h.owner, "work")
    assert status == 409 and body.get("error") == "replaces current orchestrator"  # RED
    assert patched[0] == 200
    assert h.coordinator("work") == "conn:orchestrator:work"
    replaced = h.connect(h.owner, "work", {"replace": True})
    assert replaced[0] == 201
    assert h.coordinator("work") == "conn:" + replaced[1]["connection"]["name"]
    assert replaced[1]["connection"]["name"].startswith("orchestrator:work:")


def test_connect_rejects_non_object_body(projects_hub):
    h = projects_hub
    created = h.create_board(h.owner, "alpha")
    status, _ = h.connect(h.owner, "alpha", ["replace"])
    assert status == 400
    assert created[0] == 201


def test_failed_coordinator_set_leaves_no_unheld_connection(projects_hub, monkeypatch):
    from commonplace.boards.service import BoardService

    h = projects_hub
    created = h.create_board(h.owner, "alpha")

    def refuse(self, *args, **kwargs):
        raise RuntimeError("coordinator write failed")

    monkeypatch.setattr(BoardService, "board_set", refuse)
    status, _ = h.connect(h.owner, "alpha")
    active = h.db._conn.execute(
        "SELECT COUNT(*) FROM connections WHERE workspace_id=? AND client=? AND revoked_at IS NULL",
        (h.workspace, "commonplace-orchestrator"),
    ).fetchone()[0]
    assert active == 0  # RED
    assert created[0] == 201
    assert status == 500
    assert h.coordinator("alpha") is None
