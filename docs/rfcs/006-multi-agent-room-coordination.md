# RFC 006: Multi-Agent Room Coordination Protocol

Status: Draft — revised per reviewer feedback  
Owner: Igor Lins e Silva & Antigravity (`windows:antigravity:mempalace`)  
Created: 2026-09-05  
Revised: 2026-09-07 — added participation modes, async/desktop compatibility, simplified gating  
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
6. **Mode-Appropriate Complexity**: Not every room needs adversarial gating. A status standup, a sequential design review, or a single speaker briefing should not pay the cognitive or latency cost of the full debate protocol. The protocol defines four participation modes; agents select one per room session.
7. **Desktop and GUI Compatibility**: The protocol must work with desktop applications (e.g. Antigravity, Cursor) that run their own event loops. Worker implementations must not assume a headless server with free-running daemon threads.

---

## Participation Modes

Not all multi-agent rooms are debates. The original prototype assumed adversarial brainstorming (thesis → antithesis → synthesis), but many real workflows involve agents that share information without arguing, or rooms where only one agent speaks at a time in a predetermined order. The protocol defines four modes:

### Mode 1: `debate` (original, default for open brainstorming)

Agents evaluate relevance, novelty, and urgency before taking the floor. Urgency-weighted jitter backoff with pre-flight cancellation. Best for architectural design, peer critique, and exploratory research where multiple perspectives should collide.

### Mode 2: `round_robin` (strict turn-taking)

Each agent speaks exactly once per round, in declaration order. No gating, no jitter, no pre-flight cancellation. The floor passes sequentially: A → B → C → A → B → C. Best for standups, status reports, and structured reviews where every voice must be heard.

Implementation: Each agent's `to_agent` field targets the next agent in the rotation. An agent speaks only when the previous agent's event names it as `to_agent`. The last agent in the round passes back to the first. No urgency scoring is needed.

### Mode 3: `sequential` (single-speaker briefing)

One designated speaker posts; all others listen and do not respond unless explicitly addressed via `to_agent`. No gating heuristic runs on listeners. Best for briefings, demos, announcements, and any scenario where one agent has the floor and others should not interject.

Implementation: The speaker posts with `to_agent: "*"`. Listeners advance their cursor but never evaluate the participation gate. If a listener is explicitly named in `to_agent`, it may respond once, then control returns to the speaker.

### Mode 4: `broadcast` (fire-and-forget)

Any agent may post at any time. No gating, no turn-taking, no floor control. Messages are appended as they arrive. Best for firehose telemetry, event logging, and scenarios where ordering doesn't matter and collisions are acceptable.

Implementation: Agents append directly without entering the floor controller. SQLite WAL ordering provides causal consistency. This is the simplest mode and the natural fallback if no mode is declared.

### Mode Selection

Mode is declared in the kickoff event's `metadata.mode` field. If absent, `debate` is assumed for backward compatibility. The `mempalace room listen` CLI command accepts `--mode round_robin|sequential|broadcast|debate`.

---

## Protocol Specification

### 1. Room Envelope & Naming Conventions

Rooms are addressed using RFC 003 streams and broadcast addressing:

- `stream`: `room/<room-name>` (e.g. `room/architecture`, `room/brainstorm`)
- `room`: Lifecycle sub-channel, default `discussion` (or `salon`, `critique`, `synthesis`)
- `topic`: Focus lane (e.g. `hybrid-engine`, `auth-v2`)
- `to_agent`: `*` (broadcast to all listeners)
- `type`: `room.message` (standard conversational turns) or `room.reaction` (lightweight signals)

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

### 2. Autonomous Participation Gate (debate mode only)

> **Note:** The gating heuristic below applies only to `debate` mode. In `round_robin`, `sequential`, and `broadcast` modes, agents do not evaluate the gate — turn-taking is structural, not heuristic.

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

### 3. Decentralized Floor Controller (debate mode only)

> **Note:** The floor controller applies only to `debate` mode. `round_robin` uses sequential `to_agent` passing. `sequential` grants the floor to one speaker. `broadcast` has no floor control.

To eliminate race collisions and reflect human conversational dynamics, agents do not append immediately upon deciding to speak. Instead, they enter an **Urgency-Weighted Jitter Window**:

$$\Delta t = \frac{T_{\text{base}}}{\text{Urgency}} + \text{Uniform}(0, J_{\text{max}})$$

- $T_{\text{base}} = 0.8\text{s}$ (configurable per room)
- $J_{\text{max}} = 0.25\text{s}$

#### Backoff Tiers

- **Urgency 5**: $\Delta t \approx 0.16\text{s} - 0.35\text{s}$ (instant interjection for critical corrections)

- **Urgency 4**: $\Delta t \approx 0.20\text{s} - 0.45\text{s}$
- **Urgency 3**: $\Delta t \approx 0.27\text{s} - 0.52\text{s}$
- **Urgency 2**: $\Delta t \approx 0.40\text{s} - 0.65\text{s}$

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

## Desktop and Async Compatibility

The original prototype used Python `threading.Thread` daemon workers, which do not play well with desktop applications that run their own event loops (e.g. Antigravity, Cursor, Electron-based tools). A daemon thread that calls `time.sleep()` in a busy loop blocks the interpreter and can starve GUI render frames.

The protocol specifies two execution strategies:

### Strategy A: Async/Await (default for desktop and web)

Workers are coroutines scheduled on the host application's event loop (`asyncio`, `trio`, or a GUI-native loop). Backoff is implemented via `asyncio.sleep()` — never `time.sleep()`. The agent yields control between logstream polls, allowing the GUI to render and the user to interact.

```python
async def room_worker(persona, logstream, ...):
    while running:
        events = logstream.list_events(since_event_id=cursor)
        if not events:
            await asyncio.sleep(0.1)
            continue
        gate = persona.evaluate(events)
        if gate.action == "PASS":
            continue
        await asyncio.sleep(backoff_delay)  # yields to event loop
        # pre-flight check, then speak
```

### Strategy B: Threaded (default for headless servers and CLI)

The original `threading.Thread` approach is retained for headless server deployments and CLI tools where blocking sleep is acceptable. This is the fallback when no event loop is available.

### Selection

The `mempalace room listen` CLI command detects the runtime:

- If an event loop is already running (e.g. inside an async framework), use Strategy A.
- If running in a plain CLI with no event loop, use Strategy B.
- The `--async` flag forces Strategy A; `--threaded` forces Strategy B.

---

## Planned CLI Affordances

We propose three high-level CLI commands under `mempalace room`:

```bash
# 1. Join and declare presence in an open room
mempalace room join --room architecture --persona "systems, low-level memory, SIMD"

# 2. Listen continuously — mode selects turn-taking strategy
mempalace room listen --room architecture --mode debate --idle-timeout 30s
mempalace room listen --room standup --mode round_robin --idle-timeout 10s
mempalace room listen --room briefing --mode sequential --speaker alice
mempalace room listen --room events --mode broadcast

# 3. Post a direct thought to the room with an explicit urgency tier
mempalace room post --room architecture --topic hybrid-engine --urgency 4 --body "..."

# 4. Desktop/GUI compatibility — async worker yields to host event loop
mempalace room listen --room architecture --mode debate --async
```

---

## Non-Goals

1. **Not a General Chat Application**: This protocol is designed for LLM agents and human-agent hybrid brainstorms, not a human IRC/Slack replacement.
2. **No Central Lock Coordinator**: Does not introduce Redis, distributed locks, or Raft clusters. SQLite WAL ordering + HLC provides all required causal consistency.
3. **No Forced Handoffs**: Unlike `task.request`, a `room.message` carries no obligation for any specific peer to reply.

---

## Verification & Conformance

The prototype implementation has been validated in `examples/multi_agent_room/room_prototype.py` against a dedicated SQLite sandbox:

- Multi-threaded concurrent worker execution (Strategy B).
- Async coroutine worker execution (Strategy A) for desktop/GUI compatibility.
- All four participation modes: `debate`, `round_robin`, `sequential`, `broadcast`.
- Deterministic pre-emption trigger tests (debate mode).
- Zero-leakage verification against the primary user palace.
