# Autonomous Multi-Agent Room Coordination Prototype

This directory contains the working reference prototype for **RFC 006: Multi-Agent Room Coordination Protocol**.

It demonstrates decentralized, autonomous turn-taking among LLM agents in a shared room over MemPalace's native logstream engine, eliminating infinite echo loops and simultaneous race collisions.

---

## Key Features

1. **Autonomous Participation Gating**:
   Agents evaluate relevance, novelty, and urgency before taking the floor. Turns lacking substantive novelty are voluntarily suppressed (`PASS`), writing zero bytes to the wire.
2. **Urgency-Weighted Jitter Backoff**:
   Delays speech inversely to urgency:
   $$\Delta t = \frac{T_{\text{base}}}{\text{urgency}} + \text{jitter}(0, 0.25\text{s})$$
   Critical corrections (Urgency 5) fire in $\sim 0.2\text{s}$; secondary observations (Urgency 2-3) pause for $0.5\text{s} - 1.0\text{s}$.
3. **Pre-Flight Collision Cancellation**:
   If an agent is queued to speak and a peer takes the floor during the backoff window with a resolving post, the agent detects the intervening event and automatically aborts (`PREEMPTED_PASS`).
4. **Isolated Sandbox Mode**:
   Runs against a dedicated SQLite sandbox (`scratch/isolated_palace/logstream.sqlite3`), ensuring zero event pollution or writer contention on active user palaces.

---

## Running the Prototype

```bash
# Run the autonomous room simulation with 3 concurrent agent personas
uv run python examples/multi_agent_room/room_prototype.py
```

### Expected Output
The script seeds a complex architectural dilemma, launches three concurrent worker threads (`rust-architect`, `python-pragmatist`, and `coordination-mesh`), and prints the resulting event timeline, turn decisions, and telemetry metrics (e.g. chatter suppression rate, pre-empted cancellations).
