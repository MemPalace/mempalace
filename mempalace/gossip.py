"""gossip.py — Lightning-fast fact propagation across the MemPalace graph.

This is a minimal MVP for the gossip protocol described in
``MEMPALACE_GOSSIP_PROTOCOL_IMPLEMENTATION.md``. It does not run as a
background daemon; it is a library that callers invoke to propagate a newly
learned fact through a configurable set of chatter nodes and have derived
triples written back into the temporal knowledge graph.

Key abstractions:

- ``ChatterNode``: a specialized propagation agent bound to a wing/hall with
  specialties and a gossip radius.
- ``GossipMessage``: the in-flight fact with metadata (priority, TTL, hops).
- ``GossipProtocol``: the router that matches a fact to chatter nodes and
  writes derived triples into the ``KnowledgeGraph``.
"""

from __future__ import annotations

import json
import logging
import os
import random
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .config import MempalaceConfig, normalize_wing_name
from .knowledge_graph import KnowledgeGraph
from .palace_graph import build_graph as _build_graph
from .hallways import list_hallways

logger = logging.getLogger("mempalace_gossip")


# ─────────────────────────────────────────────────────────────────────────────
# Default configuration — mirrors the 2026-07-09 spec
# ─────────────────────────────────────────────────────────────────────────────

# Default configuration installed for new palaces. It is deliberately neutral
# (no project-specific wings, halls, or topics) so that enabling gossip does
# not silently inject an unrelated organizational model into the user's palace.
DEFAULT_GOSSIP_CONFIG: dict[str, Any] = {
    "version": "1.0",
    "max_hops": 3,
    "ttl_seconds": 60,
    "fanout": 5,
    "gossip_probability": 0.7,
    "initiation_probability": 0.3,
    "chatter_frequency_ms": 1000,
    "noise_tolerance": 0.2,
    "amplification_factor": 1.5,
    "chatter_nodes": [],
    "topics": [],
}

# Example configuration used by tests and documentation. It illustrates a
# populated gossip network but is never written automatically.
EXAMPLE_GOSSIP_CONFIG: dict[str, Any] = {
    "version": "1.0",
    "max_hops": 3,
    "ttl_seconds": 60,
    "fanout": 5,
    "gossip_probability": 0.7,
    "initiation_probability": 0.3,
    "chatter_frequency_ms": 1000,
    "noise_tolerance": 0.2,
    "amplification_factor": 1.5,
    "chatter_nodes": [
        {
            "id": "chatter_orkid_tech",
            "name": "Chatter Orkid Tech",
            "wing": "orkid",
            "hall": "technical",
            "role": "technical_gossip",
            "specialties": ["contracts", "backend", "defi", "trading"],
            "gossip_radius": ["orkid", "past-performance"],
            "chatter_level": "high",
            "propagation_speed": "instant",
        },
        {
            "id": "chatter_marketing",
            "name": "Chatter Marketing",
            "wing": "brutal-marketing",
            "hall": "general",
            "role": "marketing_gossip",
            "specialties": ["brand", "messaging", "content", "storytelling"],
            "gossip_radius": ["brutal-marketing", "orkid"],
            "chatter_level": "high",
            "propagation_speed": "instant",
        },
        {
            "id": "chatter_performance",
            "name": "Chatter Performance",
            "wing": "past-performance",
            "hall": "analytics",
            "role": "performance_gossip",
            "specialties": ["metrics", "validation", "optimization", "data"],
            "gossip_radius": ["past-performance", "orkid"],
            "chatter_level": "high",
            "propagation_speed": "instant",
        },
        {
            "id": "chatter_strategy",
            "name": "Chatter Strategy",
            "wing": "orkid",
            "hall": "strategy",
            "role": "strategy_gossip",
            "specialties": ["trading", "revenue", "business", "planning"],
            "gossip_radius": ["orkid", "brutal-marketing"],
            "chatter_level": "medium",
            "propagation_speed": "fast",
        },
        {
            "id": "chatter_security",
            "name": "Chatter Security",
            "wing": "orkid",
            "hall": "security",
            "role": "security_gossip",
            "specialties": ["audit", "compliance", "risk", "validation"],
            "gossip_radius": ["orkid", "brutal-marketing"],
            "chatter_level": "medium",
            "propagation_speed": "fast",
        },
        {
            "id": "chatter_creative",
            "name": "Chatter Creative",
            "wing": "brutal-marketing",
            "hall": "creative",
            "role": "creative_gossip",
            "specialties": ["design", "visual", "brand", "aesthetic"],
            "gossip_radius": ["brutal-marketing", "orkid"],
            "chatter_level": "medium",
            "propagation_speed": "fast",
        },
        {
            "id": "chatter_negentropy",
            "name": "Chatter Negentropy",
            "wing": "negentropy",
            "hall": "creative",
            "role": "theory_gossip",
            "specialties": ["information theory", "physics", "complexity", "optimization"],
            "gossip_radius": ["negentropy", "orkid"],
            "chatter_level": "low",
            "propagation_speed": "normal",
        },
    ],
    "topics": [
        {
            "name": "technical_breakthroughs",
            "priority": "critical",
            "keywords": ["breakthrough", "innovation", "discovery", "achievement"],
        },
        {
            "name": "performance_alerts",
            "priority": "high",
            "keywords": ["alert", "issue", "problem", "degradation", "failure"],
        },
        {
            "name": "marketing_campaigns",
            "priority": "high",
            "keywords": ["campaign", "launch", "announcement", "promotion", "content"],
        },
        {
            "name": "business_opportunities",
            "priority": "medium",
            "keywords": ["opportunity", "revenue", "growth", "partnership", "deal"],
        },
        {
            "name": "security_issues",
            "priority": "critical",
            "keywords": ["vulnerability", "security", "risk", "threat", "breach"],
        },
        {
            "name": "creative_inspiration",
            "priority": "low",
            "keywords": ["inspiration", "idea", "creative", "design", "concept"],
        },
    ],
    "channels": {
        "critical": {"latency_ms": 5, "reliability": 0.99, "capacity": "unlimited"},
        "high": {"latency_ms": 50, "reliability": 0.95, "capacity": "high"},
        "medium": {"latency_ms": 200, "reliability": 0.90, "capacity": "medium"},
        "low": {"latency_ms": 1000, "reliability": 0.85, "capacity": "low"},
    },
}

PRIORITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}

CHATTER_LEVEL_MULTIPLIER = {"high": 1.5, "medium": 1.0, "low": 0.5}


# ─────────────────────────────────────────────────────────────────────────────
# Persistence
# ─────────────────────────────────────────────────────────────────────────────


def _default_gossip_path(config: MempalaceConfig = None) -> str:
    """Return the default gossip config path.

    If a ``MempalaceConfig`` is provided, the file is stored beside the palace
    (same pattern as ``tunnels.json`` and ``hallways.json``). Otherwise it falls
    back to ``~/.mempalace/gossip_config.json``.
    """
    if config is not None:
        return os.path.join(os.path.dirname(config.palace_path), "gossip_config.json")
    return os.path.join(os.path.expanduser("~"), ".mempalace", "gossip_config.json")


def _merge_with_default(raw: dict[str, Any]) -> dict[str, Any]:
    """Ensure every key from the default config exists in the loaded config."""
    merged = dict(DEFAULT_GOSSIP_CONFIG)
    merged.update({k: v for k, v in raw.items() if k not in ("chatter_nodes", "topics")})
    # Replace collections wholesale rather than deep-merging; callers can edit
    # the JSON file if they want the default list removed.
    if "chatter_nodes" in raw:
        merged["chatter_nodes"] = raw["chatter_nodes"]
    if "topics" in raw:
        merged["topics"] = raw["topics"]
    if "channels" in raw:
        merged["channels"] = raw["channels"]
    return merged


def load_gossip_config(path: Optional[str] = None, config: MempalaceConfig = None) -> dict[str, Any]:
    """Load gossip configuration from ``path`` or the default location.

    If the file does not exist, the default configuration is returned and
    persisted so users can edit it by hand.
    """
    path = path or _default_gossip_path(config)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            return _merge_with_default(raw)
        except (OSError, json.JSONDecodeError):
            logger.warning("gossip: failed to load %s; using defaults", path, exc_info=True)
    return save_gossip_config(DEFAULT_GOSSIP_CONFIG, path)


def save_gossip_config(cfg: dict[str, Any], path: Optional[str] = None) -> dict[str, Any]:
    """Persist gossip configuration atomically."""
    path = path or _default_gossip_path()
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".gossip-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
        try:
            os.chmod(tmp_path, 0o600)
        except OSError:
            pass
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return cfg


# ─────────────────────────────────────────────────────────────────────────────
# Data model
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class ChatterNode:
    """A specialized gossip agent inside the palace."""

    id: str
    name: str
    wing: str
    hall: str
    role: str
    specialties: list[str] = field(default_factory=list)
    gossip_radius: list[str] = field(default_factory=list)
    chatter_level: str = "medium"
    propagation_speed: str = "normal"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ChatterNode:
        valid = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in valid})

    def match_score(self, text: str, source_wing: Optional[str] = None) -> float:
        """Score how relevant this chatter node is to a piece of text."""
        text = text.lower()
        hits = sum(1 for kw in self.specialties if kw.lower() in text)
        score = hits * 0.25
        if source_wing and normalize_wing_name(source_wing) in {
            normalize_wing_name(w) for w in self.gossip_radius
        }:
            score += 0.5
        if self.chatter_level == "high":
            score += 0.2
        elif self.chatter_level == "low":
            score -= 0.1
        return max(0.0, min(1.0, score))


@dataclass
class GossipMessage:
    """A single fact being propagated through the gossip network."""

    subject: str
    predicate: str
    obj: str
    source_wing: Optional[str] = None
    source_room: Optional[str] = None
    priority: str = "normal"
    topic: Optional[str] = None
    hops: int = 0
    max_hops: int = 3
    ttl_seconds: int = 60
    _started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def text(self) -> str:
        return f"{self.subject} {self.predicate} {self.obj}"

    def is_expired(self) -> bool:
        return (datetime.now(timezone.utc) - self._started_at).total_seconds() > self.ttl_seconds

    def child(self, wing: str, room: Optional[str] = None) -> GossipMessage:
        """Return a copy with incremented hop count."""
        return GossipMessage(
            subject=self.subject,
            predicate=self.predicate,
            obj=self.obj,
            source_wing=wing,
            source_room=room,
            priority=self.priority,
            topic=self.topic,
            hops=self.hops + 1,
            max_hops=self.max_hops,
            ttl_seconds=self.ttl_seconds,
            _started_at=self._started_at,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Gossip router
# ─────────────────────────────────────────────────────────────────────────────


class GossipProtocol:
    """Route a fact through the palace gossip network and persist derived triples."""

    def __init__(
        self,
        config_path: Optional[str] = None,
        mempalace_config: Optional[MempalaceConfig] = None,
        kg: Optional[KnowledgeGraph] = None,
        kg_path: Optional[str] = None,
        config: Optional[dict[str, Any]] = None,
    ):
        self.mempalace_config = mempalace_config
        self.config = config if config is not None else load_gossip_config(config_path, mempalace_config)
        self._chatter_nodes = [ChatterNode.from_dict(n) for n in self.config["chatter_nodes"]]
        self.kg = kg
        self._kg_path = kg_path

    @property
    def chatter_nodes(self) -> list[ChatterNode]:
        return list(self._chatter_nodes)

    def _knowledge_graph(self, kg_path: Optional[str] = None) -> KnowledgeGraph:
        if self.kg is not None:
            return self.kg
        path = kg_path or self._kg_path
        if path is not None:
            return KnowledgeGraph(db_path=path)
        return KnowledgeGraph()

    def detect_topic(self, text: str, priority: Optional[str] = None) -> tuple[str, str]:
        """Return (topic_name, priority) based on keyword matching."""
        text = text.lower()
        best_topic: Optional[dict[str, Any]] = None
        best_hits = 0
        for topic in self.config["topics"]:
            hits = sum(1 for kw in topic["keywords"] if kw.lower() in text)
            if hits > best_hits:
                best_hits = hits
                best_topic = topic
        if best_topic and best_hits > 0:
            return best_topic["name"], best_topic["priority"]
        if priority in PRIORITY_ORDER:
            return "general", priority
        return "general", "normal"

    def _get_hallway_context(
        self, message: GossipMessage
    ) -> tuple[dict[str, float], set[str]]:
        """Return (hall_scores, related_entities) derived from within-wing hallways.

        Hallways are entity-pair co-occurrence records built at mine time. When a
        gossip message mentions an entity that co-occurs with other entities in
        the source wing, we use those co-occurrences to boost chatter nodes that
        live in the same rooms/halls or cover the related entities.
        """
        if not message.source_wing:
            return {}, set()
        try:
            hallways = list_hallways(
                wing=message.source_wing, config=self.mempalace_config
            )
        except Exception:
            logger.debug("gossip: could not load hallways", exc_info=True)
            return {}, set()

        entities = {message.subject.lower(), message.obj.lower()}
        hall_scores: dict[str, float] = {}
        related: set[str] = set()

        for h in hallways:
            a = (h.get("entity_a") or "").lower()
            b = (h.get("entity_b") or "").lower()
            if a in entities or b in entities:
                other = b if a in entities else a
                related.add(other)
                count = h.get("co_occurrence_count") or 1
                for room in h.get("rooms") or []:
                    room_key = room.lower()
                    # Accumulate a small boost per co-occurrence in this room.
                    hall_scores[room_key] = hall_scores.get(room_key, 0.0) + min(
                        0.15, 0.05 + count * 0.01
                    )

        return hall_scores, related

    def select_chatter_nodes(
        self,
        message: GossipMessage,
        fanout: Optional[int] = None,
    ) -> list[ChatterNode]:
        """Rank and select chatter nodes for a given message.

        Selection combines the base specialty/topic score with within-wing
        hallway context: chatter nodes in the source wing whose hall/room
        appears in co-occurrence records for the subject/object get a boost, as
        do nodes whose specialties overlap with related entities.
        """
        fanout = fanout if fanout is not None else self.config.get("fanout", 5)
        hall_scores, related_entities = self._get_hallway_context(message)

        scored = []
        for node in self._chatter_nodes:
            score = node.match_score(message.text, message.source_wing)

            # Hallway-aware boosts (only for nodes in the source wing).
            if message.source_wing and normalize_wing_name(
                message.source_wing
            ) == normalize_wing_name(node.wing):
                if node.hall and node.hall.lower() in hall_scores:
                    score += hall_scores[node.hall.lower()]

                # Also boost if a related entity matches a specialty.
                if related_entities and node.specialties:
                    overlaps = related_entities & {
                        s.lower() for s in node.specialties
                    }
                    score += min(0.3, len(overlaps) * 0.1)

            score = max(0.0, min(2.0, score))
            if score > 0:
                scored.append((node, score))

        scored.sort(key=lambda x: x[1], reverse=True)
        return [n for n, _ in scored[:fanout]]

    def _resolve_targets(
        self,
        node: ChatterNode,
        message: GossipMessage,
        room_graph: Optional[tuple[dict, list]] = None,
    ) -> list[tuple[str, Optional[str]]]:
        """Return (wing, room) targets for a chatter node.

        If ``room_graph`` is provided (the ``(nodes, edges)`` tuple returned by
        ``palace_graph.build_graph``), the function also includes rooms that are
        connected to the source room through shared wings/halls. Otherwise only
        the node's gossip radius is used.
        """
        targets: list[tuple[str, Optional[str]]] = []
        for w in node.gossip_radius:
            targets.append((w, None))

        if room_graph is None:
            try:
                room_graph = _build_graph()
            except Exception:
                logger.debug("gossip: could not build room graph", exc_info=True)
                room_graph = ({}, [])

        nodes, edges = room_graph
        source_room = message.source_room
        if source_room and source_room in nodes:
            source_data = nodes[source_room]
            for room, data in nodes.items():
                if room == source_room:
                    continue
                shared_wings = set(source_data.get("wings", [])) & set(data.get("wings", []))
                shared_halls = set(source_data.get("halls", [])) & set(data.get("halls", []))
                if shared_wings or shared_halls:
                    for w in data.get("wings", []):
                        if normalize_wing_name(w) in {
                            normalize_wing_name(r) for r in node.gossip_radius
                        }:
                            targets.append((w, room))

        # Deduplicate while preserving order
        seen: set[tuple[str, Optional[str]]] = set()
        unique: list[tuple[str, Optional[str]]] = []
        for t in targets:
            if t not in seen:
                seen.add(t)
                unique.append(t)
        return unique

    def _should_forward(self, message: GossipMessage, node: ChatterNode) -> bool:
        """Apply gossip probability, chatter level, and TTL checks."""
        if message.is_expired():
            return False
        if message.hops >= getattr(message, "max_hops", self.config.get("max_hops", 3)):
            return False
        base = self.config.get("gossip_probability", 0.7)
        level_mult = CHATTER_LEVEL_MULTIPLIER.get(node.chatter_level, 1.0)
        probability = min(1.0, base * level_mult)
        return random.random() < probability

    def _compute_valid_to(self, message: GossipMessage) -> Optional[str]:
        """Return an ISO timestamp for the gossip TTL."""
        if message.ttl_seconds is None or message.ttl_seconds <= 0:
            return None
        end = message._started_at + timedelta(seconds=message.ttl_seconds)
        return end.strftime("%Y-%m-%dT%H:%M:%SZ")

    def propagate(
        self,
        subject: str,
        predicate: str,
        obj: str,
        source_wing: Optional[str] = None,
        source_room: Optional[str] = None,
        priority: Optional[str] = None,
        fanout: Optional[int] = None,
        max_hops: Optional[int] = None,
        room_graph: Optional[tuple[dict, list]] = None,
        kg_path: Optional[str] = None,
    ) -> dict[str, Any]:
        """Propagate a fact through the gossip network.

        Returns a report describing which chatter nodes fired, which targets
        were reached, and which triples were written to the knowledge graph.
        """
        text = f"{subject} {predicate} {obj}"
        detected_topic, detected_priority = self.detect_topic(text, priority)
        priority = priority or detected_priority

        message = GossipMessage(
            subject=subject,
            predicate=predicate,
            obj=obj,
            source_wing=source_wing,
            source_room=source_room,
            priority=priority,
            topic=detected_topic,
            ttl_seconds=self.config.get("ttl_seconds", 60),
            max_hops=max_hops if max_hops is not None else self.config.get("max_hops", 3),
        )

        selected = self.select_chatter_nodes(message, fanout=fanout)
        report: dict[str, Any] = {
            "subject": subject,
            "predicate": predicate,
            "object": obj,
            "topic": message.topic,
            "priority": message.priority,
            "chatter_nodes": [],
            "suppressed": 0,
            "propagated": [],
            "triples_written": 0,
        }

        kg = self._knowledge_graph(kg_path)
        valid_to = self._compute_valid_to(message)
        source_file = f"gossip://{message.topic}"

        for node in selected:
            if not self._should_forward(message, node):
                report["suppressed"] += 1
                continue

            targets = self._resolve_targets(node, message, room_graph=room_graph)
            if not targets:
                continue

            node_report = {
                "chatter_id": node.id,
                "chatter_name": node.name,
                "targets": [],
                "attempted_targets": 0,
                "successful_targets": 0,
                "failed_targets": [],
            }

            any_success = False
            for target_wing, target_room in targets:
                # The derived fact is the original fact plus a gossip provenance.
                # We write one triple per (wing, room) target.
                pred = f"gossiped_in_{normalize_wing_name(target_wing)}"
                if target_room:
                    pred = f"gossiped_in_room_{target_room}"

                node_report["attempted_targets"] += 1
                try:
                    kg.add_triple(
                        subject,
                        pred,
                        obj,
                        valid_from=message._started_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        valid_to=valid_to,
                        confidence=0.8,
                        source_file=source_file,
                    )
                    report["triples_written"] += 1
                    node_report["successful_targets"] += 1
                    node_report["targets"].append(
                        {"wing": target_wing, "room": target_room}
                    )
                    any_success = True
                except Exception as exc:
                    logger.debug("gossip: kg add failed for %s/%s", target_wing, target_room, exc_info=True)
                    node_report["failed_targets"].append(
                        {
                            "wing": target_wing,
                            "room": target_room,
                            "error": str(exc),
                        }
                    )

            report["chatter_nodes"].append(node_report)
            if any_success:
                report["propagated"].append(node.id)

        return report

    def chatter_status(self) -> dict[str, Any]:
        """Return a snapshot of the gossip network configuration."""
        return {
            "version": self.config.get("version", "1.0"),
            "max_hops": self.config.get("max_hops", 3),
            "ttl_seconds": self.config.get("ttl_seconds", 60),
            "fanout": self.config.get("fanout", 5),
            "chatter_nodes": [asdict(n) for n in self._chatter_nodes],
            "topics": self.config.get("topics", []),
        }


def gossip(
    subject: str,
    predicate: str,
    obj: str,
    source_wing: Optional[str] = None,
    source_room: Optional[str] = None,
    priority: Optional[str] = None,
    fanout: Optional[int] = None,
    config_path: Optional[str] = None,
    kg: Optional[KnowledgeGraph] = None,
    config: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Convenience wrapper: create a protocol and propagate a single fact."""
    protocol = GossipProtocol(config_path=config_path, kg=kg, config=config)
    return protocol.propagate(
        subject,
        predicate,
        obj,
        source_wing=source_wing,
        source_room=source_room,
        priority=priority,
        fanout=fanout,
    )
