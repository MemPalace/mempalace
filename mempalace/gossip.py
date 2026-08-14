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
import threading
from collections import Counter, deque
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
    "randomness_factor": 0.3,
    "redundancy_handling": "deduplicate",
    "gossip_decay": "exponential",
    "echo_chamber_reinforcement_count": 3,
    "echo_chamber_attenuation": 0.5,
    "echo_chamber_similarity_threshold": 0.8,
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
    "randomness_factor": 0.3,
    "redundancy_handling": "deduplicate",
    "gossip_decay": "exponential",
    "echo_chamber_reinforcement_count": 3,
    "echo_chamber_attenuation": 0.5,
    "echo_chamber_similarity_threshold": 0.8,
    "gossip_on_gossip": {
        "enabled": True,
        "interval_seconds": 60,
        "trending_threshold": 0.25,
        "viral_min_count": 2,
        "max_trending": 3,
        "max_viral": 3,
        "health_hysteresis_passes": 2,
    },
    "chatter_nodes": [
        {
            "id": "chatter_orkid_tech",
            "name": "Chatter Orkid Tech",
            "wing": "orkid",
            "hall": "technical",
            "role": "technical_gossip",
            "specialties": ["contracts", "backend", "defi", "trading"],
            "rooms": ["contracts"],
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
            "rooms": ["launch_blog"],
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
            "rooms": ["audit_report"],
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
            "rooms": ["contracts"],
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
            "rooms": ["audit_report"],
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
            "rooms": ["launch_blog"],
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
            "rooms": ["launch_blog"],
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
        "critical": {
            "latency_ms": 5,
            "reliability": 0.99,
            "capacity": "unlimited",
            "ttl_seconds": 30,
            "fanout": 7,
            "gossip_probability": 0.95,
            "max_hops": 3,
        },
        "high": {
            "latency_ms": 50,
            "reliability": 0.95,
            "capacity": "high",
            "ttl_seconds": 60,
            "fanout": 5,
            "gossip_probability": 0.85,
            "max_hops": 3,
        },
        "medium": {
            "latency_ms": 200,
            "reliability": 0.90,
            "capacity": "medium",
            "ttl_seconds": 120,
            "fanout": 4,
            "gossip_probability": 0.75,
            "max_hops": 3,
        },
        "low": {
            "latency_ms": 1000,
            "reliability": 0.85,
            "capacity": "low",
            "ttl_seconds": 300,
            "fanout": 2,
            "gossip_probability": 0.60,
            "max_hops": 2,
        },
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
    if "gossip_on_gossip" in raw and isinstance(raw["gossip_on_gossip"], dict):
        merged["gossip_on_gossip"] = {
            **DEFAULT_GOSSIP_CONFIG["gossip_on_gossip"],
            **raw["gossip_on_gossip"],
        }
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
    rooms: list[str] = field(default_factory=list)
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
    channel: str = "medium"
    hops: int = 0
    max_hops: int = 3
    ttl_seconds: int = 60
    gossip_probability: Optional[float] = None
    path: list[str] = field(default_factory=list)
    _started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def text(self) -> str:
        return f"{self.subject} {self.predicate} {self.obj}"

    def is_expired(self) -> bool:
        return (datetime.now(timezone.utc) - self._started_at).total_seconds() > self.ttl_seconds

    def child(self, wing: str, room: Optional[str] = None, node_id: Optional[str] = None) -> GossipMessage:
        """Return a copy with incremented hop count and updated path."""
        new_path = list(self.path)
        if node_id:
            new_path.append(node_id)
        return GossipMessage(
            subject=self.subject,
            predicate=self.predicate,
            obj=self.obj,
            source_wing=wing,
            source_room=room,
            priority=self.priority,
            topic=self.topic,
            channel=self.channel,
            hops=self.hops + 1,
            max_hops=self.max_hops,
            ttl_seconds=self.ttl_seconds,
            gossip_probability=self.gossip_probability,
            path=new_path,
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

        # State for gossip-on-gossip health hysteresis and rate limiting.
        self._health_label: Optional[str] = None
        self._health_label_count: int = 0
        self._last_propagated_health_label: Optional[str] = None
        self._last_meta_gossip_at: Optional[datetime] = None

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
        """Return (room_scores, related_entities) derived from within-wing hallways.

        Hallways are entity-pair co-occurrence records built at mine time. When a
        gossip message mentions an entity that co-occurs with other entities in
        the source wing, we use those co-occurrences to boost chatter nodes whose
        ``rooms`` overlap with the co-occurrence rooms, as well as nodes whose
        specialties overlap with the related entities.
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
        room_scores: dict[str, float] = {}
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
                    room_scores[room_key] = room_scores.get(room_key, 0.0) + min(
                        0.15, 0.05 + count * 0.01
                    )

        return room_scores, related

    def _channel_for_priority(self, priority: str) -> str:
        """Map a message priority to a channel key."""
        mapping = {
            "critical": "critical",
            "high": "high",
            "medium": "medium",
            "low": "low",
        }
        return mapping.get(priority, "medium")

    def _channel_params(self, message: GossipMessage) -> dict[str, Any]:
        """Return channel-overridden propagation parameters."""
        channel = self.config.get("channels", {}).get(message.channel, {})
        defaults = {
            "ttl_seconds": self.config.get("ttl_seconds", 60),
            "fanout": self.config.get("fanout", 5),
            "gossip_probability": self.config.get("gossip_probability", 0.7),
            "max_hops": self.config.get("max_hops", 3),
            "randomness_factor": self.config.get("randomness_factor", 0.3),
            "noise_tolerance": self.config.get("noise_tolerance", 0.2),
        }
        if not isinstance(channel, dict):
            return defaults
        return {**defaults, **{k: v for k, v in channel.items() if k in defaults}}

    def _random_walk_swap(
        self, message: GossipMessage, selected: list[ChatterNode]
    ) -> list[ChatterNode]:
        """Occasionally replace selected nodes with random off-radius nodes."""
        params = self._channel_params(message)
        randomness = float(params.get("randomness_factor", 0.3))
        max_steps = 5

        if not message.source_wing:
            return selected

        source = normalize_wing_name(message.source_wing)
        off_radius = [
            n
            for n in self._chatter_nodes
            if n not in selected
            and source not in {normalize_wing_name(w) for w in n.gossip_radius}
        ]
        if not off_radius:
            return selected

        swapped = list(selected)
        swaps = 0
        for i in range(len(swapped)):
            if off_radius and random.random() < randomness and swaps < max_steps:
                replacement = random.choice(off_radius)
                swapped[i] = replacement
                off_radius.remove(replacement)
                swaps += 1
        return swapped

    def select_chatter_nodes(
        self,
        message: GossipMessage,
        fanout: Optional[int] = None,
    ) -> list[ChatterNode]:
        """Rank and select chatter nodes for a given message.

        Selection combines the base specialty/topic score with within-wing
        hallway context and the active channel's fanout, noise tolerance, and
        random-walk swap parameters. Nodes whose ``rooms`` overlap with the
        co-occurrence rooms or whose specialties match related entities get a
        boost before the noise tolerance filter is applied.
        """
        params = self._channel_params(message)
        fanout = (
            fanout
            if fanout is not None
            else params.get("fanout", self.config.get("fanout", 5))
        )
        noise_tolerance = float(params.get("noise_tolerance", 0.2))
        room_scores, related_entities = self._get_hallway_context(message)

        scored = []
        for node in self._chatter_nodes:
            score = node.match_score(message.text, message.source_wing)

            # Hallway-aware boosts (only for nodes in the source wing).
            if message.source_wing and normalize_wing_name(
                message.source_wing
            ) == normalize_wing_name(node.wing):
                # Boost by room-level affinity. A node's hall and the
                # co-occurrence rooms are in different namespaces, so we match
                # rooms to the node's ``rooms`` list rather than to ``hall``.
                if room_scores and node.rooms:
                    overlaps = set(room_scores.keys()) & {
                        r.lower() for r in node.rooms
                    }
                    for room in overlaps:
                        score += room_scores[room]

                # Also boost if a related entity matches a specialty.
                if related_entities and node.specialties:
                    overlaps = related_entities & {
                        s.lower() for s in node.specialties
                    }
                    score += min(0.3, len(overlaps) * 0.1)

            score = max(0.0, min(2.0, score))
            if score >= noise_tolerance:
                scored.append((node, score))
        scored.sort(key=lambda x: x[1], reverse=True)
        selected = [n for n, _ in scored[:fanout]]

        # Random walk: occasionally swap a selected node for a random one.
        selected = self._random_walk_swap(message, selected)

        return selected

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

    def _attenuate_echo_chamber(
        self, message: GossipMessage, node: ChatterNode, probability: float
    ) -> float:
        """Dampen probability when the node has already seen similar messages.

        Each path entry is either a node id or ``node_id:message_text``.  The
        similarity threshold determines whether a prior visit counts as an echo.
        """
        threshold = float(
            self.config.get("echo_chamber_similarity_threshold", 0.8)
        )
        reinforcement_count = int(
            self.config.get("echo_chamber_reinforcement_count", 3)
        )
        attenuation = float(self.config.get("echo_chamber_attenuation", 0.5))

        current_text = message.text
        current_tokens = set(current_text.lower().split())
        echo_visits = 0

        for entry in message.path or []:
            if not entry:
                continue
            if ":" in entry:
                stored_id, stored_text = entry.split(":", 1)
            else:
                stored_id = entry
                stored_text = current_text
            if stored_id != node.id:
                continue
            stored_tokens = set(stored_text.lower().split())
            union = current_tokens | stored_tokens
            if not union:
                continue
            if len(current_tokens & stored_tokens) / len(union) >= threshold:
                echo_visits += 1

        if echo_visits >= reinforcement_count:
            return 0.0
        for _ in range(echo_visits):
            probability *= attenuation
        return probability

    def _should_forward(self, message: GossipMessage, node: ChatterNode) -> bool:
        """Apply gossip probability, chatter level, amplification, TTL, and echo checks."""
        if message.is_expired():
            return False
        if message.hops >= getattr(message, "max_hops", self.config.get("max_hops", 3)):
            return False

        params = self._channel_params(message)
        base = (
            message.gossip_probability
            if message.gossip_probability is not None
            else float(params.get("gossip_probability", self.config.get("gossip_probability", 0.7)))
        )
        level_mult = CHATTER_LEVEL_MULTIPLIER.get(node.chatter_level, 1.0)
        probability = min(1.0, base * level_mult)

        # Amplification factor boosts high-priority information.
        if message.priority in {"critical", "high"}:
            amp = float(self.config.get("amplification_factor", 1.5))
            probability = min(1.0, probability * amp)

        probability = self._attenuate_echo_chamber(message, node, probability)
        return random.random() < probability

    def _compute_valid_to(self, message: GossipMessage) -> Optional[str]:
        """Return an ISO timestamp for the gossip TTL."""
        if message.ttl_seconds is None or message.ttl_seconds <= 0:
            return None
        end = message._started_at + timedelta(seconds=message.ttl_seconds)
        return end.strftime("%Y-%m-%dT%H:%M:%SZ")

    def _propagate_message(
        self,
        message: GossipMessage,
        fanout: Optional[int] = None,
        room_graph: Optional[tuple[dict, list]] = None,
        kg_path: Optional[str] = None,
    ) -> tuple[dict[str, Any], list[GossipMessage]]:
        """Propagate one message and return (report, child_messages).

        The report describes which chatter nodes fired and which triples were
        written. The child messages carry the fact forward to each (wing, room)
        target with an incremented hop count. Callers such as ``GossipDaemon``
        can re-queue children for multi-hop gossip waves.
        """
        selected = self.select_chatter_nodes(message, fanout=fanout)
        report: dict[str, Any] = {
            "subject": message.subject,
            "predicate": message.predicate,
            "object": message.obj,
            "topic": message.topic,
            "priority": message.priority,
            "chatter_nodes": [],
            "suppressed": 0,
            "propagated": [],
            "triples_written": 0,
        }
        children: list[GossipMessage] = []

        kg = self._knowledge_graph(kg_path)
        valid_to = self._compute_valid_to(message)
        # Encode the original predicate so analytics can recover the source
        # fact identity without being confused by destination predicates.
        source_file = f"gossip://{message.topic}/{message.predicate}"

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
                        message.subject,
                        pred,
                        message.obj,
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
                    children.append(message.child(target_wing, target_room, node_id=node.id))
                except Exception as exc:
                    logger.debug(
                        "gossip: kg add failed for %s/%s", target_wing, target_room, exc_info=True
                    )
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

        return report, children

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

        # Build a preliminary message to resolve the channel and its parameters.
        preliminary = GossipMessage(
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
        channel = self._channel_for_priority(priority)
        params = self.config.get("channels", {}).get(channel, {})

        # Preserve explicit caller arguments over channel defaults.
        final_max_hops = (
            max_hops
            if max_hops is not None
            else params.get("max_hops", preliminary.max_hops)
        )

        message = GossipMessage(
            subject=subject,
            predicate=predicate,
            obj=obj,
            source_wing=source_wing,
            source_room=source_room,
            priority=priority,
            topic=detected_topic,
            channel=channel,
            ttl_seconds=params.get("ttl_seconds", preliminary.ttl_seconds),
            max_hops=final_max_hops,
            gossip_probability=params.get("gossip_probability"),
        )

        report, _ = self._propagate_message(
            message, fanout=fanout, room_graph=room_graph, kg_path=kg_path
        )
        return report

    def chatter_status(self) -> dict[str, Any]:
        """Return a snapshot of the gossip network configuration."""
        return {
            "version": self.config.get("version", "1.0"),
            "max_hops": self.config.get("max_hops", 3),
            "ttl_seconds": self.config.get("ttl_seconds", 60),
            "fanout": self.config.get("fanout", 5),
            "gossip_probability": self.config.get("gossip_probability", 0.7),
            "randomness_factor": self.config.get("randomness_factor", 0.3),
            "noise_tolerance": self.config.get("noise_tolerance", 0.2),
            "amplification_factor": self.config.get("amplification_factor", 1.5),
            "echo_chamber_reinforcement_count": self.config.get(
                "echo_chamber_reinforcement_count", 3
            ),
            "echo_chamber_attenuation": self.config.get(
                "echo_chamber_attenuation", 0.5
            ),
            "echo_chamber_similarity_threshold": self.config.get(
                "echo_chamber_similarity_threshold", 0.8
            ),
            "gossip_on_gossip": self.config.get("gossip_on_gossip", {}),
            "chatter_nodes": [asdict(n) for n in self._chatter_nodes],
            "topics": self.config.get("topics", []),
            "channels": self.config.get("channels", {}),
        }

    def analytics(self, as_of: str = None) -> dict[str, Any]:
        """Return meta-gossip analytics for this protocol's knowledge graph."""
        return GossipAnalytics(self._knowledge_graph()).snapshot(
            self._chatter_nodes, self.config, as_of=as_of
        )

    def _gossip_on_gossip_config(self) -> dict[str, Any]:
        """Return the gossip-on-gossip configuration subsection."""
        return self.config.get("gossip_on_gossip", DEFAULT_GOSSIP_CONFIG["gossip_on_gossip"])

    def _network_health_label(self, health: dict[str, Any]) -> str:
        """Map network health metrics to a simple label."""
        active = health.get("active_triples", 0)
        expired = health.get("expired_triples", 0)
        if active == 0:
            return "critical" if expired > 0 else "healthy"
        ratio = expired / max(1, active)
        if ratio > 2.0:
            return "critical"
        if ratio > 1.0:
            return "degraded"
        return "healthy"

    def _hysteresis_ready(self, label: str) -> bool:
        """Return True once a new health label has persisted for the configured passes."""
        if label == self._health_label:
            self._health_label_count += 1
        else:
            self._health_label = label
            self._health_label_count = 1

        passes = int(self._gossip_on_gossip_config().get("health_hysteresis_passes", 2))
        return self._health_label_count >= passes and label != self._last_propagated_health_label

    def gossip_on_gossip(self, as_of: str = None) -> dict[str, Any]:
        """Turn the current analytics snapshot into first-class gossip messages.

        Self-referential propagation is gated by the ``gossip_on_gossip`` config
        section and by per-metric thresholds. Trending topics, viral facts, and
        network-health state are written back to the knowledge graph with
        ``source_file = "gossip://meta"`` so the next analytics pass can observe
        them too.
        """
        gog = self._gossip_on_gossip_config()
        if not gog.get("enabled", True):
            return {"meta_facts": [], "network_health_label": None, "propagated_count": 0}

        interval = float(gog.get("interval_seconds", 60))
        now = datetime.now(timezone.utc)
        if self._last_meta_gossip_at is not None:
            elapsed = (now - self._last_meta_gossip_at).total_seconds()
            if elapsed < interval:
                return {"meta_facts": [], "network_health_label": None, "propagated_count": 0}

        snapshot = self.analytics(as_of=as_of)
        gog_cfg = self._gossip_on_gossip_config()

        meta_facts: list[dict[str, Any]] = []

        # Trending topics
        trending_threshold = float(gog_cfg.get("trending_threshold", 0.25))
        max_trending = int(gog_cfg.get("max_trending", 3))
        for t in snapshot["trending_topics"][:max_trending]:
            if t.get("share", 0.0) >= trending_threshold:
                meta_facts.append(
                    {
                        "subject": "gossip://meta/topic",
                        "predicate": "is_trending",
                        "obj": t["topic"],
                        "priority": "high",
                        "source_wing": "mempalace",
                    }
                )

        # Viral facts
        viral_min_count = int(gog_cfg.get("viral_min_count", 2))
        max_viral = int(gog_cfg.get("max_viral", 3))
        for v in snapshot["viral_facts"][:max_viral]:
            if v.get("count", 0) >= viral_min_count:
                fact_key = f"{v['subject']}|{v['predicate']}|{v['object']}"
                meta_facts.append(
                    {
                        "subject": "gossip://meta/fact",
                        "predicate": "is_viral",
                        "obj": fact_key,
                        "priority": "high",
                        "source_wing": "mempalace",
                    }
                )

        # Network health (with hysteresis)
        health = snapshot["network_health"]
        health_label = self._network_health_label(health)
        health_propagated = False
        if self._hysteresis_ready(health_label):
            meta_facts.append(
                {
                    "subject": "gossip://meta/network",
                    "predicate": "health",
                    "obj": health_label,
                    "priority": "high" if health_label in {"degraded", "critical"} else "medium",
                    "source_wing": "mempalace",
                }
            )
            self._last_propagated_health_label = health_label
            health_propagated = True

        propagated = 0
        for fact in meta_facts:
            report = self.propagate(
                fact["subject"],
                fact["predicate"],
                fact["obj"],
                source_wing=fact.get("source_wing"),
                priority=fact.get("priority"),
                fanout=None,
            )
            propagated += 1 if report.get("triples_written", 0) > 0 else 0

        self._last_meta_gossip_at = now

        return {
            "meta_facts": meta_facts,
            "network_health_label": health_label,
            "network_health_propagated": health_propagated,
            "propagated_count": propagated,
        }


class GossipAnalytics:
    """Meta-gossip: analyze the gossip triple graph for trends and health."""

    def __init__(self, kg: KnowledgeGraph):
        self.kg = kg

    @staticmethod
    def _parse_topic(source_file: Optional[str]) -> Optional[str]:
        if not source_file or not source_file.startswith("gossip://"):
            return None
        # Format is "gossip://<topic>" or "gossip://<topic>/<original_predicate>".
        return source_file[9:].split("/")[0] or "unknown"

    @staticmethod
    def _parse_original_predicate(source_file: Optional[str]) -> Optional[str]:
        if not source_file or not source_file.startswith("gossip://"):
            return None
        parts = source_file[9:].split("/")
        return parts[1] if len(parts) > 1 else None

    def _active_triples(self, as_of: str = None) -> list[dict]:
        """Return gossip triples active at ``as_of`` (or now)."""
        reference = as_of or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        triples = self.kg.query_gossip_triples(as_of=reference)
        return [
            t
            for t in triples
            if t.get("valid_to") is None or t["valid_to"] > reference
        ]

    def trending_topics(
        self, n: int = 5, as_of: str = None
    ) -> list[dict[str, Any]]:
        """Return the top N topics by active gossip triple count."""
        triples = self._active_triples(as_of=as_of)
        topic_counts = Counter(
            self._parse_topic(t.get("source_file")) for t in triples
        )
        total = max(1, sum(topic_counts.values()))
        ranked = topic_counts.most_common(n)
        return [
            {
                "topic": topic,
                "count": count,
                "share": round(count / total, 3),
            }
            for topic, count in ranked
        ]

    def viral_facts(
        self, min_count: int = 2, as_of: str = None
    ) -> list[dict[str, Any]]:
        """Return facts that appear in multiple gossip triples (viral content).

        Facts are identified by the original (subject, predicate, object) so that
        spreading a fact to many wings/rooms does not fragment its identity.
        The count is the total number of derived triples, and ``destinations``
        is the number of distinct gossiped-in destinations.
        """
        triples = self._active_triples(as_of=as_of)
        FactKey = tuple[str, str, str]
        fact_counts: dict[FactKey, int] = Counter()
        fact_dests: dict[FactKey, set[str]] = {}
        for t in triples:
            original_pred = self._parse_original_predicate(t.get("source_file")) or t["predicate"]
            fact = (t["subject"], original_pred, t["object"])
            fact_counts[fact] += 1
            fact_dests.setdefault(fact, set()).add(t["predicate"])

        viral = [
            {
                "subject": fact[0],
                "predicate": fact[1],
                "object": fact[2],
                "count": count,
                "destinations": len(fact_dests.get(fact, set())),
            }
            for fact, count in fact_counts.items()
            if count >= min_count
        ]
        return sorted(viral, key=lambda x: x["count"], reverse=True)

    def network_health(
        self, chatter_nodes: list[ChatterNode], config: dict[str, Any], as_of: str = None
    ) -> dict[str, Any]:
        """Return gossip network health metrics as of one reference timestamp."""
        reference = as_of or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        triples = self._active_triples(as_of=reference)
        total = len(triples)
        expired = [
            t
            for t in self.kg.query_gossip_triples()
            if t.get("valid_to") is not None
            and t["valid_to"] <= reference
            and t.get("valid_from") is not None
            and t["valid_from"] <= reference
        ]

        by_topic = Counter(
            self._parse_topic(t.get("source_file")) for t in triples
        )

        return {
            "active_triples": total,
            "expired_triples": len(expired),
            "chatter_nodes": len(chatter_nodes),
            "fanout": config.get("fanout", 5),
            "ttl_seconds": config.get("ttl_seconds", 60),
            "topic_coverage": len(by_topic),
            "topics": dict(by_topic.most_common()),
        }

    def snapshot(
        self,
        chatter_nodes: list[ChatterNode],
        config: dict[str, Any],
        as_of: str = None,
    ) -> dict[str, Any]:
        """Return a single analytics snapshot."""
        return {
            "trending_topics": self.trending_topics(as_of=as_of),
            "viral_facts": self.viral_facts(as_of=as_of),
            "network_health": self.network_health(
                chatter_nodes, config, as_of=as_of
            ),
        }


class GossipDaemon:
    """Background daemon for multi-hop gossip propagation and TTL cleanup.

    The daemon holds a queue of ``GossipMessage`` objects. Each ``run_once``
    call drains the queue, propagates each active message through the protocol,
    and enqueues the resulting child messages so the next tick can continue the
    wave. Messages that have expired or exceeded ``max_hops`` are dropped.

    ``start`` spawns a background thread; ``stop`` signals it to exit cleanly.
    """

    def __init__(
        self,
        protocol: GossipProtocol,
        interval_seconds: float = 30.0,
        max_requeue: int = 1000,
    ):
        self.protocol = protocol
        self.interval_seconds = max(1.0, float(interval_seconds))
        self.max_requeue = max_requeue
        self._queue: deque[GossipMessage] = deque()
        self._stop_event = threading.Event()
        self._worker: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def schedule(
        self,
        subject: str,
        predicate: str,
        obj: str,
        source_wing: Optional[str] = None,
        source_room: Optional[str] = None,
        priority: Optional[str] = None,
        max_hops: Optional[int] = None,
    ) -> GossipMessage:
        """Create a gossip message and add it to the daemon queue."""
        text = f"{subject} {predicate} {obj}"
        detected_topic, detected_priority = self.protocol.detect_topic(text, priority)
        priority = priority or detected_priority

        message = GossipMessage(
            subject=subject,
            predicate=predicate,
            obj=obj,
            source_wing=source_wing,
            source_room=source_room,
            priority=priority,
            topic=detected_topic,
            ttl_seconds=self.protocol.config.get("ttl_seconds", 60),
            max_hops=max_hops
            if max_hops is not None
            else self.protocol.config.get("max_hops", 3),
        )
        with self._lock:
            self._queue.append(message)
        return message

    def run_once(
        self,
        fanout: Optional[int] = None,
        room_graph: Optional[tuple[dict, list]] = None,
        kg_path: Optional[str] = None,
    ) -> dict[str, Any]:
        """Process the current queue once and return a summary report.

        Exceptions during a single message are isolated so one bad message does
        not discard unprocessed siblings or already-produced children.
        """
        with self._lock:
            pending = list(self._queue)
            self._queue.clear()

        report: dict[str, Any] = {
            "processed": 0,
            "suppressed": 0,
            "expired": 0,
            "triples_written": 0,
            "children_queued": 0,
            "failed": 0,
        }
        children: list[GossipMessage] = []

        for message in pending:
            if message.is_expired():
                report["expired"] += 1
                continue
            if message.hops >= message.max_hops:
                report["suppressed"] += 1
                continue

            try:
                node_report, child_messages = self.protocol._propagate_message(
                    message,
                    fanout=fanout,
                    room_graph=room_graph,
                    kg_path=kg_path,
                )
            except Exception:
                logger.warning(
                    "gossip-daemon: message %s failed; moving to retry and continuing",
                    message.subject,
                    exc_info=True,
                )
                report["failed"] += 1
                # Isolated failure: do not requeue the failed message, do not
                # discard children already produced or pending messages.
                continue

            report["processed"] += 1
            report["triples_written"] += node_report["triples_written"]
            children.extend(child_messages)

        # Enqueue children while respecting the cap, accounting for any messages
        # that concurrent schedulers may have added.
        with self._lock:
            available = max(0, self.max_requeue - len(self._queue))
            kept = [
                c
                for c in children
                if not c.is_expired() and c.hops < c.max_hops
            ][:available]
            for c in kept:
                self._queue.append(c)
        report["children_queued"] = len(kept)

        return report

    def _loop(self) -> None:
        """Background thread entry point."""
        while not self._stop_event.is_set():
            try:
                self.run_once()
            except Exception:
                logger.warning("gossip-daemon: run_once failed", exc_info=True)
            self._stop_event.wait(self.interval_seconds)

    def start(self) -> None:
        """Start the background thread if it is not already running."""
        if self._worker is not None and self._worker.is_alive():
            return
        self._stop_event.clear()
        self._worker = threading.Thread(target=self._loop, daemon=True)
        self._worker.start()

    def stop(self) -> None:
        """Signal the background thread to stop and wait for it."""
        self._stop_event.set()
        if self._worker is not None and self._worker.is_alive():
            self._worker.join(timeout=2.0)


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
