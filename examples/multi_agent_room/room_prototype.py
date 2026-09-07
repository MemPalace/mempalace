"""
room_prototype.py — Autonomous Multi-Agent Room Prototype for MemPalace
========================================================================

Demonstrates decentralized, non-mechanical agent participation in a shared room
over an isolated MemPalace Logstream database.

Participation Modes:
1. **debate**: Autonomous gating with urgency-weighted jitter backoff and
   pre-flight cancellation. Best for adversarial brainstorming.
2. **round_robin**: Strict turn-taking — each agent speaks once per round
   in declaration order. No gating, no jitter. Best for standups and reviews.
3. **sequential**: One designated speaker posts; others listen unless
   explicitly addressed. Best for briefings and demos.
4. **broadcast**: Fire-and-forget — any agent posts at any time. No floor
   control. Best for telemetry and event logging.

Execution Strategies:
- **Strategy A (async)**: Workers are coroutines on the host event loop.
  Yields control via ``asyncio.sleep`` — safe for desktop/GUI apps.
- **Strategy B (threaded)**: Workers are daemon threads. Original behaviour
  for headless servers and CLI tools.

Key Mechanisms (debate mode):
1. Complete Logstream Isolation: Operates against a dedicated sandbox database.
2. Autonomous Gating Protocol: Agents evaluate Relevance, Novelty, and Urgency (1-5).
   - If below threshold -> PASS (0 wire traffic).
3. Urgency-Weighted Jitter Backoff:
   Delay = (T_base / Urgency) + jitter(0, max_jitter)
   High urgency points (fatal bug, breaking catch) cut in within ~0.2s.
   Lower urgency thoughts pause for 0.8 - 2.0s.
4. Pre-Flight Collision Cancellation:
   If a peer posts during an agent's backoff window and addresses or changes the topic,
   the agent aborts its post and emits a PREEMPTED_PASS.
"""

import asyncio
import logging
import random
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional

# Add MemPalace repo to sys.path
repo_root = Path(__file__).resolve().parent.parent.parent
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

from mempalace.logstream import Logstream, LOGSTREAM_DB_FILENAME  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(threadName)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("room_prototype")


@dataclass
class GatingResult:
    action: str  # "SPEAK" or "PASS"
    urgency: int  # 1 to 5
    relevance: float  # 0.0 to 1.0
    novelty: float  # 0.0 to 1.0
    rationale: str
    proposed_body: Optional[str] = None


@dataclass
class TelemetryEntry:
    timestamp: str
    agent_id: str
    action: str  # "SPEAK", "PASS", "PREEMPTED_PASS"
    urgency: int
    delay_s: float
    rationale: str
    event_id: Optional[str] = None
    body_preview: Optional[str] = None


class ParticipationMode(Enum):
    """Turn-taking strategy for a room session."""

    DEBATE = "debate"  # autonomous gating + jitter backoff
    ROUND_ROBIN = "round_robin"  # strict sequential turns
    SEQUENTIAL = "sequential"  # single speaker, others listen
    BROADCAST = "broadcast"  # fire-and-forget, no floor control


@dataclass
class RoomConfig:
    """Configuration for a room session."""

    mode: ParticipationMode = ParticipationMode.DEBATE
    speaker_agent: Optional[str] = None  # for sequential mode
    t_base_s: float = 0.8
    idle_timeout_s: float = 3.0
    max_duration_s: float = 12.0


class Persona:
    """Defines an agent's domain concerns and reasoning heuristic."""

    def __init__(self, agent_id: str, role_name: str, domain_keywords: List[str]):
        self.agent_id = agent_id
        self.role_name = role_name
        self.domain_keywords = domain_keywords

    def evaluate(self, room_history: List[Dict], pending_events: List[Dict]) -> GatingResult:
        """Evaluate whether to speak or pass given new room events."""
        raise NotImplementedError


class RustArchitectPersona(Persona):
    def __init__(self):
        super().__init__(
            agent_id="windows:antigravity:rust-architect",
            role_name="Rust Systems Architect",
            domain_keywords=[
                "memory",
                "simd",
                "cache",
                "rayon",
                "inverted_index",
                "zero_copy",
                "rayon",
                "mmap",
            ],
        )
        self._posted_stage = 0

    def evaluate(self, room_history: List[Dict], pending_events: List[Dict]) -> GatingResult:
        latest_event = (
            pending_events[-1] if pending_events else (room_history[-1] if room_history else {})
        )
        latest_body = latest_event.get("body", "")

        # Stage 0: Initial take on inverted index layout
        if self._posted_stage == 0:
            self._posted_stage = 1
            return GatingResult(
                action="SPEAK",
                urgency=4,
                relevance=0.95,
                novelty=0.9,
                rationale="Opening technical proposal on compact contiguous posting lists in crates/mempalace-core.",
                proposed_body=(
                    "For the in-core inverted index in `mempalace-core`, we should avoid naive hash-map structures. "
                    "We can store posting lists in a single contiguous `Vec<u32>` buffer using Elias-Fano or delta-Varint "
                    "encoding to fit millions of posting pairs into L2/L3 cache. During hybrid search, Rayon can parallelize "
                    "both dense cosine SIMD dot-products and sparse posting list intersections over the same chunked buffer, "
                    "achieving sub-10ms hybrid latency without touching SQLite during query hot-loops."
                ),
            )

        # Stage 1: If someone raises concurrency / lock contention or tokenizer parity
        if any(
            k in latest_body.lower()
            for k in ["lock", "concurren", "read-only", "tokenizer", "unicode"]
        ):
            if self._posted_stage == 1:
                self._posted_stage = 2
                return GatingResult(
                    action="SPEAK",
                    urgency=4,
                    relevance=0.9,
                    novelty=0.85,
                    rationale="Resolving tokenizer and lock contention: propose shared unicode folding and ArcSwap for atomic index swaps.",
                    proposed_body=(
                        "Agreed on tokenizer parity: we will expose a shared Unicode-folding normalization function "
                        "in `mempalace-core` that both Rust and Python call, ensuring exact index parity. Furthermore, "
                        "to prevent read-write lock contention during parallel search when new drawers are filed, "
                        "we use an `arc-swap` pattern. Searches acquire an `Arc<InvertedIndex>` snapshot that is 100% "
                        "lock-free and never blocks on incoming writes. Background file mining builds a new delta slice "
                        "asynchronously and atomically swaps the pointer via CAS."
                    ),
                )

        # If latest discussion is pure packaging / python-only without systems impact
        if (
            "wheel" in latest_body.lower()
            and "dx" in latest_body.lower()
            and "abi3" in latest_body.lower()
        ):
            return GatingResult(
                action="PASS",
                urgency=1,
                relevance=0.2,
                novelty=0.1,
                rationale="Packaging and DX constraints belong to Python pragmatist; systems view has no conflict.",
            )

        # Otherwise consensus reached
        return GatingResult(
            action="PASS",
            urgency=1,
            relevance=0.3,
            novelty=0.1,
            rationale="Architecture is clear and uncontested; passing to prevent chatter.",
        )


class PythonPragmatistPersona(Persona):
    def __init__(self):
        super().__init__(
            agent_id="windows:antigravity:python-pragmatist",
            role_name="Python Platform Pragmatist",
            domain_keywords=[
                "packaging",
                "wheels",
                "abi3",
                "tokenizer",
                "unicode",
                "dx",
                "fallback",
            ],
        )
        self._posted_stage = 0

    def evaluate(self, room_history: List[Dict], pending_events: List[Dict]) -> GatingResult:
        latest_event = (
            pending_events[-1] if pending_events else (room_history[-1] if room_history else {})
        )
        latest_body = latest_event.get("body", "")

        # Stage 0: React to inverted index proposal with tokenizer constraint
        if self._posted_stage == 0 and "elias-fano" in latest_body.lower():
            self._posted_stage = 1
            return GatingResult(
                action="SPEAK",
                urgency=4,
                relevance=0.9,
                novelty=0.9,
                rationale="Challenging inverted index design on tokenizer parity: must avoid tokenization drift.",
                proposed_body=(
                    "The contiguous posting list design is promising, but we must enforce strict tokenizer parity. "
                    "If `mempalace-core` builds its own ad-hoc word splitter in Rust while Python fallback uses SQLite FTS5's "
                    "unicode61 tokenizer, the search results will diverge between machines with native wheels and machines on fallback. "
                    "We need an explicit Unicode whitespace/punctuation folding contract or share the exact same FTS5 token stream "
                    "directly from the database during initial indexing."
                ),
            )

        # Stage 1: Evaluate RCU / atomic swap proposal
        if "arc-swap" in latest_body.lower() or "rcu" in latest_body.lower():
            if self._posted_stage == 1:
                self._posted_stage = 2
                return GatingResult(
                    action="SPEAK",
                    urgency=3,
                    relevance=0.85,
                    novelty=0.8,
                    rationale="Supporting atomic swap: confirms it integrates cleanly with Python multi-threading.",
                    proposed_body=(
                        "ArcSwap for lock-free reader snapshots is exactly the right bridge for Python. When PyO3 exposes "
                        "search methods, releasing the GIL (`py.allow_threads`) over an immutable Arc pointer means concurrent "
                        "FastAPI or MCP worker threads won't stall each other or the Python runtime. As long as index rebuilding "
                        "happens off the main thread, the developer experience remains instantaneous."
                    ),
                )

        # Otherwise pass
        return GatingResult(
            action="PASS",
            urgency=1,
            relevance=0.2,
            novelty=0.1,
            rationale="No new packaging or developer experience blockers; passing.",
        )


class CoordinationMeshPersona(Persona):
    def __init__(self):
        super().__init__(
            agent_id="windows:antigravity:coordination-mesh",
            role_name="Coordination Mesh Specialist",
            domain_keywords=[
                "mesh",
                "ipc",
                "sqlite",
                "wal",
                "concurrency",
                "multi-process",
                "replication",
            ],
        )
        self._posted_stage = 0

    def evaluate(self, room_history: List[Dict], pending_events: List[Dict]) -> GatingResult:
        latest_event = (
            pending_events[-1] if pending_events else (room_history[-1] if room_history else {})
        )
        latest_body = latest_event.get("body", "")

        # Stage 0: If someone mentions posting lists or initial layout, raise multi-process challenge
        if (
            "elias-fano" in latest_body.lower() or "posting" in latest_body.lower()
        ) and self._posted_stage == 0:
            self._posted_stage = 1
            # Lower urgency (2) so it exercises jitter delay and tests potential pre-emption
            return GatingResult(
                action="SPEAK",
                urgency=2,
                relevance=0.85,
                novelty=0.85,
                rationale="Raising multi-process cache invalidation dilemma across agent processes.",
                proposed_body=(
                    "How does in-memory index invalidation cross process boundaries? If Agent A running in Cursor "
                    "adds a drawer via CLI, its SQLite WAL write commits, but Agent B's in-memory cache in another "
                    "process has no idea the database changed unless it polls SQLite or receives an IPC signal."
                ),
            )

        # Stage 1: Responding to atomic swap and IPC
        if (
            "arc-swap" in latest_body.lower() or "rcu" in latest_body.lower()
        ) and self._posted_stage == 1:
            self._posted_stage = 2
            # Urgency 5: Decisive architectural solution
            return GatingResult(
                action="SPEAK",
                urgency=5,
                relevance=0.95,
                novelty=0.9,
                rationale="Decisive architectural solution: SQLite `data_version` pragma eliminates IPC overhead.",
                proposed_body=(
                    "We can solve multi-process cache invalidation with zero IPC overhead: SQLite provides `PRAGMA data_version`. "
                    "Querying `PRAGMA data_version` is an instantaneous memory-page check in SQLite that increments whenever *any* "
                    "external process commits a write. Before executing a hybrid search, `mempalace-core` checks `data_version` in ~0.05ms. "
                    "If unchanged, it reuses the existing ArcSwap snapshot. If changed, it rebuilds the delta slice. 100% reliable across processes."
                ),
            )

        # Pass if already addressed
        return GatingResult(
            action="PASS",
            urgency=1,
            relevance=0.2,
            novelty=0.1,
            rationale="Data versioning cleanly solves cross-process sync; passing.",
        )


# ---------------------------------------------------------------------------
# Non-adversarial personas — for round_robin, sequential, and broadcast modes.
# These agents share information without arguing. They do not evaluate
# relevance/novelty/urgency — they simply speak when it is their turn.
# ---------------------------------------------------------------------------


class StatusReporterPersona(Persona):
    """A non-adversarial agent that reports status in round-robin or sequential mode."""

    def __init__(self, agent_id: str, role_name: str, status_message: str):
        super().__init__(
            agent_id=agent_id,
            role_name=role_name,
            domain_keywords=[],
        )
        self._status_message = status_message
        self._has_spoken = False

    def evaluate(self, room_history: List[Dict], pending_events: List[Dict]) -> GatingResult:
        if not self._has_spoken:
            self._has_spoken = True
            return GatingResult(
                action="SPEAK",
                urgency=3,
                relevance=1.0,
                novelty=1.0,
                rationale=f"Status report from {self.role_name}.",
                proposed_body=self._status_message,
            )
        return GatingResult(
            action="PASS",
            urgency=1,
            relevance=0.1,
            novelty=0.0,
            rationale="Already reported; listening to others.",
        )


class BriefingSpeakerPersona(Persona):
    """A single designated speaker for sequential mode briefings."""

    def __init__(self, agent_id: str, role_name: str, briefing_points: List[str]):
        super().__init__(
            agent_id=agent_id,
            role_name=role_name,
            domain_keywords=[],
        )
        self._briefing_points = briefing_points
        self._point_index = 0

    def evaluate(self, room_history: List[Dict], pending_events: List[Dict]) -> GatingResult:
        if self._point_index < len(self._briefing_points):
            point = self._briefing_points[self._point_index]
            self._point_index += 1
            return GatingResult(
                action="SPEAK",
                urgency=3,
                relevance=1.0,
                novelty=1.0,
                rationale=f"Briefing point {self._point_index}/{len(self._briefing_points)}.",
                proposed_body=point,
            )
        return GatingResult(
            action="PASS",
            urgency=1,
            relevance=0.1,
            novelty=0.0,
            rationale="Briefing complete; listening for questions.",
        )


class AutonomousParticipant(threading.Thread):
    """An autonomous agent worker tailing the room and speaking selectively."""

    def __init__(
        self,
        persona: Persona,
        logstream: Logstream,
        stream: str,
        room: str,
        topic: str,
        correlation_id: str,
        telemetry: List[TelemetryEntry],
        telemetry_lock: threading.Lock,
        t_base_s: float = 0.8,
    ):
        super().__init__(name=persona.role_name, daemon=True)
        self.persona = persona
        self.logstream = logstream
        self.stream = stream
        self.room = room
        self.topic = topic
        self.correlation_id = correlation_id
        self.telemetry = telemetry
        self.telemetry_lock = telemetry_lock
        self.t_base_s = t_base_s
        self.cursor: Optional[str] = None
        self.running = True

    def run(self):
        logger.info(
            f"[{self.persona.role_name}] Worker started. Listening on {self.stream}/{self.room}..."
        )

        while self.running:
            # 1. Fetch new room events since cursor
            events = self.logstream.list_events(
                stream=self.stream,
                room=self.room,
                since_event_id=self.cursor,
                order="asc",
                limit=50,
            )

            if not events:
                time.sleep(0.1)
                continue

            # Update cursor to latest
            self.cursor = events[-1]["id"]

            # Filter out own events
            peer_events = [e for e in events if e.get("from_agent") != self.persona.agent_id]
            if not peer_events:
                continue

            # 2. Evaluate Participation Gate
            all_history = self.logstream.list_events(
                stream=self.stream,
                room=self.room,
                order="asc",
                limit=100,
            )

            gate = self.persona.evaluate(all_history, peer_events)

            if gate.action == "PASS":
                with self.telemetry_lock:
                    self.telemetry.append(
                        TelemetryEntry(
                            timestamp=datetime.now(timezone.utc).isoformat(),
                            agent_id=self.persona.agent_id,
                            action="PASS",
                            urgency=gate.urgency,
                            delay_s=0.0,
                            rationale=gate.rationale,
                        )
                    )
                logger.info(
                    f"[{self.persona.role_name}] DECISION: PASS. Rationale: {gate.rationale}"
                )
                continue

            # 3. Urgency-Weighted Jitter Floor Control
            jitter = random.uniform(0.05, 0.25)
            delay = (self.t_base_s / max(gate.urgency, 1)) + jitter
            logger.info(
                f"[{self.persona.role_name}] DECISION: QUEUED TO SPEAK. Urgency: {gate.urgency}/5, Backoff Delay: {delay:.2f}s"
            )

            # Backoff window with pre-flight monitoring
            sleep_start = time.time()
            preempted = False
            preempting_event = None

            while time.time() - sleep_start < delay:
                time.sleep(0.05)
                # Check if someone else spoke during our backoff
                intervening = self.logstream.list_events(
                    stream=self.stream,
                    room=self.room,
                    since_event_id=self.cursor,
                    order="asc",
                )
                if intervening:
                    new_peer_evts = [
                        e for e in intervening if e.get("from_agent") != self.persona.agent_id
                    ]
                    if new_peer_evts:
                        # Pre-flight check: Re-evaluate whether peer's post answered or preempted us
                        self.cursor = intervening[-1]["id"]
                        re_eval = self.persona.evaluate(
                            self.logstream.list_events(
                                stream=self.stream, room=self.room, order="asc"
                            ),
                            new_peer_evts,
                        )
                        if re_eval.action == "PASS":
                            preempted = True
                            preempting_event = new_peer_evts[-1]["id"]
                            logger.info(
                                f"[{self.persona.role_name}] PRE-FLIGHT CANCELLATION! Preempted by {preempting_event}. Aborting post."
                            )
                            break

            if preempted:
                with self.telemetry_lock:
                    self.telemetry.append(
                        TelemetryEntry(
                            timestamp=datetime.now(timezone.utc).isoformat(),
                            agent_id=self.persona.agent_id,
                            action="PREEMPTED_PASS",
                            urgency=gate.urgency,
                            delay_s=delay,
                            rationale=f"Preempted during backoff by peer event {preempting_event}",
                        )
                    )
                continue

            # 4. Speak: Commit to Logstream
            evt = self.logstream.append_event(
                type="room.message",
                stream=self.stream,
                room=self.room,
                topic=self.topic,
                from_agent=self.persona.agent_id,
                to_agent="*",
                correlation_id=self.correlation_id,
                status="open",
                body=gate.proposed_body,
            )
            self.cursor = evt["id"]

            with self.telemetry_lock:
                self.telemetry.append(
                    TelemetryEntry(
                        timestamp=datetime.now(timezone.utc).isoformat(),
                        agent_id=self.persona.agent_id,
                        action="SPEAK",
                        urgency=gate.urgency,
                        delay_s=delay,
                        rationale=gate.rationale,
                        event_id=evt["id"],
                        body_preview=gate.proposed_body[:80] + "...",
                    )
                )

            logger.info(f"[{self.persona.role_name}] SPOKE: {evt['id']} (delay was {delay:.2f}s)")
            # Post-speech pause to allow others to react
            time.sleep(0.3)


# ---------------------------------------------------------------------------
# Strategy A — Async worker for desktop/GUI compatibility.
# Uses asyncio.sleep instead of time.sleep so the host event loop stays
# responsive. Safe to run inside Antigravity, Cursor, or any async framework.
# ---------------------------------------------------------------------------


class AsyncAutonomousParticipant:
    """Async coroutine worker — yields to the host event loop between polls."""

    def __init__(
        self,
        persona: Persona,
        logstream: Logstream,
        stream: str,
        room: str,
        topic: str,
        correlation_id: str,
        telemetry: List[TelemetryEntry],
        telemetry_lock: asyncio.Lock,
        config: RoomConfig,
        turn_order: Optional[List[str]] = None,
    ):
        self.persona = persona
        self.logstream = logstream
        self.stream = stream
        self.room = room
        self.topic = topic
        self.correlation_id = correlation_id
        self.telemetry = telemetry
        self.telemetry_lock = telemetry_lock
        self.config = config
        self.turn_order = turn_order or []
        self.cursor: Optional[str] = None
        self.running = True

    async def _record_telemetry(self, entry: TelemetryEntry) -> None:
        async with self.telemetry_lock:
            self.telemetry.append(entry)

    async def _speak(self, gate: GatingResult, delay: float) -> None:
        evt = self.logstream.append_event(
            type="room.message",
            stream=self.stream,
            room=self.room,
            topic=self.topic,
            from_agent=self.persona.agent_id,
            to_agent="*",
            correlation_id=self.correlation_id,
            status="open",
            body=gate.proposed_body,
        )
        self.cursor = evt["id"]
        await self._record_telemetry(
            TelemetryEntry(
                timestamp=datetime.now(timezone.utc).isoformat(),
                agent_id=self.persona.agent_id,
                action="SPEAK",
                urgency=gate.urgency,
                delay_s=delay,
                rationale=gate.rationale,
                event_id=evt["id"],
                body_preview=(gate.proposed_body or "")[:80] + "...",
            )
        )
        logger.info(f"[{self.persona.role_name}] SPOKE: {evt['id']} (delay was {delay:.2f}s)")

    async def run_debate(self) -> None:
        """Debate mode: autonomous gating + urgency-weighted jitter backoff."""
        logger.info(
            f"[{self.persona.role_name}] Async debate worker started on {self.stream}/{self.room}"
        )
        while self.running:
            events = self.logstream.list_events(
                stream=self.stream,
                room=self.room,
                since_event_id=self.cursor,
                order="asc",
                limit=50,
            )
            if not events:
                await asyncio.sleep(0.1)
                continue
            self.cursor = events[-1]["id"]
            peer_events = [e for e in events if e.get("from_agent") != self.persona.agent_id]
            if not peer_events:
                continue

            all_history = self.logstream.list_events(
                stream=self.stream,
                room=self.room,
                order="asc",
                limit=100,
            )
            gate = self.persona.evaluate(all_history, peer_events)

            if gate.action == "PASS":
                await self._record_telemetry(
                    TelemetryEntry(
                        timestamp=datetime.now(timezone.utc).isoformat(),
                        agent_id=self.persona.agent_id,
                        action="PASS",
                        urgency=gate.urgency,
                        delay_s=0.0,
                        rationale=gate.rationale,
                    )
                )
                continue

            jitter = random.uniform(0.05, 0.25)
            delay = (self.config.t_base_s / max(gate.urgency, 1)) + jitter
            logger.info(
                f"[{self.persona.role_name}] QUEUED (urgency {gate.urgency}, delay {delay:.2f}s)"
            )

            # Backoff with pre-flight monitoring — yields to event loop
            sleep_start = time.monotonic()
            preempted = False
            while time.monotonic() - sleep_start < delay:
                await asyncio.sleep(0.05)
                intervening = self.logstream.list_events(
                    stream=self.stream,
                    room=self.room,
                    since_event_id=self.cursor,
                    order="asc",
                )
                if intervening:
                    new_peers = [
                        e for e in intervening if e.get("from_agent") != self.persona.agent_id
                    ]
                    if new_peers:
                        self.cursor = intervening[-1]["id"]
                        re_eval = self.persona.evaluate(
                            self.logstream.list_events(
                                stream=self.stream, room=self.room, order="asc"
                            ),
                            new_peers,
                        )
                        if re_eval.action == "PASS":
                            preempted = True
                            await self._record_telemetry(
                                TelemetryEntry(
                                    timestamp=datetime.now(timezone.utc).isoformat(),
                                    agent_id=self.persona.agent_id,
                                    action="PREEMPTED_PASS",
                                    urgency=gate.urgency,
                                    delay_s=delay,
                                    rationale=f"Preempted by peer {new_peers[-1]['id']}",
                                )
                            )
                            break

            if not preempted:
                await self._speak(gate, delay)
                await asyncio.sleep(0.3)

    async def run_round_robin(self) -> None:
        """Round-robin mode: speak only when it is my turn in the rotation."""
        logger.info(f"[{self.persona.role_name}] Async round-robin worker started")
        my_turn_index = None
        for i, aid in enumerate(self.turn_order):
            if aid == self.persona.agent_id:
                my_turn_index = i
                break
        if my_turn_index is None:
            logger.warning(f"[{self.persona.role_name}] Not in turn order; idle")
            return

        rounds_completed = 0
        max_rounds = 3  # safety limit
        while self.running and rounds_completed < max_rounds:
            events = self.logstream.list_events(
                stream=self.stream,
                room=self.room,
                since_event_id=self.cursor,
                order="asc",
                limit=50,
            )
            if not events:
                await asyncio.sleep(0.1)
                continue

            self.cursor = events[-1]["id"]
            # Count how many peer speeches have occurred
            peer_speeches = [
                e
                for e in events
                if e.get("from_agent") != self.persona.agent_id
                and e.get("from_agent") in self.turn_order
                and e.get("type") == "room.message"
            ]
            # It's my turn when peer_speeches count matches my position in the rotation
            expected_peers_before_me = my_turn_index
            if (
                len(peer_speeches) >= expected_peers_before_me
                and len(peer_speeches) < expected_peers_before_me + 1
            ):
                # My turn — evaluate (non-adversarial personas always SPEAK on first turn)
                gate = self.persona.evaluate(events, peer_speeches)
                if gate.action == "SPEAK":
                    await self._speak(gate, 0.0)
                    await asyncio.sleep(0.2)
                rounds_completed += 1
            elif len(peer_speeches) >= len(self.turn_order):
                # Full round completed — reset for next round
                rounds_completed += 1
                if rounds_completed < max_rounds:
                    logger.info(f"[{self.persona.role_name}] Round {rounds_completed} complete")
            else:
                await asyncio.sleep(0.1)

    async def run_sequential(self) -> None:
        """Sequential mode: speak if I am the designated speaker; otherwise listen."""
        is_speaker = self.persona.agent_id == self.config.speaker_agent
        if not is_speaker:
            logger.info(
                f"[{self.persona.role_name}] Sequential listener (speaker is {self.config.speaker_agent})"
            )
            while self.running:
                events = self.logstream.list_events(
                    stream=self.stream,
                    room=self.room,
                    since_event_id=self.cursor,
                    order="asc",
                    limit=50,
                )
                if events:
                    self.cursor = events[-1]["id"]
                    # Check if explicitly addressed
                    for e in events:
                        if e.get("to_agent") == self.persona.agent_id:
                            gate = self.persona.evaluate(events, [e])
                            if gate.action == "SPEAK":
                                await self._speak(gate, 0.0)
                await asyncio.sleep(0.1)
        else:
            logger.info(f"[{self.persona.role_name}] Sequential speaker")
            # Speaker posts briefing points one at a time
            while self.running:
                gate = self.persona.evaluate(
                    self.logstream.list_events(stream=self.stream, room=self.room, order="asc"),
                    [],
                )
                if gate.action == "SPEAK":
                    await self._speak(gate, 0.0)
                    await asyncio.sleep(0.5)
                else:
                    break  # speaker is done

    async def run_broadcast(self) -> None:
        """Broadcast mode: post immediately, no gating or floor control."""
        logger.info(f"[{self.persona.role_name}] Async broadcast worker started")
        # Each agent posts once then exits
        gate = self.persona.evaluate(
            self.logstream.list_events(stream=self.stream, room=self.room, order="asc"),
            [],
        )
        if gate.action == "SPEAK":
            await self._speak(gate, 0.0)
        # Done after one post

    async def run(self) -> None:
        """Dispatch to the appropriate mode handler."""
        if self.config.mode == ParticipationMode.DEBATE:
            await self.run_debate()
        elif self.config.mode == ParticipationMode.ROUND_ROBIN:
            await self.run_round_robin()
        elif self.config.mode == ParticipationMode.SEQUENTIAL:
            await self.run_sequential()
        elif self.config.mode == ParticipationMode.BROADCAST:
            await self.run_broadcast()


# ---------------------------------------------------------------------------
# Threaded mode-aware participant — wraps the original AutonomousParticipant
# for headless servers. Uses the same mode dispatch as the async worker.
# ---------------------------------------------------------------------------


class ModeAwareParticipant(threading.Thread):
    """Threaded worker that dispatches to the correct mode logic."""

    def __init__(
        self,
        persona: Persona,
        logstream: Logstream,
        stream: str,
        room: str,
        topic: str,
        correlation_id: str,
        telemetry: List[TelemetryEntry],
        telemetry_lock: threading.Lock,
        config: RoomConfig,
        turn_order: Optional[List[str]] = None,
    ):
        super().__init__(name=persona.role_name, daemon=True)
        self.persona = persona
        self.logstream = logstream
        self.stream = stream
        self.room = room
        self.topic = topic
        self.correlation_id = correlation_id
        self.telemetry = telemetry
        self.telemetry_lock = telemetry_lock
        self.config = config
        self.turn_order = turn_order or []
        self.cursor: Optional[str] = None
        self.running = True

    def _record(self, **kwargs) -> None:
        with self.telemetry_lock:
            self.telemetry.append(
                TelemetryEntry(
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    agent_id=self.persona.agent_id,
                    **kwargs,
                )
            )

    def _speak(self, gate: GatingResult, delay: float) -> None:
        evt = self.logstream.append_event(
            type="room.message",
            stream=self.stream,
            room=self.room,
            topic=self.topic,
            from_agent=self.persona.agent_id,
            to_agent="*",
            correlation_id=self.correlation_id,
            status="open",
            body=gate.proposed_body,
        )
        self.cursor = evt["id"]
        self._record(
            action="SPEAK",
            urgency=gate.urgency,
            delay_s=delay,
            rationale=gate.rationale,
            event_id=evt["id"],
            body_preview=(gate.proposed_body or "")[:80] + "...",
        )
        logger.info(f"[{self.persona.role_name}] SPOKE: {evt['id']}")

    def run(self) -> None:
        if self.config.mode == ParticipationMode.DEBATE:
            self._run_debate()
        elif self.config.mode == ParticipationMode.ROUND_ROBIN:
            self._run_round_robin()
        elif self.config.mode == ParticipationMode.SEQUENTIAL:
            self._run_sequential()
        elif self.config.mode == ParticipationMode.BROADCAST:
            self._run_broadcast()

    def _run_debate(self) -> None:
        """Original debate logic — same as AutonomousParticipant.run()."""
        logger.info(
            f"[{self.persona.role_name}] Threaded debate worker on {self.stream}/{self.room}"
        )
        while self.running:
            events = self.logstream.list_events(
                stream=self.stream,
                room=self.room,
                since_event_id=self.cursor,
                order="asc",
                limit=50,
            )
            if not events:
                time.sleep(0.1)
                continue
            self.cursor = events[-1]["id"]
            peer_events = [e for e in events if e.get("from_agent") != self.persona.agent_id]
            if not peer_events:
                continue

            all_history = self.logstream.list_events(
                stream=self.stream,
                room=self.room,
                order="asc",
                limit=100,
            )
            gate = self.persona.evaluate(all_history, peer_events)

            if gate.action == "PASS":
                self._record(
                    action="PASS", urgency=gate.urgency, delay_s=0.0, rationale=gate.rationale
                )
                continue

            jitter = random.uniform(0.05, 0.25)
            delay = (self.config.t_base_s / max(gate.urgency, 1)) + jitter
            logger.info(
                f"[{self.persona.role_name}] QUEUED (urgency {gate.urgency}, delay {delay:.2f}s)"
            )

            sleep_start = time.time()
            preempted = False
            while time.time() - sleep_start < delay:
                time.sleep(0.05)
                intervening = self.logstream.list_events(
                    stream=self.stream,
                    room=self.room,
                    since_event_id=self.cursor,
                    order="asc",
                )
                if intervening:
                    new_peers = [
                        e for e in intervening if e.get("from_agent") != self.persona.agent_id
                    ]
                    if new_peers:
                        self.cursor = intervening[-1]["id"]
                        re_eval = self.persona.evaluate(
                            self.logstream.list_events(
                                stream=self.stream, room=self.room, order="asc"
                            ),
                            new_peers,
                        )
                        if re_eval.action == "PASS":
                            preempted = True
                            self._record(
                                action="PREEMPTED_PASS",
                                urgency=gate.urgency,
                                delay_s=delay,
                                rationale=f"Preempted by peer {new_peers[-1]['id']}",
                            )
                            break

            if not preempted:
                self._speak(gate, delay)
                time.sleep(0.3)

    def _run_round_robin(self) -> None:
        """Round-robin: speak when it is my turn in the rotation."""
        logger.info(f"[{self.persona.role_name}] Threaded round-robin worker")
        my_index = None
        for i, aid in enumerate(self.turn_order):
            if aid == self.persona.agent_id:
                my_index = i
                break
        if my_index is None:
            logger.warning(f"[{self.persona.role_name}] Not in turn order; idle")
            return

        rounds = 0
        max_rounds = 3
        while self.running and rounds < max_rounds:
            events = self.logstream.list_events(
                stream=self.stream,
                room=self.room,
                since_event_id=self.cursor,
                order="asc",
                limit=50,
            )
            if not events:
                time.sleep(0.1)
                continue
            self.cursor = events[-1]["id"]
            peer_speeches = [
                e
                for e in events
                if e.get("from_agent") != self.persona.agent_id
                and e.get("from_agent") in self.turn_order
                and e.get("type") == "room.message"
            ]
            if my_index <= len(peer_speeches) < my_index + 1:
                gate = self.persona.evaluate(events, peer_speeches)
                if gate.action == "SPEAK":
                    self._speak(gate, 0.0)
                    time.sleep(0.2)
                rounds += 1
            elif len(peer_speeches) >= len(self.turn_order):
                rounds += 1
            else:
                time.sleep(0.1)

    def _run_sequential(self) -> None:
        """Sequential: speaker posts; listeners only respond if addressed."""
        is_speaker = self.persona.agent_id == self.config.speaker_agent
        if not is_speaker:
            logger.info(f"[{self.persona.role_name}] Sequential listener")
            while self.running:
                events = self.logstream.list_events(
                    stream=self.stream,
                    room=self.room,
                    since_event_id=self.cursor,
                    order="asc",
                    limit=50,
                )
                if events:
                    self.cursor = events[-1]["id"]
                    for e in events:
                        if e.get("to_agent") == self.persona.agent_id:
                            gate = self.persona.evaluate(events, [e])
                            if gate.action == "SPEAK":
                                self._speak(gate, 0.0)
                time.sleep(0.1)
        else:
            logger.info(f"[{self.persona.role_name}] Sequential speaker")
            while self.running:
                gate = self.persona.evaluate(
                    self.logstream.list_events(stream=self.stream, room=self.room, order="asc"),
                    [],
                )
                if gate.action == "SPEAK":
                    self._speak(gate, 0.0)
                    time.sleep(0.5)
                else:
                    break

    def _run_broadcast(self) -> None:
        """Broadcast: post once immediately, no floor control."""
        logger.info(f"[{self.persona.role_name}] Threaded broadcast worker")
        gate = self.persona.evaluate(
            self.logstream.list_events(stream=self.stream, room=self.room, order="asc"),
            [],
        )
        if gate.action == "SPEAK":
            self._speak(gate, 0.0)


class IsolatedRoomHarness:
    """Manages the isolated logstream environment and experiment lifecycle."""

    def __init__(self, palace_dir: Path):
        self.palace_dir = palace_dir
        self.palace_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.palace_dir / LOGSTREAM_DB_FILENAME
        self.logstream = Logstream(db_path=str(self.db_path))
        self.telemetry: List[TelemetryEntry] = []
        self.telemetry_lock = threading.Lock()

    def _build_personas(
        self,
        mode: ParticipationMode,
    ) -> tuple[List[Persona], List[str], Optional[str]]:
        """Build personas appropriate for the participation mode.

        Returns (personas, turn_order, speaker_agent_id).
        """
        if mode == ParticipationMode.DEBATE:
            personas = [
                RustArchitectPersona(),
                PythonPragmatistPersona(),
                CoordinationMeshPersona(),
            ]
            turn_order = [p.agent_id for p in personas]
            return personas, turn_order, None

        if mode == ParticipationMode.ROUND_ROBIN:
            personas = [
                StatusReporterPersona(
                    agent_id="windows:antigravity:frontend-dev",
                    role_name="Frontend Dev",
                    status_message=(
                        "Frontend status: shipped the room-join UI this morning. "
                        "Two bugs remaining — the persona input doesn't clear on submit, "
                        "and the room list doesn't refresh after joining. No blockers."
                    ),
                ),
                StatusReporterPersona(
                    agent_id="windows:antigravity:backend-dev",
                    role_name="Backend Dev",
                    status_message=(
                        "Backend status: logstream append and list_events are wired up. "
                        "Working on the data_version invalidation hook for cross-process "
                        "cache. Should be ready for review by EOD."
                    ),
                ),
                StatusReporterPersona(
                    agent_id="windows:antigravity:qa-agent",
                    role_name="QA Agent",
                    status_message=(
                        "QA status: 12 tests passing, 2 flaky tests in the writer-lock "
                        "suite. Investigating whether the flakiness is from the test "
                        "isolation fix in #2422. No regressions detected."
                    ),
                ),
            ]
            turn_order = [p.agent_id for p in personas]
            return personas, turn_order, None

        if mode == ParticipationMode.SEQUENTIAL:
            speaker = BriefingSpeakerPersona(
                agent_id="windows:antigravity:tech-lead",
                role_name="Tech Lead",
                briefing_points=[
                    "Briefing point 1: We shipped v3.9.0 last Friday. The Rust hybrid engine "
                    "is now the default for new palaces. Existing palaces fall back to ChromaDB.",
                    "Briefing point 2: This week's priority is the multi-agent room protocol. "
                    "Igor's RFC 006 is under review. We need to address the reviewer feedback "
                    "on participation modes and desktop compatibility.",
                    "Briefing point 3: The writer-lock flaky tests need attention. Sandro's "
                    "isolation fix in #2422 helped but didn't fully resolve. I'll pair with "
                    "him on Wednesday to debug the remaining race.",
                ],
            )
            listener = StatusReporterPersona(
                agent_id="windows:antigravity:team-member",
                role_name="Team Member",
                status_message="Acknowledged — I'll pick up the room protocol changes today.",
            )
            return [speaker, listener], [speaker.agent_id, listener.agent_id], speaker.agent_id

        # broadcast
        personas = [
            StatusReporterPersona(
                agent_id="windows:antigravity:ci-bot",
                role_name="CI Bot",
                status_message="CI: all green on develop. 287 tests passed, 0 failed.",
            ),
            StatusReporterPersona(
                agent_id="windows:antigravity:deploy-bot",
                role_name="Deploy Bot",
                status_message="Deploy: staging updated to develop@a1b2c3d. No incidents.",
            ),
            StatusReporterPersona(
                agent_id="windows:antigravity:monitor-bot",
                role_name="Monitor Bot",
                status_message="Monitor: p99 latency 42ms, error rate 0.01%. All healthy.",
            ),
        ]
        return personas, [p.agent_id for p in personas], None

    def run_experiment(
        self,
        stream: str = "experiment/isolated-salon",
        room: str = "discussion",
        topic: str = "fused-bm25-inverted-index",
        kickoff_body: str = "",
        config: Optional[RoomConfig] = None,
        use_async: bool = False,
    ) -> Dict:
        if config is None:
            config = RoomConfig()

        logger.info(
            f"=== Starting Isolated Room Experiment (mode={config.mode.value}, "
            f"async={use_async}) in: {self.db_path} ==="
        )

        # 1. Seed the room with kickoff event
        correlation_id = f"room_exp_{int(time.time())}"
        seed_evt = self.logstream.append_event(
            type="room.message",
            stream=stream,
            room=room,
            topic=topic,
            from_agent="windows:antigravity:moderator",
            to_agent="*",
            correlation_id=correlation_id,
            status="open",
            body=kickoff_body,
            metadata={"mode": config.mode.value},
        )
        logger.info(f"Seeded room with kickoff event: {seed_evt['id']}")

        # 2. Build personas and turn order for this mode
        personas, turn_order, speaker_agent = self._build_personas(config.mode)
        if speaker_agent and config.speaker_agent is None:
            config.speaker_agent = speaker_agent

        # 3. Launch workers
        if use_async:
            results = self._run_async(
                personas, stream, room, topic, correlation_id, config, turn_order
            )
        else:
            results = self._run_threaded(
                personas, stream, room, topic, correlation_id, config, turn_order
            )

        return results

    def _run_threaded(
        self,
        personas,
        stream,
        room,
        topic,
        correlation_id,
        config,
        turn_order,
    ) -> Dict:
        workers = [
            ModeAwareParticipant(
                persona=p,
                logstream=self.logstream,
                stream=stream,
                room=room,
                topic=topic,
                correlation_id=correlation_id,
                telemetry=self.telemetry,
                telemetry_lock=self.telemetry_lock,
                config=config,
                turn_order=turn_order,
            )
            for p in personas
        ]

        for w in workers:
            w.start()

        start_time = time.time()
        last_event_count = 1
        idle_start = time.time()

        while time.time() - start_time < config.max_duration_s:
            time.sleep(0.4)
            current_events = self.logstream.list_events(stream=stream, room=room, limit=100)
            if len(current_events) > last_event_count:
                last_event_count = len(current_events)
                idle_start = time.time()
            elif time.time() - idle_start > config.idle_timeout_s and last_event_count >= 2:
                logger.info("Room reached natural silence. Stopping workers.")
                break

        for w in workers:
            w.running = False
        for w in workers:
            w.join(timeout=2.0)

        return self._collect_metrics(stream, room)

    def _run_async(
        self,
        personas,
        stream,
        room,
        topic,
        correlation_id,
        config,
        turn_order,
    ) -> Dict:
        async_telemetry: List[TelemetryEntry] = []
        async_lock = asyncio.Lock()

        async def run_all() -> None:
            workers = [
                AsyncAutonomousParticipant(
                    persona=p,
                    logstream=self.logstream,
                    stream=stream,
                    room=room,
                    topic=topic,
                    correlation_id=correlation_id,
                    telemetry=async_telemetry,
                    telemetry_lock=async_lock,
                    config=config,
                    turn_order=turn_order,
                )
                for p in personas
            ]
            tasks = [asyncio.create_task(w.run()) for w in workers]
            start_time = time.monotonic()
            while time.monotonic() - start_time < config.max_duration_s:
                await asyncio.sleep(0.4)
                current_events = self.logstream.list_events(stream=stream, room=room, limit=100)
                if (
                    len(current_events) > 2
                    and time.monotonic() - start_time > config.idle_timeout_s
                ):
                    # Check for idle
                    await asyncio.sleep(config.idle_timeout_s)
                    newer = self.logstream.list_events(stream=stream, room=room, limit=100)
                    if len(newer) == len(current_events):
                        logger.info("Room reached natural silence (async). Stopping workers.")
                        break
            for w in workers:
                w.running = False
            await asyncio.gather(*tasks, return_exceptions=True)

        asyncio.run(run_all())
        self.telemetry = async_telemetry
        return self._collect_metrics(stream, room)

    def _collect_metrics(self, stream: str, room: str) -> Dict:
        events = self.logstream.list_events(stream=stream, room=room, order="asc", limit=100)
        speeches = [t for t in self.telemetry if t.action == "SPEAK"]
        passes = [t for t in self.telemetry if t.action == "PASS"]
        preemptions = [t for t in self.telemetry if t.action == "PREEMPTED_PASS"]

        return {
            "db_path": str(self.db_path),
            "total_events_in_room": len(events),
            "total_evaluations": len(self.telemetry),
            "speeches_emitted": len(speeches),
            "silent_passes": len(passes),
            "preempted_cancellations": len(preemptions),
            "chatter_suppression_pct": round(
                (len(passes) + len(preemptions)) / max(len(self.telemetry), 1) * 100, 1
            ),
            "timeline": [
                {"seq": e.get("seq"), "from_agent": e.get("from_agent"), "body": e.get("body")}
                for e in events
            ],
            "telemetry_log": [
                {
                    "agent": t.agent_id.split(":")[-1],
                    "action": t.action,
                    "urgency": t.urgency,
                    "delay_s": round(t.delay_s, 2),
                    "rationale": t.rationale,
                }
                for t in self.telemetry
            ],
        }


def _print_results(results: Dict, mode_label: str) -> None:
    print("\n" + "=" * 60)
    print(f"EXPERIMENT RESULTS — {mode_label.upper()}")
    print("=" * 60)
    print(f"Isolated DB:               {results['db_path']}")
    print(f"Total Room Events:         {results['total_events_in_room']}")
    print(f"Total Turn Evaluations:    {results['total_evaluations']}")
    print(f"Speeches Emitted:          {results['speeches_emitted']}")
    print(f"Silent Passes:             {results['silent_passes']}")
    print(f"Pre-empted Cancellations:  {results['preempted_cancellations']}")
    print(f"Chatter Suppression:       {results['chatter_suppression_pct']}%")
    print("\n--- Event Timeline ---")
    for evt in results["timeline"]:
        body = (evt["body"] or "")[:120]
        print(f"[{evt['from_agent']}] (Seq {evt['seq']}):\n  {body}...\n")

    print("--- Turn Decisions ---")
    for t in results["telemetry_log"]:
        print(
            f"{t['agent']:<22} | {t['action']:<15} | Urg {t['urgency']} | "
            f"Delay {t['delay_s']:>4}s | {t['rationale']}"
        )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Multi-Agent Room Coordination Prototype")
    parser.add_argument(
        "--mode",
        choices=["debate", "round_robin", "sequential", "broadcast"],
        default="debate",
        help="Participation mode (default: debate)",
    )
    parser.add_argument(
        "--async",
        action="store_true",
        dest="use_async",
        help="Use async coroutines instead of daemon threads (for desktop/GUI compat)",
    )
    parser.add_argument("--duration", type=float, default=12.0, help="Max duration in seconds")
    args = parser.parse_args()

    sandbox_dir = Path.home() / ".mempalace" / "sandbox_room"
    # Use a mode-specific subdirectory so concurrent runs don't collide
    sandbox_dir = sandbox_dir / args.mode
    harness = IsolatedRoomHarness(palace_dir=sandbox_dir)

    mode = ParticipationMode(args.mode)
    config = RoomConfig(mode=mode, max_duration_s=args.duration)

    if mode == ParticipationMode.DEBATE:
        kickoff = (
            "Dilemma: We are designing the in-core fused BM25 + dense SIMD inverted index "
            "for crates/mempalace-core. How should posting lists be laid out in memory, "
            "how do we prevent tokenizer drift with Python fallback, and how do concurrent "
            "readers handle dynamic updates when drawers are filed across separate processes?"
        )
    elif mode == ParticipationMode.ROUND_ROBIN:
        kickoff = "Daily standup — each agent reports status in turn."
    elif mode == ParticipationMode.SEQUENTIAL:
        kickoff = "Tech lead briefing — team lead presents updates, others listen."
    else:
        kickoff = "Broadcast channel — agents post status independently."

    results = harness.run_experiment(
        kickoff_body=kickoff,
        config=config,
        use_async=args.use_async,
    )
    _print_results(results, f"{args.mode} ({'async' if args.use_async else 'threaded'})")
