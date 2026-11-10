"""Public hallway eligibility and visible-page witnesses in both stored formats."""

import json
from types import SimpleNamespace

import pytest

from commonplace import sessions
from commonplace.gateway import WorkerClient
from commonplace.handoff.models import ThreadMessage
from tests._commonplace_helpers import (  # noqa: F401
    assert_member_drawer_access,
    functional_worker_transport,
    hub_fixture,
    shared_readers,
)

TOOL = "mempalace_list_hallways"


@pytest.fixture
def world(hub, monkeypatch, tmp_path, functional_worker_transport):  # noqa: F811
    from mempalace import hallways

    monkeypatch.setenv("COMMONPLACE_RULES", "off")
    member = hub.db.create_user("reader@example.test")
    hub.db.add_member(hub.workspace, member, "owner", actor=hub.admin)
    # Private-read fixture holders must be owners; filer boundaries remain independent.
    store = hub.gateway.handoff.store_for(hub.workspace)
    readable = store.store(
        hub.workspace,
        "shared",
        [ThreadMessage(0, "user", "message", "shared", "shared")],
        actor=member,
    )
    hidden = store.store(
        hub.workspace,
        f"capture:{hub.admin}:hidden",
        [ThreadMessage(0, "user", "message", "private", "private")],
        actor=hub.admin,
        wing="private_chats",
    )
    token = hub.token(
        "hallway-reader",
        user=member,
        tools=[TOOL],
        scopes=["memory:read", "private:read", "threads:read"],
        read_wings=["*"],
        trust="trusted",
    )
    principal = hub.db.authenticate_token(token)
    assert principal.role == "owner" and principal.policy.trust == "trusted"
    assert principal.policy.sees_all_wings
    assert all(
        principal.policy.has_scope(s) for s in ("memory:read", "private:read", "threads:read")
    )
    assert principal.policy.refusal(TOOL) is None
    assert hub.gateway.handoff.recall.can_read(principal, readable.id)
    assert not hub.gateway.handoff.recall.can_read(principal, hidden.id)
    assert store.get(hidden.id) is not None
    calls = []
    original = WorkerClient.call_tool

    def call(self, p, name, args):
        result = original(self, p, name, args)
        if name == TOOL:
            calls.append((args, result))
        return result

    monkeypatch.setattr(WorkerClient, "call_tool", call)
    w = SimpleNamespace(
        h=hub,
        principal=principal,
        token=token,
        readable=readable,
        hidden=hidden,
        hallways=hallways,
        root=tmp_path,
        calls=calls,
    )
    yield w
    hub.gateway.handoff.close()
    hub.gateway.webhooks.stop()


def record(w, ident, **changes):
    return {
        "id": ident,
        "wing": "wing_fixture",
        "rooms": [w.readable.id],
        "label": "Family 👩‍💻 25% complete %ab",
        "co_occurrence_count": 2,
        **changes,
    }


def install(w, monkeypatch, backend, records, name):
    path = w.root / name / "hallways.json"
    path.parent.mkdir()
    monkeypatch.setattr(w.hallways, "_get_hallway_file", lambda *a, **k: str(path))
    monkeypatch.setattr(w.hallways, "_legacy_hallway_file", lambda: str(path))
    if backend == "sqlite":
        w.hallways._save_hallways(records)
    else:
        path.write_text(json.dumps({"hallways": records}), encoding="utf-8")
    assert w.hallways.list_hallways() == records
    from mempalace import mcp_server

    mcp_server._invalidate_overview_caches()


def public(w, **args):
    value, raw = w.h.call(w.token, TOOL, wing=args.pop("wing", "wing_fixture"), **args)
    assert isinstance(value, dict) and isinstance(value.get("hallways"), list), raw
    return value


@pytest.mark.parametrize(
    "backend,mode",
    [(b, m) for b in ("sqlite", "json") for m in ("rooms", "label_only")],
    ids=[f"{b}-{m}" for b in ("sqlite", "json") for m in ("rooms", "label_only")],
)
def test_card253_s03_hallway(world, monkeypatch, backend, mode):
    w = world
    visible = record(w, "visible")
    hidden = record(
        w,
        "hidden",
        **(
            {"rooms": [w.readable.id, w.hidden.id]}
            if mode == "rooms"
            else {"rooms": [], "label": f"legacy {w.hidden.id}"}
        ),
    )
    install(w, monkeypatch, backend, [visible, hidden], "present")
    reply = public(w, limit=10, offset=0)
    assert w.calls
    install(w, monkeypatch, backend, [visible], "absent")
    control = public(w, limit=10, offset=0)
    assert "hidden" not in [h["id"] for h in reply["hallways"]], "s03 hidden hallway retained"
    assert reply == control


@pytest.mark.parametrize("backend", ["sqlite", "json"])
def test_card253_s03_page(world, monkeypatch, backend):
    w = world
    visible = [record(w, "first"), record(w, "second")]
    hidden = record(w, "hidden", rooms=[w.hidden.id], co_occurrence_count=99)
    install(w, monkeypatch, backend, [*visible, hidden], "present")
    replies = [public(w, limit=1, offset=i) for i in (0, 1)]
    assert w.calls
    install(w, monkeypatch, backend, visible, "absent")
    controls = [public(w, limit=1, offset=i) for i in (0, 1)]
    assert replies == controls, "s03 hidden hallway shifted page"


@pytest.mark.parametrize("backend", ["sqlite", "json"])
def test_card253_s03_readable_guard(world, monkeypatch, backend):
    w = world
    rows = [record(w, "first"), record(w, "second", label=f"shared {w.readable.id}")]
    install(w, monkeypatch, backend, rows, "readable")
    expected = {"hallways": rows, "total": 2, "count": 2, "limit": 10, "offset": 0}
    assert public(w, limit=10, offset=0) == expected
    assert public(w, limit=10, offset=0) == expected
    owner = sessions.person_principal(w.h.db, w.h.admin, w.h.workspace)
    assert owner.person and owner.role == "owner" and owner.policy.trust == "trusted"
    assert owner.policy.has_scope("threads:read")
    assert w.h.gateway.handoff.recall.can_read(owner, w.readable.id)
    assert (
        w.h.gateway.call_memory_tool(owner, TOOL, {"wing": "wing_fixture", "limit": 10}) == expected
    )


@pytest.mark.parametrize("backend", ["sqlite", "json"])
def test_card253_s03_eligibility_protocol(world, monkeypatch, backend):
    from commonplace.handoff import mirror_access

    w = world
    rows = [
        record(w, "visible"),
        record(w, "readable-mirror", wing="private_threads"),
        record(w, "label", label=f"{w.readable.id} {w.hidden.id}"),
        record(w, "bad", rooms="not a list"),
        record(w, "blank", rooms=[""]),
        record(w, "mirror", wing="private_threads", rooms=[]),
        record(w, "private-label", label=f"capture:{w.h.admin}:hidden"),
        record(w, "legacy", rooms=[], label="plain label"),
    ]
    install(w, monkeypatch, backend, rows, "eligibility")
    gates = []
    original = mirror_access.gate

    def gate(*a):
        result = original(*a)
        if isinstance(a[-1], dict) and "hallways" in a[-1]:
            gates.append(a[-1]["hallways"] == result["hallways"])
        return result

    monkeypatch.setattr(mirror_access, "gate", gate)
    reply = public(w, limit=10, wing=None)
    assert reply["total"] == reply["count"] == len(reply["hallways"]), (
        "s03 readable mirror dropped after counting"
    )
    assert [r["id"] for r in reply["hallways"]] == ["visible", "readable-mirror", "legacy"]
    assert gates == [True]  # No filtering deferred until after the page.
    assert public(w, limit=1, offset=8, wing=None) == {
        "hallways": [],
        "total": 3,
        "count": 0,
        "offset": 8,
        "limit": 1,
    }
    assert w.hallways.list_hallways() == rows  # O4: no storage cleanup.
    store = w.h.gateway.handoff.store_for(w.h.workspace)
    with store._lock:
        store._conn.execute(
            "UPDATE threads SET wing='private_chats', source_thread_id=? WHERE id=?",
            (f"capture:{w.h.admin}:revoked", w.readable.id),
        )
    assert [r["id"] for r in public(w, limit=10, wing=None)["hallways"]] == ["legacy"]


@pytest.mark.parametrize("backend", ["sqlite", "json"])
def test_card253_s03_complete_pages(world, monkeypatch, backend):
    w = world
    rows = [record(w, f"h-{i}", rooms=[w.hidden.id]) for i in range(501)]
    rows += [record(w, "late-first"), record(w, "late-second")]
    install(w, monkeypatch, backend, rows, "many")
    assert [r["id"] for r in public(w, limit=1, offset=1)["hallways"]] == ["late-second"]
    probes = [r[0]["thread_scope"] for r in w.calls if r[0]["thread_scope"].get("probe")]
    assert len(probes) == 2 and probes[1]["after"] == 500


@pytest.mark.parametrize("backend", ["sqlite", "json"])
@pytest.mark.parametrize("mode", ["changed", "malformed", "failure", "empty"])
def test_card253_s03_snapshot_and_faults(world, monkeypatch, backend, mode):
    w = world
    install(w, monkeypatch, backend, [record(w, "visible")], "fault")
    original = WorkerClient.call_tool
    hits = []

    def call(self, p, name, args):
        scope = args.get("thread_scope", {})
        if name == TOOL and scope.get("probe"):
            result = original(self, p, name, args)
            hits.append("probe")
            if mode == "malformed":
                result["probe"]["next"] = 0
            if mode == "empty":
                result["probe"]["rows"][0]["record"]["rooms"] = [w.hidden.id]
            return result
        if name == TOOL and "allow" in scope:
            hits.append("final")
            if mode == "failure":
                raise TimeoutError("synthetic incomplete acquisition")
            if mode == "changed":
                install(
                    w, monkeypatch, backend, [record(w, "visible", rooms=[w.hidden.id])], "moved"
                )
        return original(self, p, name, args)

    monkeypatch.setattr(WorkerClient, "call_tool", call)
    assert public(w, limit=10, offset=0) == {
        "hallways": [],
        "total": 0,
        "count": 0,
        "offset": 0,
        "limit": 10,
    }
    assert hits and ("final" in hits) is (mode in ("changed", "failure"))


@pytest.mark.parametrize("backend", ["sqlite", "json"])
def test_card253_s03_principal_isolation(world, monkeypatch, backend):
    w = world
    rows = [record(w, "visible"), record(w, "owner-only", rooms=[w.hidden.id])]
    install(w, monkeypatch, backend, rows, "principals")
    first = public(w, limit=10)
    owner = sessions.person_principal(w.h.db, w.h.admin, w.h.workspace)
    assert owner.policy.has_scope("threads:read") and owner.policy.trust == "trusted"
    assert w.h.gateway.handoff.recall.can_read(owner, w.hidden.id)
    other = w.h.gateway.call_memory_tool(owner, TOOL, {"wing": "wing_fixture", "limit": 10})
    assert other["hallways"] == rows and other["total"] == 2
    assert public(w, limit=10) == first
    assert first["hallways"] == rows[:1] and first["total"] == 1


def test_card253_s03_deadline_and_missing_context(hub, monkeypatch):
    """Explicit deadline failures do not install functional transport allowances."""
    from commonplace.handoff.hallway_reads import project
    from commonplace.rules import checks

    owner = sessions.person_principal(hub.db, hub.admin, hub.workspace)
    empty = {"hallways": [], "total": 0, "count": 0, "offset": 3, "limit": 2}
    missing = SimpleNamespace(workspace_id=None)
    assert (
        project(hub.gateway, missing, hub.gateway.mediator.workers, {"offset": 3, "limit": 2})
        == empty
    )
    hits = []

    def expired(timeout):
        hits.append(timeout)
        raise TimeoutError("synthetic deadline")

    monkeypatch.setattr(checks, "remaining", expired)
    assert (
        project(hub.gateway, owner, hub.gateway.mediator.workers, {"offset": 3, "limit": 2})
        == empty
    )
    assert hits
    hub.gateway.handoff.close()
    hub.gateway.webhooks.stop()


@pytest.mark.parametrize("backend", ["sqlite", "json"])
def test_card253_s03_worker_revalidation(world, monkeypatch, backend):
    from mempalace import mcp_server

    w = world
    install(w, monkeypatch, backend, [record(w, "visible", entity_a="kept entity")], "original")
    assert public(w, limit=10)["count"] == 1
    from mempalace.provenance import verified

    with verified(w.principal.identity()):
        probe = mcp_server.tool_list_hallways(wing="wing_fixture", thread_scope={"probe": True})
        rows = probe["probe"]["rows"]
        assert probe["probe"]["next"] is None
        assert set(rows[0]["record"]) == {"id", "wing", "rooms", "label"}
        install(w, monkeypatch, backend, [record(w, "visible", rooms=[w.hidden.id])], "changed")
        assert mcp_server.tool_list_hallways(wing="wing_fixture", thread_scope={"allow": rows}) == {
            "hallways": []
        }
        assert mcp_server.tool_list_hallways(wing="wing_fixture", thread_scope={"allow": []}) == {
            "hallways": []
        }


@pytest.mark.parametrize("backend", ["sqlite", "json"])
def test_card253_s03_client_scope_is_ignored(world, monkeypatch, backend):
    w = world
    rows = [record(w, "visible"), record(w, "hidden", rooms=[w.hidden.id], label=w.hidden.id)]
    install(w, monkeypatch, backend, rows, "client-scope")
    listing = w.h.rpc(w.token, "tools/list")["result"]["tools"]
    found = next(tool for tool in listing if tool["name"] == TOOL)
    assert "thread_scope" not in found["inputSchema"]["properties"]
    expected = public(w, limit=10)
    w.calls.clear()
    for forged in ({"probe": True}, {"allow": []}, {"allow": [{"position": 2, "record": rows[1]}]}):
        reply = public(w, limit=10, thread_scope=forged)
        assert reply == expected
        assert "probe" not in reply and w.hidden.id not in json.dumps(reply)
    assert expected["hallways"] == rows[:1]
    assert all(call[0]["thread_scope"] != {"probe": True} for call in w.calls)
    # The internal calls carry server-created cursors/descriptors, never the
    # client's forged descriptor. Existing S1 mediation is shared by all tools.
    finals = [
        args["thread_scope"]["allow"] for args, _ in w.calls if "allow" in args["thread_scope"]
    ]
    assert finals and all(batch[0]["record"]["id"] == "visible" for batch in finals)


@pytest.mark.parametrize("backend", ["sqlite", "json"])
def test_card253_s03_unverified_worker_scope_refused(world, monkeypatch, backend):
    from mempalace import mcp_server
    from mempalace.provenance import verified

    w = world
    install(w, monkeypatch, backend, [record(w, "hidden", rooms=[w.hidden.id])], "unverified")
    assert public(w, limit=10)["hallways"] == []
    with verified(None):
        response = mcp_server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": TOOL,
                    "arguments": {"wing": "wing_fixture", "thread_scope": {"probe": True}},
                },
            }
        )
    from commonplace.gateway import _tool_result

    assert _tool_result(response) == {
        "error": "Internal hallway scope requires verified gateway identity"
    }
    assert w.hidden.id not in json.dumps(response)


@pytest.mark.parametrize("backend", ["sqlite", "json"])
@pytest.mark.parametrize("size", [500, 501])
def test_card253_s03_exact_page_boundary(world, monkeypatch, backend, size):
    w = world
    rows = [record(w, f"row-{i}") for i in range(size)]
    install(w, monkeypatch, backend, rows, "boundary")
    reply = public(w, limit=1, offset=size - 1)
    assert reply == {
        "hallways": rows[-1:],
        "total": size,
        "count": 1,
        "offset": size - 1,
        "limit": 1,
    }
    probes = [result["probe"] for _, result in w.calls if "probe" in result]
    assert len(probes) == (1 if size == 500 else 2)
    assert probes[0]["next"] == (None if size == 500 else 500)
    assert probes[-1]["next"] is None


@pytest.mark.parametrize("backend", ["sqlite", "json"])
def test_card253_s03_readable_private_label(world, monkeypatch, backend):
    from commonplace.handoff.provenance_refs import label_tokens

    w = world
    source = f"capture:{w.principal.user_id}:readable-label"
    store = w.h.gateway.handoff.store_for(w.h.workspace)
    thread = store.store(
        w.h.workspace,
        source,
        [ThreadMessage(0, "user", "message", "own", "own")],
        actor=w.principal.user_id,
        wing="private_chats",
    )
    assert w.h.gateway.handoff.recall.can_read(w.principal, thread.id)
    label = f"shared {source} {source} 👩‍💻 25%"
    assert label_tokens(label) == (source,)
    row = record(w, "private-label-readable", label=label)
    install(w, monkeypatch, backend, [row], "private-label-readable")
    assert public(w, limit=10) == {
        "hallways": [row],
        "total": 1,
        "count": 1,
        "offset": 0,
        "limit": 10,
    }


@pytest.mark.parametrize("backend", ["sqlite", "json"])
def test_card253_s03_malformed_strength(world, monkeypatch, backend):
    w = world
    rows = [
        record(w, "malformed", co_occurrence_count="3abc"),
        record(w, "normal", co_occurrence_count=4),
        record(w, "zero", co_occurrence_count=0),
    ]
    install(w, monkeypatch, backend, rows, "strength")
    reply = public(w, limit=10)
    assert reply["hallways"] == [rows[1], rows[2]]
    assert reply["total"] == reply["count"] == 2


@pytest.mark.parametrize("backend", ["sqlite", "json"])
@pytest.mark.parametrize("kind", ["provenance", "singular_room", "drawer_preview"])
def test_card253_s03_unreviewed_keys(world, monkeypatch, backend, kind):
    w = world
    extra = {
        "provenance": {"source_thread": w.hidden.id},
        "singular_room": {"room": w.hidden.id},
        "drawer_preview": {
            "drawer_preview": f"secret {w.hidden.id}",
            "drawer_wing": "private_threads",
            "drawer_room": w.hidden.id,
        },
    }[kind]
    row = record(w, "legacy-extra", **extra)
    rows = [record(w, "visible"), row] if kind == "singular_room" else [row]
    install(w, monkeypatch, backend, rows, "unreviewed")
    reply = public(w, limit=10)
    assert w.calls
    assert w.hidden.id not in json.dumps(reply), f"s03 {kind} leaked hidden id"
    expected = [{k: v for k, v in r.items() if k not in extra} for r in rows]
    assert reply["hallways"] == expected
    assert reply["count"] == reply["total"] == len(expected)
    assert w.hallways.list_hallways() == rows


@pytest.mark.parametrize("backend", ["sqlite", "json"])
def test_card253_s03_label_scan_bound(world, monkeypatch, backend):
    from commonplace.handoff import provenance_refs

    w = world
    cap = provenance_refs.MAX_REFERENCE_CHARS
    rows = [
        record(w, "at-limit", label="x" * cap),
        record(w, "over-limit", label="capture:" * (cap // 8 + 1)),
    ]
    install(w, monkeypatch, backend, rows, "label-bound")
    scanned = []
    original = provenance_refs.label_tokens

    def scan(label):
        assert len(label) <= cap, "over-limit label reached token scan"
        scanned.append(len(label))
        return original(label)

    monkeypatch.setattr(provenance_refs, "label_tokens", scan)
    reply = public(w, limit=10)
    assert reply == {"hallways": rows[:1], "total": 1, "count": 1, "offset": 0, "limit": 10}
    assert scanned and max(scanned) == cap
    assert w.hallways.list_hallways() == rows


REVIEWED_VALUE_CHANGES = {
    "rooms_embedded": lambda w: {"rooms": [f"legacy {w.hidden.id}"]},
    "id_embedded": lambda w: {"id": f"legacy-{w.hidden.id}"},
    "created_at": lambda w: {"created_at": f"legacy {w.hidden.id}"},
    "count_text": lambda w: {"co_occurrence_count": f"n {w.hidden.id}"},
    "entity_a": lambda w: {"entity_a": w.hidden.id},
    "control_label": lambda w: {"label": f"plain {w.hidden.id}"},
}


@pytest.mark.parametrize("backend", ["sqlite", "json"])
@pytest.mark.parametrize("case", sorted(REVIEWED_VALUE_CHANGES))
def test_card253_s03_reviewed_values(world, monkeypatch, backend, case):
    w = world
    row = record(w, f"probe-{case}", **REVIEWED_VALUE_CHANGES[case](w))
    install(w, monkeypatch, backend, [row], f"probe-{case}")
    reply = public(w, limit=10)
    assert w.hidden.id not in json.dumps(reply), f"probe {case} leaked hidden id"


@pytest.mark.parametrize("backend", ["sqlite", "json"])
def test_card253_s03_dynamics_fields(world, monkeypatch, backend):
    from mempalace.dynamics import initialize_dynamics_fields

    w = world
    row = initialize_dynamics_fields(record(w, "auto"))
    install(w, monkeypatch, backend, [row], "probe-dynamics")
    got = public(w, limit=10)["hallways"]
    assert got == [row], f"probe dynamics missing keys {sorted(set(row) - set(got[0]))}"


@pytest.mark.parametrize("field", ["strength", "stability", "access_count", "co_occurrence_count"])
@pytest.mark.parametrize("value", ["2", True, None, float("inf")])
def test_card253_s03_numeric_types(world, field, value):
    from commonplace.handoff.mirror_access import hallway_visible, reader

    w = world
    access = reader(w.h.gateway, w.principal)
    assert not hallway_visible(record(w, "bad", **{field: value}), access, w.principal.policy)
    assert hallway_visible(record(w, "good", **{field: 2.5}), access, w.principal.policy)
    assert public(w, limit=10)["count"] == 0


def test_card253_s03_gate_projects_shared_fields(world):
    from commonplace.handoff.hallway_fields import PUBLIC_KEYS
    from commonplace.handoff.mirror_access import gate
    from mempalace.dynamics import initialize_dynamics_fields

    w = world
    row = initialize_dynamics_fields(record(w, "kept", source_thread=w.hidden.id))
    result = {"hallways": [row], "count": 1, "total": 1, "offset": 0, "limit": 10}
    got = gate(w.h.gateway, w.principal, "all_wings", result)
    assert got["hallways"] == [{key: row[key] for key in PUBLIC_KEYS if key in row}]
    assert w.hidden.id not in json.dumps(got)
    assert public(w, limit=10)["count"] == 0


@pytest.mark.parametrize("backend", ["sqlite", "json"])
@pytest.mark.parametrize(
    "field",
    [
        "id",
        "wing",
        "rooms",
        "label",
        "entity_a",
        "entity_b",
        "created_by",
        "created_at",
        "last_activated",
    ],
)
def test_card253_s03_all_public_strings(world, monkeypatch, backend, field):
    w = world
    value = f"legacy {w.hidden.id}"
    row = record(w, "public-string", **{field: [value] if field == "rooms" else value})
    install(w, monkeypatch, backend, [row], "public-string")
    assert public(w, wing=None, limit=10) == {
        "hallways": [],
        "total": 0,
        "count": 0,
        "offset": 0,
        "limit": 10,
    }


@pytest.mark.parametrize("kind", ["wildcard", "literal"])
def test_member_shared_reader_contract(hub, shared_readers, kind):  # noqa: F811
    principal = hub.db.authenticate_token(shared_readers[kind])
    assert principal.role == "member" and not principal.person
    assert not principal.policy.sees_all_wings
    assert not principal.policy.has_scope("private:read")
    assert principal.policy.can_read_wing("wing_fixture")
    assert principal.policy.can_read_wing("shared")
    assert not principal.policy.can_read_wing("private_notes")
    assert not principal.policy.can_read_wing("private_chats")
    assert_member_drawer_access(hub, shared_readers[kind])
