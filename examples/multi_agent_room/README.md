# Autonomous Multi-Agent Room Coordination Prototype

This directory contains the working reference prototype for **RFC 006: Multi-Agent Room Coordination Protocol**.

It demonstrates decentralized, autonomous turn-taking among LLM agents in a shared room over MemPalace's native logstream engine, eliminating infinite echo loops and simultaneous race collisions.

---

## Participation Modes

Not all multi-agent rooms are debates. The prototype supports four turn-taking modes:

| Mode | Description | Use Case |
|------|-------------|----------|
| `debate` | Autonomous gating + urgency-weighted jitter backoff with pre-flight cancellation | Architectural brainstorming, peer critique, exploratory research |
| `round_robin` | Strict turn-taking — each agent speaks once per round in declaration order | Standups, status reports, structured reviews |
| `sequential` | One designated speaker posts; others listen unless explicitly addressed | Briefings, demos, announcements |
| `broadcast` | Fire-and-forget — any agent posts at any time, no floor control | Telemetry, event logging, firehose status |

### Key Features

1. **Autonomous Participation Gating** (debate mode):
   Agents evaluate relevance, novelty, and urgency before taking the floor. Turns lacking substantive novelty are voluntarily suppressed (`PASS`), writing zero bytes to the wire.
2. **Urgency-Weighted Jitter Backoff** (debate mode):
   Delays speech inversely to urgency:
   $$\Delta t = \frac{T_{\text{base}}}{\text{urgency}} + \text{jitter}(0, 0.25\text{s})$$
   Critical corrections (Urgency 5) fire in $\sim 0.2\text{s}$; secondary observations (Urgency 2-3) pause for $0.5\text{s} - 1.0\text{s}$.
3. **Pre-Flight Collision Cancellation** (debate mode):
   If an agent is queued to speak and a peer takes the floor during the backoff window with a resolving post, the agent detects the intervening event and automatically aborts (`PREEMPTED_PASS`).
4. **Non-Adversarial Personas** (round_robin, sequential, broadcast):
   `StatusReporterPersona` and `BriefingSpeakerPersona` share information without arguing — they speak when it is their turn, without evaluating relevance or novelty.
5. **Isolated Sandbox Mode**:
   Runs against a dedicated SQLite sandbox (`scratch/isolated_palace/logstream.sqlite3`), ensuring zero event pollution or writer contention on active user palaces.

---

## Execution Strategies

The prototype supports two execution strategies for desktop/GUI compatibility:

- **Threaded (default)**: Daemon threads with `time.sleep`. Best for headless servers and CLI tools.
- **Async**: Coroutines with `asyncio.sleep` that yield to the host event loop. Best for desktop applications (Antigravity, Cursor) that run their own GUI event loops.

---

## Running the Prototype

```bash
# Debate mode (original — adversarial brainstorming with gating)
uv run python examples/multi_agent_room/room_prototype.py --mode debate

# Round-robin mode (standup — each agent reports in turn)
uv run python examples/multi_agent_room/room_prototype.py --mode round_robin

# Sequential mode (briefing — one speaker, others listen)
uv run python examples/multi_agent_room/room_prototype.py --mode sequential

# Broadcast mode (fire-and-forget status posts)
uv run python examples/multi_agent_room/room_prototype.py --mode broadcast

# Async execution (for desktop/GUI compatibility)
uv run python examples/multi_agent_room/room_prototype.py --mode debate --async

# Custom duration
uv run python examples/multi_agent_room/room_prototype.py --mode round_robin --duration 15
```

### Expected Output
The script seeds a room with a kickoff event, launches concurrent worker threads (or async coroutines), and prints the resulting event timeline, turn decisions, and telemetry metrics (e.g. chatter suppression rate, pre-empted cancellations).
