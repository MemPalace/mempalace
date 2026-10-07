# OpenCode sample sessions — fixture builder

This directory does **not** ship a recorded OpenCode `.db` file. The actual
SQLite schema (verified 2026-05-12 against `opencode-ai 1.14.39` at
`~/.local/share/opencode/opencode.db` on JP's Ubuntu desktop) is replicated
verbatim by the builder script `build_fixture.py`, which produces an
in-memory or on-disk SQLite database populated with synthetic-but-realistic
session data the adapter and its tests consume.

## Why builder, not recorded fixture

1. OpenCode `.db` files contain the raw text content of every user/assistant
   turn including any pasted file paths, tokens, or secrets — committing a
   real recording would leak data even after redaction passes.
2. The schema is small (3 tables we touch — `session`, `message`, `part`).
   Constructing a fixture in Python is shorter than a serialized binary.
3. Schema fidelity is the property that matters for adapter testing; content
   fidelity matters for `convo_miner`-style integration testing, which is
   covered by chunking tests on the synthesized transcripts.

## Schema captured (verbatim from JP's live DB on 2026-05-12)

```
CREATE TABLE `session` (
    `id` text PRIMARY KEY,
    `project_id` text NOT NULL,
    `parent_id` text,
    `slug` text NOT NULL,
    `directory` text NOT NULL,
    `title` text NOT NULL,
    `version` text NOT NULL,
    `share_url` text,
    `summary_additions` integer,
    `summary_deletions` integer,
    `summary_files` integer,
    `summary_diffs` text,
    `revert` text,
    `permission` text,
    `time_created` integer NOT NULL,
    `time_updated` integer NOT NULL,
    `time_compacting` integer,
    `time_archived` integer,
    `workspace_id` text,
    `path` text,
    `agent` text,
    `model` text
);

CREATE TABLE `message` (
    `id` text PRIMARY KEY,
    `session_id` text NOT NULL,
    `time_created` integer NOT NULL,
    `time_updated` integer NOT NULL,
    `data` text NOT NULL
);

CREATE TABLE `part` (
    `id` text PRIMARY KEY,
    `message_id` text NOT NULL,
    `session_id` text NOT NULL,
    `time_created` integer NOT NULL,
    `time_updated` integer NOT NULL,
    `data` text NOT NULL
);

CREATE INDEX `message_session_time_created_id_idx` ON `message` (`session_id`,`time_created`,`id`);
CREATE INDEX `part_message_id_id_idx` ON `part` (`message_id`,`id`);
CREATE INDEX `part_session_idx` ON `part` (`session_id`);
CREATE INDEX `session_project_idx` ON `session` (`project_id`);
```

`message.data` is JSON containing `{"role": "user|assistant", ...}`.
`part.data` is JSON containing `{"type": "text|tool-input|tool-output|...", "text": "..."}`.

OpenCode-PR-#23 reverse-engineered the same schema; `session.directory` is
the path the session was started in, used by this adapter to route the
session's drawers to a wing.
