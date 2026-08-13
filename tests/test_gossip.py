"""Tests for mempalace.gossip.

Most tests run with an explicit ``KnowledgeGraph`` pointed at a temporary
SQLite path so they do not touch the user's real palace. Room graphs are
passed as explicit tuples to avoid pulling in a Chroma/pgvector collection.
"""

import json
import os
import tempfile
from pathlib import Path

from mempalace.gossip import (
    EXAMPLE_GOSSIP_CONFIG,
    ChatterNode,
    GossipMessage,
    GossipProtocol,
    _default_gossip_path,
    gossip,
    load_gossip_config,
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
    node = ChatterNode.from_dict({
        "id": "x",
        "name": "X",
        "wing": "orkid",
        "hall": "technical",
        "role": "tech",
        "specialties": ["defi"],
        "gossip_radius": ["orkid"],
        "chatter_level": "high",
        "propagation_speed": "instant",
    })
    assert node.id == "x"
    assert node.chatter_level == "high"
    assert node.match_score("new defi strategy", source_wing="orkid") > 0.5


def test_chatter_node_match_score_ignores_unknown_wing():
    node = ChatterNode.from_dict({
        "id": "x",
        "name": "X",
        "wing": "orkid",
        "hall": "technical",
        "role": "tech",
        "specialties": ["defi"],
        "gossip_radius": ["orkid"],
    })
    assert node.match_score("defi", source_wing="unknown") < node.match_score("defi", source_wing="orkid")


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
    """A hallway matching the subject and the node's hall/room boosts selection."""
    kg_path = _temp_db()
    try:
        protocol = GossipProtocol(kg_path=kg_path)

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
                    "rooms": ["security"],
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

        # chatter_security is in hall "security" and has specialties audit/risk/compliance.
        assert "chatter_security" in ids
        assert ids[0] == "chatter_security"
    finally:
        Path(kg_path).unlink(missing_ok=True)


def test_select_chatter_nodes_without_hallways(monkeypatch):
    """When hallways are empty, selection falls back to base scoring."""
    kg_path = _temp_db()
    try:
        protocol = GossipProtocol(kg_path=kg_path)
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
