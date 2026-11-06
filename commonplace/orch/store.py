"""Orchestrator config versions, owner-only accept and the restore flag.

Everything lives in the workspace's ``runner.sqlite3`` (the runner's job store), in the
tables of ``schema.sql``. Board key = board slug.

Versions: every board has ``v1, v2, ...`` with a ``parent_version``, a ``channel``
(``live`` or ``exp:<name>``), an optional unverified ``source_ref`` and the sha256 of the
body. Wing writers propose (``status='proposed'``); the orchestrator owner accepts,
rejects, retires an experiment or writes a version directly (accepted at once). At most
one accepted row per (board, channel); accepting supersedes the previous one. Only
accepted versions run.

Cursor rows (``orch_cursor``/``orch_cursor_gen``): a live accept that makes the board
configured (``[runner]`` present, where it was not) sets ``cursor=NULL, gen=gen+1``
("arming"); one that removes ``[runner]`` deletes the cursor row; one that keeps it does
not touch the cursor. Arming (reading ``boards.sqlite3``) and the sweep belong to the
triggers module.

Restore: ``commonplace.backup.restore()`` leaves ``RESTORE_MARKER`` in every restored
workspace directory. The first explicit ``Store`` open (a surface, the CLI, a lease call or
a start; never the trigger sweep, which opens ``Store.existing`` and skips while a marker
is pending) converts it into ``orch_state.restore_flag=1``
with a new incarnation (old lease tokens, bound to the incarnation, die) and drops every
orchestrator lease in the same transaction, and deletes the marker only after that commit. While the flag is set, lease claims, every config-driven
start and the sweep refuse (``check_restore_in``). Only ``restore_ack`` with the exact
text ``"effects after <T> may be unknown"`` clears it; it re-arms the cursor of every
board configured at that moment.

Authority: ``orch_owner`` (a signed-in person with role owner, or the local CLI). The
store does not know board wings: callers check wing read access (reads) and wing write
access (``propose``) before calling.
"""

from __future__ import annotations

import functools
import hmac
import json
import re
import secrets
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, fields
from datetime import datetime, timezone
from pathlib import Path

from commonplace.backup import RESTORE_MARKER
from commonplace.extensions import ToolError
from mempalace.sqlite_crypto import connect, row_factory

from . import schema

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib

SCHEMA_SQL = Path(__file__).with_name("schema.sql")
DB_NAME = "runner.sqlite3"
BOARD = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
CHANNEL = re.compile(r"live|exp:[a-z0-9-]{1,32}")
SOURCE_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/@:+#-]{0,199}")
LIVE = "live"
MAX_OPEN_PER_BOARD = 20
MAX_OPEN_PER_PROPOSER = 5
MAX_EXPERIMENTS = 8
ACK_TEMPLATE = "effects after {after} may be unknown"
TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
__all__ = [
    "ACK_TEMPLATE",
    "AcceptResult",
    "LIVE",
    "OrchError",
    "RESTORE_MARKER",
    "RestoreState",
    "Store",
    "Version",
    "accepted_in",
    "check_restore_in",
    "configured_boards_in",
    "configured_in",
    "cursor_in",
    "drop_cursor_in",
    "incarnation_in",
    "marker_pending",
    "open_existing",
    "open_store",
    "orch_owner",
    "require_owner",
    "reset_cursor_in",
    "resolve_in",
    "restore_state_in",
]

_FORBIDDEN = frozenset({"owner_only", "restore_text"})
_CONFLICT = frozenset(
    {
        "not_proposed",
        "sha256_mismatch",
        "too_many_open",
        "too_many_experiments",
        "no_live",
        "not_configured",
        "unknown_experiment",
        "experiment_not_allowed",
        "config_invalid",
        "restore_pending",
        "restore_not_pending",
    }
)


class OrchError(ToolError):
    """A refusal. ``code`` is stable; ``status`` is the HTTP status a surface should use;
    ``str()`` is ``"no: <message>"`` and never contains config text."""

    def __init__(self, code: str, message: str, *, path: str = "", status: int | None = None):
        self.code = code
        self.path = path
        if status is None:
            status = (
                403
                if code in _FORBIDDEN
                else 404
                if code == "not_found"
                else 409
                if code in _CONFLICT
                else 400
            )
        self.status = status
        super().__init__(f"no: {message} ({code})")


def _config_error(exc: schema.ConfigError, *, status: int | None = None) -> OrchError:
    where = exc.path or "config"
    return OrchError(exc.code, f"{where}: {exc.message or exc.code}", path=exc.path, status=status)


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime(TIME_FORMAT)


@functools.lru_cache(maxsize=128)
def _parse_body(body: str) -> schema.Config:
    return schema.parse(body)


@dataclass(frozen=True)
class Version:
    board: str
    version: int
    parent_version: int | None
    channel: str
    status: str
    source_ref: str | None
    sha256: str
    body: str
    proposed_by: str
    proposer_trust: str
    decided_by: str | None
    decided_at: str | None
    created_at: str

    @property
    def ref(self) -> str:
        """``v<n>``."""
        return f"v{self.version}"

    def config(self) -> schema.Config:
        """Parsed body (cached by text); raises ``schema.ConfigError``."""
        return _parse_body(self.body)

    def public(self, *, body: bool = False) -> dict:
        """JSON-safe dict for surfaces; the body only on request."""
        data = {f.name: getattr(self, f.name) for f in fields(self) if f.name != "body"}
        data["ref"] = self.ref
        if body:
            data["body"] = self.body
        return data


_COLUMNS = tuple(f.name for f in fields(Version))


@dataclass(frozen=True)
class AcceptResult:
    version: Version  # the accepted row
    previous: Version | None  # superseded accepted row of the same channel, if any
    changes: tuple[schema.Change, ...]  # schema.diff(previous or empty, new)
    cursor: str | None  # live only: "arming" | "dropped" | None (untouched)
    gen: int | None  # new generation when cursor == "arming"

    @property
    def arm_needed(self) -> bool:
        """True when the caller should run the triggers module's arm_cursor(board) now."""
        return self.cursor == "arming"


@dataclass(frozen=True)
class RestoreState:
    incarnation: str
    flagged: bool
    after: str | None  # T, "YYYY-MM-DDTHH:MM:SSZ" (backup time), while flagged
    ack_text: str | None  # exact text restore_ack needs, while flagged
    restored_at: str | None
    acked_by: str | None
    acked_at: str | None


def orch_owner(principal) -> bool:
    """``(person and role == "owner" and not a runner child) or auth == "local"``."""
    if getattr(principal, "auth", None) == "local":
        return True
    return bool(
        getattr(principal, "person", False)
        and getattr(principal, "role", None) == "owner"
        and getattr(principal, "runner", None) is None
    )


def require_owner(principal) -> None:
    """Raise ``OrchError('owner_only', 403)`` unless ``orch_owner(principal)``."""
    if not orch_owner(principal):
        raise OrchError(
            "owner_only", "only the workspace owner (signed in, or the CLI) can do that"
        )


def _board(board) -> str:
    if not isinstance(board, str) or not BOARD.fullmatch(board):
        raise OrchError("invalid_board", "board must be a board slug", path="board")
    return board


def _channel(channel) -> str:
    if not isinstance(channel, str) or not CHANNEL.fullmatch(channel):
        raise OrchError("invalid_channel", "channel must be live or exp:<name>", path="channel")
    return channel


def _version(row) -> Version | None:
    return None if row is None else Version(**{name: row[name] for name in _COLUMNS})


def _has_runner(body: str) -> bool:
    try:
        return _parse_body(body).has_runner
    except schema.ConfigError:
        try:
            return "runner" in tomllib.loads(body)
        except Exception:
            return True  # unreadable live config: fail closed (configured)


def _checked_config(version: Version) -> schema.Config:
    """Config of a stored version for a run; refuses a tampered or no longer valid body."""
    if not hmac.compare_digest(schema.sha256_text(version.body), version.sha256):
        raise OrchError("config_invalid", f"stored {version.ref} does not match its sha256")
    try:
        return version.config()
    except schema.ConfigError as exc:
        raise OrchError(
            "config_invalid", f"stored {version.ref} is no longer valid ({exc.code})"
        ) from None


# ---------------------------------------------------- connection-level helpers
# For callers that already hold a connection (or a BEGIN IMMEDIATE transaction) on the
# same runner.sqlite3, e.g. fenced_start, lease claims and the sweep.


def accepted_in(conn, board: str, channel: str = LIVE) -> Version | None:
    """The accepted version of ``(board, channel)`` or ``None``."""
    return _version(
        conn.execute(
            "SELECT * FROM orch_configs WHERE board=? AND channel=? AND status='accepted'",
            (board, channel),
        ).fetchone()
    )


def configured_in(conn, board: str) -> bool:
    """True when the board's accepted live config has ``[runner]``.

    A stored live body that no longer parses counts as configured (fail closed)."""
    live = accepted_in(conn, board, LIVE)
    return live is not None and _has_runner(live.body)


def configured_boards_in(conn) -> list[str]:
    """Sorted slugs of every configured board (the sweep scope)."""
    rows = conn.execute(
        "SELECT board, body FROM orch_configs WHERE channel='live' AND status='accepted' "
        "ORDER BY board"
    ).fetchall()
    return [row["board"] for row in rows if _has_runner(row["body"])]


def resolve_in(conn, board: str, config_ref: str | None = None) -> Version:
    """The accepted version a config-driven start on ``board`` runs.

    ``config_ref`` ``None``/``"live"`` -> live; ``"exp:<name>"`` -> that experiment, which
    must still pass ``schema.check_experiment`` against the current live config.
    Refusals: ``not_configured`` (no live config with ``[runner]``), ``invalid_channel``,
    ``unknown_experiment``, ``experiment_not_allowed``, ``config_invalid``."""
    _board(board)
    channel = LIVE if config_ref is None else _channel(config_ref)
    live = accepted_in(conn, board, LIVE)
    if live is None or not _has_runner(live.body):
        raise OrchError("not_configured", "the board has no accepted live config with [runner]")
    live_config = _checked_config(live)
    if channel == LIVE:
        return live
    exp = accepted_in(conn, board, channel)
    if exp is None:
        raise OrchError("unknown_experiment", "no accepted config on that experiment channel")
    try:
        schema.check_experiment(live_config, _checked_config(exp))
    except schema.ConfigError as exc:
        raise _config_error(exc, status=409) from None
    return exp


def restore_state_in(conn) -> RestoreState:
    """The restore flag; read-only. A store that was never opened (its tables come from
    an external seed, so ``orch_state`` or its row is missing) never converted a marker,
    so it is not flagged; a pending marker is the caller's check (``marker_pending``)."""
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='orch_state'"
    ).fetchone():
        return RestoreState("", False, None, None, None, None, None)
    row = conn.execute("SELECT * FROM orch_state WHERE id=1").fetchone()
    if row is None:  # tables created by an external seed, store never opened
        return RestoreState("", False, None, None, None, None, None)
    flagged = bool(row["restore_flag"])
    after = row["restore_after"] if flagged else None
    return RestoreState(
        incarnation=row["incarnation"],
        flagged=flagged,
        after=after,
        ack_text=ACK_TEMPLATE.format(after=after) if flagged else None,
        restored_at=row["restored_at"],
        acked_by=row["acked_by"],
        acked_at=row["acked_at"],
    )


def check_restore_in(conn) -> None:
    """Raise ``OrchError('restore_pending', 409)`` while the restore flag is set."""
    if restore_state_in(conn).flagged:
        raise OrchError(
            "restore_pending",
            "this data was restored from a backup; the owner must acknowledge the restore first",
        )


def incarnation_in(conn) -> str:
    """Current incarnation (32 hex); changes when a restore marker is converted."""
    return restore_state_in(conn).incarnation


def cursor_in(conn, board: str) -> tuple[int | None, int] | None:
    """``(cursor, gen)`` of the board's cursor row, or ``None`` when there is none."""
    row = conn.execute("SELECT cursor, gen FROM orch_cursor WHERE board=?", (board,)).fetchone()
    return None if row is None else (row[0], row[1])


def _last_gen(conn, board: str) -> int:
    row = conn.execute(
        "SELECT MAX(g) FROM (SELECT last_gen AS g FROM orch_cursor_gen WHERE board=? "
        "UNION ALL SELECT gen FROM orch_cursor WHERE board=?)",
        (board, board),
    ).fetchone()
    return row[0] or 0


def reset_cursor_in(conn, board: str) -> int:
    """Set ``cursor=NULL, gen=last_gen+1`` (row re-created if missing); return the gen.
    Caller holds a write transaction."""
    gen = _last_gen(conn, board) + 1
    conn.execute(
        "INSERT OR REPLACE INTO orch_cursor_gen(board, last_gen) VALUES (?, ?)", (board, gen)
    )
    conn.execute(
        "INSERT OR REPLACE INTO orch_cursor(board, cursor, gen) VALUES (?, NULL, ?)", (board, gen)
    )
    return gen


def drop_cursor_in(conn, board: str) -> None:
    """Delete the board's cursor row; ``orch_cursor_gen`` keeps the last gen."""
    conn.execute(
        "INSERT OR REPLACE INTO orch_cursor_gen(board, last_gen) VALUES (?, ?)",
        (board, _last_gen(conn, board)),
    )
    conn.execute("DELETE FROM orch_cursor WHERE board=?", (board,))


# ------------------------------------------------------------------- markers


def _marker_time(value) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime(TIME_FORMAT)


def _read_marker(path: Path) -> tuple[str, str]:
    """``(T, marker id)``; an unreadable marker still flags (T = its mtime)."""
    info = path.lstat()
    fallback_id = f"stat:{info.st_mtime_ns}:{info.st_size}"
    fallback_after = datetime.fromtimestamp(info.st_mtime, timezone.utc).strftime(TIME_FORMAT)
    if path.is_symlink() or info.st_size > 4096:
        return fallback_after, fallback_id
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return fallback_after, fallback_id
    if not isinstance(data, dict):
        return fallback_after, fallback_id
    marker_id = data.get("id")
    if not isinstance(marker_id, str) or not re.fullmatch(r"[0-9a-f]{32}", marker_id):
        marker_id = fallback_id
    return _marker_time(data.get("backup_created_at")) or fallback_after, marker_id


# ---------------------------------------------------------------------- store


class Store:
    """``runner.sqlite3`` in ``directory`` (a resolved workspace directory).

    Opening creates the tables (idempotent) and converts a restore marker; both write.
    ``Store.existing`` opens without running any statement.
    ``now`` returns ``"YYYY-MM-DDTHH:MM:SSZ"`` (injected in tests)."""

    def __init__(self, directory, *, now: Callable[[], str] | None = None):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = str(self.directory / DB_NAME)
        self._now = now or _utcnow
        with self.connection() as conn:
            conn.executescript(SCHEMA_SQL.read_text(encoding="utf-8"))
            conn.execute(
                "INSERT OR IGNORE INTO orch_state(id, incarnation, restore_flag, updated_at) "
                "VALUES (1, ?, 0, ?)",
                (secrets.token_hex(16), self._now()),
            )
        self._convert_marker()

    @classmethod
    def existing(cls, directory, *, now: Callable[[], str] | None = None) -> Store:
        """A store over an already initialized ``runner.sqlite3`` that opens without a
        statement: no schema script, no ``orch_state`` insert, no marker conversion.

        For the trigger sweep, whose only ``runner.sqlite3`` writes are the arming
        ``UPDATE`` and ``fenced_start``. The caller checks first that the tables exist and
        that no restore marker is pending (``marker_pending``)."""
        st = cls.__new__(cls)
        st.directory = Path(directory)
        st.path = str(st.directory / DB_NAME)
        st._now = now or _utcnow
        return st

    def _convert_marker(self) -> None:
        marker = self.directory / RESTORE_MARKER
        if not marker_pending(self.directory):
            return
        after, marker_id = _read_marker(marker)
        with self.transaction() as conn:
            row = conn.execute("SELECT restore_marker FROM orch_state WHERE id=1").fetchone()
            if row["restore_marker"] != marker_id:
                now = self._now()
                conn.execute(
                    "UPDATE orch_state SET incarnation=?, restore_flag=1, restore_after=?, "
                    "restore_marker=?, restored_at=?, acked_by=NULL, acked_at=NULL, "
                    "updated_at=? WHERE id=1",
                    (secrets.token_hex(16), after, marker_id, now, now),
                )
                if conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='orch_leases'"
                ).fetchone():
                    from . import durable  # durable imports this module

                    durable.drop_leases_in(conn, now=now)
        # Only after the flag is committed: a crash before this line re-reads the same
        # marker id next time and leaves the committed flag and incarnation as they are.
        marker.unlink(missing_ok=True)

    @contextmanager
    def connection(self) -> Iterator:
        """Autocommit connection (``isolation_level=None``), Row factory, 10 s busy wait."""
        conn = connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = row_factory
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator:
        """``BEGIN IMMEDIATE`` ... COMMIT, rollback on any exception."""
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    # reads
    def get(self, board: str, version: int) -> Version:
        """Raises ``not_found`` (404)."""
        _board(board)
        with self.connection() as conn:
            return self._get_in(conn, board, version)

    @staticmethod
    def _get_in(conn, board: str, version) -> Version:
        if type(version) is not int or version < 1:
            raise OrchError("not_found", "no such config version", path="version")
        found = _version(
            conn.execute(
                "SELECT * FROM orch_configs WHERE board=? AND version=?", (board, version)
            ).fetchone()
        )
        if found is None:
            raise OrchError("not_found", "no such config version", path="version")
        return found

    def versions(
        self, board: str, *, channel: str | None = None, status: str | None = None
    ) -> list[Version]:
        """Newest first."""
        _board(board)
        sql, args = "SELECT * FROM orch_configs WHERE board=?", [board]
        if channel is not None:
            sql, args = sql + " AND channel=?", [*args, _channel(channel)]
        if status is not None:
            sql, args = sql + " AND status=?", [*args, status]
        with self.connection() as conn:
            rows = conn.execute(sql + " ORDER BY version DESC", args).fetchall()
        return [_version(row) for row in rows]

    def accepted(self, board: str, channel: str = LIVE) -> Version | None:
        _board(board)
        _channel(channel)
        with self.connection() as conn:
            return accepted_in(conn, board, channel)

    def resolve(self, board: str, config_ref: str | None = None) -> Version:
        with self.connection() as conn:
            return resolve_in(conn, board, config_ref)

    def configured_boards(self) -> list[str]:
        with self.connection() as conn:
            return configured_boards_in(conn)

    def diff(self, board: str, old: int, new: int) -> list[schema.Change]:
        """``orch diff vA vB``."""
        _board(board)
        with self.connection() as conn:
            a, b = self._get_in(conn, board, old), self._get_in(conn, board, new)
        try:
            return schema.diff(a.config(), b.config())
        except schema.ConfigError as exc:
            raise _config_error(exc) from None

    # writes
    def _parse(self, board, text, sha256, connections) -> tuple[str, schema.Config]:
        try:
            config = schema.parse(text, expected_sha256=sha256)
        except schema.ConfigError as exc:
            raise _config_error(exc) from None
        body = text.decode("utf-8") if isinstance(text, (bytes, bytearray)) else text
        self._check(board, config, connections)
        return body, config

    @staticmethod
    def _check(board: str, config: schema.Config, connections) -> None:
        if config.project != board:
            raise OrchError(
                "project_mismatch", "the config's project must be the board slug", path="project"
            )
        try:
            schema.check_connections(config, list(connections))
        except schema.ConfigError as exc:
            raise _config_error(exc) from None

    @staticmethod
    def _experiment_base(conn, board: str, config: schema.Config) -> Version:
        live = accepted_in(conn, board, LIVE)
        if live is None:
            raise OrchError("no_live", "an experiment needs an accepted live config")
        try:
            schema.check_experiment(_checked_config(live), config)
        except schema.ConfigError as exc:
            raise _config_error(exc, status=409) from None
        return live

    def _insert(self, conn, principal, board, channel, body, config, source_ref) -> Version:
        if channel != LIVE:
            base = self._experiment_base(conn, board, config)
        else:
            base = None
        parent = accepted_in(conn, board, channel) or base
        version = conn.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 FROM orch_configs WHERE board=?", (board,)
        ).fetchone()[0]
        trust = getattr(getattr(principal, "policy", None), "trust", None) or "unknown"
        conn.execute(
            "INSERT INTO orch_configs(board, version, parent_version, channel, status, "
            "source_ref, sha256, body, proposed_by, proposer_trust, created_at) "
            "VALUES (?, ?, ?, ?, 'proposed', ?, ?, ?, ?, ?, ?)",
            (
                board,
                version,
                parent.version if parent else None,
                channel,
                source_ref,
                config.sha256,
                body,
                principal.actor,
                trust,
                self._now(),
            ),
        )
        return self._get_in(conn, board, version)

    @staticmethod
    def _source_ref(source_ref):
        if source_ref is not None and (
            not isinstance(source_ref, str) or not SOURCE_REF.fullmatch(source_ref)
        ):
            raise OrchError("invalid_source_ref", "source_ref is not a ref", path="source_ref")
        return source_ref

    def propose(
        self,
        principal,
        board: str,
        text: str | bytes,
        *,
        connections: Iterable[str],
        channel: str = LIVE,
        sha256: str | None = None,
        source_ref: str | None = None,
    ) -> Version:
        """New ``proposed`` version. Caller has checked wing write access.

        ``text`` is parsed (``schema.parse``, sha256 checked first when given), planner
        connections are checked against ``connections`` (workspace connection names),
        ``config.project`` must equal ``board`` (``project_mismatch``); an experiment needs
        a live config and must pass ``check_experiment`` against it (``no_live``). At most
        20 open proposals per board and 5 per proposer (``too_many_open``). Stamps
        ``principal.actor`` and ``principal.policy.trust``."""
        _board(board)
        _channel(channel)
        self._source_ref(source_ref)
        body, config = self._parse(board, text, sha256, connections)
        with self.transaction() as conn:
            open_board, open_mine = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(proposed_by=?), 0) FROM orch_configs "
                "WHERE board=? AND status='proposed'",
                (principal.actor, board),
            ).fetchone()
            if open_board >= MAX_OPEN_PER_BOARD or open_mine >= MAX_OPEN_PER_PROPOSER:
                raise OrchError(
                    "too_many_open",
                    f"at most {MAX_OPEN_PER_BOARD} open proposals per board and "
                    f"{MAX_OPEN_PER_PROPOSER} per proposer",
                )
            return self._insert(conn, principal, board, channel, body, config, source_ref)

    def write(
        self,
        principal,
        board: str,
        text: str | bytes,
        *,
        connections: Iterable[str],
        sha256: str,
        channel: str = LIVE,
        source_ref: str | None = None,
    ) -> AcceptResult:
        """Owner writes a version directly (accepted at once); same checks as propose +
        accept."""
        require_owner(principal)
        _board(board)
        _channel(channel)
        self._source_ref(source_ref)
        if sha256 is None:
            raise OrchError("sha256_invalid", "the approved sha256 is required", path="sha256")
        connections = list(connections)
        body, config = self._parse(board, text, sha256, connections)
        with self.transaction() as conn:
            proposed = self._insert(conn, principal, board, channel, body, config, source_ref)
            return self._accept_in(conn, principal, proposed, connections)

    def accept(
        self, principal, board: str, version: int, *, sha256: str, connections: Iterable[str]
    ) -> AcceptResult:
        """Owner accepts a ``proposed`` version whose sha256 equals ``sha256`` (the text the
        owner approved). Re-validates the body, connections and (exp) the allow-list
        against the current live config; at most 8 accepted experiments per board. The
        previous accepted row of the channel becomes ``superseded``; live cursor rules in
        the module docstring. Refusals: ``owner_only``, ``not_found``, ``not_proposed``,
        ``sha256_mismatch``, schema codes, ``too_many_experiments``, ``no_live``."""
        require_owner(principal)
        _board(board)
        if not isinstance(sha256, str) or not schema.SHA256_HEX.fullmatch(sha256):
            raise OrchError("sha256_invalid", "must be 64 lowercase hex characters", path="sha256")
        connections = list(connections)
        with self.transaction() as conn:
            proposed = self._get_in(conn, board, version)
            if proposed.status != "proposed":
                raise OrchError("not_proposed", f"{proposed.ref} is {proposed.status}")
            if not hmac.compare_digest(proposed.sha256, sha256):
                raise OrchError(
                    "sha256_mismatch",
                    "the version does not match the approved sha256",
                    path="sha256",
                )
            return self._accept_in(conn, principal, proposed, connections)

    def _accept_in(self, conn, principal, proposed: Version, connections) -> AcceptResult:
        board, channel = proposed.board, proposed.channel
        try:
            config = schema.parse(proposed.body, expected_sha256=proposed.sha256)
        except schema.ConfigError as exc:
            raise _config_error(exc) from None
        self._check(board, config, connections)
        previous = accepted_in(conn, board, channel)
        cursor = gen = None
        if channel != LIVE:
            self._experiment_base(conn, board, config)
            if previous is None:
                count = conn.execute(
                    "SELECT COUNT(*) FROM orch_configs WHERE board=? AND channel!='live' "
                    "AND status='accepted'",
                    (board,),
                ).fetchone()[0]
                if count >= MAX_EXPERIMENTS:
                    raise OrchError(
                        "too_many_experiments", f"at most {MAX_EXPERIMENTS} experiments per board"
                    )
        else:
            before = previous is not None and _has_runner(previous.body)
            if config.has_runner and not before:
                gen, cursor = reset_cursor_in(conn, board), "arming"
            elif before and not config.has_runner:
                drop_cursor_in(conn, board)
                cursor = "dropped"
        now = self._now()
        if previous is not None:
            conn.execute(
                "UPDATE orch_configs SET status='superseded' "
                "WHERE board=? AND version=? AND status='accepted'",
                (board, previous.version),
            )
        conn.execute(
            "UPDATE orch_configs SET status='accepted', decided_by=?, decided_at=? "
            "WHERE board=? AND version=? AND status='proposed'",
            (principal.actor, now, board, proposed.version),
        )
        try:
            old = previous.config() if previous is not None else None
        except schema.ConfigError:
            old = None
        if old is None:
            changes = tuple(
                schema.Change(path=key, op="add", new=value)
                for key, value in sorted(config.to_dict().items())
            )
        else:
            changes = tuple(schema.diff(old, config))
        return AcceptResult(
            version=self._get_in(conn, board, proposed.version),
            previous=previous,
            changes=changes,
            cursor=cursor,
            gen=gen,
        )

    def reject(self, principal, board: str, version: int) -> Version:
        """Owner rejects a ``proposed`` version."""
        require_owner(principal)
        _board(board)
        with self.transaction() as conn:
            proposed = self._get_in(conn, board, version)
            if proposed.status != "proposed":
                raise OrchError("not_proposed", f"{proposed.ref} is {proposed.status}")
            conn.execute(
                "UPDATE orch_configs SET status='rejected', decided_by=?, decided_at=? "
                "WHERE board=? AND version=? AND status='proposed'",
                (principal.actor, self._now(), board, version),
            )
            return self._get_in(conn, board, version)

    def retire(self, principal, board: str, channel: str) -> list[Version]:
        """Owner closes ``exp:<name>``: its accepted row -> superseded, its open proposals ->
        rejected. Returns the changed rows; ``unknown_experiment`` when nothing changed."""
        require_owner(principal)
        _board(board)
        if _channel(channel) == LIVE:
            raise OrchError("invalid_channel", "only an experiment can be retired", path="channel")
        with self.transaction() as conn:
            rows = conn.execute(
                "SELECT version, status FROM orch_configs WHERE board=? AND channel=? "
                "AND status IN ('accepted', 'proposed')",
                (board, channel),
            ).fetchall()
            if not rows:
                raise OrchError("unknown_experiment", "no open config on that experiment channel")
            now = self._now()
            for row in rows:
                conn.execute(
                    "UPDATE orch_configs SET status=?, decided_by=?, decided_at=? "
                    "WHERE board=? AND version=?",
                    (
                        "superseded" if row["status"] == "accepted" else "rejected",
                        principal.actor,
                        now,
                        board,
                        row["version"],
                    ),
                )
            return [self._get_in(conn, board, row["version"]) for row in rows]

    # restore
    def restore_state(self) -> RestoreState:
        with self.connection() as conn:
            return restore_state_in(conn)

    def restore_ack(self, principal, text: str) -> list[tuple[str, int]]:
        """Owner clears the restore flag with the exact ``RestoreState.ack_text``.

        Every board configured now gets ``cursor=NULL, gen+1`` (arming) in the same
        transaction; returns ``[(board, gen), ...]``. Refusals: ``owner_only``,
        ``restore_not_pending``, ``restore_text``."""
        require_owner(principal)
        with self.transaction() as conn:
            state = restore_state_in(conn)
            if not state.flagged:
                raise OrchError("restore_not_pending", "there is no restore to acknowledge")
            if not isinstance(text, str) or not hmac.compare_digest(
                text.encode("utf-8"), state.ack_text.encode("utf-8")
            ):
                raise OrchError(
                    "restore_text", "type the exact acknowledgement text shown by restore status"
                )
            reset = [(board, reset_cursor_in(conn, board)) for board in configured_boards_in(conn)]
            now = self._now()
            conn.execute(
                "UPDATE orch_state SET restore_flag=0, acked_by=?, acked_at=?, updated_at=? "
                "WHERE id=1",
                (principal.actor, now, now),
            )
            return reset


def open_store(gateway, workspace_id: str, **kwargs) -> Store:
    """``Store(gateway.workspace_dir(workspace_id))``, the runner job store's directory."""
    return Store(gateway.workspace_dir(workspace_id), **kwargs)


def open_existing(gateway, workspace_id: str, **kwargs) -> Store:
    """``Store.existing`` over the workspace's ``runner.sqlite3`` (no statement on open)."""
    return Store.existing(gateway.workspace_dir(workspace_id), **kwargs)


def marker_pending(directory) -> bool:
    """True while ``directory`` holds a restore marker not yet converted to the flag."""
    marker = Path(directory) / RESTORE_MARKER
    return marker.exists() or marker.is_symlink()
