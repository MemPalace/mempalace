"""Owner-managed runner requesters: Admin listing, candidates and the owner-only per-id edits."""

# ruff: noqa: F811

import json
from types import SimpleNamespace

import pytest

from commonplace.policy import Policy
from commonplace.runner import registry
from commonplace.web import admin
from tests._commonplace_helpers import hub_fixture as hub_fixture
from tests.runner_harness import (
    cli_token,
    http,
    member,
    owners,  # noqa: F401
    signup_hub,  # noqa: F401
)
from tests.test_admin_api import agent, clock, cookie, get
from tests.test_admin_runtime_api import OLD, machine
from tests.test_runner_owner import edit, enroll, machine_row

MISSING = "conn_" + "0" * 16


def requesters(hub, owner, mach):
    return machine_row(hub, owner, mach)["requesters"]


def version(hub, owner, mach):
    return machine_row(hub, owner, mach)["grant_version"]


def call(hub, owner, mach, method, suffix, body, read, auth):
    """One per-requester request; ``read`` is the grant version the caller read (None: no If-Match)."""
    if not auth:
        auth = {"cookie": owner["cookie"]}
    headers = None if read is None else {"If-Match": str(read)}
    return http(
        hub.port,
        method,
        "/api/runners/" + mach["machine_id"] + suffix,
        body=body,
        headers=headers,
        **auth,
    )


def add(hub, owner, mach, ref, read="now", **auth):
    read = version(hub, owner, mach) if read == "now" else read
    return call(hub, owner, mach, "POST", "/requesters", {"requester": ref}, read, auth)


def remove(hub, owner, mach, ref, read="now", **auth):
    read = version(hub, owner, mach) if read == "now" else read
    return call(hub, owner, mach, "DELETE", "/requesters/" + ref, None, read, auth)


def store(hub, mach, refs):
    hub.db._conn.execute(
        "UPDATE runner_machines SET requesters=? WHERE id=?",
        (json.dumps(refs), mach["machine_id"]),
    )


def foreign_agent(hub, name="foreign-agent"):
    other = hub.db.create_user(f"{name}@example.com")
    workspace = hub.db.create_workspace(name, other)
    cid, _ = hub.db.create_token(
        workspace_ref=workspace,
        user_ref=other,
        name=name,
        client="synthetic",
        policy=Policy.build(scopes=["memory:read"], read_wings=["notes"], trust="trusted"),
        actor=other,
        expires_days=None,
    )
    return cid


# ── Admin read side ──


def test_admin_machine_rows_list_requesters(hub):
    clock(hub)
    mid, *_ = machine(hub)
    live, _ = agent(hub, "planner")
    gone, _ = agent(hub, "retired", expires_at=OLD)
    lowered, _ = agent(
        hub,
        "lowered",
        policy=Policy.build(
            scopes=["memory:read"], read_wings=["notes"], trust="untrusted"
        ).to_json(),
    )
    oauth, _ = agent(hub, "oauth-app", auth="oauth")
    outside = foreign_agent(hub)
    hub.db._conn.execute(
        "UPDATE runner_machines SET requesters=? WHERE id=?",
        (json.dumps([live, gone, lowered, oauth, outside, MISSING, "not an id"]), mid),
    )
    status, data, _ = get(hub, "runtime", cookie(hub))
    assert status == 200
    row = data["machines"]["items"][0]
    assert row["requesters"] == [
        {"id": live, "name": "planner", "state": "active"},
        {"id": gone, "name": "retired", "state": "inactive"},
        {"id": lowered, "name": "lowered", "state": "inactive"},
        {"id": oauth, "name": "oauth-app", "state": "inactive"},
        {"id": outside, "name": None, "state": "inactive"},
        {"id": MISSING, "name": None, "state": "inactive"},
    ]
    assert row["requesters_total"] == 7
    assert isinstance(row["grant_version"], int) and row["grant_version"] >= 1
    assert "foreign-agent" not in json.dumps(data) and "token" not in json.dumps(row)


def test_admin_lists_only_eligible_requester_candidates(hub, monkeypatch):
    clock(hub)
    mid, mc, wc, *_ = machine(hub)
    good, _ = agent(hub, "planner")
    agent(hub, "expired", expires_at=OLD)
    agent(hub, "revoked", revoked_at=OLD)
    agent(hub, "oauth-app", auth="oauth")
    untrusted = Policy.build(scopes=["memory:read"], read_wings=["notes"], trust="untrusted")
    agent(hub, "untrusted-agent", policy=untrusted.to_json())
    foreign_agent(hub)
    status, data, _ = get(hub, "runtime", cookie(hub))
    assert status == 200
    candidates = data["machines"]["requester_candidates"]
    assert [item["id"] for item in candidates] == [good]
    assert candidates[0]["name"] == "planner"
    assert data["machines"]["requester_candidates_more"] is False
    assert mc not in json.dumps(candidates) and wc not in json.dumps(candidates)
    assert "foreign-agent" not in json.dumps(data)
    # Past the cap the list says it is cut short.
    monkeypatch.setattr(admin, "CANDIDATE_LIMIT", 1)
    agent(hub, "second-planner")
    data = get(hub, "runtime", cookie(hub))[1]["machines"]
    assert len(data["requester_candidates"]) == 1 and data["requester_candidates_more"] is True


def test_requester_view_is_built_only_for_the_page(hub, monkeypatch):
    clock(hub)
    ids = [machine(hub, n)[0] for n in (1, 2, 3)]
    live, _ = agent(hub, "planner")
    for mid in ids:
        hub.db._conn.execute(
            "UPDATE runner_machines SET requesters=? WHERE id=?", (json.dumps([live]), mid)
        )
    monkeypatch.setattr(admin, "PAGE_SIZE", 2)
    built = []
    real = admin.requester_views

    def counting(*args, **kwargs):
        built.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(admin, "requester_views", counting)
    data = get(hub, "runtime", cookie(hub))[1]["machines"]
    assert len(data["items"]) == 2 and data["has_more"] is True
    assert len(built) == 2, "requesters are resolved only for the machines on the page"


# ── Owner writes ──


def test_owner_adds_and_removes_one_requester_at_a_time(signup_hub, owners):
    hub, owner = signup_hub, owners["A"]
    mach = enroll(hub, owner)
    first, _ = cli_token(owner, "planner-one")
    second, _ = cli_token(owner, "planner-two")
    status, body = add(hub, owner, mach, first)
    assert status == 200 and body["requesters"] == [first], (status, body)
    assert add(hub, owner, mach, second)[0] == 200
    assert add(hub, owner, mach, second)[0] == 200, "adding twice is harmless"
    assert requesters(hub, owner, mach) == [first, second]
    assert remove(hub, owner, mach, first)[0] == 200
    assert remove(hub, owner, mach, first)[0] == 200, "removing twice is harmless"
    assert requesters(hub, owner, mach) == [second]


def test_add_and_remove_keep_entries_the_page_cannot_show(signup_hub, owners):
    hub, owner = signup_hub, owners["A"]
    mach = enroll(hub, owner)
    hidden = [f"conn_{n:016x}" for n in range(1, 34)]
    store(hub, mach, hidden)
    fresh, _ = cli_token(owner, "planner-new")
    status, body = add(hub, owner, mach, fresh)
    assert status == 200, (status, body)
    assert requesters(hub, owner, mach) == [*hidden, fresh]
    assert remove(hub, owner, mach, hidden[0])[0] == 200
    assert requesters(hub, owner, mach) == [*hidden[1:], fresh], "the 33rd entry stays"
    assert hidden[32] in requesters(hub, owner, mach)


def test_adding_another_requester_does_not_undo_a_removal(signup_hub, owners):
    hub, owner = signup_hub, owners["A"]
    mach = enroll(hub, owner)
    x, _ = cli_token(owner, "planner-x")
    y, _ = cli_token(owner, "planner-y")
    z, _ = cli_token(owner, "planner-z")
    assert add(hub, owner, mach, x)[0] == 200 and add(hub, owner, mach, y)[0] == 200
    stale = version(hub, owner, mach)
    # One session removes X; another session, still showing [X, Y], adds Z with the current version.
    assert remove(hub, owner, mach, x)[0] == 200
    assert add(hub, owner, mach, z)[0] == 200
    assert requesters(hub, owner, mach) == [y, z]
    # Removing an entry that is already gone changes nothing and is reported as unchanged.
    status, body = remove(hub, owner, mach, x)
    assert status == 200 and body["changed"] is False
    assert requesters(hub, owner, mach) == [y, z]
    assert stale < version(hub, owner, mach)


def test_a_stale_add_cannot_give_back_a_requester_another_owner_removed(signup_hub, owners):
    hub, owner = signup_hub, owners["A"]
    mach = enroll(hub, owner)
    x, _ = cli_token(owner, "planner-x")
    read = version(hub, owner, mach)  # the page lists X under Add
    assert add(hub, owner, mach, x)[0] == 200  # another owner adds X ...
    assert remove(hub, owner, mach, x)[0] == 200  # ... and then removes it
    status, body = add(hub, owner, mach, x, read=read)
    assert status == 409, (status, body)
    assert requesters(hub, owner, mach) == []
    # After reloading, the same add is a fresh decision.
    assert add(hub, owner, mach, x)[0] == 200
    assert requesters(hub, owner, mach) == [x]


def test_a_stale_remove_cannot_take_out_a_requester_that_was_added_again(signup_hub, owners):
    hub, owner = signup_hub, owners["A"]
    mach = enroll(hub, owner)
    x, _ = cli_token(owner, "planner-x")
    assert add(hub, owner, mach, x)[0] == 200
    read = version(hub, owner, mach)  # the page lists X under Remove
    assert remove(hub, owner, mach, x)[0] == 200
    assert add(hub, owner, mach, x)[0] == 200  # another owner decided again
    status, body = remove(hub, owner, mach, x, read=read)
    assert status == 409, (status, body)
    assert requesters(hub, owner, mach) == [x]


def test_a_per_requester_edit_needs_the_grant_version(signup_hub, owners):
    hub, owner = signup_hub, owners["A"]
    mach = enroll(hub, owner)
    x, _ = cli_token(owner, "planner-x")
    assert add(hub, owner, mach, x, read=None)[0] == 428
    assert add(hub, owner, mach, x)[0] == 200
    assert remove(hub, owner, mach, x, read=None)[0] == 428
    assert requesters(hub, owner, mach) == [x]
    status, body = call(hub, owner, mach, "POST", "/requesters", {"requester": x}, "abc", {})
    assert status == 400, (status, body)


def test_adding_a_held_requester_reports_no_change(signup_hub, owners):
    hub, owner = signup_hub, owners["A"]
    mach = enroll(hub, owner)
    x, _ = cli_token(owner, "planner-x")
    assert add(hub, owner, mach, x)[1]["changed"] is True
    before = version(hub, owner, mach)
    status, body = add(hub, owner, mach, x)
    assert status == 200 and body["changed"] is False
    assert version(hub, owner, mach) == before, "a no-op does not bump the version"


def test_the_full_list_patch_takes_an_optional_version(signup_hub, owners):
    hub, owner = signup_hub, owners["A"]
    mach = enroll(hub, owner)
    x, _ = cli_token(owner, "planner-x")
    y, _ = cli_token(owner, "planner-y")
    read = version(hub, owner, mach)

    def patch_with(requested, read):
        headers = None if read is None else {"If-Match": str(read)}
        return http(
            hub.port,
            "PATCH",
            "/api/runners/" + mach["machine_id"],
            cookie=owner["cookie"],
            body={"requesters": requested},
            headers=headers,
        )

    assert patch_with([x], read)[0] == 200
    status, body = patch_with([y], read)  # built before the first edit
    assert status == 409, (status, body)
    assert requesters(hub, owner, mach) == [x]
    assert patch_with([y], version(hub, owner, mach))[0] == 200
    assert requesters(hub, owner, mach) == [y]
    # Without If-Match it still replaces the whole list.
    assert patch_with([x], None)[0] == 200
    assert requesters(hub, owner, mach) == [x]


def test_requester_edits_are_audited_with_the_agent(signup_hub, owners):
    hub, owner = signup_hub, owners["A"]
    mach = enroll(hub, owner)
    x, _ = cli_token(owner, "planner-x")
    assert add(hub, owner, mach, x)[0] == 200
    assert remove(hub, owner, mach, x)[0] == 200
    entries = {
        row["action"]: json.loads(row["target_ids"])
        for row in hub.db.audit_entries(owner["workspace"])
        if row["action"].startswith("runner.requester_")
    }
    assert entries == {
        "runner.requester_add": [mach["machine_id"], x],
        "runner.requester_remove": [mach["machine_id"], x],
    }


def test_adding_vets_only_the_added_entry(signup_hub, owners):
    hub, owner = signup_hub, owners["A"]
    mach = enroll(hub, owner)
    lapsed, _ = cli_token(owner, "planner-lapsed")
    hub.db._conn.execute(
        "UPDATE connections SET expires_at='2000-01-01T00:00:00Z' WHERE id=?", (lapsed,)
    )
    other, _ = cli_token(owner, "planner-other")
    untrusted, _ = cli_token(owner, "planner-untrusted", trust="untrusted")
    for bad in (lapsed, untrusted, MISSING, mach_worker(hub, owner, mach)):
        status, body = add(hub, owner, mach, bad)
        assert status == 400, (bad, status, body)
    assert requesters(hub, owner, mach) == []
    # A lapsed entry the machine already holds does not block adding or removing others.
    store(hub, mach, [lapsed])
    assert add(hub, owner, mach, other)[0] == 200
    assert remove(hub, owner, mach, other)[0] == 200
    assert requesters(hub, owner, mach) == [lapsed]


def mach_worker(hub, owner, mach):
    return machine_row(hub, owner, mach)["worker_connection_id"]


def test_removing_a_requester_keeps_a_lapsed_peer(signup_hub, owners):
    hub, owner = signup_hub, owners["A"]
    mach = enroll(hub, owner)
    live, _ = cli_token(owner, "planner-live")
    lapsed, _ = cli_token(owner, "planner-lapsed")
    status, body = edit(hub, owner, mach, body={"requesters": [live, lapsed]})
    assert status == 200, (status, body)
    hub.db._conn.execute(
        "UPDATE connections SET expires_at='2000-01-01T00:00:00Z' WHERE id=?", (lapsed,)
    )
    status, body = edit(hub, owner, mach, body={"requesters": [lapsed]})
    assert status == 200, (status, body)
    assert requesters(hub, owner, mach) == [lapsed]
    other = enroll(hub, owner, label="second-machine")
    status, body = edit(hub, owner, other, body={"requesters": [lapsed]})
    assert status == 400, (status, body)


def test_only_the_owner_writes_requesters(signup_hub, owners):
    hub, owner = signup_hub, owners["A"]
    mach = enroll(hub, owner)
    keep, _ = cli_token(owner, "planner-keep")
    other, _ = cli_token(owner, "planner-other")
    assert add(hub, owner, mach, keep)[0] == 200
    _, trusted_token = cli_token(owner, "bystander")
    callers = [
        {"cookie": member(hub, owner, owners["B"])["cookie"]},
        {"token": trusted_token},
        {"token": mach["worker_token"]},
        {"token": mach["machine_token"]},
    ]
    for auth in callers:
        for status, response in (
            edit(hub, owner, mach, body={"requesters": []}, **auth),
            add(hub, owner, mach, other, **auth),
            remove(hub, owner, mach, keep, **auth),
        ):
            assert status in (401, 403, 404), (status, response)
    status, _ = http(
        hub.port, "PATCH", "/api/runners/" + mach["machine_id"], body={"requesters": []}
    )
    assert status in (401, 403, 404)
    assert requesters(hub, owner, mach) == [keep]


def test_an_owner_of_another_workspace_cannot_edit_requesters(signup_hub, owners):
    hub, owner, outsider = signup_hub, owners["A"], owners["B"]
    assert outsider["workspace"] != owner["workspace"]
    mach = enroll(hub, owner)
    keep, _ = cli_token(owner, "planner-keep")
    theirs, _ = cli_token(outsider, "outsider-agent")
    assert add(hub, owner, mach, keep)[0] == 200
    auth = {"cookie": outsider["cookie"]}
    read = version(hub, owner, mach)
    attempts = [
        edit(hub, owner, mach, body={"requesters": [theirs]}, **auth),
        edit(hub, owner, mach, body={"requesters": []}, **auth),
        add(hub, owner, mach, theirs, read=read, **auth),
        add(hub, owner, mach, keep, read=read, **auth),
        remove(hub, owner, mach, keep, read=read, **auth),
    ]
    for status, response in attempts:
        assert status in (403, 404), (status, response)
    assert requesters(hub, owner, mach) == [keep]
    # The machine's own owner cannot name another workspace's agent either.
    for status, response in (
        add(hub, owner, mach, theirs),
        add(hub, outsider, mach, theirs, read=read),
    ):
        assert status in (400, 404), (status, response)
    assert requesters(hub, owner, mach) == [keep]


def test_registry_refuses_a_non_owner_on_its_own(signup_hub, owners):
    hub, owner = signup_hub, owners["A"]
    mach = enroll(hub, owner)
    keep, _ = cli_token(owner, "planner-keep")
    assert add(hub, owner, mach, keep)[0] == 200
    caller = SimpleNamespace(
        person=True, role="member", workspace_id=owner["workspace"], user_id=owner["user_id"]
    )
    mid = mach["machine_id"]
    for call in (
        lambda: registry.patch(hub.db, caller, mid, {"requesters": []}),
        lambda: registry.add_requester(hub.db, caller, mid, keep, version=1),
        lambda: registry.remove_requester(hub.db, caller, mid, keep, version=1),
    ):
        with pytest.raises(PermissionError):
            call()
    agent_caller = SimpleNamespace(
        person=False, role="owner", workspace_id=owner["workspace"], user_id=owner["user_id"]
    )
    with pytest.raises(PermissionError):
        registry.remove_requester(hub.db, agent_caller, mid, keep, version=1)
    assert requesters(hub, owner, mach) == [keep]


def test_a_registry_permission_error_is_a_403(signup_hub, owners, monkeypatch):
    hub, owner = signup_hub, owners["A"]
    mach = enroll(hub, owner)

    def refuse(*args, **kwargs):
        raise PermissionError("a signed-in workspace owner is required")

    for name in ("patch", "add_requester", "remove_requester"):
        monkeypatch.setattr(registry, name, refuse)
    ref, _ = cli_token(owner, "planner-ref")
    for status, response in (
        edit(hub, owner, mach, body={"requesters": []}),
        add(hub, owner, mach, ref),
        remove(hub, owner, mach, ref),
    ):
        assert status == 403, (status, response)
