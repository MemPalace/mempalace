# Bots

A bot is a private chat thread that belongs to one person. Each person gets one bot per
project they can read, a General bot, and any custom bots they create. A bot is not a user
and has no token or rights of its own: whatever it reads is read as the person it belongs to,
and what that person may not see, it cannot show.

This page covers the storage, the read and management routes, and talking to a bot: a turn
streams the reply of the workspace's chat model, runs tools as the person and stores the reply.

## Routes

All routes need a signed-in person session. An agent token or machine credential gets 404,
exactly like a path that does not exist; an anonymous caller gets 401. Writes pass the
gateway's same-origin check. A bot id that is unknown or belongs to someone else is 404.

| Route | Body, result |
|---|---|
| `GET /api/bots` | `{bots, model:{ready,label,code}, can_configure}`; creates the default bots on first use |
| `POST /api/bots` | `{name, job?, instructions?, board?}`, 201 `{bot}` |
| `PATCH /api/bots/<id>` | `{name?, job?, instructions?, archived?}`, `{bot}` |
| `GET /api/bots/<id>/messages?after=&before=&limit=` | `{bot, messages, waiting, running}` |
| `POST /api/bots/<id>/messages` | `{text, request_id}`, a `text/event-stream`; with `?stream=0`, `{message, reply}` when the turn ends |
| `POST /api/bots/<id>/stop` | `{request_id}`, `{ok}` (true when that turn was running) |
| `POST /api/bots/<id>/confirm` | `{pending, accept}`, `{message}` (an `event` message with the action block) |
| `POST /api/bots/<id>/read` | `{upto}`, `{ok}` |
| `GET /api/bots/<id>/memory` | `{memories:[{id,text,at}]}` |
| `POST /api/bots/<id>/memory` | `{text}`, `{memory}` |
| `DELETE /api/bots/<id>/memory/<mid>` | `{ok}` |

A bot is `{id, name, job, instructions, kind, board, archived, last, unread, waiting, state}`.
`kind` is `project`, `general` or `custom`. `last` is the newest message (`role`, `text`
shortened to 200 characters, `at`) or null. `unread` counts bot and event messages after the
read mark. `waiting` is the number of the person's open decisions for the bot's project, or
across every readable project for General and for a custom bot without a project. `state` is
`needs_you` while something waits, `working` while a turn runs, else `idle`. The thread
listing's `running` is true while a turn of this bot runs.

Defaults: one `project` bot for each non-archived project the person can read, named after
it, plus one `general` bot. An archived default is never created again. The roster is sorted
by last activity; bots that were never used come last.

`model` reports whether the workspace has a usable chat model. `code` is empty when it does,
otherwise `needs_configuration`, `needs_turn_on` (the project inherits a hosted setting it has
not been turned on for) or `key_missing`. `can_configure` is true for the workspace owner.

Bounds: name 1 to 60 characters, job at most 200, instructions at most 4000, message text 1
to 8000, memory note 1 to 2000, request id 8 to 64 of `A-Za-z0-9_-`, at most 50 live bots per
person (409 `limit`), `limit` at most 100 (default 50). Out-of-bound input is 400 `invalid`.

## Blocks

A message may carry blocks: small references that are filled in when the thread is read,
with the reader's own permissions. A `work` block shows a task's title, a status word
(`queued`, `working`, `needs_you`, `ready`, `blocked`, `done`), a note and links; a `decision`
block shows the open question and its options, or the answer. An `action` block is one line
for a tool the bot used (`tool`, `label`, `ok`, and a `code` when refused). A `confirm` block
adds the server-built `label` and `state` (`open`, `done`, `cancelled`, `expired`) of an action
waiting for the person's tap. A `setup` block says no model is usable and whether the reader
can configure one. An item the reader cannot see, or that no longer exists, comes back as
`gone`. Tasks are always named by their number.

## Talking to a bot

`POST /api/bots/<id>/messages` stores the person's message, starts a turn on its own thread
and relays that turn as server-sent events. Each frame is `event:` plus a JSON `data:` line and
is flushed at once; a `: ping` comment goes out every 10 seconds.

| Event | Data |
|---|---|
| `ack` | `{message}`: the stored user message; sent before any model work |
| `delta` | `{text}`: reply text, in order, while the model is still sending; whole words only, redacted over the whole reply so far, and together exactly the stored reply text |
| `action` | `{step, tool, label, state, code?}`: `running`, then `ok` or `refused` |
| `block` | a hydrated block a tool returned (a work item, a `confirm` chip) |
| `done` | `{message, timing:{first_token_ms, total_ms}}`: the stored bot message |
| `error` | `{code, text, message}`: `needs_configuration`, `limit`, `rate`, `steps`, `too_long`, `model_error`, `timeout`, `stopped` or `interrupted`, a fixed sentence and the stored bot message |
| `running` | `{}`: a replay of a request whose turn is still running; read the thread later |

Before the stream starts, errors are plain HTTP `{error, code}`: 400 `invalid`, 404 for an
unknown or foreign bot, 409 `busy` while another turn of this bot runs, 409 `conflict` for a
request id sent again with other text, 429 `busy` when the person already has 4 turns running
or the process 32. Once the message is stored, every outcome is a stream event and a stored
bot message.

**A turn.** The model is the one the owner configured in chat settings for the bot's project
(the workspace setting for a bot without one). Without a usable model the reply is a stored
`setup` block and `error needs_configuration`; no request is sent. The model reads a fixed
system prompt with the bot's name, job and persona notes (quoted; they grant nothing), up to 20
memory notes, a short snapshot of the bot's project, and the last 20 messages, all redacted and
quoted as data. A turn takes at most 6 tool steps, 30 seconds per model request, 60 seconds in
total, 800 output tokens per request and 48,000 input characters; a turn that needs more tool
steps ends with `error steps`.

**Input budget.** Every model request fits one budget of 48,000 characters, measured the way
the transport measures it (system prompt, tool schemas and all messages). The system prompt,
the tool schemas, the new message and the turn's own tool rounds always go in; then, while
they fit, memory notes newest first (whole notes only; the model is told how many were left
out), the project snapshot, and earlier messages newest first. A message that cannot fit even
alone is refused before any request with `error too_long`. Each tool result fed back is cut to
4,000 characters, and later requests of a turn drop older context first to stay in budget.

The reply is stored in one
write, redacted, with the model label and a state: `ok`, `stopped` or `failed:<code>`. Partial
text is kept when a turn stops or fails. Provider error text never reaches the person.

**Streamed text.** The reply so far is redacted as a whole each time text arrives, and only
the part that can no longer change is sent: whole words, and a line that carries a label a
secret may follow (a header, a key with `:` or `=`, a quoted value, a key block) only once the
line is complete. So a secret split across chunks never reaches the client unredacted, and at
the end the client normally holds exactly the stored reply text. Should redaction ever change
text that was already sent, the stream sends no further text for that turn (the hub logs
`code=redaction_shift`). The reply the client shows is always `done.message.text` (or
`error.message.text`), which is the stored reply; deltas are a preview of it.

**Replay.** Sending the same `request_id` with the same text again never runs anything twice:
a finished request answers `ack` and its stored `done` or `error`; a running one answers `ack`
and `running`. A running turn holds a lease in the workspace store that it renews every 5
seconds, so a turn running in another hub process counts as running too. A request that has a
stored message but no reply and no running turn (no fresh lease for 20 seconds, for example
because the hub restarted mid-turn) is answered with `error interrupted` and a stored `failed:interrupted` reply listing what the
turn had journaled; an action that had started but not finished reads "Outcome unknown". The
same report is written when the thread is next read. It is never rerun. Should a turn still
finish after its request was closed this way, its reply is not stored a second time: the
stream reports the stored outcome and the hub logs `code=late_reply`.

**A dropped client.** The turn does not depend on the connection. A client that disconnects,
or a socket write blocked for 10 seconds, only ends the relay; the reply is stored and shows
when the thread is read again.

**Stop.** `POST /api/bots/<id>/stop {request_id}` ends that turn. A tool call in flight
finishes first (a write is never cut off); the partial text is kept and stored with state
`stopped`.

**Limits.** Bots and project chat share one model budget. One unit is one model request sent;
a bot takes its unit just before sending, in one step with the daily check (so concurrent turns
cannot pass the limit together), and gives it back when the request never went out. Bots count
theirs per person and day, and project chat's daily count includes them, so both
draw on the same workspace `daily_limit` and per-person `user_daily_limit`, and on the same
20-requests-per-minute rate per person. A used-up budget is `error limit` (or `rate`).

**Tools.** The model may call the tools offered for the turn. Arguments are checked against
each tool's schema first. Every tool runs as the person, re-resolved for each call, so what
the person may not do the bot cannot do either; each write gets an audit row like a REST write
(tool `bot:<name>`, no text). Text a tool writes must be a verbatim part of the person's
current message, at least 2 characters after trimming, or the tool refuses with
`not_your_words`. Every call is journaled before it
runs and after it finishes; its result goes back to the model as quoted data. Built in:
`remember(text)` saves a memory note (the person's own words) and `forget(id)` removes one.

**Confirm.** A consequential tool never runs from a model's tool call. The turn stores a
pending action with its arguments frozen (including the item's `version` when the call names
an item by `ref`) and a label the server builds, valid for 10 minutes, and shows a `confirm`
block. When the item named by `ref` cannot be read, nothing is created and the call is refused
(`unreadable`). `POST /api/bots/<id>/confirm {pending, accept:true}` moves it from `open` to `done`
once and runs exactly the stored action as the person; the result is an `event` message with
the action block. `accept:false` cancels it. A second confirm is 409 `conflict`, an expired one
409 `expired`, another person's 404. A confirm tool receives the frozen `version` among its
arguments.

**Writing a tool.** `commonplace/bots/tools.py` defines `Tool` (name, description, a closed
JSON Schema, kind `read` / `routine` / `confirm`, `run`, `label`, scope, `available`),
`Result` and `register()`. An optional `check(turn, args)` (the last field) runs after
argument validation and before anything is labelled, stored or written: a confirm tool that
writes the person's text calls `turn.words()` there, so model text is refused before a
pending action or chip exists. `/confirm` rebuilds the turn with the message that asked for
the action (even when newer messages exist) and calls `check` again before `run`; a routine
tool's `check` also runs before `run`. A `project`-scope tool acts on the bot's own project; for a bot
without one the model must name a readable project in a `project` argument, and
`Turn.board(args)` returns it. Tool modules are listed in `bots.TOOL_MODULES` and imported at
install; a missing module is skipped.

## Project tools

Filled by a later slice.

## Machines

Filled by a later slice.

## Updates between turns

When something happens to work a bot filed, or the project's orchestrator answers a message a
bot forwarded, the person sees it in that bot's thread without asking: one short message with
the role `event`, and the roster follows (`unread` counts it, `state` changes).

| Happens | The thread gets | Blocks |
|---|---|---|
| A question for the person on a task the bot filed | `A question about “<title>” needs you.` | `decision` |
| The task is ready for review | `“<title>” is ready for you to look at.` | `work` |
| The task is done | `“<title>” is done.` | `work` |
| The orchestrator replies to a post the bot forwarded | the reply, word for word | `orchestrator` |

- Only the bot that filed the task or forwarded the post gets the message. Other bots, and
  other people's bots, get nothing.
- Only while the person can still see the item: their access is checked again for every
  event, with the same rules as project chat. No access, no message.
- Exactly once: a task posts when its status changes into one of the three above, so the
  same event twice, two events for one change, or events handled out of order add one
  message each, in order. A forwarded post is
  answered by the first reply from the project's orchestrator after it (the rule project
  chat uses); later replies are not attached to it.
- A title is other people's text: it is redacted and shortened for the message and never
  interpreted. The `orchestrator` block lets a client label the message as the
  orchestrator's own words; display redaction applies as for every message.
- `state` on a bot is `needs_you` while a question waits for the person, else `working` while
  a task it filed is in progress, else `idle`.
- Not covered: a start that fails to launch. The hub emits no event for it, and nothing here
  polls, so the bot says nothing until the person asks.

## Privacy and storage

- Threads live in the workspace file `bots.sqlite3`, next to the other workspace stores, and
  are encrypted with the workspace when encryption is on. Nothing from a conversation goes
  into the control database, logs, audit reasons or error messages.
- Nobody reads another person's bot, the workspace owner included.
- Messages are stored exactly as typed and never changed or deleted (the store refuses both).
  Each user message has one reply at most, and a repeated request id with the same text
  returns the stored message instead of a second one. Display text is redacted; the stored
  text is not.
- A memory note is removed with a tombstone: it disappears from the list, the stored row stays.
- Bots use the model the owner configured in chat settings. Without a usable model there is
  no canned answer.

## Using bots in the web app

This part describes what the screens do; the server side is in [Routes](#routes),
[Blocks](#blocks) and [Privacy and storage](#privacy-and-storage).

**Bots** is where you land after signing in (until a first project exists, the first-run screen
comes first). It is the first entry in the left rail and the first tab on a phone; **Memory** is
the only other primary entry. The number next to Bots counts the questions waiting on you.

**Where the rest went.** Projects, Needs me and Console sit under **Workspace** in the account
menu (your name at the bottom of the rail, the menu button at the top left on a phone), next to
Rules, Workspace settings and, for owners, Admin. Nothing was removed: every project, task,
chat, Needs me and Console link keeps working, and the project chat dock still opens on a
project screen.

Every bot you can talk to is listed:
one per project, a General bot, and any you made yourself. The list is in the order the server
gives it, most recently used first.

**The roster.** Each row shows the bot's initial, its name, the last line of the conversation (or
its job before you have said anything), how long ago that was, and the number of messages you have
not seen. One dot at the right says what needs attention: a solid accent dot means something is
waiting on you, a green pulsing dot means work is running. No dot means nothing is going on.
**New bot** asks for a name, what the bot is for, and an optional note on how it should work.

**A thread.** Tap a bot to open its conversation (on a phone it takes the whole screen; the back
arrow returns to the list). Type and press Enter to send; Shift+Enter starts a new line. Your
message appears at once and the bot's reply fills in as it is written. While the bot is answering,
Send turns into **Stop**; stopping keeps what was already written.

Inside the reply you may see:

- a small grey line of **actions** ("Filed “Fix login”", "Told the orchestrator"); several actions
  share one line, and a refused one is shown in red;
- a **work** block with the task's title and a status chip (Queued, Working, Needs you, Ready,
  Blocked, Done). Tap it to read the latest note and open its links;
- a **question** with a button for each answer and a field for an answer in your own words. Your
  tap is your own answer and goes straight to the task;
- a **confirmation** for the few actions that need your tap before they run. Confirm runs what
  the line says; Cancel drops it;
- a **message from the orchestrator**, labelled "Orchestrator", when the project's orchestrator
  replies to something the bot forwarded (see [Updates between turns](#updates-between-turns));
- a **setup** note when no chat model can answer yet. Workspace owners get a link to the settings;
  members are told to ask the owner.

Above the message box a single line, **Waiting on you (n)**, collects every open question for this
bot so you never have to scroll for them. Tap it to answer from there.

**Memory and settings.** Tap the bot's name above the thread. The sheet lets you rename the bot,
change its job and its working note, read what it remembers, add a note or remove one, and archive
the bot. Notes are kept word for word.

**If a message does not go through.** Your text stays in the box and a line says why (the bot is
still busy, too many messages, no connection). Press Send again: the same message is sent under
the same id (see [Routes](#routes) for what the server does with a repeated message). If the
connection drops while the bot is answering, the page keeps checking and shows the reply when it
is stored.

**Refresh rhythm.** The list refreshes every 15 seconds and an open thread every 5 seconds, faster
while a reply is being written. Nothing refreshes while the tab is hidden.
