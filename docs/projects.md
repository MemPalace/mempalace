# Projects and orchestrators

Owner [Admin overview](admin.md) reads existing project configuration without creating seats. It shows effective caps and actual usage under the current orchestration counting mode.

Every project (board) gets one main orchestrator seat automatically, normally
named `orchestrator:<project>`. Older projects get their seat when first viewed.
An existing coordinator stays the main orchestrator until an owner replaces it.

## Connect an agent

Connect creates a token for the project's automatic orchestrator seat; an agent
is attached only once it uses that token. A signed-in workspace owner uses
**Connect agent** in the chat header or project settings, then follows the
short checklist: copy the setup for their agent app (Claude Code, Codex, Cursor
or generic MCP), paste it into the app's MCP servers, and start the app.
The token is shown once; closing the result hides it. Connect does not start
an agent process. If another active coordinator holds the project, replacing
it requires confirmation.

The connection has standard trust and the standard profile, with memory read
and write access to the project's wing. A wing can serve several boards, so
this is a memory-access boundary, not a guarantee of access to only one board.
For a project using the shared wing, Connect requires explicit confirmation
that the agent will have read and write access to all shared memory.

Tokens expire after 90 days. Connecting again rotates an active token and
invalidates the old one. After revocation or expiry, Connect creates a fresh
connection. Giving a project its own memory section revokes its displaced seat
token; Connect then creates a fresh one. Connections can also be rotated or revoked
on the Agents screen.

Register a `space_message` webhook for the project if the agent should wake
when someone posts; Connect supplies setup details but does not subscribe it.
Without a webhook, the agent reads messages on its next check.

Connect additionally recommends `card.moved` so worker completions and other card
moves can wake the seat. Connect and reconnect neither register nor modify
webhooks; existing users must explicitly update their subscriptions for card moves.

The header shows the main orchestrator and its connection state. “Token
created · no agent has used it yet” means the credentials exist but no agent has
used them; it reads “connected” only after an agent has. Private status stays private. An unconnected seat leaves release and
supervision with the existing people; a seat takes those duties only after its
agent has used the connection within the last 24 hours.

## Model choices

Owners can save provider and model labels for the main orchestrator and workers
in project settings. These choices are stored only: saving them does not call a
provider or start work. Applying them through the runner comes later. They do
not change the separate workspace chat model settings.

Model resolution uses the project's choice, then the workspace default for
that role, then the runner's own default. Clearing a project choice restores
that fallback. No API key is stored with these labels.

## Events and API

Creating a board emits `space_created` with `data.board` set to its slug and no
chat text. Wildcard webhook subscribers receive this event too. A failed seat
listener does not block board creation; the next project view repairs the seat.

See [project routes](boards/API.md#project-routes) for the API contract and
[project chat](chat.md#coordinator-contract-and-card-fallback) for message routing.

## Give a project its own memory section

Shared projects show why Connect would let an agent read and change all shared
memory. The primary action, **Give this project its own memory section**, calls
`POST /api/projects/<slug>/wing` with exactly `{}`. The alternative explicitly
allows access to all shared memory. Settings exposes the same own-section action.

An owner can convert a shared wing to `project_<slug>` (hyphens become underscores).
New boards still default to shared; owners opt into this conversion. A non-shared
wing is unchanged, but its coordinator and seat are still checked. Wildcard wings
are refused. Project views remain readable for pattern wings, but Connect keeps
its existing 400 rejection for them. Another board using that wing, including an archived board, blocks
conversion. Active connections that would gain read or write access also block it;
the response gives their count and redacted names so the owner can narrow them.
The scan cannot see a connection minted after it; this race is accepted.

Existing memory stays verbatim in its old wing. Cards, messages and memory links
are untouched. Future lessons follow the board wing; task-linked memory saves
without an explicit theme or pinned write wing default to a concrete `project_*`
wing if writable. This also applies to boards already using project wings. Other
boards and wildcard wings keep the shared default. Facts and handoffs are unchanged.
Board- or repo-scoped `session_start` searches only the current wing. Unscoped
memory search still finds pre-change shared memories with normal access checks.

Shared-only agents lose visibility of this board, chat, cards and webhooks;
shared runner workers stop claiming its work. `affected` reports other active
connections by redacted name, lost permission (`read`, `write`, `read+write`) and
whether they are runner-managed. It does not revoke those connections. Operators
must re-point them. A new seat cannot read old shared card links. Agents may get
one rules refresh on their next checked write after visibility changes.

A coordinator without access is cleared. Displaced orchestrator-seat tokens are
revoked, rather than rotated; reconnect creates a fresh token, often with a name
suffix because revoked names stay reserved. Runner-linked seats are reported in
`runner_linked`: their tokens remain active with their old policy until managed
through the runner registry, but their seat pointers are cleared. Repeating the
operation repairs interrupted reconciliation. Connect also reconciles mismatched
seats on every board, even boards never converted by this action.

There is no undo action here. Owner-held trusted or standard-trust PATs
may invoke the equivalent MCP `board_own_wing` if they can read and write both
wings. A seat minted by Connect lacks access to the target wing and cannot do
this itself. Restricted connections are refused.
The operator connects the project's seat afterwards.

## Opt-in agent orchestration

Any signed-in workspace person may read `GET /api/settings/orchestration`; only
owners may change it with PUT.

Workspace owners can enable **Orchestrators manage agent starts** in Settings
(`GET`/`PUT /api/settings/orchestration`, body `{"enabled":true}`). It defaults
to off. With it on, only the board coordinator's active PAT or a signed-in owner
can request starts on boards without runner config. Configured boards keep their
accepted config and `orch_card_start` rules.

Projects default to three slots; a project limit overrides that default. Runner
config uses the smaller of its cap and the project row. Ordinary agents count
once even with several doing cards; runner workers count per card. Queued starts
reserve slots at creation. Leases scan queued starts in order, skipping projects
at their cap, and admit the oldest eligible start using active slots. A capped
project does not block another project on the same machine. Board lists expose
`max_agents` and active `used` only while the switch is on. A person cannot reserve
another card for an agent already doing work on that board (`agent_busy`).
A person claiming a card manually is exempt from the agent cap.

Connect still creates a standard-trust seat. Owners separately approve permissions
in Project settings, choosing machines and confirming trust. The API is
`POST /api/projects/<slug>/orchestrator/trust` with
`{"confirm":true,"machines":["<machine-id>"]}`. Trust allows the seat to request
starts, inspect status, cancel its own starts, read permitted project data and
repository rules, release/approve and annotate cards, and stamp trusted submissions.
It does not allow board creation or handoff launch. Invalid grants return 400,
unknown machines 404, and revoked/disabled machines or conflicting authority 409.
A database lock timeout returns 409 `busy`. The whole change rolls back. Only signed-in workspace owners may use this route.

`{"trusted":false}` demotes the seat and removes only its requester id from
machines, preserving other entries even when stale. Switching orchestration off
atomically demotes all project seats and removes their requester ids. Other
requesters retain the existing behavior. Card/board and coordinator checks run
before the start transaction. Requester and current machine authority are then
rechecked inside it. A demotion, switch-off or revocation committed before that
control check prevents the start.
A settings read failure refuses admission with `settings_unavailable`; an absent
table means off.

With orchestration on, Connect suggests `space_message,card.created` subscriptions.
The recommendation also includes `card.moved`; pass the three events as the array
`["space_message", "card.moved", "card.created"]`.
Without a runner or a subscribed card webhook, new work waits for the seat's next
session; this switch does not run the orchestrator continuously.

## Opt-in card inbox and splitting

The workspace orchestration switch defaults off. When enabled, the main board
coordinator and signed-in workspace owners can call `orchestrator_inbox(board)`
or `GET /api/projects/<board>/orchestrator/inbox`. It returns open top-level
cards without a split marker, excluding coordinator-created roots and children;
Doing and Review roots remain visible. Each root includes its stable keyed
children. Existing connections gain the `space_message,card.created` setup hint
on the project view as soon as the switch is enabled; reconnecting is unnecessary.

Create missing children with `task_add(parent=N, labels=["split:N:build"])`.
The parent must be a root on the same board. One transaction checks the key
and inserts the child; concurrent requests cannot create duplicate keys.
`split_key_exists` (409) includes the existing child's `number`. The coordinator
chooses the keys and keeps them stable across retries; the hub never splits.
Finish with `card_split_done(task=N, state="split"|"kept")`, or
`POST /api/projects/<board>/orchestrator/split` with `{"task":N,"state":"kept"}`.
The first marker wins and is recorded in card history.

On a runner-configured board, enabled orchestration changes root todo triggers
to role `main` (or to the `[runner]` role when the config has no role named
`main`), with an inbox snapshot and instructions in the launch message.
Split children never trigger that start. The existing event deduplication,
one-active-start rule and 24-hour non-person start restriction still apply.
Every runner sweep reconciles unsplit roots older than 10 minutes without an
active or started job. Attempts use `split-retry:<card>:<n>`, with gaps of
10/30/90 minutes and at most three attempts. Two concurrent sweeps admit one
attempt under the runner fence. Three refusals show `not_released: split stuck`
in card waiting reasons and checkpoints; a person must resolve the cause and
arrange completion. Marking the root ends reconciliation.

Without a runner, delivery uses a subscribed `card.created` webhook and the
inbox on the next session. Without either a runner or a webhook, a new card
waits until that session; an always-running coordinator is separate work.
Turning the switch off preserves configured todo triggers and the prior card
shapes; the new blocker arguments and inbox tools are unavailable.

An unavailable inbox snapshot does not discard a committed runner lease: the
response keeps the pinned role instructions and asks the agent to read the inbox
before acting. Checkpoint inbox context is best effort. Label edits cannot remove
or replace a stable split-child key or make a root disappear into a split label.
