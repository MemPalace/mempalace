-- Commonplace boards: data model
-- Illustrative design sketch, not the executable schema.
-- See commonplace/boards/schema.sql and commonplace/boards/store.py for the current schema and migrations.
-- SQLite, one file per workspace: <palace>/boards.sqlite3, next to the knowledge graph.
-- Design: docs/boards/DESIGN.md. API: docs/boards/API.md.
--
-- Conventions
--   * Actor refs are strings: "user:<user id>" for people, "conn:<connection name>" for agents.
--   * Every written row carries the server stamp (PRODUCT-DESIGN.md §7). The gateway fills
--     the stamp_* columns from the verified token, never from tool arguments.
--   * Titles, bodies and event text are stored verbatim.
--   * Times are UTC ISO-8601 strings from the server clock.

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
INSERT OR IGNORE INTO meta (key, value) VALUES ('changes', 0), ('schema_version', 1);

CREATE TABLE IF NOT EXISTS boards (
    id               INTEGER PRIMARY KEY,
    slug             TEXT NOT NULL UNIQUE,          -- short name agents type: "mempalace"
    name             TEXT NOT NULL,
    wing             TEXT NOT NULL,                 -- scope: a token sees the board only if this wing is in read_wings
    done_needs_review INTEGER NOT NULL DEFAULT 0,   -- 1: an agent's move to done lands in review instead
    claim_minutes    INTEGER NOT NULL DEFAULT 30,   -- claim length for task_take_next
    archived         INTEGER NOT NULL DEFAULT 0,
    stamp_user       TEXT NOT NULL,
    stamp_connection TEXT,
    stamp_trust      TEXT NOT NULL,
    created_at       TEXT NOT NULL
);

-- Repo URLs, normalised (host/owner/name, no scheme, no .git), so hooks can pass the git remote.
CREATE TABLE IF NOT EXISTS board_repos (
    repo     TEXT PRIMARY KEY,
    board_id INTEGER NOT NULL REFERENCES boards(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS tasks (
    id            INTEGER PRIMARY KEY,              -- shown as #142; one sequence per workspace
    board_id      INTEGER NOT NULL REFERENCES boards(id),
    title         TEXT NOT NULL,
    body          TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL
                  CHECK (status IN ('backlog', 'todo', 'doing', 'review', 'done')),
    position      REAL NOT NULL,                    -- float, so a drag rewrites one row
    assignee      TEXT,                             -- actor ref; in todo it reserves the card for that actor
    claim_until   TEXT,                             -- agents only, while status = 'doing'; people's cards never expire
    claim_expired_at TEXT,
    agent_parent TEXT,
    proposal_status TEXT CHECK (proposal_status IN ('pending', 'approved', 'rejected')),
    proposed_to TEXT,
    approved_by TEXT,
    review_kind TEXT,
    review_context TEXT,
    ask           TEXT,                             -- set while status = 'review': the question for the recipient
    ask_options   TEXT,                             -- JSON array of short choices, or NULL for free text
    ask_blocks    TEXT,                             -- JSON array of {label, detail?, preview?} for a structured ask, else NULL
    ask_to        TEXT,                             -- recipient actor ref (authorized coordinator or person; legacy default: owner)
    return_to     TEXT,                             -- actor ref the card goes back to after the decision
    flags         TEXT,                             -- JSON array from the §9 injection check, e.g. ["curl_pipe"]
    version       INTEGER NOT NULL DEFAULT 1,       -- bumped on every change; web UI sends it back on drag
    stamp_user       TEXT NOT NULL,                 -- creator
    stamp_connection TEXT,
    stamp_trust      TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    updated_by       TEXT NOT NULL,                 -- actor ref
    done_at          TEXT,
    CHECK (status = 'doing' OR claim_until IS NULL),
    CHECK (status != 'doing' OR assignee IS NOT NULL),
    CHECK ((status = 'review') = (ask IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS tasks_by_lane ON tasks (board_id, status, position);
CREATE INDEX IF NOT EXISTS tasks_by_assignee ON tasks (assignee, status);
CREATE INDEX IF NOT EXISTS tasks_open_claims ON tasks (claim_until) WHERE status = 'doing';

-- Append-only log per task: comments, moves, claims, handoffs, asks and decisions.
-- This is the progress log agents read and the history the web UI shows.
CREATE TABLE IF NOT EXISTS task_events (
    id         INTEGER PRIMARY KEY,
    task_id    INTEGER NOT NULL REFERENCES tasks(id),
    kind       TEXT NOT NULL CHECK (kind IN (
                   'add', 'edit', 'move', 'comment', 'claim', 'renew', 'release',
                   'expire', 'handoff', 'ask', 'decide', 'link', 'unlink')),
    context    TEXT,
    text       TEXT,                                -- verbatim comment, handoff note, ask or reply
    from_status TEXT,
    to_status   TEXT,
    target     TEXT,                                -- actor ref (handoff, assign) or drawer id (link)
    choice     TEXT,                                -- decide: the option picked
    stamp_user       TEXT NOT NULL,
    stamp_connection TEXT,                          -- NULL for people in the web UI
    stamp_trust      TEXT NOT NULL,
    flags      TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS task_events_by_task ON task_events (task_id, id);

CREATE TABLE IF NOT EXISTS task_links (
    task_id    INTEGER NOT NULL REFERENCES tasks(id),
    drawer_id  TEXT NOT NULL,
    stamp_user       TEXT NOT NULL,
    stamp_connection TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (task_id, drawer_id)
);

CREATE INDEX IF NOT EXISTS task_links_by_drawer ON task_links (drawer_id);

-- Change counter: the web UI polls meta.changes and refetches only when it moved.
CREATE TRIGGER IF NOT EXISTS boards_changed_i AFTER INSERT ON boards
BEGIN UPDATE meta SET value = value + 1 WHERE key = 'changes'; END;
CREATE TRIGGER IF NOT EXISTS boards_changed_u AFTER UPDATE ON boards
BEGIN UPDATE meta SET value = value + 1 WHERE key = 'changes'; END;
CREATE TRIGGER IF NOT EXISTS tasks_changed_i AFTER INSERT ON tasks
BEGIN UPDATE meta SET value = value + 1 WHERE key = 'changes'; END;
CREATE TRIGGER IF NOT EXISTS tasks_changed_u AFTER UPDATE ON tasks
BEGIN UPDATE meta SET value = value + 1 WHERE key = 'changes'; END;
CREATE TRIGGER IF NOT EXISTS events_changed_i AFTER INSERT ON task_events
BEGIN UPDATE meta SET value = value + 1 WHERE key = 'changes'; END;
CREATE TRIGGER IF NOT EXISTS links_changed_i AFTER INSERT ON task_links
BEGIN UPDATE meta SET value = value + 1 WHERE key = 'changes'; END;
CREATE TRIGGER IF NOT EXISTS links_changed_d AFTER DELETE ON task_links
BEGIN UPDATE meta SET value = value + 1 WHERE key = 'changes'; END;

-- The log is append-only.
CREATE TRIGGER IF NOT EXISTS task_events_no_update BEFORE UPDATE ON task_events
BEGIN SELECT RAISE(ABORT, 'task_events is append-only'); END;
CREATE TRIGGER IF NOT EXISTS task_events_no_delete BEFORE DELETE ON task_events
BEGIN SELECT RAISE(ABORT, 'task_events is append-only'); END;

-- Tasks are never deleted. Removing a card moves it to done with a 'move' event
-- whose text says why; workspace deletion drops the whole file.
CREATE TRIGGER IF NOT EXISTS tasks_no_delete BEFORE DELETE ON tasks
BEGIN SELECT RAISE(ABORT, 'tasks are never deleted'); END;
