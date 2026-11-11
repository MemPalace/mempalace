"""Revoking a runner machine from Admin stays inside the owner's own workspace."""

from tests.runner_harness import (
    owners,  # noqa: F401
    signup_hub,  # noqa: F401
)
from tests.test_runner_owner import GRANT, edit, enroll, http, listing


def test_other_workspace_owner_cannot_revoke_or_see_the_machine(signup_hub, owners):  # noqa: F811
    hub, owner, stranger = signup_hub, owners["A"], owners["B"]
    machine = enroll(hub, owner)
    assert listing(hub, stranger) == []
    status, body = edit(hub, stranger, machine, "DELETE")
    assert status == 404, (status, body)
    assert hub.db.authenticate_token(machine["machine_token"]) is not None
    assert hub.db.authenticate_token(machine["worker_token"]) is not None
    assert [row["revoked_at"] for row in listing(hub, owner)] == [None]


def test_revoke_returns_no_secret_and_list_stays_secret_free(signup_hub, owners):  # noqa: F811
    hub, owner = signup_hub, owners["A"]
    machine = enroll(hub, owner)
    status, body = edit(hub, owner, machine, "DELETE")
    assert status == 200, (status, body)
    for text in (repr(body), repr(listing(hub, owner))):
        assert machine["machine_token"] not in text and machine["worker_token"] not in text
    assert hub.db.authenticate_token(machine["machine_token"]) is None
    status, body = http(hub.port, "POST", "/api/runners", cookie=owner["cookie"], body=GRANT)
    assert status == 201, (status, body)
