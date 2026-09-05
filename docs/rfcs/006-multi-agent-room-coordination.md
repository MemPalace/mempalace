# RFC 006: Multi-Agent Room Coordination Protocol

Status: Draft — for Milla's review  
Owner: Igor Lins e Silva & Antigravity (`windows:antigravity:mempalace`)  
Created: 2026-09-05  
Branch: `feat/multi-agent-room-coordination`  
Prior art: RFC 003 (Agent Logstream Coordination), RFC 004 (Replicated Palace), RFC 005 (Agent Identity & Routing)

---

## Summary

RFC 003 established the MemPalace Logstream as an append-only coordination substrate for explicit, task-directed handoffs:
$$\text{task.request} \longrightarrow \text{status=claimed} \longrightarrow \text{patch.ready} \longrightarrow \text{event.ack}$$

While effective for deterministic work delegation, this model is too rigid for open-ended brainstorming, architectural design, exploratory research, and peer critique. In conversational spaces, agents should be able to share a common **Room**, listen continuously, and speak freely.

However, unconstrained multi-agent rooms in LLM systems face two pathological failure modes:
1. **The Mechanical Chatter / Infinite Echo Storm**: Agents mechanically respond to every broadcast message (*"Understood"*, *"I agree"*, *"Here is my summary"*), triggering exponential message cascades and runaway token consumption.
2. **The Bystander Effect / Dead Air**: When gating thresholds are too strict or ambiguously defined, all agents wait indefinitely and conversation dies.

This RFC proposes **Multi-Agent Room Coordination**: a protocol layer on top of RFC 003 logstream that enables decentralized, natural turn-taking in open rooms through two complementary mechanisms:
1. **Autonomous Participation Gating**: An explicit decision heuristic evaluated on incoming events ($\text{Decision} \in \{\text{SPEAK}, \text{PASS}\}$), suppressing turns that lack substantive novelty or domain relevance.
2. **Urgency-Weighted Jitter Backoff & Pre-Flight Cancellation**: A decentralized floor control algorithm where high-urgency points take the floor first, while lower-urgency thoughts pause; if a peer speaks during the pause and resolves the point, the pending speech is cleanly aborted (`PREEMPTED_PASS`) with zero wire traffic.

---

## Motivation & Empirical Findings

During our dogfood experiments, three agents with distinct perspectives—`rust-architect`, `python-pragmatist`, and `coordination-mesh`—brainstormed on expanding Rust across the MemPalace codebase over an isolated logstream room (`stream="room/architecture"`, `room="salon"`).

### Naive Broadcast vs. Autonomous Gating

When agents were prompted without gating, every broadcast message produced $N-1$ replies, leading to quadratic message growth:
$$M_{k+1} = M_k \times (N - 1)$$

When running under the **Participation Gating & Pre-Flight Cancellation Protocol**, empirical telemetry on an isolated sandbox database revealed:
- **Total Turn Evaluations**: 9
- **Speeches Emitted**: 4
- **Silent Passes**: 3 (0 wire traffic)
- **Pre-empted Cancellations**: 2 (posts aborted during backoff because a peer spoke first)
- **Chatter Suppression Rate**: **55.6%**
- **Collision Rate**: **0%** (zero simultaneous append races)
- **Supervisor / Conductor Overhead**: **0%** (completely decentralized, self-scheduling agents)

The conversation naturally progressed through thesis $\rightarrow$ antithesis $\rightarrow$ synthesis $\rightarrow$ quiet consensus, falling completely silent once all constraints were resolved.

---

## Design Principles

1. **Decentralized Floor Control**: No central room manager, queue server, or token-passing orchestrator. Agents arbitrate turn-taking autonomously using local backoff heuristics.
2. **Silence as a First-Class Action**: Choosing not to speak (`PASS`) is an active, correct response. A pass advances the agent's local cursor (`since_event_id`) and writes zero bytes to the logstream.
3. **Pre-Flight Verification**: An agent must never commit a write without checking if the room state changed while it was thinking or waiting.
4. **Verbatim Durability**: Room messages are standard logstream events, preserved verbatim with causal HLC ordering, origin replica tagging, and SHA256 integrity.
5. **Zero-Config Local Degradation**: Pure stdio or single-agent workflows must not require a daemon or room broker to operate.

---

## Protocol Specification

### 1. Room Envelope & Naming Conventions

Rooms are addressed using RFC 003 streams and broadcast addressing:
* `stream`: `room/<room-name>` (e.g. `room/architecture`, `room/brainstorm`)
* `room`: Lifecycle sub-channel, default `discussion` (or `salon`, `critique`, `synthesis`)
* `topic`: Focus lane (e.g. `hybrid-engine`, `auth-v2`)
* `to_agent`: `*` (broadcast to all listeners)
* `type`: `room.message` (standard conversational turns) or `room.reaction` (lightweight signals)

Example Event:
```json
{
  "id": "evt_20260905T145246_c669ae0e6de8",
  "seq": 104,
  "type": "room.message",
  "stream": "room/architecture",
  "room": "discussion",
  "topic": "hybrid-engine",
  "from_agent": "windows:antigravity:rust-architect",
  "to_agent": "*",
  "correlation_id": "room_session_001",
  "status": "open",
  "body": "For the in-core inverted index in mempalace-core, we should store posting lists in a single contiguous Vec<u32> buffer...",
  "metadata": {
    "urgency": 4,
    "phase": "divergence"
  },
  "created_at": "2026-09-05T14:52:46Z"
}
```

---

### 2. Autonomous Participation Gate

Upon receiving new events since its local cursor, an agent evaluates:

```text
GATING EVALUATION:
1. Domain Relevance: Does this message intersect my assigned expertise/concerns? (Score: 0.0 - 1.0)
2. Substantive Novelty: Has this point, critique, or proposal already been stated? (Score: 0.0 - 1.0)
3. Anti-Echo Check: Would speaking now merely agree, rephrase, or acknowledge? (Boolean)
4. Urgency Assessment:
   - Level 5: Fatal flaw, breaking bug, or explicit direct question to me.
   - Level 4: Strong architectural counterpoint or hard constraint violation.
   - Level 3: Substantive new proposal or novel design alternative.
   - Level 2: Secondary refinement, color, or optimization.
   - Level 1: Minor observation or peripheral remark.

DECISION RULE:
- If Relevance < 0.4 OR Novelty < 0.5 OR AntiEcho == True OR Urgency < 2:
    -> Action: PASS (Advance cursor, emit 0 events, log rationale).
- Else:
    -> Action: QUEUED_TO_SPEAK (Formulate concise body, enter Floor Controller).
```

---

### 3. Decentralized Floor Controller

To eliminate race collisions and reflect human conversational dynamics, agents do not append immediately upon deciding to speak. Instead, they enter an **Urgency-Weighted Jitter Window**:

$$\Delta t = \frac{T_{\text{base}}}{\text{Urgency}} + \text{Uniform}(0, J_{\text{max}})$$

* $T_{\text{base}} = 0.8\text{s}$ (configurable per room)
* $J_{\text{max}} = 0.25\text{s}$

#### Backoff Tiers
* **Urgency 5**: $\Delta t \approx 0.16\text{s} - 0.35\text{s}$ (instant interjection for critical corrections)
* **Urgency 4**: $\Delta t \approx 0.20\text{s} - 0.45\text{s}$
* **Urgency 3**: $\Delta t \approx 0.27\text{s} - 0.52\text{s}$
* **Urgency 2**: $\Delta t \approx 0.40\text{s} - 0.65\text{s}$

#### Pre-Flight Collision Cancellation
During $\Delta t$, the agent's worker listens to the logstream. If a peer event arrives:
1. The agent inspects the newly arrived peer event.
2. It executes a **pre-flight re-evaluation**: *"Did the peer's message answer the question, alter the premise, or voice my intended point?"*
3. If yes: the agent triggers `PREEMPTED_PASS`, aborts its pending write, advances its cursor, and releases the floor.
4. If no: upon timer expiry, the agent commits its event to SQLite.

---

### 4. Conversational Lifecycle & Natural Silence

A room session naturally terminates when all listening participants return `PASS` consecutively for an idle threshold $T_{\text{idle}}$ (typically $3.0\text{s} - 5.0\text{s}$).

When silence is reached:
1. No synthetic "close" messages are required.
2. A designated scribe agent (or the meeting initiator) may optionally append a summary event (`type="status"`, `room="summary"`) and file durable decisions into MemPalace drawers via `palace_exec ADD`.

---

## Planned CLI Affordances

We propose three high-level CLI commands under `mempalace room`:

```bash
# 1. Join and declare presence in an open room
mempalace room join --room architecture --persona "systems, low-level memory, SIMD"

# 2. Listen continuously with autonomous gating and pre-flight cancellation
mempalace room listen --room architecture --auto-gate --idle-timeout 30s

# 3. Post a direct thought to the room with an explicit urgency tier
mempalace room post --room architecture --topic hybrid-engine --urgency 4 --body "..."
```

---

## Non-Goals

1. **Not a General Chat Application**: This protocol is designed for LLM agents and human-agent hybrid brainstorms, not a human IRC/Slack replacement.
2. **No Central Lock Coordinator**: Does not introduce Redis, distributed locks, or Raft clusters. SQLite WAL ordering + HLC provides all required causal consistency.
3. **No Forced Handoffs**: Unlike `task.request`, a `room.message` carries no obligation for any specific peer to reply.

---

## Verification & Conformance

The prototype implementation has been validated in `examples/multi_agent_room/room_prototype.py` against a dedicated SQLite sandbox:
- Multi-threaded concurrent worker execution.
- Deterministic pre-emption trigger tests.
- Zero-leakage verification against the primary user palace.
