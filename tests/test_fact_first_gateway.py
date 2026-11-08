"""Fact-first recall uses the real gateway's KG scope, even with drawer fan-out."""

import logging

import pytest

from tests._commonplace_helpers import hub_fixture  # noqa: F401
from tests.test_fact_ranking_round_two import LONG_QUERY


def plant(hub):
    owner = hub.owner_token()
    for wing, value, sensitivity in (
        ("shared", "public-value", "public"),
        ("shared", "private-value", "private"),
        ("project_only", "project-value", "internal"),
        ("vault", "hidden-value", "internal"),
    ):
        result, text = hub.call(
            owner,
            "mempalace_kg_save",
            subject="Kestrel",
            predicate="deploy_port",
            object=value,
            wing=wing,
            sensitivity=sensitivity,
            source_thread="mcp:source",
            source_seq_start=0,
            source_seq_end=1,
            single_valued=False,
        )
        assert result and result.get("success"), text
    result, text = hub.call(
        owner,
        "mempalace_add_drawer",
        wing="shared",
        room="r",
        content="Kestrel drawer stays verbatim",
    )
    assert result and result.get("success"), text
    return owner


@pytest.mark.parametrize("wing", [None, "shared"])
def test_search_matches_scoped_kg_visibility_with_probe_and_final_read(hub, monkeypatch, wing):
    plant(hub)
    token = hub.token(
        "reader",
        profile="full",
        scopes=["memory:read", "kg:read"],
        read_wings=["shared", "project_*"],
        trust="standard",
    )
    kg, text = hub.call(
        token, "mempalace_kg_query", entity="Kestrel", **({"wings": [wing]} if wing else {})
    )
    assert kg, text
    calls = []
    original = hub.gateway.mediator.workers.call_tool

    def recorded(principal, name, args):
        calls.append((name, args))
        return original(principal, name, args)

    monkeypatch.setattr(hub.gateway.mediator.workers, "call_tool", recorded)
    result, raw = hub.call(
        token, "mempalace_search", query="Kestrel", **({"wing": wing} if wing else {})
    )
    assert result, raw
    assert {f["id"] for f in result["facts"]} == {f["id"] for f in kg["facts"]}
    assert result["facts"] and result["results"]
    assert not any(value in raw for value in ("private-value", "hidden-value"))
    assert (
        sum(name == "mempalace_kg_query" and args.get("search", False) for name, args in calls) == 2
    )
    # Limited readers resolve stored wing spellings before the scoped search.
    assert sum(name == "mempalace_kg_stats" for name, _ in calls) == 0
    assert [args for name, args in calls if name == "mempalace_kg_list_wings"] == [{}]
    kg_calls = [args for name, args in calls if name == "mempalace_kg_query"]
    assert kg_calls[0]["thread_scope"]["probe"] is True
    assert "allow" in kg_calls[1]["thread_scope"]


@pytest.mark.parametrize(
    "scopes,tools", [(["memory:read"], "full"), (["memory:read", "kg:read"], ["mempalace_search"])]
)
def test_search_without_kg_permission_never_returns_facts(hub, scopes, tools):
    plant(hub)
    token = hub.token(
        "drawer-only",
        profile="full",
        tools=None if tools == "full" else tools,
        scopes=scopes,
        read_wings=["*"],
    )
    result, raw = hub.call(token, "mempalace_search", query="Kestrel", wing="shared")
    assert result and result["facts"] == [], raw
    assert "public-value" not in raw and "private-value" not in raw


def test_kg_only_wing_is_searchable_without_any_drawer(hub):
    plant(hub)
    token = hub.token(
        "facts-only",
        profile="full",
        scopes=["memory:read", "kg:read"],
        read_wings=["project_only"],
        trust="standard",
    )
    result, raw = hub.call(token, "mempalace_search", query="Kestrel")
    assert result and [f["object"] for f in result["facts"]] == ["project-value"], raw
    assert result["results"] == []


def test_failed_fact_scope_check_does_not_gate_drawers_or_widen_scope(hub, monkeypatch):
    plant(hub)
    token = hub.token(
        "reader", profile="full", scopes=["memory:read", "kg:read"], read_wings=["shared"]
    )
    plain, _ = hub.call(token, "mempalace_search", query="Kestrel", include_facts=False)
    original = hub.gateway.mediator.workers.call_tool

    def broken(principal, name, arguments):
        return (
            {"error": "fact index unavailable"}
            if name == "mempalace_kg_query" and arguments.get("thread_scope", {}).get("probe")
            else original(principal, name, arguments)
        )

    monkeypatch.setattr(hub.gateway.mediator.workers, "call_tool", broken)
    combined, raw = hub.call(token, "mempalace_search", query="Kestrel")
    assert combined and combined["facts"] == [], raw
    assert combined["results"] == plain["results"]


@pytest.mark.parametrize("wing", [None, "shared"])
def test_unexpected_fact_worker_error_keeps_drawers_and_empty_facts(hub, monkeypatch, wing):
    plant(hub)
    token = hub.token(
        "reader", profile="full", scopes=["memory:read", "kg:read"], read_wings=["shared"]
    )
    args = {"query": "Kestrel", **({"wing": wing} if wing else {})}
    plain, _ = hub.call(token, "mempalace_search", include_facts=False, **args)
    original = hub.gateway.mediator.workers.call_tool

    def broken(principal, name, arguments):
        if name == "mempalace_kg_query" and arguments.get("search"):
            raise RuntimeError("private detail")
        return original(principal, name, arguments)

    monkeypatch.setattr(hub.gateway.mediator.workers, "call_tool", broken)
    combined, raw = hub.call(token, "mempalace_search", **args)
    assert combined["results"] == plain["results"]
    assert combined["facts"] == [] and "facts_error" not in combined
    assert "private detail" not in raw


def test_multiwing_drawer_error_is_not_decorated_with_facts(hub, monkeypatch):
    plant(hub)
    token = hub.token(
        "reader", profile="full", scopes=["memory:read", "kg:read"], read_wings=["shared"]
    )
    original = hub.gateway.mediator.workers.call_tool

    def failed_drawers(principal, name, arguments):
        if name == "mempalace_search":
            return {"error": "Drawer search failed"}
        return original(principal, name, arguments)

    monkeypatch.setattr(hub.gateway.mediator.workers, "call_tool", failed_drawers)
    result, _ = hub.call(token, "mempalace_search", query="Kestrel")
    assert result == {"error": "Drawer search failed"}


def test_captured_cli_output_and_local_hub_are_scoped(hub):
    owner = plant(hub)
    token = hub.token(
        "cli",
        profile="full",
        scopes=["memory:read", "kg:read"],
        read_wings=["shared"],
        trust="standard",
    )
    result, raw = hub.call(
        token, "mempalace_search", query="Kestrel", wing="shared", cli_compatible=True
    )
    assert result and result["facts"], raw
    output = result["cli_output"]
    assert output.index("Facts") < output.index("[1]")
    assert "public-value" in output and "private-value" not in raw and "hidden-value" not in raw
    all_facts, raw = hub.call(owner, "mempalace_search", query="Kestrel")
    assert len(all_facts["facts"]) == 4, raw


def test_long_query_returns_facts_for_owner_and_scoped_reader_without_warnings(hub, caplog):
    owner = plant(hub)
    reader = hub.token(
        "reader",
        profile="full",
        scopes=["memory:read", "kg:read"],
        read_wings=["shared", "project_*"],
        trust="standard",
    )
    caplog.clear()
    caplog.set_level(logging.WARNING)
    for token, count in ((owner, 4), (reader, 2)):
        short, raw = hub.call(token, "mempalace_search", query="Kestrel deploy port")
        assert short, raw
        long, raw = hub.call(token, "mempalace_search", query=LONG_QUERY)
        assert long and "facts_error" not in long, raw
        assert len(long["facts"]) == count
        assert {f["id"] for f in long["facts"]} == {f["id"] for f in short["facts"]}
        kg, raw = hub.call(token, "mempalace_kg_query", entity=LONG_QUERY, search=True)
        assert kg and "facts_error" not in kg, raw
        assert {f["id"] for f in kg["facts"]} == {f["id"] for f in long["facts"]}
    assert not caplog.records


@pytest.mark.parametrize("tool", ["mempalace_search", "mempalace_kg_query"])
@pytest.mark.parametrize("scoped", [False, True])
def test_fact_query_sanitizer_warns_once_through_gateway(hub, monkeypatch, caplog, tool, scoped):
    owner = plant(hub)
    token = (
        hub.token(
            "reader", profile="full", scopes=["memory:read", "kg:read"], read_wings=["shared"]
        )
        if scoped
        else owner
    )
    query = "Kestrel " + "deployment " * 20 + "port?"
    args = {"query": query} if tool == "mempalace_search" else {"entity": query, "search": True}
    fact_warnings = []
    call_tool = hub.gateway.mediator.workers.call_tool

    def warnings():
        return [record for record in caplog.records if "Query sanitized:" in record.message]

    def recorded(principal, name, args):
        before = len(warnings())
        result = call_tool(principal, name, args)
        if name == "mempalace_kg_query" and args.get("search"):
            fact_warnings.append(len(warnings()) - before)
        return result

    monkeypatch.setattr(hub.gateway.mediator.workers, "call_tool", recorded)
    caplog.clear()
    caplog.set_level(logging.WARNING, logger="mempalace.query_sanitizer")
    result, raw = hub.call(token, tool, **args)
    assert result and "facts_error" not in result, raw
    assert len(result["facts"]) == (1 if scoped else 4)
    # Drawer fan-out has independent tool requests; each fact request must
    # sanitize exactly once, regardless of those drawer calls.
    if tool == "mempalace_kg_query":
        assert len(warnings()) == 1
    else:
        # The metadata-only probe does not sanitize or rank fact content.
        assert fact_warnings == [0, 1]


@pytest.mark.parametrize("owner", [False, True])
@pytest.mark.parametrize("wing", [None, "shared"])
def test_empty_cli_capture_omits_facts_for_direct_and_fanout_search(hub, owner, wing):
    owner_token = plant(hub)
    token = (
        owner_token
        if owner
        else hub.token(
            "reader", profile="full", scopes=["memory:read", "kg:read"], read_wings=["shared"]
        )
    )
    args = {"query": "database migration", "cli_compatible": True}
    if wing:
        args["wing"] = wing
    for include_facts in (False, True):
        result, raw = hub.call(token, "mempalace_search", include_facts=include_facts, **args)
        assert result and "cli_output" in result, raw
        assert "facts" not in result and "facts_error" not in result


def test_search_source_opens_verbatim_range_through_gateway_recall(hub):
    from commonplace.handoff.models import ThreadMessage
    from commonplace.handoff.recall import ThreadNotFound

    token = hub.token(
        "source-reader",
        profile="full",
        scopes=[
            "memory:read",
            "memory:write",
            "kg:read",
            "kg:write",
            "threads:read",
            "private:read",
        ],
        read_wings=["*"],
        write_wings=["shared"],
        trust="trusted",
    )
    principal = hub.db.authenticate_token(token)
    context = hub.gateway.handoff
    text = "For Kestrel the deploy port is 8443.\nKeep these exact words."
    stored = context.store_for(hub.workspace).store(
        hub.workspace,
        "source-exact",
        [
            ThreadMessage(seq=0, role="user", kind="message", text=text, raw=text),
        ],
        actor=principal.actor,
    )
    saved, raw = hub.call(
        token,
        "mempalace_kg_save",
        subject="Kestrel",
        predicate="deploy_port",
        object="8443",
        wing="shared",
        source_thread=stored.id,
        source_seq_start=0,
        source_seq_end=0,
    )
    assert saved and saved["success"], raw
    drawer, raw = hub.call(
        token, "mempalace_add_drawer", wing="shared", room="source", content=text
    )
    assert drawer and drawer["success"], raw
    result, raw = hub.call(token, "mempalace_search", query="Kestrel deploy port")
    assert result and result["facts"], raw
    (source,) = result["facts"][0]["sources"]
    assert source["source_link"] == "/app/threads/" + stored.id
    opened = context.recall.get(
        principal, source["source_thread"], source["source_seq_start"], source["source_seq_end"]
    )
    assert [m.text for m in opened] == [text]
    restricted = hub.token(
        "no-source", profile="full", scopes=["memory:read", "kg:read"], read_wings=["shared"]
    )
    with pytest.raises(ThreadNotFound):
        context.recall.get(hub.db.authenticate_token(restricted), stored.id)
