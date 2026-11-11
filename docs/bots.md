# Bots

A bot is a private chat thread that belongs to one person. Each person gets one bot per
project they can read, a General bot, and any custom bots they create. A bot is not a user
and has no token or rights of its own: whatever it reads is read as the person it belongs to,
and what that person may not see, it cannot show.

This page covers the storage and the read and management routes. Sending a message to a
bot, stopping a turn and confirming an action come with the turn engine and are not part of
this page yet.

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
| `POST /api/bots/<id>/read` | `{upto}`, `{ok}` |
| `GET /api/bots/<id>/memory` | `{memories:[{id,text,at}]}` |
| `POST /api/bots/<id>/memory` | `{text}`, `{memory}` |
| `DELETE /api/bots/<id>/memory/<mid>` | `{ok}` |

A bot is `{id, name, job, instructions, kind, board, archived, last, unread, waiting, state}`.
`kind` is `project`, `general` or `custom`. `last` is the newest message (`role`, `text`
shortened to 200 characters, `at`) or null. `unread` counts bot and event messages after the
read mark. `waiting` is the number of the person's open decisions for the bot's project, or
across every readable project for General and for a custom bot without a project. `state` is
`needs_you` while something waits, else `idle`.

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
block shows the open question and its options, or the answer. An item the reader cannot see,
or that no longer exists, comes back as `gone`. Tasks are always named by their number.

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
