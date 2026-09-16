"""Build a synthetic OpenCode SQLite fixture matching the live schema.

Live schema captured 2026-05-12 from opencode-ai 1.14.39 at
``~/.local/share/opencode/opencode.db``; see README.md in this directory.

The builder is a *fixture factory* — tests call ``build_fixture(path,
sessions=...)`` to populate any SQLite path with a known shape. No
recorded `.db` files ship in this directory because the content of a
real OpenCode session is unsanitizably user-private.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional


# Verbatim DDL from JP's live opencode.db on 2026-05-12.
# We only include the columns the adapter actually reads + their indexes;
# additional columns that OpenCode populates (slug, version, agent, model,
# workspace_id) are kept here so a fixture mirrors the real on-disk shape
# instead of a stripped-down minimum.
DDL = [
    """
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
    """,
    """
    CREATE TABLE `message` (
        `id` text PRIMARY KEY,
        `session_id` text NOT NULL,
        `time_created` integer NOT NULL,
        `time_updated` integer NOT NULL,
        `data` text NOT NULL
    );
    """,
    """
    CREATE TABLE `part` (
        `id` text PRIMARY KEY,
        `message_id` text NOT NULL,
        `session_id` text NOT NULL,
        `time_created` integer NOT NULL,
        `time_updated` integer NOT NULL,
        `data` text NOT NULL
    );
    """,
    "CREATE INDEX `message_session_time_created_id_idx` ON `message` (`session_id`,`time_created`,`id`);",
    "CREATE INDEX `part_message_id_id_idx` ON `part` (`message_id`,`id`);",
    "CREATE INDEX `part_session_idx` ON `part` (`session_id`);",
    "CREATE INDEX `session_project_idx` ON `session` (`project_id`);",
]


@dataclass
class SyntheticPart:
    """One part of a message — usually one ``type=text`` part per turn."""

    type: str = "text"
    text: str = ""
    extra: dict = field(default_factory=dict)


@dataclass
class SyntheticMessage:
    role: str  # "user" or "assistant"
    parts: List[SyntheticPart] = field(default_factory=list)
    extra: dict = field(default_factory=dict)


@dataclass
class SyntheticSession:
    session_id: str
    project_id: str
    directory: str
    title: str
    messages: List[SyntheticMessage] = field(default_factory=list)
    time_created_ms: int = 1_715_000_000_000
    slug: Optional[str] = None
    version: str = "0.1.0"
    agent: Optional[str] = None
    model: Optional[str] = None
    workspace_id: Optional[str] = None


def build_fixture(
    path: str,
    *,
    sessions: Iterable[SyntheticSession],
) -> str:
    """Create a SQLite file at ``path`` populated with the given sessions.

    Returns the path back so callers can chain into ``SourceRef``.
    """
    p = Path(path)
    if p.exists():
        p.unlink()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    try:
        for ddl in DDL:
            conn.execute(ddl)
        for sess in sessions:
            conn.execute(
                """
                INSERT INTO session (
                    id, project_id, parent_id, slug, directory, title,
                    version, share_url, summary_additions, summary_deletions,
                    summary_files, summary_diffs, revert, permission,
                    time_created, time_updated, time_compacting, time_archived,
                    workspace_id, path, agent, model
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    sess.session_id,
                    sess.project_id,
                    None,
                    sess.slug or sess.session_id,
                    sess.directory,
                    sess.title,
                    sess.version,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    sess.time_created_ms,
                    sess.time_created_ms,
                    None,
                    None,
                    sess.workspace_id,
                    sess.directory,
                    sess.agent,
                    sess.model,
                ),
            )
            for msg_idx, msg in enumerate(sess.messages):
                msg_id = f"msg_{sess.session_id}_{msg_idx}"
                msg_time = sess.time_created_ms + msg_idx * 1000
                msg_data = {"role": msg.role, **msg.extra}
                conn.execute(
                    """INSERT INTO message (id, session_id, time_created,
                                            time_updated, data)
                       VALUES (?,?,?,?,?)""",
                    (msg_id, sess.session_id, msg_time, msg_time, json.dumps(msg_data)),
                )
                for part_idx, part in enumerate(msg.parts):
                    part_id = f"part_{sess.session_id}_{msg_idx}_{part_idx}"
                    part_data = {"type": part.type, "text": part.text, **part.extra}
                    conn.execute(
                        """INSERT INTO part (id, message_id, session_id,
                                             time_created, time_updated, data)
                           VALUES (?,?,?,?,?,?)""",
                        (
                            part_id,
                            msg_id,
                            sess.session_id,
                            msg_time + part_idx,
                            msg_time + part_idx,
                            json.dumps(part_data),
                        ),
                    )
        conn.commit()
    finally:
        conn.close()
    return str(p)


# Canonical fixture used by the adapter unit + conformance tests.
# Three sessions across two project directories, with a mix of:
#   * normal user/assistant text exchanges
#   * tool-input parts (skipped on extraction)
#   * tool-output parts (skipped on extraction)
#   * empty-text parts (skipped on extraction)
#   * a session with too few real turns (skipped at the session level)
CANONICAL_SESSIONS: List[SyntheticSession] = [
    SyntheticSession(
        session_id="ses_aaa111",
        project_id="proj_frontend",
        directory="/home/jp/Projects/frontend",
        title="Refactor TanStack Query wrapper",
        agent="opencode",
        model="anthropic/claude-sonnet-4-6",
        messages=[
            SyntheticMessage(
                role="user",
                parts=[
                    SyntheticPart(
                        text="Can you refactor the fetch wrapper to use TanStack Query mutations?"
                    )
                ],
            ),
            SyntheticMessage(
                role="assistant",
                parts=[
                    SyntheticPart(
                        text="Sure. The mutation hook would look like this:\n\nuseMutation({ mutationFn: postUser })\n\nThis gives you onSuccess/onError callbacks."
                    ),
                    # Tool-input part should be SKIPPED on extraction.
                    SyntheticPart(
                        type="tool-input",
                        text="",
                        extra={"name": "edit", "input": {"path": "src/api.ts"}},
                    ),
                    # Tool-output part should be SKIPPED on extraction.
                    SyntheticPart(
                        type="tool-output",
                        text="",
                        extra={"name": "edit", "output": "file edited"},
                    ),
                ],
            ),
            SyntheticMessage(
                role="user",
                parts=[SyntheticPart(text="And how do I invalidate the query cache after?")],
            ),
            SyntheticMessage(
                role="assistant",
                parts=[
                    SyntheticPart(
                        text="Call queryClient.invalidateQueries({ queryKey: ['users'] }) inside onSuccess."
                    )
                ],
            ),
        ],
    ),
    SyntheticSession(
        session_id="ses_bbb222",
        project_id="proj_frontend",
        directory="/home/jp/Projects/frontend",
        title="Add session login route",
        time_created_ms=1_715_001_000_000,
        agent="opencode",
        model="anthropic/claude-sonnet-4-6",
        messages=[
            SyntheticMessage(
                role="user",
                parts=[SyntheticPart(text="Add a /login route with JWT auth.")],
            ),
            SyntheticMessage(
                role="assistant",
                parts=[
                    SyntheticPart(
                        text="Add the route handler in routes/login.ts and verify the token against the JWT secret on each request."
                    )
                ],
            ),
        ],
    ),
    SyntheticSession(
        session_id="ses_ccc333",
        project_id="proj_backend",
        directory="/home/jp/Projects/backend",
        title="Alembic migration question",
        time_created_ms=1_715_002_000_000,
        agent="opencode",
        model="anthropic/claude-sonnet-4-6",
        messages=[
            SyntheticMessage(
                role="user",
                parts=[
                    SyntheticPart(
                        text="How do I downgrade an Alembic migration safely in production?"
                    )
                ],
            ),
            SyntheticMessage(
                role="assistant",
                parts=[
                    SyntheticPart(
                        text="Run alembic downgrade -1 against a staging copy first; verify schema, then run the same command against production inside a transaction."
                    )
                ],
            ),
        ],
    ),
    # Sessions with <2 real text parts should be SKIPPED entirely.
    SyntheticSession(
        session_id="ses_ddd444",
        project_id="proj_backend",
        directory="/home/jp/Projects/backend",
        title="Cancelled session",
        time_created_ms=1_715_003_000_000,
        messages=[
            SyntheticMessage(
                role="user",
                parts=[SyntheticPart(text="wait nvm")],
            ),
        ],
    ),
]


if __name__ == "__main__":  # pragma: no cover - manual fixture generation
    import sys

    target = sys.argv[1] if len(sys.argv) > 1 else "/tmp/opencode_fixture.db"
    build_fixture(target, sessions=CANONICAL_SESSIONS)
    print(f"wrote fixture to {target}")
