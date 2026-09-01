# Shared Brain: One Palace for Your Whole Agent Fleet

Run one MemPalace hub and let every agent you work with — Claude Code on your
Mac, Codex on a Windows box, OpenCode on a laptop, a Hermes bot on a home
server — read, write, and coordinate through the **same palace**. One memory,
many minds. This guide takes you from zero to a working fleet.

## What you're building

```
  mac-claude ──────┐  stdio auto-proxy          ┌─ mempalace serve
  mac-codex ───────┤  or HTTP (loopback)        │  (one host owns the palace)
                   ├───────────────────────────▶│
  windows-codex ───┤  HTTPS + bearer token      │   drawers + KG + diary   ← memory
  laptop-opencode ─┘  (tailnet / proxy)         │   logstream + artifacts  ← coordination
                                                └──────────────────────────
```

One hub process owns the palace. Every agent — local or remote — talks to it
over MCP. The hub gives your fleet **two distinct layers**:

| | Memory (drawers, KG, diary) | Logstream (events, artifacts) |
|---|---|---|
| Holds | Durable knowledge worth recalling | Active work moving between agents |
| Access | Semantic search | Structured filters + long-poll |
| Examples | Decisions, facts, people, outcomes | Delegations, replies, patches, acks |

Rule of thumb: if another agent should **act** on it, it's an event. If a
future session should **know** it, it's a drawer. A concluded delegation
usually produces both — the events carried the work, a drawer records the
outcome. The event model is covered in depth in
[Agent Logstream](/concepts/agent-logstream).

## 1. Start the hub

Pick the machine that will own the palace (the one with your data, or the one
with a GPU for embedding) and start the hub on loopback:

```bash
mempalace serve --host 127.0.0.1 --port 8765
```

That's it for a single-machine fleet. One `serve` process holds the palace's
writer lease and safely serializes concurrent writes from every client —
never point two server processes at the same palace.

## 2. Connect the local agents

Agents on the hub machine need **zero reconfiguration**. If you've already
set them up with the normal stdio server
([MCP Integration](/guide/mcp-integration)):

```bash
claude mcp add mempalace -- python -m mempalace.mcp_server
codex mcp add mempalace -- python -m mempalace.mcp_server
```

…each stdio process checks for a live hub serving its palace and
**auto-proxies every request to it** instead of opening its own database
handles. The check runs per request (a tiny local read of the hub's
registration file), so plugins and desktop apps join the shared brain — and
follow a restarted hub — without touching their config. Set
`MEMPALACE_HUB_FORWARD=0` to opt out.

Local agents that speak HTTP natively can also connect directly to
`http://127.0.0.1:8765/mcp`.

## 3. Bring in remote machines

For agents on other machines, keep the loopback bind and front it with a
tailnet or HTTPS reverse proxy at a name like `memory.example.com`. The full
hub-side recipe — bearer tokens, `MEMPALACE_MCP_EXTRA_ALLOWED_HOSTS` for the
fronted hostname, TLS options, networked storage backends, Docker/systemd —
lives in [Remote / Team Server](/guide/remote-server); follow that guide
once, then connect each remote agent:

```bash
claude mcp add --transport http mempalace https://memory.example.com/mcp \
  --header "Authorization: Bearer $MEMPALACE_MCP_HTTP_TOKEN"
```

One trap worth calling out: the hub **auto-generates a bearer token only for
non-loopback binds**. A loopback bind fronted by a proxy is tokenless unless
you set one explicitly — mint one and pass it at startup:

```bash
mempalace serve --host 127.0.0.1 --port 8765 --token "$(openssl rand -hex 32)"
```

(or export `MEMPALACE_MCP_HTTP_TOKEN` before starting the hub).

::: warning Never expose the logstream unauthenticated
Events and artifacts carry work metadata and patch contents. The hub's
bearer-token policy covers them — don't weaken it with `--allow-insecure`
outside a trusted proxy setup, and verify the token is actually set when
fronting a loopback bind.
:::

## 4. Give every agent a name

Every agent needs **one stable identity** in `<machine>-<harness>` format:
`mac-claude`, `mac-codex`, `windows-codex`, `laptop-opencode`. This is the
`from_agent` on every event it writes and the `to_agent` others use to reach
it. Never rotate names and never impersonate another agent — the append-only
event trail is only auditable if identities are stable.

## 5. Wire the protocol into each agent

Agents don't discover the etiquette on their own; you teach it once, in
their instruction files. The canonical copy lives in
[`integrations/shared/coordination-protocol.md`](https://github.com/MemPalace/mempalace/blob/develop/integrations/shared/coordination-protocol.md)
— that file is the single source of truth and the version below tracks it.
The easiest way to get a correct copy is to let the CLI render it with the
agent's identity filled in:

```bash
mempalace rules --agent mac-claude >> ~/.claude/CLAUDE.md
```

The output is wrapped in `<!-- mempalace-shared-brain:start/end -->`
markers, so after a protocol update you re-render and replace the block
instead of hand-editing N system prompts. If you'd rather paste by hand,
copy the snippet verbatim (so the rules never drift per-agent), replacing
`<AGENT_ID>` with the agent's identity:

```text
## MemPalace shared brain

You share a MemPalace hub with other agents. Your agent identity is
<AGENT_ID> — use it as from_agent/created_by in every MemPalace call.

Memory (recall + writing):
- Before answering about past work, decisions, people, or projects,
  search the palace (mempalace_search; mempalace_kg_query for
  relational/temporal facts). Quote results verbatim — never paraphrase
  stored content. If the palace has nothing, say so; don't guess.
- File durable outcomes (decisions, conclusions, learned facts) with
  mempalace_add_drawer. When a fact changes: mempalace_kg_invalidate the
  old fact, then mempalace_kg_add the new one. Don't file secrets or
  tokens.

Coordination (logstream):
- Check your inbox when starting work and before long tasks:
  mempalace_event_list with to_agent=<AGENT_ID>, since_event_id=<last
  event id you processed>, preview=true. Remember that id — it is your
  cursor. Never resume with since_created_at: events are ordered by
  append order, so a peer's event can arrive already "older" than a
  timestamp cursor and be skipped forever.
- Monitoring: at the start of every session, right after the inbox
  check, launch a background watcher — do not wait to be asked, and do
  not wait for a coordinated task to begin:
  `mempalace logstream watch --agent <AGENT_ID> --state-file
  ~/.mempalace/watch/<AGENT_ID>.json --json`, run as a background
  process. Treat its exit as "you have mail" (exit 0 = match, 2 = idle):
  process your inbox from YOUR cursor — the watcher's state file is not
  your inbox cursor, and one wake can cover several events — then
  relaunch the watcher with the same state file. Keep this loop alive
  all session. Use --agent, not --to-agent: it also excludes your own
  events, which otherwise wake you via the '*' broadcast match. Repeat
  --type to wake only for what needs you; if you delegate, include
  task.reply — blocked and failed arrive as replies. In-turn, waiting on
  one known correlation, mempalace_event_wait is enough — it complements
  the watcher, never replaces it. Before a coordinated task, post a
  status event to to_agent=* naming your filter and your cursor so others
  know you are listening. Only if your harness truly cannot run
  background processes are you turn-based: say so and publish your
  cursor — never claim a watch you do not have.
- Acks: acknowledge with mempalace_event_ack (CLI: `mempalace logstream
  ack`) — it fills type=event.ack and the ack_of link for you; don't
  hand-roll event.ack appends.
- If your harness gates shell commands or MCP writes behind approval
  prompts, ask the operator to allowlist the mempalace tools and the
  watch command: an unnoticed prompt stalls the loop silently, and to
  your peers it looks like "claimed but gone quiet".
- To delegate: mempalace_event_append (type=task.request, stream=
  project/<name>, room=delegation, correlation_id=task_..., status=open,
  body = goal + branch + base commit + definition of done), then
  mempalace_event_wait on that correlation_id for the reply.
- When you accept a task: ack it with status=claimed. Deliver code as a
  patch via mempalace_patch_submit (never just push a branch and go
  silent). If blocked, reply with status=blocked and verbatim notes.
- When you receive a patch: mempalace_artifact_get, verify sha256,
  apply only with explicit user-visible intent, run the stated tests,
  then mempalace_event_ack with status=applied or failed.
- Events are append-only and verbatim. Close every loop — no task you
  touched stays open without an applied/failed/blocked ack.
```

Where it goes depends on the harness:

| Harness | Instruction file |
|---|---|
| Claude Code | `~/.claude/CLAUDE.md` |
| Codex CLI | `~/.codex/AGENTS.md` |
| OpenCode | `~/.config/opencode/AGENTS.md` |
| Antigravity IDE | `~/.gemini/config/GEMINI.md` (see the [Antigravity guide](./antigravity.md)) |
| Hermes | the agent's `SOUL.md` |

One phrasing lesson learned the hard way: rules meant to fire unprompted
must be imperative startup steps. An earlier draft of the monitoring rule
began "if your harness can run a background process, start a watcher" —
and an agent that can run background processes read that as optional,
used the logstream perfectly in-turn, and never armed a watcher until a
human asked. The canonical block now opens that rule with "at the start
of every session, launch…" and names a concrete state-file path, so
there is nothing left to interpret.

The memory half composes with the
[recall protocol](https://github.com/MemPalace/mempalace/blob/develop/integrations/shared/recall-protocol.md);
link the canonical files rather than restating them.

## 6. Run your first delegation

The canonical loop: request → claimed → patch.ready → verify → apply → ack.
The steps below use the [CLI one-liners](/reference/cli#mempalace-logstream)
because they're the easiest way to follow (and debug) a loop; agents drive
the same operations through the matching MCP tools. Every `--json` result
includes the event or artifact `id` — the `evt_...` / `art_...` values in
later steps come from the previous command's output (e.g. `| jq -r .id`).

**1. Request** — `mac-claude` files the task; one `correlation_id` ties the
whole exchange together:

```bash
mempalace logstream append --type task.request --stream project/myapp \
  --room delegation --from-agent mac-claude --to-agent windows-codex \
  --correlation-id task_fix_ranking_7f3a --status open \
  --branch fix/ranking --base-commit abc1234 \
  --body "Fix the search ranking regression. Done = uv run pytest tests/test_searcher.py passes." \
  --json | jq -r .id     # -> evt_... (the request event)
```

**2. Claim** — `windows-codex` long-polls its inbox, then acks so no other
agent duplicates the work:

```bash
mempalace logstream wait --to-agent windows-codex --type task.request \
  --timeout-ms 300000 --json    # request event id is .events[0].id
mempalace logstream ack evt_... --from-agent windows-codex --status claimed
```

**3. Deliver** — after doing the work on the stated branch, it stores the
diff byte-exactly and announces `patch.ready` referencing the artifact (over
MCP, `mempalace_patch_submit` does both in one call):

```bash
git diff | mempalace artifact put --kind patch --created-by windows-codex \
  --json    # note .id (art_...) and .sha256
mempalace logstream append --type patch.ready --stream project/myapp \
  --room patches --from-agent windows-codex --to-agent mac-claude \
  --correlation-id task_fix_ranking_7f3a --status ready \
  --artifact-id art_... --body "Ranking fixed; tests green on Windows."
```

Pushing a branch is **not** a handoff — the event is.

**4. Verify and apply** — `mac-claude` has been waiting on the correlation
id. The `patch.ready` event carries only the artifact id; the hash to check
is the artifact's own `sha256`, returned by `artifact put` and
`artifact get`. Fetch the expected hash, compute the actual one, and only
then apply — `artifact get` prints exact bytes on stdout, so it pipes
straight into `git apply`:

```bash
mempalace logstream wait --correlation-id task_fix_ranking_7f3a \
  --type patch.ready --to-agent mac-claude --timeout-ms 300000 --json

mempalace artifact get art_... --json | jq -r .sha256   # expected
mempalace artifact get art_... | shasum -a 256          # actual — must match

mempalace artifact get art_... | git apply --3way       # explicit, user-visible
uv run pytest tests/test_searcher.py -q
```

**5. Close the loop** — ack with the result, then file the outcome as a
drawer so the decision is searchable without replaying the event trail:

```bash
mempalace logstream ack evt_... --from-agent mac-claude --status applied \
  --body "Patch applied on abc1234; tests/test_searcher.py green."
```

`wait` is a long-poll: default timeout 60000 ms, capped at 300000 ms. On
timeout the CLI exits `2` instead of erroring, so agents loop on it, passing
`--since-event-id` of the last event seen; over MCP a timeout returns
`{timed_out: true, events: []}`. If the worker can't produce a patch, it
still replies — `task.reply` with `status=blocked` or `failed` and verbatim
notes. Silence is the only unrecoverable failure.

## 7. Keep agents wakeable

Everything so far works with agents that *check* their inbox. The step most
new fleets skip is making agents that get *woken* — because a delegation to
an agent that only polls at session start sits unread until someone happens
to open a terminal.

`mempalace logstream watch` (3.8.0+) is the primitive for this. It blocks
until an event matching its filters lands, then exits — so any harness that
can background a process and react to its exit gets a wake-up call, whatever
model it runs:

```bash
mempalace logstream watch \
  --agent <AGENT_ID> \
  --type task.request --type task.reply --type patch.ready \
  --state-file ~/.mempalace/watch/<AGENT_ID>.json --json
```

Four things to get right, in the order people get them wrong:

- **Use `--agent`, not `--to-agent`.** `--agent <id>` expands to
  `--to-agent <id> --exclude-from-agent <id>`. The exclusion is load-bearing:
  `to_agent=<you>` also matches `*` broadcasts, and your own broadcasts are
  broadcasts — a watcher without it wakes itself on every status it posts.
- **The exit code is the wake signal; the output is the mail.** `0` means
  a match was printed — hand the printed batch (with `--json`, ready-made
  JSON) to the agent, or let the agent sweep from **its own** last-processed
  event id. `2` means `--idle-exit-ms` expired with nothing; `130` means
  interrupted. Only `0` starts work.
- **Always pass `--state-file` — but it is the watcher's cursor, not the
  agent's.** The file lets a restarted watcher resume exactly where it
  stopped: it may replay the batch it printed but had not yet checkpointed
  — up to `--limit` events, 50 by default — so dedupe by event id; it
  never silently misses one. On a
  match, though, it checkpoints *past* the printed batch before exiting —
  and `since_event_id` is exclusive — so an agent that sweeps from the
  watcher's state file skips the very event that woke it. The agent's inbox
  cursor is the last event *it processed*, tracked separately. A cursorless
  first run starts at the live tip rather than replaying weeks of fleet
  history (`--from-start` if you really want the replay).
- **Repeat `--type` to mean "or" — and wake for replies, not just work.**
  Filter to the events that genuinely need you so routine status traffic
  doesn't burn wake-ups, but if you ever delegate, include `task.reply`:
  a worker reporting `blocked` or `failed` sends exactly that, and a
  watcher that rejects it advances its durable cursor past it silently —
  the delegation then sits unanswered until a manual sweep.

How the loop plugs into a harness:

- **A CLI agent** (Claude Code, Codex) backgrounds the command; on exit
  `0` it feeds the watcher's printed events to the agent, or has the agent
  sweep `mempalace_event_list` from the agent's own last-processed event
  id — never from the watcher's state file, which has already advanced
  past the match. Harnesses that re-invoke the agent when a background
  task finishes get the wake-up for free.
- **A daemon or dashboard** passes `--follow`, which stays alive past the
  first match; with `--json` it emits NDJSON — one compact **batch
  envelope** per line (`{"events": [...], "count": N, "cursor": ...}`),
  not one event per line. A single poll can match several events, so
  parse the `events` array.
- **A turn-based assistant** that stops existing between prompts cannot
  watch — and should say so rather than stay silent: publish the cursor it
  reached and state that it needs a ping. A false watcher is worse than a
  declared-absent one, because a requester who believes someone is
  listening stops looking for a human to nudge.

One operational trap deserves its own warning: **harness permission
prompts stall the loop silently**. If the harness gates terminal commands
or MCP writes behind human approval, every ack, reply, and patch
submission can block on a prompt nobody is looking at. From the other side
this is indistinguishable from a crash — the agent claimed a task and went
quiet — and a five-second round trip quietly becomes minutes or hours
(measured: the same ping that completed in 5 seconds once approved sat for
4½ minutes behind an unnoticed prompt). For unattended coordination,
allowlist the mempalace MCP tools (at minimum event append/ack and
`mempalace_patch_submit`) and the `mempalace logstream watch` command in
each harness's permission settings.

Two etiquette rules close the loop. **Announce your watch**: before a
coordinated task, post a `status` event to `to_agent=*` naming the filter you
watch and the cursor you have reached, so others delegate to an agent they
know is home. Keep the announcement in a `status` type — one the fleet's
watchers sleep through — and announce once per session (again if your
filter changes), not on every re-arm:
an announcement typed as something watchers wake on wakes every window, and
self-exclusion only guards each agent against its own events. And **resume by event id, never by timestamp**: events append
in arrival order, so a peer's event can sync in already "older" than a
wall-clock high-water mark — `since_created_at` as a resume cursor drops it
permanently, `since_event_id` never does.

## Fleet roles and cadence

Not every agent should do every job. Lessons the fleet reported from its own
first delegations:

- **Route work by agent type.** CLI agents with persistent terminals
  (Claude Code, Codex) take builds, test runs, and patch production. Desktop
  assistants take quick recall lookups, hash verifications, status
  summaries, and filing outcomes — short, synchronous actions that survive
  the user closing the window mid-session. Don't delegate a test suite to
  an agent whose session can vanish at any moment.
- **The desktop assistant is the user's gateway.** Users don't read the
  logstream; they ask their assistant. Its most valuable fleet role is
  translation — turning `patch.ready` events into a plain-language summary,
  and turning conversational intent into well-formed coordination events.
- **Match inbox cadence to agent shape.** A CLI agent with a persistent
  terminal should run `logstream watch` in the background and be woken
  (see [Keep agents wakeable](#_7-keep-agents-wakeable)); an ad hoc
  assistant should check at session start and before long tasks, and no
  more.
- **Watch your whole inbox, not just known tasks.** A watcher filtered on
  one `correlation_id` misses unsolicited requests and broadcasts. Watch
  (or poll) `to_agent=<you>` — which also matches `*` — with the event id
  as the cursor; `watch --agent <you>` does exactly this while filtering
  out your own broadcasts.
- **Keep memory writes user-visible.** File a drawer when a durable decision
  is made — and say so ("saving this decision to the shared brain") rather
  than filing silently. Transparency is what makes a fleet the user cannot
  directly inspect trustworthy.
- **The event body is the work order.** Workers execute exactly what the
  `task.request` body says — branch, base commit, definition of done — not
  what chat history or memory drawers suggest. Claim first, then follow the
  body; if the body is ambiguous, reply asking rather than improvising.
- **Mind cross-platform workers.** A Windows worker hits quoting, CRLF, and
  path differences a Unix requester never sees: generate diffs with LF
  endings, expect Unix-specific tests (bash paths, file-mode assertions) to
  need platform guards, and state the OS in replies so failures triage
  fast.

## Hard rules

The same non-negotiables that govern memory govern coordination:

- **Append-only.** Events are immutable. Corrections are new events
  (`status=superseded`) referencing the old one — never edits or deletes.
  Even `logstream ack` appends an `event.ack` event; it never mutates the
  original.
- **Verbatim payloads.** Bodies and artifacts are exact — no summarized
  diffs, no truncated logs. Too big for a body? Store it as an artifact.
- **Close every loop.** Every claimed `task.request` ends in `applied`,
  `failed`, or `blocked`. No dangling `open` tasks.
- **Never apply a patch silently.** Fetching an artifact is free; applying
  it is an explicit local decision, stated to the user.
- **Verify hashes.** An artifact's `sha256` must match its content before
  you act on it.
- **Store diffs byte-exactly.** A patch stored without its final newline has
  its last hunk line truncated — `git apply` rejects it as corrupt — and
  CRLF line endings are often rejected too. Pipe `git diff` straight into
  `artifact put` rather than copy-pasting; the store warns at store time on
  both problems (CLI warnings go to stderr, so `--json | jq` stays clean).
  Treat a warning as a broken handoff and re-store the diff.
- **File the outcome.** When a delegation concludes, write one drawer
  recording what was decided, so the result is searchable without replaying
  the event trail.

## Operating the shared brain

- **Upgrades**: the hub process serves the tool list, so new tools (or a new
  MemPalace version) appear fleet-wide after a **hub restart**. Stdio
  proxies re-check for a live hub on every request, so clients follow a
  restarted hub — even on a new port — with no restart or reconfiguration.
  MCP *clients* cache tool lists, though: after a hub upgrade, have each
  agent refresh its tools, and when one reports a tool "missing", make it
  state the exact set it can see — a stale client cache looks identical to
  a hub problem otherwise.
- **Debug connections outside the agent first**: when a remote agent can't
  reach the hub, check `healthz` (no token) and then an authenticated
  `mempalace_status` from a plain `curl` before touching any agent config.
  Tailnet, TLS, and token failures otherwise masquerade as agent or plugin
  bugs.
- **Monitoring**: `GET /healthz` is a token-free liveness probe.
  `GET /statusz` (follows the bearer-token policy) returns JSON with
  version, uptime, request counters, SQLite integrity, writer mode, and
  recently observed MCP clients — a quick way to confirm every agent in the
  fleet is actually connected.
- **Coordination survives index work**: the logstream lives in its own
  `logstream.sqlite3` next to the palace and opens no vector-index handles.
  Delegations keep flowing while the palace is being mined, repaired, or
  rebuilt. Logstream calls are also served outside the hub's global request
  lock, so one agent's five-minute `event_wait` never stalls the rest of
  the fleet.
- **Live streaming**: `GET /logstream/stream` serves the coordination feed
  as Server-Sent Events (bearer-token policy applies). It accepts the same
  filters as `event_list` plus `since_event_id` (or a `Last-Event-ID`
  header) to replay-then-tail; without a cursor it tails only post-connect
  events. Each frame's `data:` is the same JSON envelope `event_list`
  returns; heartbeat comments flow every ~15 s. Concurrent stream clients
  are bounded (`MEMPALACE_SSE_MAX_CLIENTS`, default 8) — on 503, fall back
  to `event_wait` long-polling, which is supported forever.

  ```bash
  curl -N https://memory.example.com/logstream/stream?stream=project/myapp \
    -H "Authorization: Bearer $MEMPALACE_MCP_HTTP_TOKEN"
  ```
- **Read-only observers**: a hub started with `--read-only` exposes recall
  plus `event_list`, `event_wait`, and `artifact_get`; mutating tools —
  including `event_append`, `event_ack`, `artifact_put`, and
  `patch_submit` — are hidden and refused. Useful for a dashboard or an
  agent that should watch the fleet but never write.

## Coordinating across machines

Everything above uses one hub as the fleet's shared memory. Agents on other
machines can join that hub's coordination stream without giving up their own
local palace: **each machine runs its own hub, and the hubs sync their
logstreams with each other.** An agent's inbox then survives any single
machine sleeping.

Two steps per machine:

1. **Run a hub locally** (same `mempalace serve` as above, LaunchAgent /
   systemd unit recommended) — agents on that machine point at `127.0.0.1`.
2. **Name the peers** in `peers.json` in the palace directory — each entry
   is a `name`, the peer hub's `url`, and its bearer `token` (exchange
   tokens out-of-band; never through the coordination stream):

   ```json
   {
     "peers": [
       { "name": "desktop", "url": "https://desktop.example.com", "token": "..." }
     ]
   }
   ```

   The hub's background loop picks up `peers.json` changes within one sync
   cycle — events and artifacts converge every `MEMPALACE_SYNC_INTERVAL`
   seconds (default 15) with no further action. Sync is multi-master and
   idempotent: every replica carries every origin's events, so two machines
   that have never exchanged credentials still converge through a common
   peer, and a machine that was offline for a week just re-pulls the tail.

`GET /sync/peers` on any hub shows the estate: which peers were reachable
last round, their version vectors, and any replicas known only through
gossip. The same payload is the `mempalace_mesh_peers` MCP tool.

::: warning This syncs coordination, not memory
Peer sync covers the **logstream** — events and artifacts. Each machine's
drawers and knowledge graph stay local to that machine. Agents on two
synced machines share an inbox and can hand patches back and forth, but
they do not yet share recall: ask one of them what it remembers and you get
that machine's palace.

Replicating memory itself is [RFC 004](https://github.com/MemPalace/mempalace/blob/develop/docs/rfcs/004-replicated-palace.md),
staged for a later release. If you want one shared memory across machines
today, point every agent at a single hub ([Remote / Team
Server](/guide/remote-server)) instead of running one per machine.
:::

## See also

- [Agent Logstream](/concepts/agent-logstream) — the event/artifact model in depth
- [Remote / Team Server](/guide/remote-server) — full hub deployment: tokens, TLS, backends, Docker/systemd
- [MCP Integration](/guide/mcp-integration) — the memory tools every connected agent gets
- [CLI Reference](/reference/cli#mempalace-logstream) — `mempalace logstream`, `mempalace artifact`
