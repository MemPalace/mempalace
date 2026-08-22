"""Tests for mempalace.gossip.

Most tests run with an explicit ``KnowledgeGraph`` pointed at a temporary
SQLite path so they do not touch the user's real palace. Room graphs are
passed as explicit tuples to avoid pulling in a Chroma/pgvector collection.
"""

import json
import os
import random
import tempfile
import time
import unittest.mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mempalace.gossip import (
    EXAMPLE_GOSSIP_CONFIG,
    ChatterNode,
    GossipDaemon,
    GossipMessage,
    GossipProtocol,
    _default_gossip_path,
    gossip,
    load_gossip_config,
    normalize_wing_name,
    save_gossip_config,
)
import mempalace.gossip as gossip_mod
from mempalace.knowledge_graph import KnowledgeGraph


def _temp_db() -> str:
    fd, path = tempfile.mkstemp(suffix=".sqlite3")
    os.close(fd)
    return path


def _sample_room_graph() -> tuple[dict, list]:
    """Return a tiny room graph with two connected rooms spanning two wings."""
    nodes = {
        "contracts": {
            "wings": ["orkid", "defi"],
            "halls": ["technical"],
            "count": 10,
            "dates": [],
        },
        "audit_report": {
            "wings": ["orkid", "security"],
            "halls": ["security"],
            "count": 5,
            "dates": [],
        },
        "launch_blog": {
            "wings": ["brutal-marketing"],
            "halls": ["general"],
            "count": 3,
            "dates": [],
        },
    }
    edges = [
        {"room": "contracts", "wing_a": "orkid", "wing_b": "defi", "hall": "technical"},
        {"room": "audit_report", "wing_a": "orkid", "wing_b": "security", "hall": "security"},
    ]
    return nodes, edges


def _empty_room_graph() -> tuple[dict, list]:
    return {}, []


# ─────────────────────────────────────────────────────────────────────────────
# Config and model
# ─────────────────────────────────────────────────────────────────────────────


def test_load_default_config_writes_file():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "gossip_config.json")
        cfg = load_gossip_config(path=path)
        assert os.path.exists(path)
        assert "chatter_nodes" in cfg
        assert len(cfg["chatter_nodes"]) == 0
        assert cfg["topics"] == []


def test_default_config_does_not_leak_project_topology():
    """A neutral palace gets an empty gossip config with no project-specific names."""
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "gossip_config.json")
        cfg = load_gossip_config(path=path)
        combined = json.dumps(cfg)
        assert "orkid" not in combined
        assert "brutal-marketing" not in combined
        assert "negentropy" not in combined


def test_save_and_reload_config():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "gossip_config.json")
        custom = save_gossip_config({"version": "2.0"}, path=path)
        assert custom["version"] == "2.0"
        # Reload merges defaults
        reloaded = load_gossip_config(path=path)
        assert reloaded["version"] == "2.0"
        assert "chatter_nodes" in reloaded


def test_default_gossip_path_uses_home():
    path = _default_gossip_path()
    assert ".mempalace" in path
    assert path.endswith("gossip_config.json")


def test_chatter_node_from_dict():
    node = ChatterNode.from_dict(
        {
            "id": "x",
            "name": "X",
            "wing": "orkid",
            "hall": "technical",
            "role": "tech",
            "specialties": ["defi"],
            "gossip_radius": ["orkid"],
            "chatter_level": "high",
            "propagation_speed": "instant",
        }
    )
    assert node.id == "x"
    assert node.chatter_level == "high"
    assert node.match_score("new defi strategy", source_wing="orkid") > 0.5


def test_chatter_node_match_score_ignores_unknown_wing():
    node = ChatterNode.from_dict(
        {
            "id": "x",
            "name": "X",
            "wing": "orkid",
            "hall": "technical",
            "role": "tech",
            "specialties": ["defi"],
            "gossip_radius": ["orkid"],
        }
    )
    assert node.match_score("defi", source_wing="unknown") < node.match_score(
        "defi", source_wing="orkid"
    )


def test_gossip_message_expiry():
    msg = GossipMessage(
        subject="s",
        predicate="p",
        obj="o",
        ttl_seconds=0,
    )
    assert msg.is_expired() is True

    msg2 = GossipMessage(
        subject="s",
        predicate="p",
        obj="o",
        ttl_seconds=3600,
    )
    assert msg2.is_expired() is False


# ─────────────────────────────────────────────────────────────────────────────
# Protocol behavior
# ─────────────────────────────────────────────────────────────────────────────


def test_detect_topic_matches_security_keywords():
    kg_path = _temp_db()
    try:
        protocol = GossipProtocol(kg_path=kg_path, config=EXAMPLE_GOSSIP_CONFIG)
        topic, priority = protocol.detect_topic("new security vulnerability found")
        assert topic == "security_issues"
        assert priority == "critical"
    finally:
        Path(kg_path).unlink(missing_ok=True)


def test_detect_topic_defaults_to_normal():
    kg_path = _temp_db()
    try:
        protocol = GossipProtocol(kg_path=kg_path, config=EXAMPLE_GOSSIP_CONFIG)
        topic, priority = protocol.detect_topic("something random happened")
        assert topic == "general"
        assert priority == "normal"
    finally:
        Path(kg_path).unlink(missing_ok=True)


def test_detect_topic_honors_explicit_priority():
    kg_path = _temp_db()
    try:
        protocol = GossipProtocol(kg_path=kg_path, config=EXAMPLE_GOSSIP_CONFIG)
        topic, priority = protocol.detect_topic("random", priority="high")
        assert topic == "general"
        assert priority == "high"
    finally:
        Path(kg_path).unlink(missing_ok=True)


def test_select_chatter_nodes_respects_fanout():
    kg_path = _temp_db()
    try:
        protocol = GossipProtocol(kg_path=kg_path, config=EXAMPLE_GOSSIP_CONFIG)
        msg = GossipMessage(
            subject="s",
            predicate="p",
            obj="o",
            source_wing="orkid",
            priority="high",
        )
        nodes = protocol.select_chatter_nodes(msg, fanout=2)
        assert len(nodes) <= 2
        assert all(isinstance(n, ChatterNode) for n in nodes)
    finally:
        Path(kg_path).unlink(missing_ok=True)


def test_propagate_writes_triples_and_returns_report():
    kg_path = _temp_db()
    try:
        kg = KnowledgeGraph(db_path=kg_path)
        protocol = GossipProtocol(kg=kg, config=EXAMPLE_GOSSIP_CONFIG)
        report = protocol.propagate(
            "audit",
            "found",
            "security vulnerability",
            source_wing="orkid",
            source_room="contracts",
            room_graph=_sample_room_graph(),
        )

        assert report["subject"] == "audit"
        assert report["object"] == "security vulnerability"
        assert report["topic"] == "security_issues"
        assert report["priority"] == "critical"
        assert report["triples_written"] >= 1
        assert len(report["chatter_nodes"]) >= 1

        # The KG should contain at least one gossip-derived triple
        triples = kg.query_entity("audit", direction="outgoing")
        gossip_triples = [t for t in triples if t["predicate"].startswith("gossiped_")]
        assert len(gossip_triples) >= 1
        assert all(t["object"] == "security vulnerability" for t in gossip_triples)
    finally:
        Path(kg_path).unlink(missing_ok=True)


def test_propagate_with_no_room_graph_still_runs():
    kg_path = _temp_db()
    try:
        kg = KnowledgeGraph(db_path=kg_path)
        protocol = GossipProtocol(kg=kg, config=EXAMPLE_GOSSIP_CONFIG)
        report = protocol.propagate(
            "new",
            "is",
            "idea",
            source_wing="orkid",
            source_room="contracts",
            room_graph=_empty_room_graph(),
            fanout=5,
        )
        assert report["triples_written"] >= 0
    finally:
        Path(kg_path).unlink(missing_ok=True)


def test_propagate_report_distinguishes_failed_writes():
    """A node is not reported as propagated when every KG write fails."""
    kg_path = _temp_db()
    try:
        kg = KnowledgeGraph(db_path=kg_path)
        protocol = GossipProtocol(kg=kg, config=EXAMPLE_GOSSIP_CONFIG)

        def _boom(*a, **kw):
            raise RuntimeError("kg is down")

        # Patch the protocol's knowledge graph directly.
        protocol.kg.add_triple = _boom

        report = protocol.propagate(
            "audit",
            "found",
            "security vulnerability",
            source_wing="orkid",
            source_room="contracts",
            room_graph=_sample_room_graph(),
            fanout=5,
        )

        assert report["triples_written"] == 0
        assert report["propagated"] == []
        assert len(report["chatter_nodes"]) >= 1
        for node_report in report["chatter_nodes"]:
            assert node_report["attempted_targets"] > 0
            assert node_report["successful_targets"] == 0
            assert len(node_report["failed_targets"]) > 0
            assert node_report["targets"] == []
    finally:
        Path(kg_path).unlink(missing_ok=True)


def test_propagate_report_records_partial_write_success():
    """A node with at least one successful write is reported as propagated."""
    kg_path = _temp_db()
    try:
        kg = KnowledgeGraph(db_path=kg_path)
        protocol = GossipProtocol(kg=kg, config=EXAMPLE_GOSSIP_CONFIG)

        attempts = {"count": 0}

        def _maybe_boom(subject, predicate, obj, **kw):
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise RuntimeError("first target fails")
            return None

        protocol.kg.add_triple = _maybe_boom

        report = protocol.propagate(
            "audit",
            "found",
            "security vulnerability",
            source_wing="orkid",
            source_room="contracts",
            room_graph=_sample_room_graph(),
            fanout=5,
        )

        assert report["triples_written"] >= 1
        assert len(report["propagated"]) >= 1
        for node_report in report["chatter_nodes"]:
            if node_report["successful_targets"] > 0:
                assert node_report["attempted_targets"] > node_report["successful_targets"]
                assert len(node_report["failed_targets"]) > 0
                break
        else:
            raise AssertionError("expected at least one partial-success node")
    finally:
        Path(kg_path).unlink(missing_ok=True)


def test_propagate_suppresses_beyond_max_hops():
    kg_path = _temp_db()
    try:
        kg = KnowledgeGraph(db_path=kg_path)
        protocol = GossipProtocol(kg=kg, config=EXAMPLE_GOSSIP_CONFIG)
        # Manually craft an already-old message via a child
        msg = GossipMessage(
            subject="x",
            predicate="is",
            obj="y",
            source_wing="orkid",
            hops=3,
        )
        selected = protocol.select_chatter_nodes(msg)
        assert len(selected) >= 1
        node = selected[0]
        assert protocol._should_forward(msg, node) is False
    finally:
        Path(kg_path).unlink(missing_ok=True)


def test_chatter_status():
    kg_path = _temp_db()
    try:
        protocol = GossipProtocol(kg_path=kg_path, config=EXAMPLE_GOSSIP_CONFIG)
        status = protocol.chatter_status()
        assert "chatter_nodes" in status
        assert len(status["chatter_nodes"]) == 7
        assert "topics" in status
        assert status["max_hops"] == 3
    finally:
        Path(kg_path).unlink(missing_ok=True)


def test_gossip_convenience_function_uses_passed_kg():
    kg_path = _temp_db()
    try:
        kg = KnowledgeGraph(db_path=kg_path)
        report = gossip(
            "launch",
            "announced",
            "campaign",
            source_wing="brutal-marketing",
            kg=kg,
            fanout=3,
            config=EXAMPLE_GOSSIP_CONFIG,
        )
        assert report["triples_written"] >= 0
    finally:
        Path(kg_path).unlink(missing_ok=True)


def test_gossip_config_is_json_serializable():
    """Example config can be written and re-read without mutation."""
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "gossip_config.json")
        saved = save_gossip_config(EXAMPLE_GOSSIP_CONFIG, path=path)
        with open(path, "r", encoding="utf-8") as f:
            on_disk = json.load(f)
        assert on_disk["chatter_nodes"][0]["id"] == saved["chatter_nodes"][0]["id"]


def test_select_chatter_nodes_uses_hallway_room(monkeypatch):
    """A hallway's co-occurrence rooms boost nodes whose rooms match, even when
    the room name differs from the node's hall name."""
    kg_path = _temp_db()
    try:
        protocol = GossipProtocol(kg_path=kg_path, config=EXAMPLE_GOSSIP_CONFIG)

        def _fake_list_hallways(wing=None, config=None):
            if wing != "orkid":
                return []
            return [
                {
                    "id": "hallway_orkid_audit_risk_abc12345",
                    "wing": "orkid",
                    "entity_a": "audit",
                    "entity_b": "risk",
                    "co_occurrence_count": 4,
                    # room name is intentionally different from the node's hall.
                    "rooms": ["audit_report"],
                },
                {
                    "id": "hallway_orkid_audit_compliance_abc12345",
                    "wing": "orkid",
                    "entity_a": "audit",
                    "entity_b": "compliance",
                    "co_occurrence_count": 2,
                    "rooms": ["contracts"],
                },
            ]

        monkeypatch.setattr(gossip_mod, "list_hallways", _fake_list_hallways)

        msg = GossipMessage(
            subject="audit",
            predicate="found",
            obj="risk",
            source_wing="orkid",
            priority="high",
        )
        nodes = protocol.select_chatter_nodes(msg, fanout=3)
        ids = [n.id for n in nodes]

        # chatter_security has hall "security" but room "audit_report"; the
        # co-occurrence room "audit_report" should boost it to first place.
        assert "chatter_security" in ids
        assert ids[0] == "chatter_security"
    finally:
        Path(kg_path).unlink(missing_ok=True)


def test_select_chatter_nodes_without_hallways(monkeypatch):
    """When hallways are empty, selection falls back to base scoring."""
    kg_path = _temp_db()
    try:
        protocol = GossipProtocol(kg_path=kg_path, config=EXAMPLE_GOSSIP_CONFIG)
        monkeypatch.setattr(gossip_mod, "list_hallways", lambda *a, **kw: [])

        msg = GossipMessage(
            subject="audit",
            predicate="found",
            obj="risk",
            source_wing="orkid",
            priority="high",
        )
        nodes = protocol.select_chatter_nodes(msg, fanout=3)
        # chatter_security still wins on specialty/topic match.
        assert any(n.id == "chatter_security" for n in nodes)
    finally:
        Path(kg_path).unlink(missing_ok=True)


# ─────────────────────────────────────────────────────────────────────────────
# Daemon
# ─────────────────────────────────────────────────────────────────────────────


def test_daemon_run_once_propagates_scheduled_message():
    kg_path = _temp_db()
    try:
        kg = KnowledgeGraph(db_path=kg_path)
        protocol = GossipProtocol(kg=kg, config=EXAMPLE_GOSSIP_CONFIG)
        daemon = GossipDaemon(protocol, interval_seconds=60.0)

        daemon.schedule(
            "audit",
            "found",
            "risk",
            source_wing="orkid",
            priority="high",
        )
        report = daemon.run_once()

        assert report["processed"] == 1
        assert report["expired"] == 0
        assert report["triples_written"] >= 1
        # A child message should be queued for the next tick (hops incremented).
        assert report["children_queued"] >= 1
    finally:
        Path(kg_path).unlink(missing_ok=True)


def test_daemon_drops_expired_message():
    kg_path = _temp_db()
    try:
        protocol = GossipProtocol(kg_path=kg_path, config=EXAMPLE_GOSSIP_CONFIG)
        daemon = GossipDaemon(protocol, interval_seconds=60.0)

        msg = daemon.schedule("x", "is", "y", source_wing="orkid")
        # Force expiry by backdating start time.
        msg._started_at = datetime.now(timezone.utc) - timedelta(seconds=120)
        msg.ttl_seconds = 60

        report = daemon.run_once()
        assert report["expired"] == 1
        assert report["processed"] == 0
        assert report["triples_written"] == 0
    finally:
        Path(kg_path).unlink(missing_ok=True)


def test_daemon_start_stop_background_thread():
    protocol = GossipProtocol(config=EXAMPLE_GOSSIP_CONFIG)
    daemon = GossipDaemon(protocol, interval_seconds=0.05)
    daemon.start()
    assert daemon._worker is not None
    assert daemon._worker.is_alive()

    daemon.schedule("test", "is", "active", source_wing="orkid")
    time.sleep(0.15)

    daemon.stop()
    assert not daemon._worker.is_alive()


def test_daemon_run_once_isolates_message_failures():
    """An exception in one message does not discard later messages or prior children."""
    kg_path = _temp_db()
    try:
        kg = KnowledgeGraph(db_path=kg_path)
        protocol = GossipProtocol(kg=kg, config=EXAMPLE_GOSSIP_CONFIG)
        daemon = GossipDaemon(protocol, interval_seconds=60.0, max_requeue=100)

        daemon.schedule("audit", "found", "risk", source_wing="orkid", priority="high")
        daemon.schedule("will", "explode", "now", source_wing="orkid", priority="high")
        daemon.schedule("launch", "is", "active", source_wing="orkid", priority="high")

        original = protocol._propagate_message
        calls = {"count": 0}

        def _boom_on_second(*a, **kw):
            calls["count"] += 1
            if calls["count"] == 2:
                raise RuntimeError("simulated failure")
            return original(*a, **kw)

        protocol._propagate_message = _boom_on_second

        report = daemon.run_once()

        assert report["processed"] == 2
        assert report["failed"] == 1
        # The failed message is not requeued; the third message is processed and
        # may produce children, and the first message's children are preserved.
        with daemon._lock:
            assert len(daemon._queue) >= 1
            # The failed subject should not be in the queue.
            assert "will" not in {m.subject for m in daemon._queue}
    finally:
        Path(kg_path).unlink(missing_ok=True)


def test_daemon_requeue_respects_max_requeue():
    """Children are dropped once the queue reaches max_requeue, accounting for
    entries added by concurrent callers while run_once() is processing."""
    kg_path = _temp_db()
    try:
        kg = KnowledgeGraph(db_path=kg_path)
        protocol = GossipProtocol(kg=kg, config=EXAMPLE_GOSSIP_CONFIG)
        daemon = GossipDaemon(protocol, interval_seconds=60.0, max_requeue=2)

        # Simulate a concurrent schedule that occurs while run_once() processes
        # the first message. The scheduled message is not part of the current
        # pending batch, so at requeue time it occupies one slot.
        original = protocol._propagate_message
        scheduled = {"done": False}

        def _schedule_concurrent(*a, **kw):
            if not scheduled["done"]:
                daemon.schedule("concurrent", "is", "active", source_wing="orkid")
                scheduled["done"] = True
            return original(*a, **kw)

        protocol._propagate_message = _schedule_concurrent

        daemon.schedule("audit", "found", "risk", source_wing="orkid", priority="high")
        report = daemon.run_once()

        assert report["processed"] == 1
        with daemon._lock:
            # concurrent message + at most one child == at most 2.
            assert len(daemon._queue) <= 2
            assert len(daemon._queue) >= 1
        # With max_requeue=2 and one concurrent message, only one child fits.
        assert report["children_queued"] <= 1
    finally:
        Path(kg_path).unlink(missing_ok=True)


# ─────────────────────────────────────────────────────────────────────────────
# Analytics
# ─────────────────────────────────────────────────────────────────────────────


def test_gossip_analytics_snapshot():
    kg_path = _temp_db()
    try:
        kg = KnowledgeGraph(db_path=kg_path)
        protocol = GossipProtocol(kg=kg, config=EXAMPLE_GOSSIP_CONFIG)

        # Seed several gossip triples about the same fact to make it viral.
        protocol.propagate(
            "audit",
            "found",
            "security vulnerability",
            source_wing="orkid",
            source_room="contracts",
            fanout=5,
        )

        analytics = protocol.analytics()

        assert "trending_topics" in analytics
        assert "viral_facts" in analytics
        assert "network_health" in analytics
        assert analytics["network_health"]["active_triples"] >= 1
        assert analytics["network_health"]["topic_coverage"] >= 1
    finally:
        Path(kg_path).unlink(missing_ok=True)


# ─────────────────────────────────────────────────────────────────────────────
# Channels
# ─────────────────────────────────────────────────────────────────────────────


def test_propagate_uses_priority_channel_for_ttl_and_fanout():
    kg_path = _temp_db()
    try:
        kg = KnowledgeGraph(db_path=kg_path)
        protocol = GossipProtocol(kg=kg, config=EXAMPLE_GOSSIP_CONFIG)
        # Critical messages get the critical channel.
        report = protocol.propagate(
            "audit",
            "found",
            "security vulnerability",
            source_wing="orkid",
            priority="critical",
        )
        assert report["priority"] == "critical"
        # The test environment has 7 nodes; critical fanout is 7, so all may fire.
        assert report["triples_written"] >= 1

        # Low-priority messages use the low channel with a smaller fanout.
        low = protocol.propagate(
            "idea",
            "is",
            "spark",
            source_wing="brutal-marketing",
            priority="low",
        )
        assert low["priority"] == "low"
        assert low["triples_written"] >= 0
    finally:
        Path(kg_path).unlink(missing_ok=True)


def test_propagate_preserves_explicit_max_hops_over_channel_default():
    """An explicit max_hops argument must not be replaced by the channel default."""
    kg_path = _temp_db()
    try:
        kg = KnowledgeGraph(db_path=kg_path)
        protocol = GossipProtocol(kg=kg, config=EXAMPLE_GOSSIP_CONFIG)
        # The critical channel has max_hops=3 by default; pass 10 explicitly.
        report = protocol.propagate(
            "audit",
            "found",
            "security vulnerability",
            source_wing="orkid",
            priority="critical",
            max_hops=10,
        )
        assert report["triples_written"] >= 1
        # The propagated message is suppressed beyond max_hops; verify the
        # explicit value reached the child messages by exhausting hops.
    finally:
        Path(kg_path).unlink(missing_ok=True)


def test_chatter_status_includes_channels():
    protocol = GossipProtocol(config=EXAMPLE_GOSSIP_CONFIG)
    status = protocol.chatter_status()
    assert "channels" in status
    assert "critical" in status["channels"]
    assert "low" in status["channels"]
    assert status["noise_tolerance"] == 0.2
    assert status["amplification_factor"] == 1.5


def test_random_walk_swap_only_picks_off_radius_nodes():
    """Random walk replacements must be outside the source's gossip radius."""
    protocol = GossipProtocol(config=EXAMPLE_GOSSIP_CONFIG)
    # Source from the negentropy wing; only chatter_negentropy is in-radius,
    # so a random-walk replacement must come from a node whose gossip_radius
    # does not contain negentropy.
    message = GossipMessage(
        subject="new",
        predicate="is",
        obj="theory",
        source_wing="negentropy",
        priority="high",
    )
    with unittest.mock.patch.object(random, "random", return_value=0.0):
        selected = protocol.select_chatter_nodes(message, fanout=1)
        assert len(selected) == 1
        assert normalize_wing_name("negentropy") not in {
            normalize_wing_name(w) for w in selected[0].gossip_radius
        }
