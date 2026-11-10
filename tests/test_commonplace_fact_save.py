"""fact_save through the hub: wing-scoped, secret-free, merged and replaced."""

import json

from commonplace import events
from tests._commonplace_helpers import hub_fixture  # noqa: F401

SCOPED = dict(
    profile="full",
    scopes=["memory:read", "memory:write", "kg:read", "kg:write"],
    read_wings=["shared"],
    write_wings=["shared"],
    write_wing="shared",
    trust="standard",
)


def _save(hub, token, **args):
    args.setdefault("source", {"thread": "mcp:test-fact-save"})
    response = hub.rpc(token, "tools/call", {"name": "fact_save", "arguments": args})
    assert "error" not in response, response
    result = response["result"]
    return result["content"][0]["text"], result.get("isError", False)


def _facts(hub, entity):
    token = hub.owner_token(name=f"reader-{hub._ids}")
    return hub.call(token, "mempalace_kg_query", entity=entity)[0]["facts"]


def test_fact_save_merges_and_replaces(hub):
    token = hub.token("scoped", **SCOPED)
    text, error = _save(
        hub,
        token,
        subject="Avery",
        predicate="works_at",
        object="Acme",
        source={"thread": "mcp:t1", "seq_start": 3},
        valid_from="2024-01-01",
    )
    assert not error and text.startswith("saved ")
    first = text.split()[1]
    text, _ = _save(
        hub,
        token,
        subject="Avery",
        predicate="works_at",
        object="Acme",
        source={"thread": "mcp:t2", "seq_start": 8},
    )
    assert text == f"merged into {first}"
    text, _ = _save(
        hub,
        token,
        subject="Avery",
        predicate="works_at",
        object="Initech",
        valid_from="2026-01-01",
    )
    assert text.endswith('replacing "Acme"')
    facts = {f["object"]: f for f in _facts(hub, "Avery")}
    assert facts["Acme"]["status"] == "superseded" and facts["Acme"]["wing"] == "shared"
    assert [s["source_thread"] for s in facts["Acme"]["sources"]] == ["mcp:t1", "mcp:t2"]
    assert facts["Initech"]["current"]
    kinds = [e["kind"] for e in events.after(hub.db, hub.workspace)]
    assert kinds.count("fact.saved") == 3
    assert "Initech" not in json.dumps(events.after(hub.db, hub.workspace))


def test_fact_save_refuses_secrets_and_stores_nothing(hub):
    owner = hub.owner_token()
    text, error = _save(hub, owner, subject="deploy", predicate="token", object="ghp_" + "a" * 36)
    assert error and "secret" in text
    assert _facts(hub, "deploy") == []


def test_fact_save_stays_in_writable_wings(hub):
    token = hub.token("scoped", **SCOPED)
    text, error = _save(hub, token, subject="a", predicate="b", object="c", theme="vault_x")
    assert error
    assert _facts(hub, "a") == []


def test_fact_save_needs_kg_write(hub):
    token = hub.token(
        "reader",
        profile="full",
        scopes=["memory:read", "memory:write"],
        read_wings=["shared"],
        write_wing="shared",
        trust="standard",
    )
    assert "fact_save" not in hub.tool_names(token)


def test_a_write_only_wing_is_refused_for_fact_writes(hub):
    owner = hub.owner_token()
    hub.call(
        owner,
        "mempalace_kg_add",
        subject="Alice",
        predicate="works_at",
        object="Acme",
        wing="shared",
    )
    writer = hub.token(
        "writer",
        profile="full",
        scopes=["memory:read", "memory:write", "kg:read", "kg:write"],
        read_wings=["mine"],
        write_wings=["shared", "mine"],
        trust="standard",
    )
    for name, args in (
        (
            "mempalace_kg_save",
            {"subject": "Alice", "predicate": "works_at", "object": "Beta", "single_valued": True},
        ),
        ("mempalace_kg_add", {"subject": "Alice", "predicate": "works_at", "object": "Acme"}),
        (
            "mempalace_kg_invalidate",
            {"subject": "Alice", "predicate": "works_at", "object": "Acme"},
        ),
    ):
        denied, text = hub.call(writer, name, wing="shared", **args)
        assert denied and denied["denied"], (name, text)
    (fact,) = _facts(hub, "Alice")
    assert fact["object"] == "Acme" and fact["current"]


def test_a_hidden_source_drawer_is_refused(hub):
    owner = hub.owner_token()
    hidden, _ = hub.call(
        owner, "mempalace_add_drawer", wing="vault_x", room="r", content="private words"
    )
    token = hub.token("scoped", **SCOPED)
    result, text = hub.call(
        token,
        "mempalace_kg_save",
        subject="a",
        predicate="b",
        object="c",
        source_drawer_id=hidden["drawer_id"],
    )
    assert result and result.get("success") is False and "not found" in result["error"], text
    assert _facts(hub, "a") == []


def test_a_retry_into_an_unreadable_wing_hides_the_kept_source(hub):
    owner = hub.owner_token()
    hub.call(
        owner,
        "mempalace_add_drawer",
        wing="inbox",
        room="r",
        content="same words",
        source_thread="owner-chat",
        source_seq_start=1,
    )
    bot = hub.token(
        "bot",
        profile="slim",
        scopes=["memory:read", "memory:write"],
        read_wings=["shared"],
        write_wing="inbox",
        trust="untrusted",
    )
    again, text = hub.call(
        bot, "mempalace_add_drawer", wing="inbox", room="r", content="same words"
    )
    assert again["success"] is True
    assert "reason" not in again and "source_kept" not in again
    assert "owner-chat" not in text
    mine, _ = hub.call(owner, "mempalace_add_drawer", wing="inbox", room="r", content="same words")
    assert mine["source_kept"]["thread"] == "owner-chat"


def test_fact_save_is_listed_only_where_it_works(hub):
    slim = hub.token(
        "slim",
        profile="slim",
        scopes=["memory:read", "memory:write", "kg:write"],
        read_wings=["shared"],
        write_wing="shared",
        trust="standard",
    )
    untrusted = hub.token(
        "untrusted",
        profile="full",
        scopes=["memory:read", "memory:write", "kg:write"],
        read_wings=["shared"],
        write_wing="inbox",
        trust="untrusted",
    )
    assert "fact_save" not in hub.tool_names(slim)
    assert "fact_save" not in hub.tool_names(untrusted)
    assert "fact_save" in hub.tool_names(hub.token("scoped", **SCOPED))
