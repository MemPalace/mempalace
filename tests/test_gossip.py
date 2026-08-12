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
    ChatterNode,
    GossipMessage,
    GossipProtocol,
    _default_gossip_path,
    gossip,
    load_gossip_config,
    save_gossip_config,
)
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
        assert len(cfg["chatter_nodes"]) == 7


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
        protocol = GossipProtocol(kg_path=kg_path)
        topic, priority = protocol.detect_topic("new security vulnerability found")
        assert topic == "security_issues"
        assert priority == "critical"
    finally:
        Path(kg_path).unlink(missing_ok=True)


def test_detect_topic_defaults_to_normal():
    kg_path = _temp_db()
    try:
        protocol = GossipProtocol(kg_path=kg_path)
        topic, priority = protocol.detect_topic("something random happened")
        assert topic == "general"
        assert priority == "normal"
    finally:
        Path(kg_path).unlink(missing_ok=True)


def test_detect_topic_honors_explicit_priority():
    kg_path = _temp_db()
    try:
        protocol = GossipProtocol(kg_path=kg_path)
        topic, priority = protocol.detect_topic("random", priority="high")
        assert topic == "general"
        assert priority == "high"
    finally:
        Path(kg_path).unlink(missing_ok=True)


def test_select_chatter_nodes_respects_fanout():
    kg_path = _temp_db()
    try:
        protocol = GossipProtocol(kg_path=kg_path)
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
        protocol = GossipProtocol(kg=kg)
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
        protocol = GossipProtocol(kg=kg)
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


def test_propagate_suppresses_beyond_max_hops():
    kg_path = _temp_db()
    try:
        kg = KnowledgeGraph(db_path=kg_path)
        protocol = GossipProtocol(kg=kg)
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
        protocol = GossipProtocol(kg_path=kg_path)
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
        )
        assert report["triples_written"] >= 0
    finally:
        Path(kg_path).unlink(missing_ok=True)


def test_gossip_config_is_json_serializable():
    """Default config can be written and re-read without mutation."""
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "gossip_config.json")
        saved = save_gossip_config(load_gossip_config(path=path), path=path)
        with open(path, "r", encoding="utf-8") as f:
            on_disk = json.load(f)
        assert on_disk["chatter_nodes"][0]["id"] == saved["chatter_nodes"][0]["id"]
