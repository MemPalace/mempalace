"""Base guards that eval slice 1 relies on (class A, plus the C4 control).

Every test here passes on the code eval was planned against and fails under a
named mutation of that base code. None imports ``commonplace.evals``:
principals are built inline with the eval field list, so the file also runs on
revisions without eval.
"""

from __future__ import annotations

import ast
import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from commonplace import capture, continuity as core, continuity_schema as schema
from commonplace.control_db import ControlDB, Principal
from commonplace.handoff.models import ThreadMessage
from commonplace.handoff.recall import Recall, ThreadNotFound
from commonplace.handoff.thread_store import ThreadConflict, ThreadStore
from commonplace.handoff.thread_store_migrate import read_store
from commonplace.import_grant import grant_id, reader_eligible
from commonplace.import_sources import parse_import
from commonplace.policy import Policy

ROOT = Path(__file__).resolve().parents[1]
EVAL_SCOPES = ["threads:read", "memory:read"]


def principal(user, ws, *, auth="local", scopes=EVAL_SCOPES, role="owner"):
    """The eval principal's field list; ``auth``/``scopes`` vary only where named."""
    return Principal(
        user_id=user,
        user_email="owner@example.test",
        workspace_id=ws,
        connection_id="",
        connection="cli",
        client="commonplace",
        auth=auth,
        role=role,
        policy=Policy.build(
            profile="full", scopes=list(scopes), read_wings=["*"], trust="trusted"
        ).for_role(role),
    )


def native(sid="S", word="first"):
    record = dict(type="user", sessionId=sid, message=dict(role="user", content=word))
    return (json.dumps(record) + "\n").encode()


def sha(raw):
    import hashlib

    return hashlib.sha256(raw).hexdigest()


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("COMMONPLACE_PLANS", "off")
    db = ControlDB(str(tmp_path / "control.sqlite3"))
    user = db.create_user("owner@example.test")
    ws = db.create_workspace("Synthetic", user, personal=True)
    schema.ensure_schema(db)
    store = ThreadStore(str(tmp_path / "threads.sqlite3"))
    yield db, store, user, ws
    store.close()
    db.close()


def push(db, store, user, ws, raw, *, stream, before=b""):
    with db._lock, db._tx() as conn:
        return core.stream_push(
            conn,
            store,
            ws=ws,
            user_id=user,
            connection_id="",
            kind="capture",
            stream_id=stream,
            source="claude-code",
            native_source="claude-code",
            data=raw,
            offset=len(before),
            prefix=sha(before),
            actor="synthetic",
            maximum=None,
            gate=lambda: None,
        )


def referenced(db):
    conn = db._conn
    ids = [r[0] for r in conn.execute("SELECT own_stored_id FROM sync_streams")]
    ids += [r[0] for r in conn.execute("SELECT fork_stored_id FROM sync_streams") if r[0]]
    ids += [r[0] for r in conn.execute("SELECT holder_stored_id FROM sync_groups")]
    return ids


# ── A1: read-only opener ──
def test_a1_read_store_creates_nothing_and_keeps_main_bytes(tmp_path):
    missing = tmp_path / "missing" / "threads.sqlite3"
    missing.parent.mkdir()
    with pytest.raises(sqlite3.Error):
        with read_store(missing) as store:
            store.messages("thr_00")
    assert list(missing.parent.iterdir()) == [], "A1: read_store created a file"

    path = tmp_path / "threads.sqlite3"
    live = ThreadStore(str(path))
    stored = live.store(
        "ws_a", "t3:x", [ThreadMessage(0, "user", "message", "hi", "{}")], actor="a"
    )
    live.close()
    before = path.read_bytes()
    with read_store(path) as store:
        assert store.messages(stored.id)[0].text == "hi"
    assert path.read_bytes() == before, "A1: main file bytes changed"


# ── A2: adoption takes only private threads ──
@pytest.mark.parametrize("wing", ["threads", "notes"])
def test_a2_adoption_rejects_non_private_wings(env, wing):
    db, store, user, ws = env
    raw = native()
    rows = parse_import("claude-code", raw.decode())
    other = store.store(ws, grant_id(user, "claude-code", "x"), rows, actor="a", wing=wing)
    private = store.store(
        ws, grant_id(user, "claude-code", "y"), rows, actor="a", wing=capture.PRIVATE_WING
    )

    def read(thread):
        messages = store.messages(thread.id)
        segments = schema.archive_segments(messages)
        return thread, b"".join(s.raw for s in segments), "", messages

    prefix = f"import-private:{user}:"
    assert core._adoption_candidate(private, prefix, "import", "", read) is not None
    assert core._adoption_candidate(other, prefix, "import", "", read) is None, (
        "A2: a non-private thread became an adoption candidate"
    )
    core.backfill(db, store, ws, user)
    assert other.id not in referenced(db), "A2: backfill adopted a non-private thread"


# ── A3: every stored ID a sync row references is private ──
def test_a3_continuity_writers_reference_only_private_rows(env):
    db, store, user, ws = env
    first, second, third = native(), native(word="second"), native(word="third")
    push(db, store, user, ws, first, stream=f"capture:{user}:claude-code:a")
    push(db, store, user, ws, first + second, stream=f"capture:{user}:claude-code:b")
    push(db, store, user, ws, first + third, stream=f"capture:{user}:claude-code:c")
    store.store(
        ws,
        grant_id(user, "claude-code", "legacy"),
        parse_import("claude-code", (first + second).decode()),
        actor="a",
        wing=capture.PRIVATE_WING,
    )
    core.backfill(db, store, ws, user)
    ids = referenced(db)
    assert ids
    markers = [i for i in ids if store.messages(i, 0, 0)[0].name == schema.MARKER]
    assert markers, "fixture must create at least one marker"
    assert all(store.get(i).wing == capture.PRIVATE_WING for i in ids), (
        "A3: a sync row references a non-private stored thread"
    )


def test_a3_threads_wing_collision_raises_and_adds_no_sync_row(env):
    db, store, user, ws = env
    # An existing group for the same native session is what would let a
    # marker-shaped row join as ``own``; without one the push refuses anyway.
    push(db, store, user, ws, native(), stream=f"capture:{user}:claude-code:first")
    identity = f"capture:{user}:claude-code:z"
    collision = store.store(
        ws,
        identity,
        [ThreadMessage(0, "system", "other", "", json.dumps({"v": 1}), name=schema.MARKER)],
        actor="a",
        wing="threads",
    )
    with pytest.raises(ThreadConflict):
        push(db, store, user, ws, native(), stream=identity)
    assert collision.id not in referenced(db), "A3: a threads-wing row became own_stored_id"


# ── A4: nothing rewrites wing/source_thread_id or deletes thread rows ──
# A best-effort lint over SQL string literals. Its rule is the union of the
# per-piece rule (every literal and f-string on its own, upper-case dynamic
# match) and the joined, anchored rule (f-strings and ``+`` chains rendered
# whole, case-insensitive). Allowlist keys name the statement kind they were
# written for, so an exemption never covers another kind of statement in the
# same file; a path-aware test checks that the scan never catches less than
# the per-piece rule with its own allowlist.
_TABLE = r"(?:\w+\s*\.\s*)?[\"'\[`]?"  # optional schema, optional quote
_CONTENT_TABLES = r"(threads|messages|line|message|block|blob|dict)"
_SET_TARGET = re.compile(r"[\"'\[`]?\b(wing|source_thread_id)\b[\"'\]`]?\s*=", re.I)
_UPDATE_THREADS = re.compile(
    r"\bUPDATE\s+(?:OR\s+\w+\s+)?" + _TABLE + r"threads\b[\"'\]`]?"
    r"(?:\s+AS\s+\w+)?\s+SET\s+(.*)",
    re.I | re.S,
)
_UPSERT_THREADS = re.compile(
    r"\bINSERT\s+(OR\s+\w+\s+)?INTO\s+" + _TABLE + r"threads\b.*\bDO\s+UPDATE\s+SET\s+(.*)",
    re.I | re.S,
)
_DESTRUCTIVE = re.compile(
    r"\b(DELETE\s+FROM|DROP\s+TABLE(\s+IF\s+EXISTS)?|REPLACE\s+INTO|INSERT\s+OR\s+REPLACE\s+INTO)"
    r"\s+" + _TABLE + _CONTENT_TABLES + r"\b",
    re.I,
)
# Per-piece dynamic rule (upper-case keywords, a placeholder or the string end).
_PIECE_DYNAMIC = re.compile(
    r"\b(DELETE\s+FROM|DROP\s+TABLE(\s+IF\s+EXISTS)?)\s*(\x00|%s|\{|$)|\bUPDATE\s*(\x00|%s|\{)"
)
# Joined dynamic rule, case-insensitive: a statement keyword opens the string or
# follows ``;``, a newline, ``(`` or ``)``; a placeholder name (or the string
# end) follows; then a SQL-shaped tail. Prose that ends in a placeholder
# ("Delete from {path}") is still flagged: an accepted false positive of a lint
# that must not miss SQL; allowlist only a real repo hit.
_ANCHOR = r"(?:^|[;\n()])\s*"
_NAME = r"(?:\x00|%s|\{[^{}]*\})"
_UPDATE_TAIL = r"(?:\s+(?:AS\s+\w+|INDEXED\s+BY\s+\w+|NOT\s+INDEXED|/\*.*?\*/))*\s+SET\b"
_JOINED_DYNAMIC = re.compile(
    _ANCHOR + r"DELETE\s+FROM\s*(?:$|" + _NAME + r"\s*(?=$|[;)]|WHERE\b|RETURNING\b))"
    r"|" + _ANCHOR + r"DROP\s+TABLE(?:\s+IF\s+EXISTS)?\s*(?:$|" + _NAME + r"\s*(?=$|[;)]))"
    r"|"
    + _ANCHOR
    + r"UPDATE\s+(?:OR\s+\w+\s+)?"
    + _NAME
    + _UPDATE_TAIL
    + r"|"
    + _ANCHOR
    + r"(?:INSERT\s+OR\s+)?REPLACE\s+INTO\s*(?:$|"
    + _NAME
    + r")",
    re.I | re.S,
)
# Dynamic table names that are reviewed and touch no thread-store table.
# Keys are (path, kind, text): "piece" is one literal or one f-string, "joined"
# is a whole ``+`` chain.
ALLOWED_DYNAMIC = {
    ("mempalace/backends/pgvector.py", "piece", "DELETE FROM \x00 WHERE id = ANY(%s)"): "drawers",
    ("mempalace/backends/pgvector.py", "piece", "DELETE FROM \x00 WHERE \x00"): "drawers",
    ("mempalace/backends/pgvector.py", "piece", "DROP TABLE IF EXISTS \x00"): "drop_table",
    (
        "mempalace/fact_index.py",
        "piece",
        "DROP TABLE IF EXISTS ",
    ): "three literal fact_search tables",
    ("mempalace/fact_index.py", "joined", "DROP TABLE IF EXISTS \x00"): "the same loop, joined",
}
# The per-piece rule's own allowlist (path and text only), for the floor test.
_PIECE_ALLOWED = {
    ("mempalace/backends/pgvector.py", "DELETE FROM \x00 WHERE id = ANY(%s)"),
    ("mempalace/backends/pgvector.py", "DELETE FROM \x00 WHERE \x00"),
    ("mempalace/backends/pgvector.py", "DROP TABLE IF EXISTS \x00"),
    ("mempalace/fact_index.py", "DROP TABLE IF EXISTS "),
}


def _render(node):
    """Text of a string expression; non-string parts become ``\x00``."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(_render(part) for part in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _render(node.left) + _render(node.right)
    return "\x00"


def _is_string(node):
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _is_string(node.left) or _is_string(node.right)
    return isinstance(node, ast.JoinedStr) or (
        isinstance(node, ast.Constant) and isinstance(node.value, str)
    )


def _pieces(tree):
    """Per-piece strings: every literal outside an f-string, and each f-string."""
    parts = {
        id(p) for node in ast.walk(tree) if isinstance(node, ast.JoinedStr) for p in node.values
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in parts:
            yield node.lineno, node.value
        elif isinstance(node, ast.JoinedStr):
            yield node.lineno, _render(node)


def _joined(tree):
    """Whole ``+`` chains that contain a string, rendered."""
    nested = {
        id(child)
        for node in ast.walk(tree)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add)
        for child in ast.iter_child_nodes(node)
    }
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.BinOp)
            and isinstance(node.op, ast.Add)
            and id(node) not in nested
            and _is_string(node)
        ):
            yield node.lineno, _render(node)


def _statements(tree):
    """``(line, text, kind)`` for every piece and every joined ``+`` chain."""
    yield from dict.fromkeys(
        [*((line, text, "piece") for line, text in _pieces(tree))]
        + [*((line, text, "joined") for line, text in _joined(tree))]
    )


def _findings(relative, source):
    found = []
    for line, text, kind in _statements(ast.parse(source)):
        for match in (_UPDATE_THREADS.search(text), _UPSERT_THREADS.search(text)):
            if match and _SET_TARGET.search(match.group(match.lastindex)):
                found.append((relative, line, "rewrites wing or source_thread_id"))
        if _DESTRUCTIVE.search(text):
            found.append((relative, line, "destructive thread-store statement"))
        dynamic = _PIECE_DYNAMIC.search(text) or _JOINED_DYNAMIC.search(text)
        if dynamic and (relative, kind, text) not in ALLOWED_DYNAMIC:
            found.append((relative, line, "dynamic table name not on the allowlist"))
    return found


def _piece_rule(source, relative="synthetic.py"):
    """The original per-piece rule alone (upper-case dynamic, unqualified tables)."""
    found = []
    for _, text in _pieces(ast.parse(source)):
        update = re.search(r"\bUPDATE\s+[\"\[`]?threads\b[\"\]`]?\s+SET\s+(.*)", text, re.I | re.S)
        upsert = re.search(
            r"\bINSERT\s+(OR\s+\w+\s+)?INTO\s+[\"\[`]?threads\b.*\bDO\s+UPDATE\s+SET\s+(.*)",
            text,
            re.I | re.S,
        )
        for match in (update, upsert):
            if match and re.search(
                r"\b(wing|source_thread_id)\s*=", match.group(match.lastindex), re.I
            ):
                found.append(text)
        destructive = (
            r"\b(DELETE\s+FROM|DROP\s+TABLE(\s+IF\s+EXISTS)?|REPLACE\s+INTO|"
            r"INSERT\s+OR\s+REPLACE\s+INTO)\s+[\"\[`]?" + _CONTENT_TABLES + r"\b"
        )
        if re.search(destructive, text, re.I) or (
            _PIECE_DYNAMIC.search(text) and (relative, text) not in _PIECE_ALLOWED
        ):
            found.append(text)
    return found


def _scan():
    found = []
    for top in ("commonplace", "mempalace"):
        for path in sorted((ROOT / top).rglob("*.py")):
            relative = path.relative_to(ROOT).as_posix()
            if "/tests/" in relative or path.name.startswith("test_"):
                continue
            found += _findings(relative, path.read_text(encoding="utf-8"))
    return found


# Positive corpus: SQL with a thread-identity rewrite, a thread-content delete or
# a dynamic table name. Must always be flagged.
CAUGHT = [
    'conn.execute(f"DROP TABLE IF EXISTS {name}")',
    'conn.execute("update threads set wing=? where id=?", args)',
    'conn.execute("UPDATE threads " + "SET wing=? WHERE id=?", args)',
    'conn.execute("delete from " + name)',
    'conn.execute(f"drop table if exists {name}")',
    'conn.execute("delete from messages where thread_id=?", args)',
    'conn.execute(("DELETE FROM " + "messages") + " WHERE 1")',
    'conn.execute("DELETE FROM %s WHERE id=?" % name, args)',
    'conn.execute("DELETE FROM messages WHERE thread_id=%s" % tid)',
    'conn.execute("UPDATE threads SET wing=%s WHERE id=%s" % (w, i))',
    'conn.execute("WITH c AS (SELECT 1) DELETE FROM " + name)',
    'conn.execute("BEGIN; DELETE FROM " + name)',
    'conn.execute("delete from {}".format(name))',
    'conn.execute(f"UPDATE threads SET wing={w} WHERE id={i}")',
    'conn.execute("UPDATE %s SET x=1" % table)',
    'conn.execute("DROP TABLE IF EXISTS " + name)',
    'conn.execute(f"DELETE FROM {table} WHERE id=?", args)',
    'conn.execute("/* audit */ DELETE FROM " + name)',
    'conn.execute("/* audit */ DELETE FROM %s WHERE id=?" % name, args)',
    'conn.execute(cte + "DELETE FROM " + name)',
    'conn.execute(cte + " DELETE FROM " + name + " WHERE id=?")',
    'conn.execute(f"{cte} DELETE FROM {name}")',
    'conn.execute("%s DELETE FROM %s" % (cte, name))',
    'conn.execute("/* x */ UPDATE %s SET wing=? WHERE id=?" % name, args)',
    'conn.execute(f"{cte} UPDATE {name} SET x=1")',
    'conn.execute(prefix + "DROP TABLE " + name)',
    'conn.execute("DELETE FROM " + name + " AS d WHERE d.id=?", args)',
    'conn.execute("DELETE FROM " + name + " INDEXED BY ix WHERE id=?", args)',
    'conn.execute("DELETE FROM " + name + " /* scoped */ WHERE id=?", args)',
    'conn.execute("DELETE FROM %s USING other WHERE other.id = %s.id" % (name, name))',
    'conn.execute("DELETE FROM " + name + " " + where)',
    'conn.execute(f"DELETE FROM {name}{where}")',
    'conn.execute(f"DELETE FROM {name} {where}")',
    'conn.execute("DELETE FROM %s %s" % (name, where))',
    'conn.execute("DELETE FROM " + name + "\\n-- note\\nWHERE id=?", args)',
    'conn.execute("DELETE FROM " + name + " LIMIT 10")',
    'conn.execute("DROP TABLE IF EXISTS " + name + " CASCADE")',
    'conn.execute("DROP TABLE IF EXISTS %s CASCADE" % name)',
    'conn.execute(f"DROP TABLE IF EXISTS {name} CASCADE")',
    'conn.execute("DROP TABLE IF EXISTS " + name + " /* x */")',
    'conn.execute("DROP TABLE IF EXISTS " + name + suffix)',
    'conn.execute(f"UPDATE{name} SET x=1")',
    'conn.executescript("/* x */ DELETE FROM %s;" % name)',
    'conn.executescript("""/*\n x\n*/ DELETE FROM %s;""" % name)',
    'conn.executescript("BEGIN;\\nDELETE FROM " + name + "\\nLIMIT 1;")',
    'conn.execute("DELETE FROM %s AS d WHERE d.id=?" % name, args)',
    'conn.execute(f"DELETE FROM {name} AS d WHERE d.id=?", args)',
    'conn.execute(f"DROP TABLE IF EXISTS {name} /* x */")',
    'conn.execute("UPDATE %s AS u SET wing=?" % name, args)',
    'conn.execute(f"UPDATE {name} AS u SET wing=?", args)',
    'conn.execute(f"UPDATE {name} INDEXED BY ix SET x=1")',
    'conn.execute("REPLACE INTO " + name + " (id) VALUES (?)", args)',
    'conn.execute("INSERT OR REPLACE INTO " + name + " (id) VALUES (?)", args)',
    'conn.execute("DELETE FROM main.messages WHERE thread_id=?", args)',
    "conn.execute(\"DELETE FROM 'messages' WHERE thread_id=?\", args)",
    'conn.execute("UPDATE main.threads SET wing=?", args)',
    'conn.execute("UPDATE threads AS t SET wing=?", args)',
    'conn.execute("UPDATE OR REPLACE threads SET wing=?", args)',
    "conn.execute('UPDATE threads SET \"wing\"=?', args)",
    'conn.execute("UPDATE " + name + " AS u SET wing=?", args)',
    'conn.execute("UPDATE " + name + " INDEXED BY ix SET x=1")',
    'conn.execute("UPDATE " + name + " /* c */ SET x=1")',
]

# Negative corpus: prose and unrelated tables. Must never be flagged.
PROSE = [
    'conn.execute("DELETE FROM drawers WHERE id=?", args)',
    'conn.execute("UPDATE boards SET wing=? WHERE id=?", args)',
    'note = f"please update {name} when you delete from the list"',
    'msg = f"Delete from {path} failed"',
    'msg = "Update {name} to retry"',
    'log.info("Drop table {name} skipped")',
    'msg = f"(retry) Delete from {path} failed"',
    'msg = f"drop table if exists {name} first"',
    'msg = f"Update {name} as needed"',
    'msg = f"Delete from {path} as a fallback"',
]

# Known false positives, accepted: prose with a placeholder followed by the end,
# ``;``, ``)``, ``WHERE`` or ``RETURNING``. Prose is never allowlisted.
KNOWN_FALSE_POSITIVES = [
    'log.info("Delete from {path}")',
    'log.info("Drop table {name}")',
    'log.info("Delete from {path} where possible")',
]

# Known limits, caught by neither rule (documented in docs/evals.md and SYSTEM.md):
KNOWN_LIMITS = [
    'conn.execute("UPDATE threads /* c */ SET wing=?", args)',
    'conn.execute("UPDATE threads INDEXED BY ix SET wing=?", args)',
    'conn.execute("UPDATE threads NOT INDEXED SET wing=?", args)',
    "conn.execute('UPDATE \"main\".threads SET wing=?', args)",
    'conn.execute("UPDATE threads SET (wing) = (?) WHERE id=?", args)',
    'conn.execute("UPDATE threads SET (x, wing) = (?, ?) WHERE id=?", args)',
    'conn.execute(f"UPDATE threads SET {col}=? WHERE id=?", args)',
    'conn.execute(\'DELETE FROM "main"."messages" WHERE thread_id=?\', args)',
    "conn.execute('DROP TABLE \"main\".messages')",
    "conn.execute('REPLACE INTO \"main\".messages (id) VALUES (?)', args)",
    'conn.execute("DELETE FROM /**/messages WHERE thread_id=?", args)',
    'conn.execute(f"delete from {name} as d where d.id=?")',
    'conn.execute("delete from %s as d where d.id=?" % name)',
    'conn.execute("/* x */ delete from " + name)',
    'conn.execute("/* x */ REPLACE INTO " + name + " (id) VALUES (?)")',
    'conn.execute("TRUNCATE TABLE messages")',
    'conn.execute("DELETE/**/FROM messages WHERE thread_id=?", args)',
    'conn.execute("DELETE FROM ONLY " + name + " WHERE id=?", args)',
    'conn.execute("DELETE FROM %(t)s WHERE id=%(i)s" % {"t": name, "i": i})',
    'conn.execute("ALTER TABLE threads DROP COLUMN wing")',
    'conn.execute("".join(["UPDATE threads ", "SET wing=?"]), args)',
    'sql = "UPDATE threads "\nsql += "SET wing=?"',
]


@pytest.mark.parametrize("snippet", CAUGHT)
def test_a4_scan_catches_lower_case_and_concatenated_sql(snippet):
    assert _findings("synthetic.py", snippet), f"A4 scan missed: {snippet}"


@pytest.mark.parametrize("snippet", PROSE)
def test_a4_scan_ignores_prose_and_unrelated_tables(snippet):
    assert _findings("synthetic.py", snippet) == [], snippet


def test_a4_scan_catches_everything_the_per_piece_rule_catches():
    corpus = CAUGHT + PROSE + KNOWN_LIMITS
    missed = [s for s in corpus if _piece_rule(s) and not _findings("synthetic.py", s)]
    assert missed == [], f"A4 scan fell below the per-piece rule: {missed}"


@pytest.mark.parametrize("relative", sorted({key[0] for key in ALLOWED_DYNAMIC} | {"synthetic.py"}))
def test_a4_scan_never_falls_below_the_per_piece_rule_on_allowlisted_paths(relative):
    # Each allowlist applies only on its own path, so the floor is checked there.
    corpus = CAUGHT + PROSE + KNOWN_LIMITS + KNOWN_FALSE_POSITIVES
    missed = [s for s in corpus if _piece_rule(s, relative) and not _findings(relative, s)]
    assert missed == [], f"A4 scan fell below the per-piece rule in {relative}: {missed}"


def test_a4_known_false_positives_are_recorded():
    # The accepted trade-off, stated as behaviour: if a rule change stops flagging
    # one of these, move it into PROSE.
    assert all(_findings("synthetic.py", s) for s in KNOWN_FALSE_POSITIVES)


def test_a4_known_limits_are_still_limits():
    # If a rule change starts catching one of these, move it into CAUGHT.
    assert [s for s in KNOWN_LIMITS if _findings("synthetic.py", s)] == []


def test_a4_no_statement_rewrites_identity_or_deletes_thread_rows():
    assert _scan() == [], "A4: thread identity or content can be rewritten or deleted"


def test_a4_scan_sees_the_known_count_updates():
    texts = [
        text
        for path in (
            "commonplace/handoff/thread_store.py",
            "commonplace/handoff/thread_store_v2.py",
        )
        for _, text, _ in _statements(ast.parse((ROOT / path).read_text(encoding="utf-8")))
        if _UPDATE_THREADS.search(text)
    ]
    assert len(texts) == 2 and all("sha256" in text for text in texts)


# ── A5–A6b and C4: the import branch is out of eval's reach ──
def _recall(tmp_path, db, *, eligible=lambda principal: True, view_calls=None):
    from commonplace.continuity_read import PrivateReader

    store = ThreadStore(str(tmp_path / "recall.sqlite3"))

    def store_for(_ws):
        return store

    private = PrivateReader(db, store_for)
    if view_calls is not None:

        def import_view(principal, current):
            view_calls.append(current.id)
            raise ThreadNotFound("spied")

        private.import_view = import_view
    return store, Recall(store_for, private=private, import_eligible=eligible)


def _grant_row(store, user, ws):
    stored = store.store(
        ws,
        f"import-private:{user}:src:t",
        [ThreadMessage(0, "user", "message", "imported", "{}")],
        actor="a",
        wing="private_chats",
    )
    assert re.fullmatch(r"thr_[0-9a-f]+", stored.id)
    return stored


def test_a5_scope_routing_never_enters_import_view(env, tmp_path, monkeypatch):
    db, _, user, ws = env
    store, recall = _recall(tmp_path, db)
    stored = _grant_row(store, user, ws)
    entered = []
    original = Recall._import_view

    def spy(self, principal, row):
        entered.append(row.id)
        return original(self, principal, row)

    monkeypatch.setattr(Recall, "_import_view", spy)
    who = principal(user, ws)
    assert recall.can_read(who, stored.id) is False
    with pytest.raises(ThreadNotFound):
        recall.thread(who, stored.id)
    assert entered == [], "A5: the eval principal was routed into the import view"
    store.close()


@pytest.mark.parametrize("granted", [False, True], ids=["A5b", "C4"])
def test_a5b_c4_scope_gates_the_import_view(env, tmp_path, granted):
    db, _, user, ws = env
    calls = []
    store, recall = _recall(tmp_path, db, view_calls=calls)
    stored = _grant_row(store, user, ws)
    scopes = EVAL_SCOPES + (["memory:import:read"] if granted else [])
    who = principal(user, ws, auth="pat", scopes=scopes)
    assert who.person is False
    recall.can_read(who, stored.id)
    if granted:
        assert calls == [stored.id], "C4: the fixture must reach import_view with the scope"
    else:
        assert calls == [], "A5b: import_view ran without the import read scope"
    store.close()


def test_a6_local_auth_never_reaches_the_eligibility_hook(env, tmp_path):
    db, _, user, ws = env
    hooks = []
    store, recall = _recall(tmp_path, db, eligible=lambda p: hooks.append(p) or True)
    who = principal(user, ws, scopes=EVAL_SCOPES + ["memory:import:read"])
    assert recall.import_reader(who) is False
    assert hooks == [], "A6: local auth reached the import eligibility hook"
    store.close()


def test_a6b_grant_eligibility_refuses_local_auth_before_lookup(tmp_path, monkeypatch):
    with closing(ControlDB(str(tmp_path / "grant.sqlite3"))) as db:
        user = db.create_user("owner@example.test")
        ws = db.create_workspace("Synthetic", user, personal=True)
        calls = []
        original = db.authenticate_connection
        monkeypatch.setattr(
            db, "authenticate_connection", lambda *a: calls.append(a) or original(*a)
        )
        who = principal(user, ws, scopes=EVAL_SCOPES + ["memory:import:read"])
        assert reader_eligible(db, who) is False
        assert calls == [], "A6b: local auth reached the live connection lookup"
