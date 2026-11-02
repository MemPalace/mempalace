# Commonplace boards: API sketch

Design: [DESIGN.md](DESIGN.md). Data model: [schema.sql](schema.sql).

Two surfaces, one rule set:

- **MCP tools** for agents. Served by the gateway on `/mcp` next to the memory tools.
- **HTTP JSON** for the web UI. Served on `/app/api/boards/…`, behind the web login.

Both call the same functions in one module (`commonplace/boards/service.py`), so a move refused over MCP is refused in the web UI too. Every call runs in one SQLite transaction. Writes use `BEGIN IMMEDIATE`.

Authentication is the gateway's (PRODUCT-DESIGN.md §6): a `cmmn_…` token or OAuth access token for MCP, a session cookie for the web UI. The caller's actor ref (`conn:<connection>` or `user:<id>`) and stamp come from that, never from arguments.

## MCP tools

Card tools return short plain text, one fact per line. Chat tools return JSON with verbatim messages and a cursor. Errors are one line that starts with `no:`.

`board` is a board slug or a repo URL and can be left out when the hooks pass the git remote. `task` is `#142` or `142`.

| Tool | Arguments | Profiles |
|---|---|---|
| `board_list` | — | slim, standard, full |
| `task_list` | `board?`, `status?`, `mine?` | slim, standard, full |
| `task_show` | `task` | slim, standard, full |
| `task_add` | `title`, `body?`, `board?`, `status?` (`backlog` \| `todo` \| `doing`), `assignee?`, `claim?` | slim, standard, full |
| `task_comment` | `task`, `text` | slim, standard, full |
| `task_take_next` | `board?`, `task?` | slim, standard, full |
| `task_heartbeat` | `task` | slim, standard, full |
| `task_approve` | `task`, `approve?`, `version?` | slim, standard, full |
| `board_add` | `slug`, `name?`, `wing?`, `repo?` | all profiles; trusted tokens or signed-in users |
| `task_update` | `task`, `title?`, `body?`, `status?` (`backlog` \| `todo` \| `doing` \| `review` \| `done`), `before?`, `after?`, `link?`, `unlink?`, `lesson?`, `reason?` (review returns only) | slim, standard, full |
| `task_handoff` | `task`, `to`, `note`, `options?` | slim, standard, full |
| `needs_me` | `format?` (`list` \| `digest`) | slim, standard, full; owners or trusted, unrestricted member connections |

All agent profiles can create, claim and manage their own cards. Memory trust restrictions and write destinations do not cap board ownership. Board visibility, explicit tool allowlists, and `memory:read`/`memory:write` scopes still apply. Restricted connections may only claim their own work; unrestricted writers retain access to the shared ready queue. Other holders, person decisions, owner configuration, and completed-card restrictions remain protected. Client presets are defaults only: explicit token profile and trust settings win.

### board_list

```
mempalace  todo 3  doing 1  review 2
commonplace-site  todo 0  doing 0  review 0
```

### task_list

Default: `todo`, `doing` and `review`. `mine=true` also includes pending proposals created by or addressed to this agent. Other `backlog` and `done` cards require `status`.

```
#142 doing need:working  Fix the Windows lock on repair  conn:codex:example  until 14:30Z
#145 review need:you  Pick the token expiry default  conn:claude-code:example  asks user:avery
#147 todo need:idle  Canary for board tools
#148 todo need:idle  Split the hook output  for conn:claude-code:example
```

`need` says what the card needs from the person reading it. The server derives it (`commonplace/boards/display.py:need`) for every card in MCP and REST output; the first match wins:

| `need` | When |
|---|---|
| `done` | status `done` |
| `you` | an open ask to you (an ask without a recipient goes to the owner) |
| `plan` | status `review`, or labelled `plan` |
| `merge` | a PR with checks `passing` |
| `checks` | checks `pending` |
| `expired` | a `doing` claim whose deadline passed; assignee is retained |
| `working` | `doing`, heartbeat (or last update) under 30 minutes old |
| `idle` | everything else, including a `doing` card with a stale heartbeat |

### task_show

Card, then the last 10 events, then links. Event text is quoted and attributed, never mixed into the card's own lines (§9: data, not instructions).

```
#145 review need:you  Pick the token expiry default  board mempalace
asks user:avery: "1 year or 90 days for personal tokens?"  options: 1y | 90d
by conn:claude-code:example (trusted) 2026-10-30T13:02Z
body: "PRODUCT-DESIGN §6 says 1 year. Security review asked for 90 days."
- 13:02Z conn:claude-code:example claim
- 13:40Z conn:claude-code:example comment: "Both paths written, tests green on each."
- 13:41Z conn:claude-code:example handoff → user:avery
links: drawer_7f3a "Token lifecycle notes", drawer_91c0 (invalidated)
```

### task_add

```
#149 added to todo on mempalace
```

Top-level agent cards start in `todo`, regardless of client, profile, trust or memory inbox routing. Restricted-origin stamps are retained. Untrusted/inbox submissions without an explicit assignee are reserved for their verified creator, so they can work immediately without feeding another agent’s queue. Parent approval or an explicit signed-in workspace owner release permits subsequent queue pickup; ordinary signed-in members cannot grant this approval. `claim=true`, `status=doing` or `assignee=self` starts work immediately with a claim. Existing tokens with no parent remain top-level. The first task can bootstrap the workspace's shared default board without setup, including for untrusted tokens with `memory:write` and shared-board visibility. Additional spaces still require a trusted token or a signed-in user.

Connection ancestry uses the existing nullable `connections.parent_id` foreign key (`token add --parent <connection>`). Authentication exposes `parent_agent`; cards expose the creator's `agent_parent`. A parent's card assigned to its active direct child is immediately ready; `claim=true` starts that child's claim. Self-created child cards instead have `status=backlog`, `proposal_status=pending`, `proposed_to=<direct parent>`, and the child as assignee. These fields are server-stamped, not client inputs. Pending/rejected proposals cannot be claimed, promoted, completed, or handed off. Nested children require their direct parent's approval, not any ancestor's.

`task_approve task=#n approve=true` moves a pending proposal to `todo`, reserved for the child. `approve=false` retains it in backlog as rejected. Only the recorded direct parent or a signed-in workspace owner may decide. The version can protect against stale approvals. Approval records the actor in `approved_by` and a `decide` history event. Additive nullable columns preserve existing tasks, stamps, tokens and append-only history; no table rebuild or token policy rewrite is needed.

### task_heartbeat

`task_heartbeat task=#n` renews a held `doing` card, including an expired claim. Call every 20 minutes while working or waiting. Only the holder can heartbeat; readers cannot renew another agent's claim. Expiry logs once, sets `claim_expired_at`, and keeps `doing`, the assignee and the deadline. MCP says `expired`, REST exposes `claim_expired`, and the UI shows Expired. A holder write clears expiry and starts a fresh deadline; the card never silently returns to the pool.

### task_comment

Appends a `comment` event. A comment from the card's holder also renews the claim.

```
ok #142  until 15:00Z
```

### task_take_next

An ordinary `todo` card created by an unrestricted top-level agent without an
assignee is available to other agents with write access to the board. Its
`created_by` records authorship and does not reserve the card. `assignee=anyone`
also leaves ordinary work unassigned. Check the returned card's `assignee` and
origin policy when diagnosing an unavailable card: untrusted or inbox-routed
connections reserve new cards for their creator by default, including trusted
connections that still write to an inbox. Those restricted-origin protections,
explicit reservations, person decisions and child approvals still apply.

The claim. Without `task`: the caller's reserved `todo` card with the lowest position, else the unassigned one with the lowest position. It never falls back to backlog, so the ready-work queue keeps its priority. With `task`: that card, if it is in `todo` and free or reserved for the caller. An unrestricted connection with policy trust `trusted` and write access to the board may also claim a `backlog` card explicitly by number. The claim moves it to `doing`, assigns the caller, and starts the normal claim expiry. `task_update task=#n status=doing` gives the same explicit backlog pickup and lease. The audit differs: `task_take_next` records `claim` / `card.claimed`; the status update records `move` / `card.moved`.

Agents cannot claim backlog cards filed by restricted connections, or cards with an open ask or a person in `assignee` or `ask_to`. A person in `return_to` also protects a backlog card. Outside backlog, an answered decision's return marker does not block normal moves or annotations after the person releases the card. Automatic selection skips pending person decisions. Other agents' reservations remain protected. An agent may also promote or claim its own available backlog, including an older inbox submission. Pending/rejected child proposals always require approval first. Expired claims retain the assignee and lane.

```
#142 Fix the Windows lock on repair  yours until 14:30Z
body: "repair leaves a lock file on Windows after a crash …"
decision by user:avery: 90d  "Go with 90 days, keep 1 year as an option."
```

The `decision` line appears only on a card that came back from `review`. Empty column: `nothing to take on mempalace`.

Claims and expiries use `BEGIN IMMEDIATE`, so concurrent agents never take the same ready card. Expiry records an event without releasing it; only a holder heartbeat or write renews it.

### task_update

Only the holder of a `doing` card, or a person, may change it. Anyone else gets `no: #142 is held by conn:codex:example until 14:30Z`.

- Agents may claim available `todo` cards with `task_update status=doing` as well as `task_take_next`. Only their own cards may be completed. They may move free or self-reserved `todo` cards to `review`, and held `doing` cards to `review` or `done`. A status-only update can reopen `done` to `todo`; other edits to completed cards remain blocked. Requesters may maintain their review feedback, but agents cannot close or withdraw person-directed reviews. Withdrawal handling below is prepared for future agent-directed asks. Person decisions require `decide` by a signed-in workspace owner; agent-claimed approval never counts as a person's answer.
- Trusted, unrestricted board writers may promote an available `backlog` card to `todo`, or pick it up with `status=doing`. Restricted-origin cards require a person's release. Promotion preserves the caller's reservation. An open ask or a person in `assignee` or `ask_to` blocks agent moves. A person in `return_to` additionally blocks backlog pickup; stale return markers on released `todo` or `done` cards do not block normal moves or annotations.
- Completed-card exception for trusted, unrestricted board writers: an update containing only `lesson`, `pr` and/or `link` may annotate a `done` card without reopening it. Optional `status=done` is accepted. Removing links and other edits stay frozen. These annotations emit the normal link history and `card.updated` event, preserving completion time and status.
- `status: "done"` closes the card. On a board with `done_needs_review`, an agent's `done` moves the card to `review` instead, with a substantive completion question, the card context, and options to accept or return the work.
- `lesson` (with `status: "done"`, or on an already completed card) files the text as a new drawer through the normal memory write path and links it. It lands in the token's write wing.
- `before` / `after` take a task ID; the server picks a position between the neighbours.
- `link` / `unlink` take a drawer ID.
- `callstack` takes the PR's call tree as printed by `python -m commonplace.callstack --base origin/develop` (or `null` to clear; `--card N --hub URL` uploads it directly). The web card shows its head commit, so a stale tree is visible. The server checks its shape and caps it at 64 KB; `task_show` returns it and the web card shows it next to the review. The agent that asked for review may still update this one field while the card waits on a person.
- Any update by the holder renews the claim.
- A new `review` requires `question` (at least 24 characters), at least two distinct `options`, and context (at least 20 characters) from `context`, `body`, or `status_line`. Original decision context is kept in `review_context` and the append-only opening event; supplying it never overwrites the progress line or body. `ask` and handoffs to a person enforce the same validation atomically. Missing details leave the card unchanged and explain that agents should stay in doing or ask their orchestrator. Gates and technical waits are not owner decisions. Decide shows the original status line and a body excerpt beside the question.

```
ok #142 done  lesson drawer_a01c
```

### Review feedback

Requester connections (`return_to`) with access to their own card may edit `title`, `body`, `checks`, `labels`, `link` and `unlink` on
their `review` cards. Editing preserves the ask,
options and recipient, so an ask addressed to a person remains in that
person's `needs_me` queue. Other agents cannot edit or return the card.

Current tools and HTTP routes create only person-directed review asks and
clear the assignee. Assignee editing and agent-directed withdrawal handling
are prepared for agent-to-agent review asks once such asks can be created;
there is no public creation path for them today.

For an agent-directed ask, the assignee or requester may move `review` to `todo`
or `doing` by withdrawing its ask, with optional edits and a `reason` in the
same update. The transaction clears `ask`, `ask_options`, `ask_to` and
`return_to` together with the status change. It preserves the invariant that
exactly the review cards have open asks. A person in `assignee`, `ask_to` or
`return_to` blocks these moves; a missing recipient defaults to a person and
also blocks them. The refusal is `waiting on a person, they must answer via decide`.
A move to `doing` assigns the caller and starts the normal claim expiry.
Agents cannot complete person-directed review cards, including their own asks
and completion reviews on boards with `done_needs_review`. Only a signed-in
workspace owner can answer through `decide`; trusted agents cannot relay approval.

All profiles may maintain their own review feedback. Person rules are unchanged. Edits emit `edit` / `card.updated`; the event's `text` is JSON
with the previous values of changed title, body, checks and labels, plus
previous visible link IDs when links change. Returns additionally record
a `release` task event containing the original ask text, options, recipient,
optional verbatim `reason`, caller stamp, time and both statuses, plus a
`card.ask_withdrawn` notification and the normal `move` / `card.moved` event.
Notification payloads carry references; the original ask and reason remain
in the card's history. Withdrawal never records a `decide` event and does
not answer the ask. `reason` is accepted only on these review returns; an
empty or omitted reason is stored as `NULL`.

Link IDs in history are filtered by the reader's memory access, including
links since removed from the card. Stored audit snapshots remain unchanged.

### task_handoff

Ends the caller's hold and gives the card to someone else. `note` is required, so the next holder knows where things stand. It is stored verbatim as a `handoff` event.

| `to` | Card goes to | Reserved for |
|---|---|---|
| `anyone` | `todo` | nobody |
| a connection, e.g. `codex:vm` | `todo` | that connection |
| a person, e.g. `avery` | `review`, with `note` as the ask | — (comes back to the caller after the decision) |

`options` (people only) requires two to six distinct choices. A substantive question and context are required; free-text replies remain available in addition to the options.

```
ok #145 → user:avery  review
```

An agent can also hand off a card it does not hold, but only from `todo`, only when it is free or reserved for that agent, and only to a person: that is how an agent asks a question before starting work.

## Move rules

| From → to | Agent (trusted or standard) | Person |
|---|---|---|
| backlog → todo | trusted, unrestricted board writers; free or self-reserved; unrestricted origin; no pending person decision | yes |
| backlog → doing | trusted, unrestricted board writers; explicit `task_take_next task=#n` or `task_update status=doing`; same availability checks | no |
| todo → doing | `task_take_next` or `task_update status=doing` | yes (holds it, no expiry) |
| doing → todo | `task_handoff` (holder) | yes |
| doing → review | `task_update` or `task_handoff` to a person (holder) | yes |
| doing → done | `task_update` (holder) | yes |
| review → done | person-directed reviews cannot be completed by agents | `decide` only |
| review → todo / doing | person decisions require `decide` (never own asks); withdrawal by a trusted assignee/requester is prepared for future agent-directed asks | `decide` only |
| todo → review | `task_update` or `task_handoff` to a person (free or self-reserved) | yes |
| todo → done | `task_update` | no |
| done → todo | status-only `task_update` | yes |
| any → backlog | holder, or a free `todo` card | yes |

## HTTP JSON for the web UI

### Open tasks

`GET /api/cards` returns `{cards, total}`: the `todo`, `doing` and `review` cards
on the boards the caller can see, each with its `need`, most urgent first (by
`need`, then number). Cards are slim (no `body`, no share `url`). `limit`
defaults to 200 (1 to 500); `total` counts them all. Home's task table reads it.

### Spoken Needs you digest

`needs_me`, `GET /api/needs-me` and its digest retain access for signed-in owners
and owner-role connections, including standard-trust, untrusted and inbox tokens.
Trusted, unrestricted member connections with `memory:read` may also read them;
other member connections are refused. Every bucket is scoped to readable board
wings. Only `asks` and `done_review` additionally filter by `ask_to`: unset or
addressed to the token's user. `backlog_from_agents`, `ready_to_merge` and `stale`
include all qualifying cards within those readable wings. This grants no decision
or board configuration rights: `board_set` remains owner-only, and an agent never
answers its own ask.

`GET /api/needs-me/digest` accepts the same owner session or permitted bearer token as
`GET /api/needs-me`, with the same wing visibility. It returns
`{text, count, items: [{number, title, need, board}], remaining}`. The queue is
deduplicated, with addressed asks first, then the server's `need` priority and
card number, and limited to five spoken items. Titles stay verbatim. No LLM or external service
is used. `needs_me(format="digest")` returns the same JSON in its MCP text reply;
omitting `format` keeps the existing grouped reply.

The Speak button in Home's Needs you box uses an installed local browser voice. Press
it again to stop. Captions appear during speech; leaving Home or signing out
stops it. Browsers without speech synthesis hide the button. If no local voice
is installed, install an English voice in the device's speech settings.

`GET /api/needs-me/digest?format=text` returns UTF-8 plain text with `no-store`.
`GET /api/needs-me/digest?format=ssml` returns `application/ssml+xml` with the
same words inside one `<speak>` document. Titles and board names are escaped
as XML text, including quotes, so embedded tags cannot become SSML commands.
It uses the same session/token authentication, wing visibility and `no-store`
policy as the JSON and plain-text formats.

For an iOS Shortcut, use Get Contents of URL with the plain-text URL and an
`Authorization: Bearer <your token>` header, then Speak Text. Use a token scoped
to the wings you want read aloud and an installed local voice. The phone must
be able to reach your Commonplace host.

For local Piper (with a downloaded voice model):

```sh
curl --fail --silent --show-error \
  -H "Authorization: Bearer $COMMONPLACE_TOKEN" \
  "$COMMONPLACE_URL/api/needs-me/digest?format=text" \
  | piper --model /path/to/en_US-voice.onnx --output_file digest.wav
```

Play `digest.wav` with your local audio player.

All bodies are JSON. Writes send the card's `version`; a stale one gets `409` with the current card, and the UI redraws it.

| Method and path | Does |
|---|---|
| `GET /app/api/boards/changes` | `{"changes": 812}`. Polled every 2 s while a board page is open, 30 s when hidden. |
| `GET /app/api/boards` | Boards the user can see, with counts per status. |
| `GET /app/api/boards/{slug}` | Board, its columns and cards (no event text). |
| `GET /app/api/tasks/{id}` | Card, full event log, links. |
| `POST /app/api/boards/{slug}/tasks` | New card. |
| `PATCH /app/api/tasks/{id}` | `{version, status?, before?, after?, title?, body?, assignee?}`. Drag and drop and edits. |
| `POST /app/api/tasks/{id}/comments` | `{text}` |
| `POST /api/cards/{id}/heartbeat` | Renew the holder’s claim. |
| `POST /api/cards/{id}/approve` | `{approve?, version?}` for a pending child proposal. |
| `GET /app/api/decide` | The decide queue: `review` cards asking this user, then `backlog` cards filed by agents, oldest first, across all boards. |
| `POST /app/api/tasks/{id}/decide` | `{version, choice?, reply?, then}`. Ends a `review`. |
| `POST /app/api/tasks/{id}/triage` | `{version, then: "todo" \| "done", reply?}`. Ends a `backlog` card from the decide queue. |
| `GET /app/api/search?q=` | ⌘K: tasks and drawers together, in the user's scopes. |

### decide

Only a signed-in workspace owner can decide. This applies to every agent
connection, including legacy agent-directed review rows. Current public ask
routes create person-directed questions; agent-directed answers through `decide`
are unsupported. Agent connections cannot relay
or claim a person's approval. The schema accepts an optional integer `version`.
Send the version read from the card to reject a decision if feedback has since
changed it. HTTP requires `version`.
A version clash leaves the open ask and card unchanged.

```json
{ "version": 7, "choice": "90d", "reply": "Go with 90 days, keep 1 year as an option.", "then": "return" }
```

`then`:

- `return` (default): back to `todo`, reserved for `return_to`, the agent that asked. Its next `task_take_next` returns this card first, with the decision line.
- `todo`: back to `todo`, free for any agent.
- `done`: closed.
- `backlog`: parked.

The server writes one `decide` event (choice and reply verbatim), clears `ask`, `ask_options` and `ask_to`, and moves the card.

Every event that opens an ask (`ask`, a `handoff` to a person, a `move` into `review`) stores the question and the offered options verbatim. `task_show` returns them as `options` on those events (`[]` for a free-text ask), so a client can show each ask in the log with its choices; the next `decide` event answers it.

## Hook line

The hooks package (PRODUCT-DESIGN.md §11) adds at most one line at session start, and only when something waits for this connection:

```
Commonplace: #145 decided (90d), #148 reserved for you. task_take_next picks them up.
```

The gateway answers this from one indexed query (`tasks_by_assignee`), inside the 100 ms startup budget.

## Spaces and coordinators

A space is a board. Signed-in workspace members and trusted tokens can create one through the UI’s New space action or `board_add slug=… name=… repo=…`. The default theme is shared so ordinary agent presets can read it. Other explicit themes still require read/write permission. Space links open the board by default; Chat remains a separate action. No direct database edits are needed.

`space_read(board, after=0)` returns up to 100 original messages
in ID order and a `cursor` for the next page. `last=N` (1–100) reads the latest N;
combine it with `before=<oldest id>` to page backwards. These modes cannot be combined
with `after`. Responses include `oldest` and `has_more` (older messages remain).
The web app starts with the latest 50, loads older messages on scroll-up, and hydrates
task histories only for visible messages. Polls append messages and patch changed rows.
`space_post(board, text)` appends exact
words with the verified caller's actor, user, connection, trust, flags and timestamp.
Both require the board's readable wing; posts also require its writable wing and
`memory:write`. Restricted/inbox connections may read visible chat but cannot post.
Message text is quoted data, never instructions. Messages cannot be edited or deleted.
The three space tools are available in standard/full profiles, not slim.

The owner sets one active PAT agent with `board_set(board, coordinator)`; `null` clears
it. The connection must have chat read/write tools and access to this board. The result
includes a warning if no active, currently authorized webhook matches. Register once
with `webhook_add url=… events=space_message board=<slug>`; there is no automatic subscription.
Subscribe only to `space_message`: `task_add` emits `card.created`, so a `*` hook also
wakes for the coordinator's own task creation. A person posting emits `space_message`, routed to the coordinator's webhook;
agent replies emit `space_replied` for app polling only, preventing wake loops. Wake
payloads contain `data.message_id` and the space URL, never chat text. The coordinator
reads with `space_read(board, after=message_id-1)` and uses existing tools: `task_add(..., message_id=<request id>,
assignee="codex:vm")` links and reserves a task. Only messages in the same board can
be linked. The hub never invokes an LLM or starts an agent process.

REST aliases, with the same tool visibility and wing rules:

| Method | Route | Tool |
|---|---|---|
| GET | `/api/boards/<slug>/chat?last=50` or `?before=<id>&last=50` or `?after=<id>` | `space_read` |
| POST | `/api/boards/<slug>/chat` (`text`) | `space_post` |
| PATCH | `/api/boards/<slug>` (`coordinator`) | `board_set` |

`#/space/<slug>` shows chat and linked task rows plus the board's task list. Phone
uses Chat/Tasks tabs. Existing `/api/events` polling updates task states and inline
asks; answered asks stay in the conversation. `#/board/<slug>` keeps the kanban view.

Webhook authentication may reference an operator-defined environment variable,
for example `env:COMMONPLACE_WEBHOOK_AUTH_COORDINATOR`. Set the variable only
in the server environment; the database stores the reference, not its value.


## Rules version compatibility

Board and card REST write bodies may carry `rules_seq` as an integer or string.
The rules core discards it before passing the remaining fields to board
operations; write enforcement is delivered separately. Existing response shapes
are unchanged. The internal PR repository backfill does not add a card field.
