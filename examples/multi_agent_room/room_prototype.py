"""
room_prototype.py — Autonomous Multi-Agent Room Prototype for MemPalace
========================================================================

Demonstrates decentralized, non-mechanical agent participation in a shared room
over an isolated MemPalace Logstream database.

Key Mechanisms:
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

import logging
import random
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
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


class IsolatedRoomHarness:
    """Manages the isolated logstream environment and experiment lifecycle."""

    def __init__(self, palace_dir: Path):
        self.palace_dir = palace_dir
        self.palace_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.palace_dir / LOGSTREAM_DB_FILENAME
        self.logstream = Logstream(db_path=str(self.db_path))
        self.telemetry: List[TelemetryEntry] = []
        self.telemetry_lock = threading.Lock()

    def run_experiment(
        self,
        stream: str = "experiment/isolated-salon",
        room: str = "discussion",
        topic: str = "fused-bm25-inverted-index",
        kickoff_body: str = "",
        max_duration_s: float = 12.0,
    ) -> Dict:
        logger.info(f"=== Starting Isolated Room Experiment in: {self.db_path} ===")

        # 1. Seed the room with kickoff dilemma
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
        )
        logger.info(f"Seeded room with kickoff event: {seed_evt['id']}")

        # 2. Spin up the 3 autonomous participant workers
        personas = [RustArchitectPersona(), PythonPragmatistPersona(), CoordinationMeshPersona()]
        workers = [
            AutonomousParticipant(
                persona=p,
                logstream=self.logstream,
                stream=stream,
                room=room,
                topic=topic,
                correlation_id=correlation_id,
                telemetry=self.telemetry,
                telemetry_lock=self.telemetry_lock,
                t_base_s=0.6,
            )
            for p in personas
        ]

        for w in workers:
            w.start()

        # 3. Monitor until natural silence / max duration
        start_time = time.time()
        last_event_count = 1
        idle_start = time.time()

        while time.time() - start_time < max_duration_s:
            time.sleep(0.4)
            current_events = self.logstream.list_events(stream=stream, room=room, limit=100)
            if len(current_events) > last_event_count:
                last_event_count = len(current_events)
                idle_start = time.time()
            elif time.time() - idle_start > 3.0 and last_event_count >= 5:
                logger.info("Room reached natural silence and consensus. Stopping workers.")
                break

        # Stop workers
        for w in workers:
            w.running = False
        for w in workers:
            w.join(timeout=1.0)

        # 4. Generate Telemetry Metrics
        events = self.logstream.list_events(stream=stream, room=room, order="asc", limit=100)

        speeches = [t for t in self.telemetry if t.action == "SPEAK"]
        passes = [t for t in self.telemetry if t.action == "PASS"]
        preemptions = [t for t in self.telemetry if t.action == "PREEMPTED_PASS"]

        metrics = {
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
                {
                    "seq": e.get("seq"),
                    "from_agent": e.get("from_agent"),
                    "body": e.get("body"),
                }
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

        return metrics


if __name__ == "__main__":
    sandbox_dir = Path.home() / ".mempalace" / "sandbox_room"
    harness = IsolatedRoomHarness(palace_dir=sandbox_dir)

    kickoff = (
        "Dilemma: We are designing the in-core fused BM25 + dense SIMD inverted index for crates/mempalace-core. "
        "How should posting lists be laid out in memory, how do we prevent tokenizer drift with Python fallback, "
        "and how do concurrent readers handle dynamic updates when drawers are filed across separate processes?"
    )

    results = harness.run_experiment(kickoff_body=kickoff, max_duration_s=12.0)

    print("\n" + "=" * 60)
    print("EXPERIMENT RESULTS & TELEMETRY")
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
        print(f"[{evt['from_agent']}] (Seq {evt['seq']}):\n  {evt['body'][:120]}...\n")

    print("--- Turn Decisions ---")
    for t in results["telemetry_log"]:
        print(
            f"{t['agent']:<22} | {t['action']:<15} | Urg {t['urgency']} | Delay {t['delay_s']:>4}s | {t['rationale']}"
        )
