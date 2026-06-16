"""Tests for the read-only SQLite connection helper and the read-path
parameterization regression guard (change: harden-mempalace-sqlite-timeouts)."""

from __future__ import annotations

import ast
import sqlite3
import time
from pathlib import Path

import pytest

from mempalace._sqlite_ro import StatementTimeout, connect_ro, open_ro

import mempalace.searcher as _searcher_mod
import mempalace.repair as _repair_mod
import mempalace.backends.chroma as _chroma_mod
import mempalace.mcp_server as _mcp_mod


@pytest.fixture()
def tiny_db(tmp_path: Path) -> str:
    """A real on-disk SQLite file (open_ro requires mode=ro on a file)."""
    p = tmp_path / "tiny.sqlite3"
    conn = sqlite3.connect(str(p))
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    conn.executemany("INSERT INTO t (v) VALUES (?)", [("a",), ("b",), ("c",)])
    conn.commit()
    conn.close()
    return str(p)


# --- P3.3 deadline -----------------------------------------------------------


def test_deadline_aborts_runaway_statement(tiny_db: str) -> None:
    """A deliberately infinite recursive CTE must be aborted near the deadline,
    not run unbounded."""
    deadline_s = 0.4
    start = time.monotonic()
    with pytest.raises(sqlite3.OperationalError):
        with connect_ro(tiny_db, deadline_s=deadline_s) as conn:
            # Infinite generator — only the progress-handler deadline stops it.
            conn.execute(
                "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c)"
                " SELECT count(*) FROM c"
            ).fetchall()
    elapsed = time.monotonic() - start
    # Aborts promptly after the deadline (generous upper bound for CI jitter).
    assert deadline_s - 0.1 <= elapsed <= deadline_s + 5.0


def test_statement_timeout_is_operationalerror() -> None:
    """The exported StatementTimeout is a sqlite3.OperationalError subclass so
    callers can catch it via the broad sqlite3.Error/OperationalError they
    already handle on read paths."""
    assert issubclass(StatementTimeout, sqlite3.OperationalError)


def test_deadline_abort_raises_statement_timeout_specifically(tiny_db: str) -> None:
    """A deadline overrun raises StatementTimeout (not just bare
    OperationalError), so callers can distinguish a runaway-statement timeout.
    Covers both the connection and the cursor execute surfaces (#1650)."""
    with pytest.raises(StatementTimeout):
        with connect_ro(tiny_db, deadline_s=0.3) as conn:
            conn.execute(
                "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c)"
                " SELECT count(*) FROM c"
            ).fetchall()
    with pytest.raises(StatementTimeout):
        with connect_ro(tiny_db, deadline_s=0.3) as conn:
            cur = conn.cursor()
            cur.execute(
                "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c)"
                " SELECT count(*) FROM c"
            ).fetchall()


def test_non_timeout_operationalerror_not_mistranslated(tiny_db: str) -> None:
    """A write attempt under query_only fails immediately (deadline not passed),
    so it stays a plain OperationalError, not StatementTimeout."""
    with connect_ro(tiny_db, deadline_s=30.0) as conn:
        with pytest.raises(sqlite3.OperationalError) as ei:
            conn.execute("INSERT INTO t (v) VALUES ('x')")
        assert not isinstance(ei.value, StatementTimeout)


def test_build_uri_handles_special_chars(tmp_path: Path) -> None:
    """Path with a '?' (valid POSIX filename char) must still open read-only —
    f-string interpolation would have split it as a URI query (#1650)."""
    weird = tmp_path / "od?d#name.sqlite3"
    conn = sqlite3.connect(str(weird))
    conn.execute("CREATE TABLE t (id INTEGER)")
    conn.execute("INSERT INTO t (id) VALUES (7)")
    conn.commit()
    conn.close()
    with connect_ro(str(weird)) as ro:
        assert ro.execute("SELECT id FROM t").fetchone()[0] == 7


def test_no_deadline_allows_normal_query(tiny_db: str) -> None:
    """deadline_s=None disables the ceiling; a normal query returns rows."""
    with connect_ro(tiny_db, deadline_s=None) as conn:
        rows = conn.execute("SELECT v FROM t ORDER BY id").fetchall()
    assert [r[0] for r in rows] == ["a", "b", "c"]


# --- P3.4 lifecycle ----------------------------------------------------------


def test_context_manager_closes_on_success(tiny_db: str) -> None:
    with connect_ro(tiny_db) as conn:
        conn.execute("SELECT 1").fetchone()
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")  # closed connection


def test_context_manager_closes_on_exception(tiny_db: str) -> None:
    captured = {}

    class Boom(RuntimeError):
        pass

    with pytest.raises(Boom):
        with connect_ro(tiny_db) as conn:
            captured["conn"] = conn
            raise Boom()
    with pytest.raises(sqlite3.ProgrammingError):
        captured["conn"].execute("SELECT 1")


def test_open_ro_caller_owns_close(tiny_db: str) -> None:
    conn = open_ro(tiny_db)
    try:
        assert conn.execute("SELECT count(*) FROM t").fetchone()[0] == 3
    finally:
        conn.close()


# --- read PRAGMAs applied ----------------------------------------------------


def test_safety_floor_pragmas_always_applied(tiny_db: str) -> None:
    """query_only + busy_timeout are the always-on safety floor."""
    with connect_ro(tiny_db) as conn:
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_read_tuning_off_by_default(tiny_db: str) -> None:
    """temp_store stays at its default (not forced to MEMORY) on the live path,
    because the tuning PRAGMAs perturb FTS5 candidate ordering for no measured
    latency gain (see module notes / bench evidence)."""
    with connect_ro(tiny_db) as conn:
        assert conn.execute("PRAGMA temp_store").fetchone()[0] != 2  # not MEMORY


def test_read_tuning_opt_in(tiny_db: str) -> None:
    """Snapshot/benchmark callers can still opt in explicitly."""
    with connect_ro(tiny_db, temp_store_memory=True) as conn:
        assert conn.execute("PRAGMA temp_store").fetchone()[0] == 2


def test_query_only_blocks_writes(tiny_db: str) -> None:
    with connect_ro(tiny_db) as conn:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO t (v) VALUES ('x')")


# --- P5 parameterization regression guard ------------------------------------

# Structural identifiers that may be interpolated into SQL text because they
# carry NO user/query value — only column expressions or pre-built clauses that
# themselves bind values via `?` placeholders. This allowlist locks in the
# currently-green injection posture: a new f-string that interpolates the user
# query (or any name not listed here) into an execute() call fails the test.
# - row_id_expr / filter_sql: pre-built clause fragments that themselves bind
#   every value via `?`.
# - placeholders: the canonical IN-list pattern -- ",".join(["?"]*n); the values
#   are bound separately as a tuple (searcher.py meta_rows query). This is the
#   one dynamic-SQL site the proposal explicitly blesses.
_ALLOWED_SQL_INTERP = {"row_id_expr", "filter_sql", "placeholders"}

_READ_PATH_MODULES = [
    _searcher_mod,
    _repair_mod,
    _chroma_mod,
    _mcp_mod,
]


def _collect_sql_assignments(tree: ast.AST) -> dict:
    """Map ``name -> value node`` for every ``name = <f-string|str-binop>``.

    Lets the guard follow the one-hop indirection
    ``sql = f"... {x} ..."; conn.execute(sql)`` that a direct first-arg-only
    check would miss (gemini review on #1650). We only record dynamic
    string-building values (JoinedStr / BinOp); plain literals are safe and left
    out so they don't mask a later reassignment.
    """
    assigns: dict = {}
    for node in ast.walk(tree):
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
            value = node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets = [node.target]
            value = node.value
        else:
            continue
        if not isinstance(value, (ast.JoinedStr, ast.BinOp)):
            continue
        for tgt in targets:
            if isinstance(tgt, ast.Name):
                assigns[tgt.id] = value
    return assigns


def _execute_sql_args(tree: ast.AST):
    """Yield the first positional arg node of every .execute/.executemany call."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in ("execute", "executemany") and node.args:
                yield node.func.attr, node.args[0]


def _check_sql_node(mod_name: str, sql_arg: ast.AST, offenders: list) -> None:
    """Flag a SQL expression node that interpolates a non-allowlisted value."""
    if isinstance(sql_arg, ast.JoinedStr):  # f-string SQL
        for piece in sql_arg.values:
            if isinstance(piece, ast.FormattedValue):
                name = _interp_name(piece.value)
                if name not in _ALLOWED_SQL_INTERP:
                    offenders.append(f"{mod_name}:{sql_arg.lineno} interpolates {name!r} into SQL")
    elif isinstance(sql_arg, ast.BinOp):  # "..." % x / "..." + x
        offenders.append(f"{mod_name}:{sql_arg.lineno} builds SQL via string operator")


def test_read_paths_never_interpolate_user_values_into_sql() -> None:
    offenders: list = []
    for mod in _READ_PATH_MODULES:
        mod_name = Path(mod.__file__).name
        src = Path(mod.__file__).read_text(encoding="utf-8")
        tree = ast.parse(src)
        assigns = _collect_sql_assignments(tree)
        for _attr, sql_arg in _execute_sql_args(tree):
            # Resolve one hop of indirection: execute(sql) where sql = f"...".
            if isinstance(sql_arg, ast.Name) and sql_arg.id in assigns:
                sql_arg = assigns[sql_arg.id]
            _check_sql_node(mod_name, sql_arg, offenders)
    assert not offenders, "user-value SQL interpolation found:\n" + "\n".join(offenders)


def test_guard_catches_variable_assigned_fstring_sql() -> None:
    """The guard must not be bypassable by assigning the f-string to a variable
    first (the gap gemini flagged on #1650)."""
    src = (
        "def q(conn, user):\n"
        '    sql = f"SELECT * FROM t WHERE x = {user}"\n'
        "    return conn.execute(sql).fetchall()\n"
    )
    tree = ast.parse(src)
    assigns = _collect_sql_assignments(tree)
    offenders: list = []
    for _attr, sql_arg in _execute_sql_args(tree):
        if isinstance(sql_arg, ast.Name) and sql_arg.id in assigns:
            sql_arg = assigns[sql_arg.id]
        _check_sql_node("synthetic.py", sql_arg, offenders)
    assert offenders and "user" in offenders[0]


def _interp_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return "<expr>"
