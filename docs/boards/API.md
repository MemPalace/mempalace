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
| `task_approve` | `task`, `approve?`, `version?`, `reason?`, `escalate?` | slim, standard, full |
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

Unrestricted top-level cards start in `todo` and can start with `claim=true`, `status=doing` or `assignee=self`. Restricted and child-origin submissions remain inactive in backlog, preserving their creator stamps and any pending proposal. A reservation, later promotion, handoff or parent/root role alone never releases restricted origin. The first task can bootstrap the workspace's shared default board with `memory:write` and shared-board visibility; additional spaces require a trusted token or signed-in user.

An owner-appointed independent board coordinator can deliberately release restricted origin with `task_approve(approve=true, reason=...)`, including while holding a doing card. It must remain live in this workspace with memory read/write, board-wing read/write and visible `space_read`, `space_post`, `task_approve` and `needs_me`; `decide` is not required. A creator and all its descendants are excluded even after promotion; independently appointed creator ancestors remain eligible. Unsafe lineage and unverified creator stamps fail closed for agent decisions and markers. Origin-only release requires a nonempty reason of at most 500 characters, changes neither holder nor lease, and preserves pending proposal/plan review. It grants no self-review, negative-decision or escalation right.

Only a signed-in workspace owner has recovery fallback: an absent/non-live configured record or a verified independent record missing a required grant permits origin release. A live creator, descendant, unsafe lineage or an unverifiable creator stamp with a live configured record instead yields `origin_configuration` with `configure_coordinator`, blocking owner release. For invalid stamps, clear the coordinator first; clearing is board-wide and affects recovery for other cards too. Owner-role tokens have no person fallback. Owner recovery of pending proposals takes two actions, origin release then separate review. Agent and owner origin items use the approvals bucket with `need=release`, `approval_kind=origin` and `actions=["approve"]`; owner configuration uses `need=you` and no approve action. Explicit person-targeted reviews remain independently visible.

When exactly one board is visible, `board` may be omitted. With several visible boards, pass its slug or repository explicitly; the server never silently chooses a different space. Agent assignees and handoff recipients must be active connections in this workspace with `memory:write` and visibility of the board. Connection names and IDs resolve to the same verified actor. A missing, revoked, expired or inaccessible recipient is refused before the card changes.

Connection ancestry uses the existing nullable `connections.parent_id` foreign key (`token add --parent <connection>`). Authentication exposes `parent_agent`; cards expose the creator's `agent_parent`. A parent's card assigned to its active direct child is immediately ready; `claim=true` starts that child's claim. Self-created child cards instead have `status=backlog`, `proposal_status=pending`, `proposed_to=<direct parent>`, and the child as assignee. These fields are server-stamped, not client inputs. Agents cannot claim, promote, complete, or hand off pending/rejected proposals. Nested proposals may be approved by their direct parent or owning top-level root, not an arbitrary intermediate ancestor. A root has no approval role solely from ancestry on an advisory plan review.

An authority change, owner decision 5 Nov 2026: `task_approve task=#n approve=true reason=…` accepts pending proposals or advisory plan reviews. A proposal moves to todo; a plan keeps status, assignee and claim and drops the plan label. Rejection retains any stored release marker; a plan returns to backlog with its label and assignee, and existing history informs the holder. Only the owner may reject a doing plan or recover a rejected proposal; a signed-in owner may also dismiss a rejected proposal to `done`. Agents supply a nonempty reason of at most 500 characters, never approve cards they hold, and may instead use `escalate=true reason=…` without `approve`. Escalation records the reason, routes to the owner and prevents further agent decisions until the owner decides. The version protects against stale decisions.

| Authority | Releases restricted work |
|---|---|
| Signed-in workspace owner | yes |
| Owning top-level root, any trust, not the holder | yes |
| Board coordinator | yes |
| Unrestricted direct parent, standard or trusted | yes |
| Unrestricted creator-parent of an assigned direct child | yes |
| Restricted non-root parent or restricted creator-parent | no |
| Holder, unrelated agent or top-level claimer | no |

Within approval decisions, only approval writes `approved_by`, and only when the new approver releases and the stored value no longer does. An owner moving restricted work to todo or doing also records an owner release. Reject never writes this column. Stored release markers are checked against current membership, activity and permissions; every agent marker is void while that agent holds the card. Approving a proposal also drops the plan label. The plan label is advisory, and a doing holder may drop it. Backlog plan-labelled cards and review-without-ask cards no longer derive need=plan. Nullable `escalated_to` is migrated additively; schema.sql is unchanged.

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

### Phone notifications

In Settings, a signed-in owner can turn on phone notifications for their home
workspace. Members cannot opt in. All three categories are enabled together:

| Category | When | Message |
|---|---|---|
| `needs_you` | An ask, completion review or agent backlog needs the owner | `#<n> needs you` |
| `blocked` | A doing card is stale and has no supervisor route | `#<n> is blocked` |
| `done` | A card naming the owner enters done | `#<n> is done` |

Phone notifications cover only your home workspace. Cards in other workspaces never notify this phone.

Only changes after you turn notifications on are sent.

Notifications pause while you are signed out everywhere, including when a sign-in expires after 30 days. Nothing you miss is sent later.

The pause applies when there is no live sign-in in the home workspace: new
deliveries are suppressed and queued deliveries are cancelled. Changes during
the pause are tracked so signing in does not replay them.

A category that keeps applying pushes once; leaving and entering it again can
push again. Your own card actions are silent; another owner's actions can notify
you. Failing checks never push. Claim expiry with a supervisor route never
pushes. `done` requires your user ID in `created_by`, `return_to`, `ask_to` or
`assignee`, and a transition after opt-in. With `done_needs_review=1`, an agent's
completion becomes `needs_you` instead of `done` (the default is `0`).

These routes require a signed-in person, accept no query parameters or options,
and use the caller's workspace. Request bodies must be absent or exactly `{}`;
session writes use the normal same-origin protection.

| Method and path | Response |
|---|---|
| `GET /api/phone-push` | 200 `{available, eligible, subscription, test}` |
| `POST /api/phone-push` | 200, same shape; Turn on, or return the existing subscription |
| `DELETE /api/phone-push` | 200 `{"ok":true}`; Turn off and cancel queued deliveries |
| `POST /api/phone-push/test` | 202 `{"status":"pending"}`; enqueue `Test from Commonplace` on your own topic |

`subscription` is `{server, topic, created_at}` or `null`; only your own active
subscription is returned. `test` is `{status:"pending"|"accepted"|"not_accepted"}`
or `null`. Turn on and Test return 409 if you are ineligible or the server is
unconfigured. Turn off still works then; an already-off subscription returns
404. A changing opt-in snapshot can return 503; retry Turn on. Re-enabling
creates a new topic. Configure the phone's read access for that topic separately.

With no `COMMONPLACE_NTFY_URL`, no thread or outbound call runs and
`available` is false. See the [operator setup and activation checklist](../../deploy/PUBLIC.md#phone-notifications-optional)
for configuration and device acceptance.

#### Privacy and disclosure

A notification that is already being sent when you turn this off can still arrive once.

A retry after a sender crash can duplicate a notification. There is no
exactly-once or receipt guarantee. “Accepted by your ntfy server” means a 2xx
response with a valid message ID, not proof that the phone received it or that
anyone tapped it.

The configured ntfy server sees the topic, card number, category, timing, hub
token's identity and opaque Click code. It keeps them for its own
`cache-duration` (default 12 hours). No card title, body, ask text, `why`, email,
slug, workspace name or actor is sent. A tap uses a permanent short link to open
the card, with sign-in when needed. Test notifications have no Click link.

The topic is a bearer-equivalent secret stored in plaintext in the control
database and its backups. Protect both. Topics never appear in logs, errors,
delivery rows or events.

On iOS without an upstream, “delivery can take hours, depending on the state of the phone. If you are using your phone, it shouldn't take more than 20-30 minutes though.” Setting `upstream-base-url` would disclose the message ID and a topic hash to a third party (e.g. ntfy.sh); this feature never enables it.

Use HTTPS for the ntfy server. Plain HTTP is accepted only for the local and
private addresses the webhook URL rule allows, because the publisher token is
sent with every message.

#### Limits

- `blocked` and `needs_you` changes without a card event are checked every five
  minutes. Event-less `needs_you` waits for a sweep where the workspace's card
  events are reconciled. Reconciliation catches up within one wake, releasing
  the work lock between batches; there is no five-batch cap.
- Event-less `needs_you` has no actor, so it can push even if you caused it by
  revoking the agent that could approve a proposal. A sweep between a board
  commit and its event emission can also push for your own action, at most once
  per card.
- An aborted batch retries from the same cursor on the next wake (a new event
  or within five minutes); it is never skipped. A persistent evaluation error
  stalls pushes and is logged. If event emission fails after a commit, `done`
  is not seen; `needs_you` is found by the sweep.
- A foreign-workspace tap asks you to sign out and sign in again for that
  workspace. The next refresh can clear this notice.
- Turn on can wait for reconciliation, a sweep or a delivery attempt in
  progress. One attempt can wait up to five seconds for the ntfy server; no
  overall wait bound is claimed. Turn off never waits for these operations.

### Spoken Needs you digest

`needs_me`, `GET /api/needs-me` and its digest retain access for signed-in owners
and owner-role connections, including standard-trust, untrusted and inbox tokens.
Trusted, unrestricted member connections with `memory:read` may also read them;
other member connections are refused. Every bucket is scoped to readable board
wings. Only `asks` and `done_review` additionally filter by `ask_to`: unset or
addressed to the token's user. `backlog_from_agents`, `plans`, `ready_to_merge` and `stale`
include all qualifying cards within those readable wings. This grants no decision
or board configuration rights: `board_set` remains owner-only, and an agent never
answers its own ask.

Agent `approvals` contains actionable proposals and plan reviews with roles and reasons. Heartbeat and startup approval reads use the same Needs-me read gate. `approvals_truncated` is a sibling boolean, never a bucket. Scans prefilter related rows, check board visibility before roles, and stop at 200 role checks (60 at session startup). Heartbeat and session startup quote titles and show a truncation note directing the caller to needs_me. Startup reads never create or migrate a board store. Owner plan rows have approve/reject actions; agent rows have notification only.

Owner-role tokens may configure a board's coordinator through `board_set` when their normal profile, trust, scope and board-write permissions allow it. This is delegated configuration authority, distinct from a person's decision: only a signed-in owner answers `decide` or recovers rejected proposals. Member-role tokens cannot configure boards.

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
| `POST /api/cards/{id}/approve` | `{approve?, version?, reason?, escalate?}` for a proposal or advisory plan review. |
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

Creating a shared space requires trusted token identity, independently of whether its memory saves go to inbox. Picking up another agent's backlog has the stricter trusted, unrestricted writer gate. Card titles and bodies follow the board's read visibility; restricted origin protects work ownership, not confidentiality. Use a private board theme for private card content.

Board creation emits the `space_created` event with resource `space:<slug>` and
data `{"board": "<slug>"}` after commit. It contains no chat text; wildcard
webhook subscribers receive it too. Each project gets one main orchestrator
seat automatically, with a lazy repair on first view if needed. See
[projects](../projects.md) for connection setup and lifecycle.

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
be linked. The hub never starts an agent process. It invokes a model only for the chat front agent, and only after a workspace owner configures one ([project chat](../chat.md)). Its only board write is filing an unclaimed chat work request as a linked todo card, as the poster ([project chat](../chat.md)).

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

## Runner chat routes

The hub never calls a model for runner answers; it relays redacted context to the poster's own runner, which runs the poster's own CLI.

These machine-only routes use the enrolled machine's bearer credential, not a
worker token, agent token or person session. Only enabled, non-revoked machines
may lease their enroller's jobs in their own workspace; binding attestation is
not required. Requests must be JSON objects with no query string or unknown keys.

| Method | Route | Body / result |
|---|---|---|
| POST | `/api/runner/chat/lease` | `{clis, context?, relay_always?}`; `clis` lists 1–8 CLI labels, `context` defaults to `"minimal"` (`"full"` is opt-in), `relay_always` defaults to false. Marks the machine online. 204 if no job; 200 with `{job, lease_token, cli, model, system, prompt, max_chars, expires_in}`. |
| POST | `/api/runner/chat/answer` | `{job, lease_token, text, cli, model}`; 200 with `{job, state: "done"}` after redaction and private reply storage. |
| POST | `/api/runner/chat/fail` | `{job, lease_token, code}`; code is `cli_failed`, `cli_timeout`, `cli_not_configured` or `bad_output`. 200 with `{job, state: "failed", code}`. |

Answer and fail require the current job's unexpired lease token and leasing
machine; a stale, duplicate or mismatched submission gets 409. Answer text must
be non-empty and at most 16,384 UTF-8 bytes. Bodies are limited to 20,480 bytes.
Unauthenticated requests get 401; non-machine credentials get 404 (a person
write may get 403 from the gateway). Disabled machines get 404, revoked tokens
may get 401. Invalid bodies get 400, oversized bodies 413 and wrong content types
415. These routes carry chat content; card-runner actions carry no user content.
See [runner setup](../runner.md#chat-answers) for context, egress and limits.

## Project routes

These routes require a signed-in person session; agent tokens are refused.
Readers need access to an active board; missing, archived or unreadable boards
return 404. Only workspace owners may Connect, give a project its own wing, or write model choices.

| Method | Route | Body / result |
|---|---|---|
| GET | `/api/projects/<slug>` | `{board, name, main, models, memory, can_configure}` |
| POST | `/api/projects/<slug>/orchestrator/connect` | `{}` or boolean `replace` / `allow_shared` flags; 201 for a new connection, 200 for rotation. |
| POST | `/api/projects/<slug>/wing` | Exactly `{}`; returns the re-read project view plus the own-wing result below. |
| PUT | `/api/projects/<slug>/models` | Optional `main` and `worker` keys, each `{provider, model}` or `null` to clear; returns the project view. |

`main` contains `role`, `name`, `source` (`seat` or `coordinator`), `connected`,
`seen`, `status`, `wakeable` and `connectable`. `seen` means the connection has
been used at least once; `connected` alone does not mean an agent is running.
Names are redacted and status follows the caller's visibility permissions.
`models` contains `main` and `worker` choices, each `{provider, model}` or `null`.
Choices store labels only; they do not invoke providers or change chat settings.

Connect creates a token for the project's automatic orchestrator seat; it does
not attach or start an agent. It returns
`{connection, token, mcp_url, board, wing, setup, warning?}`. The token
appears only in this response. The standard-trust connection can read and write
the board's wing and expires after 90 days. Rotation invalidates its old token.
`setup` supplies the project and `space_message` subscription details; the
operator must still register the webhook and run the agent.

A different active coordinator requires `replace: true` (otherwise 409).
A shared-wing project requires `allow_shared: true` (otherwise 409 with
`error: "shared_wing"`), confirming read and write access to all shared memory.
Unauthenticated requests return 401; agents and non-owner writes return 403.
Invalid bodies or model labels return 400 without echoing the supplied values.
Model writes and connections are audited without token values.


Own-wing results contain `{board, wing, previous_wing, changed, coordinator,
unbound, revoked, runner_linked, affected}`. `unbound` contains cleared actor refs;
`revoked` and `runner_linked` contain connection IDs. `affected` entries are
`{name, lost: "read"|"write"|"read+write", runner: bool}` with redacted names.
On no-op calls `affected` is empty. Side effects are audited even on a no-op;
clearing a coordinator emits `space_configured` as does converting the wing.
`memory` is `{wing, shared, own_wing}`; `shared` uses the effective shared grant,
and `own_wing` is the canonical `project_<resolved slug>` destination.

Own-wing returns 400 for any body other than `{}`, 403 for non-owner callers,
404 for unknown/archived/unreadable boards, or 409 with `error` of `wing_taken`,
`wing_in_use` (also `count` and redacted `connections`), or `not_shared` for wildcard
wings. A failed compare-and-set returns 409 `wing_changed`; retry the request.
It requires read and write access to both wings. MCP `board_own_wing {board}` runs
the same full flow for trusted or standard, unrestricted owner-held PATs with
memory-write scope and read/write access to both wings. A seat minted by Connect
does not have both grants and cannot convert its own board. Conflicts preserve the JSON error payload in
MCP error text. This is a checked write; REST person sessions skip rules freshness.

The shared-wing Connect 409 keeps `message` unchanged and adds `explanation`,
`fix: {label, method: "POST", path: "/api/projects/<slug>/wing", effect}`, and
`alternative: {label, method: "POST", path: "/api/projects/<slug>/orchestrator/connect",
body: {allow_shared: true}}`. The effect explains visibility loss, possible
coordinator clearing, and the absence of an undo action here. See
[project memory effects](../projects.md#give-a-project-its-own-memory-section).

## Rules freshness

Agent board/card writes and chat posts use `Commonplace-Rules-Seq: <value>`.
Memory saves, fact saves and manual handoff relaunch use the same header.
First call the MCP tool `rules_get format=md effective=true`, follow the full
set, and send the returned numeric `rules_seq`. REST rule routes are for people;
REST-only writers still need that MCP read. The ordinary `seq` is a change
cursor and cannot substitute for `rules_seq`.

Under enforce, missing, stale or unread versions return 428 with
`{"error": "<refresh guidance>", "rules_refresh": true}` before mutation. Read
again and retry with the new value. Warn permits the write and sends a
`Commonplace-Rules-Notice` response header. Off preserves existing behavior.
People's session writes are exempt; the owner's agent tokens are checked.
An unavailable store permits writes with an audited unchecked reason and alarm.

Only a card PATCH containing exactly `version` and `callstack` is exempt.
Proposal approvals are checked; mechanical claim heartbeats are exempt.
Automatic handoff upload, successor and launch-failed, handoff settings and
webhooks also bypass freshness. Reads, including recall POST, stay reads.
These paths retain their existing authorization. See the Rules design for the
complete list of deliberately open paths.

Board and card REST bodies may carry a compatibility `rules_seq` field, but
it is discarded before dispatch and never replaces the header. Response shapes
are unchanged. The internal PR repository backfill does not add a card field.

## Opt-in blockers and waiting reasons

When workspace orchestration is enabled, `task_add` accepts `blocked_by` and
`task_update` replaces it; REST uses the same field on card POST/PATCH.
The list contains at most 16 same-board card numbers. Empty removes all edges.
Self edges, cross-board references, cycles and traversals over 200 nodes are
refused atomically. Card views include `blocked_by` and the inverse `blocks`;
MCP `task_show` lists both directions, including satisfied dependencies.
Only a blocker's Done status satisfies it; automatic claiming skips unresolved
blockers, and runner starts refuse with `blocked_by`. Blocker changes increment
the card version and append an edit history row. Existing card ownership,
visibility and optimistic-version rules still apply.

Enabled card views include `waiting` (null or one reason). Precedence is
`blocked_by`, `waits_on_person`, `not_released`, then `project_limit` for a new
agent. Reasons include `text`; blockers include same-board numbers and titles,
and a project limit includes `used` and `cap`. Board lists include a `waiting`
count by code. A blocked-card wait alone does not appear in `needs_me`.
Explicit claims use the same waiting text for blocker and project-cap refusals;
existing-agent admission exemptions still apply. Board tags show the blocker
title or occupied-slot count instead of advertising a waiting card as available.
With orchestration off these fields and the `blocked_by` schema properties are absent,
and `blocked_by` is refused. `not_released` applies only to Todo/Backlog cards.

`orchestrator_inbox(board)` and `card_split_done(task,state)` are coordinator or
signed-in-owner tools available only with the switch on. Their REST equivalents
are `GET /api/projects/<board>/orchestrator/inbox` and
`POST /api/projects/<board>/orchestrator/split`. The POST body is exactly
`{"task":N,"state":"split"}` or `{"task":N,"state":"kept"}`. The first marker
wins; invalid input or a refused key inserts nothing. Database lock timeout
returns bounded `409` with `{"error":"busy","code":"busy"}`.

Split child keys remain attached to their original child when labels are edited.
Removing or replacing a key is refused with `split_key_immutable`; adding split
labels to roots or using a different parent is refused. The marker REST endpoint
accepts only positive integer task numbers below the SQLite integer bound.
Unreadable orchestration settings return `503 settings_unavailable`. Only actual
SQLite lock timeouts return `409 busy`; other database errors retain their failure.

`card_split_done` is a checked write: MCP callers supply the served `rules_seq`
and REST callers supply the matching `Commonplace-Rules-Seq` header when rules
freshness is enforced, just as for other card decisions.

### Canonical Space REST foundation

`GET /api/spaces` lists `{spaces:[Space]}`, `POST /api/spaces` creates a `Space`
with status 201, and `GET /api/spaces/<slug>` reads metadata. A Space contains
`slug`, `name`, `wing`, `coordinator`, top-level status `counts`, `can_configure`,
`open_url`, `version:1` and `deleted:false`. `open_url` points to
`/app/#/board/<encoded slug>`; only a signed-in workspace owner person has
`can_configure:true`. Legacy board responses remain supported.

The existing board row is the Space. Board, Memory and Chat are views of that
Space, with Board as the default view. Identity is the authenticated workspace and
exact slug; no new identity object or identity migration is needed.
List retains claim expiry; metadata is a plain active-row read without expiry,
transactions, orchestration, seats, settings or credential creation.
`POST /api/spaces` creates through `board_add`, preserving board authorization,
the transaction and the `space_created` event with `data.board`.
Non-person callers must pass the existing board tool visibility gate. Checked
create accepts freshness only through `Commonplace-Rules-Seq`; refresh failures
retain status 428 and their original payload. Optional null name/wing/repo use
slug/shared/no-repo defaults; distinct slugs may share a name.

Canonical validation is 400 `invalid_arguments`; policy or seat refusal is 403
`forbidden`; missing, hidden or archived metadata is 404 `space_not_found`.
Readable slug/repo collisions are 409 `space_exists` / `repo_assigned`, including
archived reservations. Hidden slug collisions are concealed as 404 and hidden repo
owners produce 403 without identity details. These different outcomes do not
eliminate existence oracles. Unknown subpaths are 404 `not_found`; collection
PATCH/DELETE and item POST/PATCH/DELETE are 405 `method_not_allowed` with
`Allow: GET, POST` or `Allow: GET`. Unregistered methods retain gateway errors.
An Idempotency-Key or include_deleted query is rejected with 400
`unsupported_idempotency` or `unsupported_include_deleted`.

Rename, archive, versions/retries/tombstones, project adapters, MCP/UI vocabulary,
new-user migration and desktop synchronization are deferred.
