"""Restricted drawer searches fill their limit from authorized ranked hits."""

import json
from dataclasses import replace
from statistics import median
from time import perf_counter
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from commonplace.gateway import Mediator, WorkerUnavailable, _tool_response, _tool_result
from commonplace.handoff.recall import Recall
from commonplace.policy import Policy
from commonplace.rules import checks
from tests._overview_helpers import overview_result
from tests.test_handoff_recall import MemoryStore, reader, stored


def search_world(hidden=9, readable=4, *, every_wing=False):
    principal = reader(wings=("shared", "project_*"))
    if every_wing:
        principal = replace(
            principal,
            policy=Policy.build(
                profile="full", scopes=["memory:read", "private:read"], read_wings=["*"]
            ),
        )
    hits = [
        {"drawer_id": f"hidden-{i}", "wing": "shared", "room": "thr_dead", "text": "hidden"}
        for i in range(hidden)
    ] + [
        {"drawer_id": f"visible-{i}", "wing": "shared", "room": "notes", "text": f"exact {i}"}
        for i in range(readable)
    ]
    calls = []

    def call_tool(p, name, args):
        assert p == principal
        overview = overview_result(name, args, {"shared": "notes"})
        if overview is not None:
            return overview
        assert name == "mempalace_search"
        calls.append(dict(args))
        return {
            "query": args["query"],
            "results": hits[: args.get("limit", 5)],
            "count": min(len(hits), args.get("limit", 5)),
            "total": len(hits),
            "total_before_filter": len(hits),
            "filtered_by_max_distance": hidden,
        }

    def post(p, request):
        params = request["params"]
        return _tool_response(request.get("id"), call_tool(p, params["name"], params["arguments"]))

    mediator = Mediator(Mock(), SimpleNamespace(call_tool=call_tool, post=post))
    store = MemoryStore()
    context = SimpleNamespace(
        store_for=lambda workspace: store,
        _stores={principal.workspace_id: store},
        recall=Recall(lambda workspace: store),
    )
    mediator.gateway = SimpleNamespace(handoff=context)
    return SimpleNamespace(mediator=mediator, principal=principal, calls=calls, hits=hits)


def request(world, **filters):
    return _tool_result(
        world.mediator._tools_call(
            world.principal,
            {
                "id": 1,
                "params": {
                    "name": "mempalace_search",
                    "arguments": {"query": "exact", "limit": 2, "include_facts": False, **filters},
                },
            },
        )
    )


@pytest.mark.parametrize("filters", [{}, {"wing": "shared"}, {"wings": ["shared", "vault"]}])
def test_hidden_ranked_prefix_does_not_consume_readable_limit(filters):
    world = search_world()
    result = request(world, **filters)
    assert [hit["drawer_id"] for hit in result["results"]] == ["visible-0", "visible-1"]
    assert result["count"] == 2
    assert not {"total", "total_before_filter", "filtered_by_max_distance"}.intersection(result)
    assert "hidden" not in json.dumps(result)
    assert all(call.get("include_facts") is False for call in world.calls)
    assert all(call.get("wings", ["shared"]) == ["shared"] for call in world.calls)


def test_hidden_population_does_not_appear_in_response():
    assert request(search_world(hidden=3)) == request(search_world(hidden=19))


def test_refill_helper_omits_worker_candidate_counters():
    world = search_world()
    result = world.mediator._readable_search(
        world.principal, "mempalace_search", {"query": "exact", "limit": 2}
    )
    assert [hit["drawer_id"] for hit in result["results"]] == ["visible-0", "visible-1"]
    assert result["count"] == 2
    assert not {"total", "total_before_filter", "filtered_by_max_distance"}.intersection(result)


@pytest.mark.parametrize("filters", [{"wing": "shared"}, {}])
def test_restricted_cli_search_fills_before_rendering(filters):
    world = search_world()
    result = request(world, cli_compatible=True, **filters)
    assert "exact 0" in result["cli_output"] and "exact 1" in result["cli_output"]
    assert "hidden" not in json.dumps(result)
    assert result["results"] == []
    assert all(call.get("cli_compatible") is False for call in world.calls)
    assert len(world.calls) > 1


def test_fanout_gates_candidates_before_returning_filled_limit():
    world = search_world()
    result = world.mediator._fan_out_search(
        world.principal, "mempalace_search", {"query": "exact", "limit": 2}
    )
    assert [hit["drawer_id"] for hit in result["results"]] == ["visible-0", "visible-1"]


@pytest.mark.parametrize("filters", [{}, {"wing": "shared"}])
def test_exhaustion_returns_only_remaining_readable_hits(filters):
    world = search_world(hidden=9, readable=1)
    assert [h["drawer_id"] for h in request(world, **filters)["results"]] == ["visible-0"]
    assert world.calls[-1]["limit"] == 16


def test_refill_stops_at_worker_candidate_bound():
    world = search_world(hidden=150)
    assert request(world)["results"] == []
    assert [call["limit"] for call in world.calls] == [2, 4, 8, 16, 32, 64, 100]


def test_no_refill_when_limit_is_already_readable():
    world = search_world(hidden=0)
    assert len(request(world)["results"]) == 2
    assert [call["limit"] for call in world.calls] == [2]


@pytest.mark.parametrize("filters", [{}, {"wing": "shared"}])
def test_every_wing_principal_keeps_single_worker_search(filters):
    world = search_world(every_wing=True)
    result = request(world, **filters)
    assert [call["limit"] for call in world.calls] == [2]
    assert result["total_before_filter"] == 13
    assert result["filtered_by_max_distance"] == 9


def test_refill_retains_filters_and_one_joint_wing_ranking():
    world = search_world()
    filters = {
        "wing": "shared",
        "wings": ["shared"],
        "room": "notes",
        "source_file": "/data/notes.txt",
        "since": "2026-01-01",
        "before": "2027-01-01",
        "max_distance": 0.8,
        "candidate_strategy": "vector",
    }
    assert len(request(world, **filters)["results"]) == 2
    assert len(world.calls) > 1
    assert all(all(call[key] == value for key, value in filters.items()) for call in world.calls)


def test_fanout_refill_enforces_wing_policy_before_limit():
    world = search_world(hidden=0)
    world.hits.insert(0, {"drawer_id": "secret", "wing": "vault", "room": "notes"})
    result = world.mediator._fan_out_search(
        world.principal, "mempalace_search", {"query": "exact", "limit": 2}
    )
    assert [hit["drawer_id"] for hit in result["results"]] == ["visible-0", "visible-1"]


def test_refill_preserves_worker_error():
    world = search_world()
    error = {"error": "invalid query"}
    world.mediator.workers.call_tool = lambda *args: error
    assert request(world, wing="shared") == error


def test_refill_rejects_malformed_worker_response():
    world = search_world()
    original = world.mediator.workers.call_tool
    world.mediator.workers.call_tool = lambda p, name, args: (
        None if name == "mempalace_search" else original(p, name, args)
    )
    with pytest.raises(WorkerUnavailable):
        request(world)


def test_expired_refill_discards_partial_results(monkeypatch):
    world = search_world()
    original = world.mediator.workers.call_tool

    def call_tool(*args):
        result = original(*args)
        if checks._deadline.get() is not None:
            checks._deadline.set(0)
        return result

    monkeypatch.setattr(world.mediator.workers, "call_tool", call_tool)
    assert request(world, wing="shared") == {"query": "exact", "results": [], "facts": []}


def test_pinned_thread_search_is_equal_with_cold_and_warm_store():
    world = search_world()
    store = MemoryStore()
    thread = stored(wing="shared")
    store.threads[thread.id] = thread
    stores = {}

    def store_for(workspace):
        stores[workspace] = store
        return store

    context = SimpleNamespace(store_for=store_for, _stores=stores, recall=Recall(store_for))
    world.mediator.gateway = SimpleNamespace(handoff=context)
    for hit in world.hits[9:]:
        hit["room"] = thread.id
    cold = request(world, wing="shared")
    assert [hit["drawer_id"] for hit in cold["results"]] == ["visible-0", "visible-1"]
    assert request(world, wing="shared") == cold


def test_restricted_search_latency():
    """Information-only local mediator benchmark, same synthetic ranked fixture."""
    world = search_world()
    for _ in range(20):
        request(world)
    samples = []
    for _ in range(200):
        started = perf_counter()
        result = request(world)
        samples.append((perf_counter() - started) * 1000)
    print(
        f"restricted search: median_ms={median(samples):.6f} "
        f"p95_ms={sorted(samples)[189]:.6f} readable_hits={len(result['results'])} n=200"
    )
