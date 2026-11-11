"""Live status of machines and link ends: the rule set, its configuration and the hub's signals.

Every name here is synthetic. Time is a datetime the tests move; nothing sleeps.
"""

from datetime import datetime, timedelta, timezone

import pytest

from commonplace import sessions
from commonplace.runner import jobs, registry
from commonplace.topology import Observer, TopologyError, model, status, store
from tests._commonplace_helpers import hub_fixture  # noqa: F401

T0 = datetime(2026, 11, 11, 10, 0, 0, tzinfo=timezone.utc)
CONFIG = status.StatusConfig()
RUNNER = Observer("runner", "mach_0123456789abcdef")
EVIDENCE = "ping web1: 3 packets received"


def at(seconds):
    return T0 + timedelta(seconds=seconds)


def sig(seen=None, **kwargs):
    return status.MachineSignals(machine_id="mach_0123456789abcdef", seen=seen or {}, **kwargs)


def states(machine, *, now):
    result = status.machine_status(machine, now=now, config=CONFIG)
    return result["status"], result["color"], result["reason"]


# --- machine rules --------------------------------------------------------------------


def test_fake_clock_walks_green_orange_red_green():
    machine = sig({"machine_connection": T0})
    assert states(machine, now=T0) == ("up", "green", "heartbeat")
    assert states(machine, now=at(CONFIG.green_within_s)) == ("up", "green", "heartbeat")
    assert states(machine, now=at(CONFIG.green_within_s + 1)) == (
        "degraded",
        "orange",
        "late_heartbeat",
    )
    assert states(machine, now=at(CONFIG.red_after_s)) == ("degraded", "orange", "late_heartbeat")
    assert states(machine, now=at(CONFIG.red_after_s + 1)) == ("down", "red", "no_heartbeat")
    revived = sig({"machine_connection": at(CONFIG.red_after_s + 60)})
    assert states(revived, now=at(CONFIG.red_after_s + 61)) == ("up", "green", "heartbeat")


def test_a_machine_that_never_reported_is_unknown_not_green():
    result = status.machine_status(sig(), now=T0, config=CONFIG)
    assert (result["status"], result["color"], result["reason"]) == (
        "unknown",
        "grey",
        "never_reported",
    )
    assert result["last_signal_at"] is None and result["age_s"] is None
    # a start in progress does not make a silent machine green
    assert states(sig(start_in_progress=True), now=T0)[0] == "unknown"


def test_newest_of_the_signals_counts_and_is_named():
    machine = sig({"machine_connection": T0, "chat_poll": at(100), "worker_connection": at(40)})
    result = status.machine_status(machine, now=at(130), config=CONFIG)
    assert result["status"] == "up"
    assert result["last_signal_source"] == "chat_poll"
    assert result["last_signal_at"] == "2026-11-11T10:01:40Z"
    assert result["age_s"] == 30


def test_start_in_progress_is_orange_and_late_or_down_wins_over_it():
    machine = sig({"machine_connection": T0}, start_in_progress=True)
    assert states(machine, now=at(10)) == ("degraded", "orange", "start_in_progress")
    assert states(machine, now=at(CONFIG.green_within_s + 1))[2] == "late_heartbeat"
    assert states(machine, now=at(CONFIG.red_after_s + 1))[0] == "down"


def test_revoked_and_disabled_machines_are_down_even_with_a_fresh_signal():
    seen = {"machine_connection": T0}
    assert states(sig(seen, revoked=True), now=T0) == ("down", "red", "revoked")
    assert states(sig(seen, enabled=False), now=T0) == ("down", "red", "disabled")
    assert states(sig(revoked=True), now=T0)[2] == "revoked"


def test_a_signal_dated_in_the_future_counts_as_age_zero():
    result = status.machine_status(sig({"chat_poll": at(30)}), now=T0, config=CONFIG)
    assert (result["status"], result["age_s"]) == ("up", 0)


# --- configuration ---------------------------------------------------------------------


def test_thresholds_are_configuration_not_constants():
    tight = status.StatusConfig(green_within_s=10, red_after_s=20)
    machine = sig({"machine_connection": T0})
    assert status.machine_status(machine, now=at(15), config=tight)["status"] == "degraded"
    assert status.machine_status(machine, now=at(21), config=tight)["status"] == "down"
    assert status.machine_status(machine, now=at(15), config=CONFIG)["status"] == "up"


def test_config_from_env_and_mapping_and_validation():
    env = {
        "COMMONPLACE_TOPOLOGY_GREEN_S": "30",
        "COMMONPLACE_TOPOLOGY_RED_S": "300",
        "COMMONPLACE_TOPOLOGY_PATH_FRESH_S": "60",
        "COMMONPLACE_TOPOLOGY_PATH_STALE_S": "600",
    }
    assert status.StatusConfig.from_env(env).as_dict() == {
        "green_within_s": 30,
        "red_after_s": 300,
        "path_fresh_s": 60,
        "path_unknown_after_s": 600,
    }
    assert status.StatusConfig.from_env({}) == CONFIG
    assert status.StatusConfig.from_mapping({"red_after_s": 1000}).red_after_s == 1000
    for bad in (
        lambda: status.StatusConfig.from_env({"COMMONPLACE_TOPOLOGY_GREEN_S": "soon"}),
        lambda: status.StatusConfig.from_mapping({"green": 5}),
        lambda: status.StatusConfig(green_within_s=900, red_after_s=900),
        lambda: status.StatusConfig(path_fresh_s=4000),
        lambda: status.StatusConfig(green_within_s=0),
        lambda: status.StatusConfig(green_within_s=True),
    ):
        with pytest.raises(TopologyError):
            bad()


# --- link ends -------------------------------------------------------------------------


@pytest.fixture
def topo(tmp_path):
    topo = store.TopologyStore(tmp_path / "ws_one")
    yield topo
    topo.close()


def put_node(topo, key, kind="server", **fields):
    store.upsert_node(
        topo, key, {"kind": kind, **fields}, observer=RUNNER, evidence=EVIDENCE, now=T0
    )


def put_link(topo, source, target, method, reported=None, now=T0):
    store.upsert_link(
        topo,
        source,
        target,
        method,
        {"status": reported} if reported else None,
        observer=RUNNER,
        evidence=EVIDENCE,
        now=now,
    )


def derive(topo, signals, now):
    graph = store.read_graph(topo, now=now)
    return status.derive(graph, signals, now=now, config=CONFIG)


def test_ends_follow_the_node_and_the_path_report(topo):
    put_node(topo, "laptop", "pc", runner_id=RUNNER.id)
    put_node(topo, "web1", "server")
    put_link(topo, "laptop", "web1", "tailscale", "up")
    signals = {RUNNER.id: sig({"machine_connection": T0})}
    link = derive(topo, signals, T0)["links"]["laptop>web1:tailscale"]
    assert (link["status"], link["ends"]["from"]["status"], link["ends"]["to"]["status"]) == (
        "up",
        "up",
        "up",
    )
    assert link["ends"]["from"]["basis"] == "node" and link["ends"]["to"]["basis"] == "path"
    # the runner falls silent: its end turns red, the far end keeps the path report
    later = at(CONFIG.red_after_s + 1)
    put_link(topo, "laptop", "web1", "tailscale", "up", now=later)
    link = derive(topo, signals, later)["links"]["laptop>web1:tailscale"]
    assert link["ends"]["from"]["status"] == "down"
    assert link["ends"]["to"]["status"] == "up"
    assert link["status"] == "down"


def test_a_link_without_any_signal_is_unknown_at_both_ends(topo):
    put_node(topo, "laptop", "pc", runner_id=RUNNER.id)
    put_node(topo, "web1", "server")
    put_link(topo, "laptop", "web1", "direct")
    link = derive(topo, {RUNNER.id: sig()}, T0)["links"]["laptop>web1:direct"]
    assert link["status"] == "unknown" and link["color"] == "grey"
    assert {end["status"] for end in link["ends"].values()} == {"unknown"}


def test_node_of_an_unenrolled_machine_is_unknown_with_a_reason(topo):
    put_node(topo, "laptop", "pc", runner_id=RUNNER.id)
    put_node(topo, "web1", "server")
    result = derive(topo, {}, T0)
    assert result["nodes"]["laptop"]["reason"] == "machine_not_found"
    assert result["nodes"]["web1"]["reason"] == "no_signals"
    assert result["machines"] == {}


def test_path_report_ages_from_reported_to_late_to_unknown(topo):
    put_node(topo, "a", "server")
    put_node(topo, "b", "server")
    put_link(topo, "a", "b", "direct", "up")
    put_link(topo, "a", "b", "relay", "down")

    def path(method, seconds):
        link = derive(topo, {}, at(seconds))["links"][f"a>b:{method}"]
        return link["status"], link["reason"]

    fresh, stale = CONFIG.path_fresh_s, CONFIG.path_unknown_after_s
    assert path("direct", fresh) == ("up", "reported")
    assert path("direct", fresh + 1) == ("degraded", "late_report")
    assert path("direct", stale) == ("degraded", "late_report")
    assert path("direct", stale + 1) == ("unknown", "report_stale")
    assert path("relay", stale) == ("down", "late_report")
    assert path("relay", stale + 1) == ("unknown", "report_stale")


def test_one_of_several_paths_failing_is_orange_not_red(topo):
    put_node(topo, "a", "server")
    put_node(topo, "b", "server")
    put_link(topo, "a", "b", "direct", "down")
    put_link(topo, "a", "b", "tailscale", "up")
    links = derive(topo, {}, T0)["links"]
    assert links["a>b:direct"]["status"] == "degraded"
    assert links["a>b:direct"]["reason"] == "alternate_path_up"
    assert links["a>b:direct"]["color"] == "orange"
    assert links["a>b:tailscale"]["status"] == "up"
    # the same failure with no working alternative is red
    put_link(topo, "a", "b", "tailscale", "down")
    assert derive(topo, {}, T0)["links"]["a>b:direct"]["status"] == "down"
    # an alternative in the opposite direction counts too
    put_link(topo, "b", "a", "relay", "up")
    assert derive(topo, {}, T0)["links"]["a>b:direct"]["reason"] == "alternate_path_up"


def test_fake_clock_walk_of_a_link_end_green_orange_red_green(topo):
    put_node(topo, "laptop", "pc", runner_id=RUNNER.id)
    put_node(topo, "web1", "server")
    put_link(topo, "laptop", "web1", "direct")
    seen = {"machine_connection": T0}

    def end(seconds, seen):
        link = derive(topo, {RUNNER.id: sig(seen)}, at(seconds))["links"]["laptop>web1:direct"]
        return link["ends"]["from"]["color"]

    assert end(0, seen) == "green"
    assert end(CONFIG.green_within_s + 1, seen) == "orange"
    assert end(CONFIG.red_after_s + 1, seen) == "red"
    seen = {"machine_connection": at(CONFIG.red_after_s + 5)}
    assert end(CONFIG.red_after_s + 6, seen) == "green"


def test_derive_reads_stale_status_facts_that_values_no_longer_hold(topo):
    put_node(topo, "a", "server")
    put_node(topo, "b", "server")
    put_link(topo, "a", "b", "direct", "up")
    stale_at = at(16 * 60)  # past the 15 minute fact lifetime, past path_fresh_s
    graph = store.read_graph(topo, now=stale_at)
    assert "status" not in graph["links"][0]["values"]
    link = status.derive(graph, {}, now=stale_at, config=CONFIG)["links"]["a>b:direct"]
    assert (link["status"], link["reason"]) == ("degraded", "late_report")


# --- the hub's signals -----------------------------------------------------------------


@pytest.fixture
def world(hub):
    owner = sessions.person_principal(hub.db, hub.admin, hub.workspace)
    enrolled = registry.enroll(
        hub.db,
        owner,
        {"label": "box-one", "projects": ["demo"], "topics": ["worker"], "wings": ["shared"]},
    )
    hub.machine = enrolled
    hub.owner = owner
    return hub


def touch(hub, connection_id, instant):
    with hub.db._lock:
        hub.db._conn.execute(
            "UPDATE connections SET last_used_at=? WHERE id=?",
            (model.stamp(instant), connection_id),
        )


def add_job(hub, machine_id, state, **columns):
    row = {
        "id": f"job_{state}_{columns.get('task_number', 1)}",
        "task_number": 1,
        "board_slug": "demo",
        "machine_id": machine_id,
        "project": "demo",
        "topic": "worker",
        "grant_version": 1,
        "runtime_mode": "approval-required",
        "requested_by": "user:u",
        "state": state,
        "created_at": model.stamp(T0),
        "updated_at": model.stamp(T0),
        **columns,
    }
    with jobs.open_store(hub.gateway, hub.workspace).connection() as conn:
        conn.execute(
            f"INSERT INTO runner_jobs({','.join(row)}) VALUES ({','.join('?' for _ in row)})",
            tuple(row.values()),
        )


def read(hub, now, **kwargs):
    return status.read_status(hub.gateway, hub.workspace, now=now, config=CONFIG, **kwargs)


def test_a_fresh_workspace_reads_empty_and_creates_no_topology_file(hub):
    result = read(hub, T0)
    assert result["machines"] == {} and result["nodes"] == {} and result["links"] == {}
    topo = store.open_topology(hub.gateway, hub.workspace)
    assert not topo.path.exists()


def test_machine_registry_to_status_through_the_hubs_signals(world):
    hub = world
    mid = hub.machine["machine_id"]
    assert read(hub, T0)["machines"][mid]["status"] == "unknown"
    touch(hub, hub.machine["machine_connection_id"], T0)
    assert read(hub, at(5))["machines"][mid]["last_signal_source"] == "machine_connection"
    assert read(hub, at(5))["machines"][mid]["status"] == "up"
    assert read(hub, at(CONFIG.green_within_s + 10))["machines"][mid]["status"] == "degraded"
    assert read(hub, at(CONFIG.red_after_s + 10))["machines"][mid]["status"] == "down"
    touch(hub, hub.machine["machine_connection_id"], at(CONFIG.red_after_s + 20))
    assert read(hub, at(CONFIG.red_after_s + 25))["machines"][mid]["status"] == "up"


def test_worker_chat_and_start_signals_count(world):
    hub = world
    mid = hub.machine["machine_id"]
    touch(hub, hub.machine["worker_connection_id"], T0)
    assert read(hub, at(1))["machines"][mid]["last_signal_source"] == "worker_connection"
    with hub.db._lock:
        hub.db._conn.execute(
            "INSERT INTO chat_runners_seen(machine_id,workspace_id,user_id,clis,seen_at) "
            "VALUES (?,?,?,?,?)",
            (mid, hub.workspace, hub.admin, "[]", model.stamp(at(60))),
        )
    assert read(hub, at(61))["machines"][mid]["last_signal_source"] == "chat_poll"
    add_job(hub, mid, "started", updated_at=model.stamp(at(100)), task_number=2)
    result = read(hub, at(101))["machines"][mid]
    assert (result["last_signal_source"], result["status"]) == ("start_success", "up")


def test_a_start_underway_is_orange_and_a_lapsed_one_is_not(world):
    hub = world
    mid = hub.machine["machine_id"]
    touch(hub, hub.machine["machine_connection_id"], T0)
    add_job(hub, mid, "leased", lease_until=model.stamp(at(60)), task_number=3)
    assert read(hub, at(10))["machines"][mid]["reason"] == "start_in_progress"
    assert read(hub, at(61))["machines"][mid]["reason"] == "heartbeat"
    add_job(
        hub,
        mid,
        "starting",
        starting_at=model.stamp(at(100)),
        task_number=4,
    )
    touch(hub, hub.machine["machine_connection_id"], at(100))
    assert read(hub, at(110))["machines"][mid]["reason"] == "start_in_progress"
    assert read(hub, at(100 + 121))["machines"][mid]["reason"] == "heartbeat"


def test_revoked_machine_is_down(world):
    hub = world
    mid = hub.machine["machine_id"]
    touch(hub, hub.machine["machine_connection_id"], T0)
    registry.patch(hub.db, hub.owner, mid, {"enabled": False})
    assert read(hub, at(1))["machines"][mid]["reason"] == "disabled"


def test_node_and_link_ends_in_the_same_read(world):
    hub = world
    mid = hub.machine["machine_id"]
    topo = store.open_topology(hub.gateway, hub.workspace)
    put_node(topo, "box-one", "server", runner_id=mid)
    put_node(topo, "edge", "router")
    put_link(topo, "box-one", "edge", "direct")
    touch(hub, hub.machine["machine_connection_id"], T0)
    graph = store.read_graph(topo, now=T0)
    result = read(hub, at(5), graph=graph)
    assert result["nodes"]["box-one"]["status"] == "up"
    assert result["nodes"]["edge"]["status"] == "unknown"
    ends = result["links"]["box-one>edge:direct"]["ends"]
    assert (ends["from"]["status"], ends["to"]["status"]) == ("up", "unknown")
    assert result["machines"][mid] == result["nodes"]["box-one"]
    # without a graph the function reads it itself
    assert read(hub, at(5))["links"] == result["links"]


def test_status_of_one_workspace_never_includes_another(hub):
    other = hub.db.create_workspace("away", hub.admin)
    owner = sessions.person_principal(hub.db, hub.admin, other)
    enrolled = registry.enroll(
        hub.db,
        owner,
        {"label": "box-two", "projects": ["demo"], "topics": ["worker"], "wings": ["shared"]},
    )
    result = read(hub, T0)
    assert enrolled["machine_id"] not in result["machines"]
    assert (
        enrolled["machine_id"]
        in status.read_status(hub.gateway, other, now=T0, config=CONFIG)["machines"]
    )
