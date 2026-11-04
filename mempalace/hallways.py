"""Hallways — within-wing entity-to-entity connectors.

A **hallway** is a connection between two entities (people, projects,
concepts, interests) inside one wing, materialized from their
co-occurrence across that wing's drawers. Conceptually:

    WING → has DRAWERS (each tagged with entities)
            entities → connected to other entities by HALLWAYS
                       (within-wing, built from drawer co-occurrence)
                       hallways → are the primitive
                                   tunnels → use hallways to spawn
                                             cross-wing connections

If Aya and Lumi are both mentioned in 47 drawers across the diary,
letters, and ideas rooms, there's a hallway between them. If Aya
and "consciousness" co-occur in 19 drawers, there's a hallway between
them too. The hallway *is* the structural fact of "these two entities
travel together inside this wing."

Mempalace's tunnel primitive in ``palace_graph.py`` connects rooms
across wings. This module fills the within-wing gap with an
entity-centric (not room-centric) model: hallways are about *who/what
relates to whom/what*, not *which rooms relate to which*. A planned
follow-up PR will refactor ``_compute_topic_tunnels_for_wing`` to
build cross-wing tunnels from hallway data (Wing → Drawer-entities →
Hallway → Tunnel).

Persistence is one SQLite table, ``hallways.sqlite3``, beside the palace
where ``hallways.json`` used to be, so the records survive palace rebuilds.
Each row holds one record verbatim, plus its wing and entities as indexed
columns: a mine replaces only its own wing's changed rows, and a query reads
only the rows it asks for. A palace that still has ``hallways.json`` has it
imported by the first write, and the file is then kept under a new name.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import sqlite3
from collections import defaultdict
from itertools import combinations
from pathlib import Path
from typing import Iterator, Optional

from . import paths
from .dynamics import initialize_dynamics_fields
from .fsio import (
    move_aside as _move_aside,
    read_json_for_update as _read_json_for_update,
    refuse_unparsed as _refuse_unparsed,
)
from .metadata_types import is_bookkeeping
from .timeutil import utc_now_iso

logger = logging.getLogger(__name__)

# The store is resolved through ``_get_hallway_file`` below, which mirrors
# ``palace_graph._get_tunnel_file`` (the 3.3.6 palace-scoped pattern), and the
# database sits beside that path. Tests should monkey-patch
# ``_get_hallway_file`` and ``_legacy_hallway_file`` rather than poking a
# module-level constant: both the database and the imported JSON follow it.


__all__ = [
    "compute_hallways_for_wing",
    "list_hallways",
    "delete_hallway",
]


# ─────────────────────────────────────────────────────────────────────────────
# Persistence — SQLite beside the palace, restricted perms (0600) on POSIX.
# ``hallways.json`` (MempalaceConfig.hallway_file) is the document this store
# replaced: the first write imports it in the same transaction that marks the
# store as the source of truth, and moves it aside once that commits. Until
# then readers keep reading the JSON, so a read never writes. Pre-3.3.6
# behavior (hardcoded ~/.mempalace/hallways.json) is kept only as a one-time
# orphan detection fallback, matching the palace_graph tunnel-file pattern.
# ─────────────────────────────────────────────────────────────────────────────

# meta key whose presence means the table, not hallways.json, holds the records.
_IMPORTED_KEY = "json_imported"

_DDL = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS hallways (
    id TEXT PRIMARY KEY,
    wing TEXT,
    entity_a TEXT,
    entity_b TEXT,
    co_occurrence_count,
    signature TEXT NOT NULL,
    record TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_hallways_wing ON hallways(wing);
"""

# An upsert keeps the row's rowid, so list order (insertion order) survives a
# record being rewritten in place.
_UPSERT = """
INSERT INTO hallways (id, wing, entity_a, entity_b, co_occurrence_count, signature, record)
VALUES (?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(id) DO UPDATE SET
    wing = excluded.wing,
    entity_a = excluded.entity_a,
    entity_b = excluded.entity_b,
    co_occurrence_count = excluded.co_occurrence_count,
    signature = excluded.signature,
    record = excluded.record
"""


def _get_hallway_file(config=None) -> str:
    """Return the path to the hallways.json file, derived from MempalaceConfig.palace_path.

    The store replaced that file; it is now only the import source, and the
    database beside it (:func:`_get_hallway_db`) holds the records.
    """
    from .config import MempalaceConfig

    config = config or MempalaceConfig()
    return config.hallway_file


def _get_hallway_db(config=None) -> str:
    """The hallway store: ``hallways.sqlite3`` beside :func:`_get_hallway_file`."""
    root, _ext = os.path.splitext(_get_hallway_file(config))
    return root + ".sqlite3"


def _scalar(value):
    """A value SQLite can bind as an indexed column; anything else as JSON text."""
    if value is None or isinstance(value, (str, int, float)):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _signature(record: dict) -> str:
    """A digest of what a recompute derives for one hallway; equal means nothing to rewrite.

    Dynamics fields and ``created_at`` are left out: a recompute carries the
    former over and must not rewrite a record only to re-stamp the latter. A
    128-bit digest rather than the fields themselves keeps the column from
    repeating the record beside it. The fields are JSON values, whose
    ``repr`` is deterministic and several times cheaper to build than JSON.
    """
    derived = [
        record.get(key)
        for key in ("entity_a", "entity_b", "co_occurrence_count", "rooms", "label", "created_by")
    ]
    return hashlib.blake2b(repr(derived).encode("utf-8"), digest_size=16).hexdigest()


def _row(record: dict) -> tuple:
    """The table row of one hallway record, which it stores verbatim.

    A hand-made record without an id gets the id a mine would give its pair,
    so it still has a key; the stored record keeps its own fields as written.
    """
    hallway_id = record.get("id")
    if not isinstance(hallway_id, str) or not hallway_id:
        hallway_id = _hallway_id(
            str(record.get("wing")), str(record.get("entity_a")), str(record.get("entity_b"))
        )
    return (
        hallway_id,
        _scalar(record.get("wing")),
        _scalar(record.get("entity_a")),
        _scalar(record.get("entity_b")),
        _scalar(record.get("co_occurrence_count")),
        _signature(record),
        json.dumps(record, ensure_ascii=False, separators=(",", ":"), default=str),
    )


def _connect(path: str, *, readonly: bool) -> sqlite3.Connection:
    """A connection in autocommit mode; writers open explicit transactions.

    A read-only connection never creates the database. A writable one creates
    it with mode 0600 on POSIX (hallways reveal within-wing entity
    connections), and SQLite gives its -wal and -shm files the same mode.
    """
    if readonly:
        uri = Path(os.path.abspath(path)).as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=30, isolation_level=None)
    else:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        if not os.path.exists(path):
            os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))
        conn = sqlite3.connect(path, timeout=30, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_DDL)
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _reader(config=None) -> Optional[sqlite3.Connection]:
    """A read-only connection to the store once it holds the records, else ``None``.

    ``None`` means no write has imported ``hallways.json`` yet, and the caller
    reads the JSON as before.
    """
    path = _get_hallway_db(config)
    if not os.path.exists(path):
        return None
    conn = None
    try:
        conn = _connect(path, readonly=True)
        # A writer creates the file before its tables: no meta table yet is
        # the same as nothing imported.
        has_meta = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'meta'"
        ).fetchone()
        imported = (
            has_meta
            and conn.execute("SELECT 1 FROM meta WHERE key = ?", (_IMPORTED_KEY,)).fetchone()
        )
    except sqlite3.Error:
        if conn is not None:
            conn.close()
        logger.warning(
            "hallways: %s could not be read; reading %s", path, _get_hallway_file(config)
        )
        logger.debug("hallways: store open failed", exc_info=True)
        return None
    if not imported:
        conn.close()
        return None
    return conn


def _import_json_once(conn: sqlite3.Connection, config=None) -> Optional[str]:
    """Inside a write transaction: import ``hallways.json`` if nothing has yet.

    Returns the JSON path to move aside once the transaction commits, or
    ``None``. A file that does not parse, or is not the hallway document, is
    moved aside and the write is refused, as a save over it was before. A
    record whose id repeats an earlier one replaces it; the moved-aside file
    keeps both. A later ``hallways.json`` (an older MemPalace still writing
    one) is not imported again.
    """
    if conn.execute("SELECT 1 FROM meta WHERE key = ?", (_IMPORTED_KEY,)).fetchone():
        return None
    path = _get_hallway_file(config)
    loaded = _read_json_for_update(path)
    records: list = []
    if loaded is not None:
        if isinstance(loaded, dict) and isinstance(loaded.get("hallways"), list):
            records = loaded["hallways"]
        elif isinstance(loaded, list):
            records = loaded
        else:
            _refuse_unparsed(path, "is not a hallway document")
    rows = [_row(h) for h in records if isinstance(h, dict)]
    conn.executemany(_UPSERT, rows)
    marker = {"source": path if loaded is not None else None, "records": len(rows)}
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?)",
        (_IMPORTED_KEY, json.dumps({**marker, "at": utc_now_iso()})),
    )
    if loaded is None:
        return None
    skipped = len(records) - len(rows)
    logger.info(
        "hallways: imported %d record(s) from %s into %s%s",
        len(rows),
        path,
        _get_hallway_db(config),
        f" ({skipped} non-record entries left in the kept file)" if skipped else "",
    )
    return path


@contextlib.contextmanager
def _writing(config=None) -> Iterator[sqlite3.Connection]:
    """One write transaction on the store, after the one-time JSON import.

    ``BEGIN IMMEDIATE`` takes SQLite's write lock up front, so two processes
    cannot both import or both replace a wing from the same snapshot. Readers
    keep reading the last committed state meanwhile (WAL).
    """
    conn = _connect(_get_hallway_db(config), readonly=False)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            imported = _import_json_once(conn, config)
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
    finally:
        conn.close()
    if imported:
        try:
            kept = _move_aside(imported, label="imported")
            if kept:
                logger.info("hallways: kept the imported %s as %s", imported, kept)
        except OSError:
            logger.warning(
                "hallways: %s is imported but could not be renamed; it is no longer read",
                imported,
                exc_info=True,
            )


def _records(rows) -> list[dict]:
    return [json.loads(row[0]) for row in rows]


def _hallway_file_lock(config=None):
    """The per-store lock every hallway writer holds from load to save.

    Same mechanism as ``palace_graph.create_tunnel``. Each write is also one
    SQLite transaction; the lock additionally keeps a load and the save that
    follows it (``hallways --prune-spellings``, ``wings split``) from
    interleaving with a mine's recompute.
    """
    from .palace import mine_lock

    return mine_lock(_get_hallway_file(config))


def _legacy_hallway_file() -> str:
    """The pre-palace-scoped hardcoded path. Kept only for one-time orphan detection."""
    return str(paths.state_path("hallways.json"))


def _load_hallways_for_write(config=None) -> list[dict]:
    """Hallway records a save is allowed to replace, in list order.

    Imports ``hallways.json`` first if no write has yet. A JSON that does not
    parse, or is not the hallway document, is moved aside and the save is
    refused. A read that only lists hallways stays on :func:`_load_hallways`
    and writes nothing.
    """
    with _writing(config) as conn:
        return _records(conn.execute("SELECT record FROM hallways ORDER BY rowid"))


def _load_hallways(config=None) -> list[dict]:
    """Read all hallway records in list order. ``[]`` when there are none.

    Reads the store once a write has imported ``hallways.json`` into it, and
    the JSON until then (``[]`` if that is missing or corrupt).
    """
    conn = _reader(config)
    if conn is None:
        return _load_hallways_json(config)
    with contextlib.closing(conn):
        return _records(conn.execute("SELECT record FROM hallways ORDER BY rowid"))


def _load_hallways_json(config=None) -> list[dict]:
    """Read ``hallways.json``. Returns ``[]`` if the file is missing or corrupt.

    Backwards-compatibility: prior to this migration the hallway file was
    hardcoded at ``~/.mempalace/hallways.json`` regardless of the configured
    palace_path. If the configured hallway file is missing but a legacy file
    exists at a different path, log a one-line warning naming both paths so
    users can move the file manually. We do NOT auto-migrate — auto-merging
    hallway state across two locations is too magical for a bugfix and risks
    clobbering newer data. Same posture as ``palace_graph._load_tunnels``.
    """
    current_hallway_file = _get_hallway_file(config)
    if os.path.exists(current_hallway_file):
        try:
            with open(current_hallway_file, encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, json.JSONDecodeError):
            logger.debug("hallways: load failed, treating as empty", exc_info=True)
            return []
        if isinstance(raw, dict) and "hallways" in raw:
            return raw.get("hallways") or []
        if isinstance(raw, list):
            return raw
        return []

    legacy = _legacy_hallway_file()
    if legacy != current_hallway_file and os.path.exists(legacy):
        logger.warning(
            "Legacy hallways file at '%s' is being ignored; configured location is '%s'. "
            "Move or copy the legacy file to the configured path to recover its hallways.",
            legacy,
            current_hallway_file,
        )

    pre_keyed = os.path.join(os.path.dirname(current_hallway_file), "hallways.json")
    if pre_keyed != current_hallway_file and os.path.exists(pre_keyed):
        pre_keyed_db = os.path.splitext(pre_keyed)[0] + ".sqlite3"
        db_hint = (
            f" Its SQLite store at '{pre_keyed_db}' also needs to be renamed."
            if os.path.exists(pre_keyed_db)
            else ""
        )
        logger.warning(
            "Hallways file at '%s' is being ignored; the per-palace name is now '%s'. "
            "Move or copy the file (preserving its contents) to the new path to recover its hallways.%s",
            pre_keyed,
            current_hallway_file,
            db_hint,
        )
    return []


def _save_hallways(hallways: list[dict], config=None) -> None:
    """Replace every hallway record with ``hallways``, in one transaction.

    For the whole-store rewrites (``--prune-spellings``). A mine replaces
    only its own wing (:func:`compute_hallways_for_wing`); a crash at any
    point leaves the previous records in place.
    """
    with _writing(config) as conn:
        conn.execute("DELETE FROM hallways")
        conn.executemany(_UPSERT, [_row(h) for h in hallways if isinstance(h, dict)])


# ─────────────────────────────────────────────────────────────────────────────
# Core algorithm — compute entity-pair hallways for one wing
# ─────────────────────────────────────────────────────────────────────────────


# File extensions stripped when deciding whether two entity spellings name
# the same file. A fixed set on purpose: ``ChatStore`` and ``ChatStore.send``
# are different entities and must not collapse.
_CODE_EXTENSIONS = frozenset(
    "py js ts tsx jsx mjs cjs zig swift rs go md json yaml yml toml sh c h cpp hpp "
    "java kt rb php html css sql txt cs vue svelte".split()
)


def _code_extension(entity: str) -> Optional[str]:
    """Lower-case code extension of ``entity``, or ``None`` if it has none.

    ``src/parser.c`` → ``"c"``; ``mcp_server`` and ``ChatStore.send`` →
    ``None`` (no known code extension on the basename). Unequal explicit
    extensions name different files even when their stems match.
    """
    base = str(entity).replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    stem, dot, ext = base.rpartition(".")
    if dot and stem and ext.lower() in _CODE_EXTENSIONS:
        return ext.lower()
    return None


def entity_spelling_key(entity: str) -> str:
    """Basename without a known code extension, lower-cased.

    ``src/main.zig``, ``main.zig`` and ``/Users/x/proj/src/main.zig`` all key
    to ``main``; ``mcp_server`` and ``mcp_server.py`` both key to
    ``mcp_server``. The soft key groups candidate aliases; unequal explicit
    extensions of the same stem (``parser.c`` / ``parser.h``) still key
    together here and are separated by :func:`same_file_spelling` and
    :func:`_spelling_clusters`. Used by the miner to skip self-pairs and by
    ``mempalace audit`` / ``mempalace hallways --prune-self-links`` to find
    the ones older mines already wrote.
    """
    base = str(entity).replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    stem, dot, ext = base.rpartition(".")
    if dot and stem and ext.lower() in _CODE_EXTENSIONS:
        base = stem
    return base.lower()


_GENERIC_ENTITY_RE = re.compile(r"[a-z]{2,8}")

# Names that appear in every coding transcript and identify no project: the
# harness's tool names, generic nouns, and files every repo has. Matched
# case-insensitively after stripping a trailing slash.
GENERIC_ENTITY_STOPLIST = frozenset(
    """
    bash read write edit grep glob task agent websearch webfetch toolsearch
    structuredoutput askuserquestion skill monitor notebookedit todowrite
    app server service client gateway api handler controller model view
    config settings utils util helpers helper index main core common base
    test tests spec fixture mock github github.com gitlab git npm pip uv
    docker dockerfile compose.yml docker-compose.yml package.json package-lock.json
    tsconfig.json pyproject.toml requirements.txt readme readme.md changelog.md
    license .env .gitignore makefile lib src dist build node_modules
    created_at updated_at id name type value data result results error errors
    mod types routes router store page layout schema schemas models constants
    logger middleware services component components hooks context styles style
    theme globals setup init __init__ repository entity entities interfaces
    interface enums enum validators validator
    cargo.toml cargo.lock go.mod go.sum gemfile gemfile.lock setup.py setup.cfg
    uv.lock poetry.lock yarn.lock pnpm-lock.yaml package-lock.json roadmap.md
    contributing.md security.md license.md code_of_conduct.md todo.md notes.md
    .eslintrc .prettierrc vite.config.ts next.config.js tailwind.config.js
    webpack.config.js jest.config.js vitest.config.ts vercel.json fly.toml procfile
    created_by updated_by deleted_at deleted_by user_id primary_key foreign_key
    is_active is_deleted read_text write_text
    changedetectionstrategy changedetectionstrategy.onpush changedetectionstrategy.eager
    changedetectorref oninit ondestroy onchanges injectable ngmodule httpclient
    formsmodule commonmodule usestate useeffect usememo usecallback useref
    usecontext usereducer afterviewinit routerlink
    docker-compose python.exe xcode.app assert_eq to_string serde_json
    transformers.js tool_call tool_calls authored_at memory.used total_bytes
    tsconfig.app tsconfig.spec failure_scenario
    db ctx.allocator powershell.exe cmd.exe valueerror typeerror keyerror
    docker-compose.yaml docker-compose.yml source_file memory.total buffer.from
    hashmap hashset vec box arc rc
    """.split()
)

# Source files whose stem is a generic word: ``app.js``, ``model.ts``,
# ``mod.rs``, ``main.py`` exist in most repos of that language and link
# nothing when two wings share one.
_SOURCE_EXTENSIONS = frozenset(
    """
    js mjs cjs ts tsx jsx py rs go rb php java kt swift cs c cc cpp h hpp zig
    css scss html vue svelte sql sh
    """.split()
)

# ``pathlib.Path``, ``page.evaluate``, ``console.log``: the first segment is a
# runtime, standard library or test-framework namespace, so the qualified
# name is vocabulary every project in that language shares.
_LIBRARY_NAMESPACES = frozenset(
    """
    os sys json re pathlib subprocess asyncio typing datetime time logging math
    random collections itertools functools shutil io tempfile unittest pytest
    np numpy pd pandas plt torch tf document window console process page
    browser navigator locator expect react vue angular fs path http https crypto
    util express next localstorage sessionstorage self this cls super
    """.split()
)


def is_generic_entity(name: str) -> bool:
    """A name that identifies no project: a short lower-case word (``content``,
    ``thinking``), a harness tool (``WebFetch``), a generic noun (``Server``),
    a file every repo has (``compose.yml``, ``app.js``) or a library
    reference (``pathlib.Path``).

    Symbols (``ChatStore``), qualified project names (``store.baseURL``) and
    project names pass; the boundary with a short lower-case project name is
    fuzzy by construction. Cross-wing ubiquity is judged separately, where
    the wing counts are known.
    """
    text = str(name).strip()
    if _GENERIC_ENTITY_RE.fullmatch(text):
        return True
    if "/" not in text and "." in text:
        head, _, tail = text.rpartition(".")
        if tail.lower() in _SOURCE_EXTENSIONS and head.lower() in GENERIC_ENTITY_STOPLIST:
            return True
        first = text.split(".", 1)[0].lower()
        if first in _LIBRARY_NAMESPACES and tail.lower() not in _SOURCE_EXTENSIONS:
            return True
    # A bare single-segment path (``/app``, ``/model``) or a shouting constant
    # (``MESSAGES``, ``TEMPLATES``) is structure every project has.
    if re.fullmatch(r"/[A-Za-z0-9_-]+/?", text) or re.fullmatch(r"[A-Z][A-Z0-9_]{2,15}", text):
        return True
    # A lone lower-case English word of any length (``cancelled``,
    # ``operations``) is vocabulary; project names are the exception and are
    # usually short, which the first rule already accepts as generic too.
    if re.fullmatch(r"[a-z]{9,12}", text) and not any(c in text for c in "._-/"):
        return True
    return text.rstrip("/").lower() in GENERIC_ENTITY_STOPLIST


# ``git diff`` prints both sides of a change as ``a/<path>`` and ``b/<path>``,
# so a transcript that shows a diff names every touched file twice under two
# fake top-level directories. The evidence is the pair: ``a/x`` and ``b/x``
# with the same ``x`` are one file, at any depth, while a lone ``a/x`` may be
# a real directory named ``a`` and is left alone.
_GIT_DIFF_PREFIXES = frozenset({"a", "b"})


def _path_parts(entity: str) -> list[str]:
    return [p for p in str(entity).replace("\\", "/").split("/") if p]


def _diff_alias(entity: str) -> Optional[tuple[str, str]]:
    """``("a", "src/x.py")`` for ``a/src/x.py``; ``None`` without a diff prefix."""
    parts = _path_parts(entity)
    if len(parts) >= 2 and parts[0] in _GIT_DIFF_PREFIXES:
        return parts[0], "/".join(parts[1:])
    return None


def _dir_segments(entity: str) -> list[str]:
    """Directory part of a spelling, innermost last: ``src/models/user.py`` → ``["src", "models"]``."""
    return _path_parts(entity)[:-1]


def _is_diff_pair(a: str, b: str) -> bool:
    """``a/<path>`` and ``b/<path>`` naming the same ``<path>``."""
    x, y = _diff_alias(a), _diff_alias(b)
    return bool(x and y and x[0] != y[0] and x[1] == y[1])


def _diff_resolved(entities: list[str]) -> dict[str, str]:
    """Map each ``a/`` or ``b/`` spelling whose counterpart is present to its path."""
    aliases = {e: _diff_alias(e) for e in entities}
    present = {(al[0], al[1]) for al in aliases.values() if al}
    out: dict[str, str] = {}
    for e, al in aliases.items():
        if al and ("b" if al[0] == "a" else "a", al[1]) in present:
            out[e] = al[1]
    return out


def same_file_spelling(a: str, b: str) -> bool:
    """True when two spellings can only be the same file.

    They must share a basename key *and* one path must be a suffix of the
    other: ``main.zig`` and ``src/main.zig`` are one file, while
    ``src/models/user.py`` and ``tests/models/user.py`` are two files that
    happen to share a name. Keying on the basename alone merged their
    associations and let ``--prune-spellings`` delete one of them.

    Unequal explicit code extensions stay distinct: ``src/parser.c`` and
    ``src/parser.h`` share a stem and a directory, but they are two files.
    An extensionless spelling may still alias a single extension
    (``mcp_server`` / ``mcp_server.py``).
    """
    if entity_spelling_key(a) != entity_spelling_key(b):
        return False
    ext_a, ext_b = _code_extension(a), _code_extension(b)
    if ext_a is not None and ext_b is not None and ext_a != ext_b:
        return False
    if _is_diff_pair(a, b):
        return True
    da, db = _dir_segments(a), _dir_segments(b)
    short, long_ = (da, db) if len(da) <= len(db) else (db, da)
    return not short or long_[len(long_) - len(short) :] == short


def canonical_spelling(cluster: list[str]) -> str:
    """The spelling records carry for one entity: its most qualified path first.

    A path is what tells ``src/models/user.py`` from ``tests/fixtures/user.py``
    once a record leaves its wing, so the spelling with the most directory
    segments wins; keeping the shortest threw that away and let two unrelated
    files meet as bare ``user.py`` in the cross-wing tunnel builder. Among
    spellings with no directory the shortest reads best (``ChatStore`` over
    ``ChatStore.swift``). Ties break on the text, so the choice is stable.
    """
    resolved = _diff_resolved(cluster)
    return min(
        cluster,
        key=lambda e: (
            e in resolved,  # a diff alias only when a real spelling exists
            -len(_dir_segments(resolved.get(e, e))),
            len(e),
            e,
        ),
    )


def _spelling_clusters(entities: list[str]) -> list[list[str]]:
    """Group spellings of one basename into the distinct files they name.

    Unequal explicit code extensions are different files even in the same
    directory (``src/parser.c`` / ``src/parser.h``). Within one extension,
    spellings are bucketed by directory path: a path that is a suffix of
    exactly one longer path joins it (``main.zig`` under ``src/main.zig``);
    one that could belong to two or more is left out (``user.py`` beside
    ``src/models/user.py`` and ``tests/models/user.py``). An extensionless
    spelling joins the unique extension cluster it can only name
    (``mcp_server`` under ``mcp_server.py``); if it could be more than one
    file it is left out, the same rule as an ambiguous bare path.
    Callers skip spellings that appear in no cluster.
    """
    # Resolve diff prefixes against the complete set, as their paired
    # spellings may otherwise look like incompatible directories.
    names = list(dict.fromkeys(entities))
    resolved = _diff_resolved(names)
    matches = {
        name: {
            other
            for other in names
            if same_file_spelling(resolved.get(name, name), resolved.get(other, other))
        }
        for name in names
    }
    # A short path or missing extension must never bridge incompatible
    # files. Decide ambiguity before building any cluster: adding Router
    # to src/Router first would let tests/Router join through that alias.
    # Every pair of possible hosts must agree, including intermediate
    # suffixes and extensionless paths, regardless of input order.
    unambiguous = {
        name for name, hosts in matches.items() if all(hosts <= matches[host] for host in hosts)
    }
    clusters: list[list[str]] = []
    for name in names:
        if name not in unambiguous:
            continue
        cluster = [other for other in names if other in unambiguous and other in matches[name]]
        clusters.append(cluster)
        unambiguous.difference_update(cluster)
    return clusters


def is_self_link(record) -> bool:
    """True when a hallway record joins two spellings of one entity."""
    if not isinstance(record, dict):
        return False
    a, b = record.get("entity_a"), record.get("entity_b")
    if a is None or b is None:
        return False
    return same_file_spelling(str(a), str(b))


def canonical_entities(entities: list[str]) -> list[str]:
    """One spelling per entity, in first-seen order.

    The structural extractor records a file as both its path and its
    basename, so a drawer's entity list holds ``src/main.zig`` and
    ``main.zig`` side by side. Pairing those raw spellings wrote a hallway
    from the entity to itself and four copies of every real association
    (``ChatStore`` × ``RootView`` under each spelling combination). Each
    entity keeps its :func:`canonical_spelling`, so hallways read
    ``ChatStore ↔ RootView``.
    """
    by_key: dict[str, list[str]] = {}
    order: list[str] = []
    for entity in entities:
        key = entity_spelling_key(entity)
        if key not in by_key:
            by_key[key] = []
            order.append(key)
        by_key[key].append(entity)
    out: list[str] = []
    for key in order:
        # Spellings of one basename may name several files (``src/user.py``
        # and ``tests/user.py``); each cluster keeps its own canonical
        # spelling instead of collapsing into one entity.
        for cluster in _spelling_clusters(by_key[key]):
            out.append(canonical_spelling(cluster))
    return out


def _parse_entities(value) -> list[str]:
    """Drawer ``entities`` metadata is a semicolon-separated string. Parse it.

    Returns a deterministic *list* (not a set) because order matters for
    the deduplication semantics below: a drawer that mentions ``Aya;Aya``
    should only contribute one Aya to the entity set for that drawer.
    """
    if not value:
        return []
    if isinstance(value, (list, tuple, set)):
        items = [str(v).strip() for v in value if str(v).strip()]
    elif isinstance(value, str):
        items = [v.strip() for v in value.split(";") if v.strip()]
    else:
        return []
    # Dedupe while preserving first-seen order so id derivation is stable.
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _hallway_id(wing: str, entity_a: str, entity_b: str) -> str:
    """Deterministic id derived from wing + sorted entity pair.

    Sorting before hashing makes the id symmetric — (Aya, Lumi) and
    (Lumi, Aya) produce the same record. So an idempotent re-mine
    upserts the same hallway instead of creating two parallel records.
    """
    a, b = sorted([entity_a, entity_b])
    key = f"{wing}::{a}::{b}".encode("utf-8")
    suffix = hashlib.sha256(key).hexdigest()[:8]
    return f"hallway_{wing}_{a}_{b}_{suffix}"


def _wing_file_keys(metadatas) -> dict[str, str]:
    """Map every spelling seen in the wing to the file it names.

    The value is the file's :func:`canonical_spelling`, which is both the key
    pairs are counted under and the spelling the record is materialized
    with. Clustering is wing-wide, not per drawer: one drawer saying
    ``ChatStore.swift`` and the next saying ``ChatStore`` are one file, while
    ``src/models/user.py`` and ``tests/models/user.py`` stay two files even
    though they share a basename (:func:`_spelling_clusters`).
    """
    by_base: dict[str, set[str]] = defaultdict(set)
    for meta in metadatas:
        if not isinstance(meta, dict) or is_bookkeeping(meta):
            continue
        for spelling in _parse_entities(meta.get("entities")):
            by_base[entity_spelling_key(spelling)].add(spelling)
    mapping: dict[str, str] = {}
    for spellings in by_base.values():
        for cluster in _spelling_clusters(sorted(spellings)):
            canonical = canonical_spelling(cluster)
            for spelling in cluster:
                mapping[spelling] = canonical
    return mapping


# Every drawer metadata key compute_hallways_for_wing reads.
_HALLWAY_KEYS = ("wing", "room", "entities", "is_sentinel")


def _wing_metadata_from_sqlite(col, wing: str) -> Optional[list]:
    """The wing's drawer metadata from Chroma's one-pass sqlite stream.

    Returns ``None`` when ``col`` has no such stream or reading it failed, and
    the caller pages instead. Paging is ``get(where={"wing": ...}, offset=N)``,
    which Chroma runs as SQL ``OFFSET``: every page re-walks the rows before
    it, so a large wing costs the square of its size.

    The read is scoped to the wing in sqlite, and only drawers holding
    ``entities`` are read, both through the ``(key, string_value)`` index, so
    a small wing in a large palace reads its own rows rather than the
    palace's. A drawer without entities neither forms a pair nor names a
    file, so the hallways come out the same. A wing none of whose drawers
    hold any still has to replace its old hallways with none, so a second
    scoped read, stopped at its first row, tells it apart from a wing with no
    drawers at all.
    """
    from .backends.chroma_sqlite import MetadataRecoveryPending
    from .palace import _fast_collection_metadata

    scope = {"wing": wing}
    try:
        rows = _fast_collection_metadata(col, _HALLWAY_KEYS, require_key="entities", equals=scope)
        if rows is None:
            return None
        metadatas = [m for m in rows if isinstance(m, dict) and m.get("wing") == wing]
        if not metadatas:
            probe = _fast_collection_metadata(col, _HALLWAY_KEYS, equals=scope)
            try:
                first = next(probe, None)
            finally:
                probe.close()
            if isinstance(first, dict) and first.get("wing") == wing:
                metadatas = [first]
    except MetadataRecoveryPending:
        logger.debug("compute_hallways_for_wing: metadata recovery pending for %s", wing)
        return None
    except Exception:
        logger.warning(
            "compute_hallways_for_wing: sqlite metadata scan failed for %s; paging instead",
            wing,
            exc_info=True,
        )
        return None
    return metadatas


def _wing_metadata_paged(col, wing: str) -> list:
    """The wing's drawer metadata: scoped to the wing server-side AND paginated.

    An unbounded get(where={"wing": wing}) binds one SQL variable per matched
    id and overflows SQLite's SQLITE_MAX_VARIABLE_NUMBER (32766) on wings >
    ~32k drawers; a bounded page binds at most batch_size ids. Walking
    the WHOLE collection instead and filtering client-side cost O(total palace
    drawers) on every mine, so filing one small session into an 800k-drawer
    palace pegged the CPU for minutes. The client-side wing check
    stays as a guard for stores that ignore ``where``. The loop ends on a
    short page: count() counts every wing, so it cannot bound a scoped walk.
    """
    metadatas: list = []
    batch_size = 5000
    offset = 0
    while True:
        batch = col.get(
            where={"wing": wing},
            limit=batch_size,
            offset=offset,
            include=["metadatas"],
        )
        batch_metas = (batch or {}).get("metadatas") or []
        if not batch_metas:
            break
        metadatas.extend(m for m in batch_metas if isinstance(m, dict) and m.get("wing") == wing)
        offset += len(batch_metas)
        if len(batch_metas) < batch_size:
            break
    return metadatas


def compute_hallways_for_wing(
    wing: str,
    col=None,
    min_count: int | None = None,
    config=None,
) -> list[dict]:
    """Compute entity-pair hallways for one wing.

    Algorithm:
      1. Query drawers for ``wing`` from ``col``.
      2. For each drawer with entities, every pair of distinct entities in
         that drawer is one co-occurrence. Increment a counter for each
         pair; also record the room the drawer lives in.
      3. For each (entity_a, entity_b) pair whose co-occurrence count is
         ``>= min_count``, materialize a hallway record. The record
         carries the pair, the count, and the set of rooms where they
         co-occurred (useful context for navigation).
      4. Replace this wing's records in the store: a record whose derived
         fields changed is rewritten, one the drawers no longer produce is
         dropped, and an unchanged one is left as stored. Other wings'
         records are not read or rewritten. Return the wing's records.

    Args:
        wing: wing name to scan.
        col: ChromaDB collection. A Chroma collection is read in one pass
            over ``chroma.sqlite3`` (see :func:`_wing_metadata_from_sqlite`).
            Any other must support paginated
            ``.get(where={"wing": ...}, limit=..., offset=..., include=...)``.
            The fetch is scoped to ``wing`` server-side AND paginated: an
            unbounded ``.get(where=...)`` binds one SQL variable per matched
            id and overflows SQLite's ``SQLITE_MAX_VARIABLE_NUMBER`` on wings
            above ~32k drawers, while an unscoped page walk costs
            O(total palace drawers) on every mine, pegging the CPU for
            minutes on large palaces. A bounded page never binds more
            than ``batch_size`` ids. Fake collections and alternate backends
            must implement this shape. If ``None``, returns ``[]`` (caller
            didn't supply a backing store, so nothing to compute against).
            Tests pass a controlled MagicMock.
        min_count: minimum co-occurrence count required to materialize a
            hallway between two entities. Pass an explicit ``None`` (the
            default) — or omit the arg — to resolve it from config:
            ``MEMPALACE_KG_HALLWAY_MIN_COUNT`` env first, then the
            ``hallway_min_count`` config-file value, then the historical
            default of ``2`` (single co-occurrences are noise — entities named
            together once in one drawer; two or more is a real signal). An
            explicitly supplied integer is honoured verbatim, clamped to
            ``>=1``.
        config: Optional ``MempalaceConfig`` selecting the palace-scoped
            hallway sidecar. Callers using an explicit palace path must pass
            the matching config so derived graph state cannot leak into the
            default palace.

    Returns:
        List of hallway dicts created for this wing. Records for other
        wings already on disk are preserved.
    """
    if col is None:
        logger.debug("compute_hallways_for_wing: no collection provided for %s", wing)
        return []

    # Resolve the min_count threshold: honour an explicit value, otherwise
    # fall back to config (env > file > historical default 2). This is the
    # single source of truth for the within-wing hallway threshold, so callers
    # that omit the arg and production callers that rely on the default resolve
    # it identically via MempalaceConfig.hallway_min_count — mirroring the
    # topic_tunnel_min_count convention while keeping backward compat (an
    # unconfigured corpus still gets min_count=2, so existing hallways are
    # byte-identical).
    threshold: int
    if min_count is None:
        from .config import MempalaceConfig

        threshold = (config or MempalaceConfig()).hallway_min_count
    else:
        threshold = min_count
    min_count = max(1, int(threshold))

    # 1. Query drawers for this wing. On Chroma, one linear pass over the
    #    drawers holding entities; elsewhere, wing-scoped pages.
    try:
        metadatas = _wing_metadata_from_sqlite(col, wing)
        if metadatas is None:
            metadatas = _wing_metadata_paged(col, wing)
    except Exception:
        logger.warning(
            "compute_hallways_for_wing: collection fetch failed for %s", wing, exc_info=True
        )
        return []

    if not metadatas:
        return []

    # 2. Walk drawers, counting entity-pair co-occurrence + tracking rooms.
    # pair_counts: {(entity_a, entity_b): count} — keys always sorted to
    # canonicalize the (a, b) vs (b, a) symmetry.
    # Pairs are keyed by the file an entity names, resolved wing-wide by
    # ``_wing_file_keys``: the structural extractor records a file as both
    # path and basename and one drawer may say ``ChatStore.swift`` where the
    # next says ``ChatStore``, so the key is the canonical spelling of that
    # file and the record reads ``ChatStore ↔ RootView``.
    pair_counts: dict[tuple[str, str], int] = defaultdict(int)
    pair_rooms: dict[tuple[str, str], set[str]] = defaultdict(set)
    file_keys = _wing_file_keys(metadatas)

    for meta in metadatas:
        if not isinstance(meta, dict):
            continue
        # Sentinel drawers carry no real content — skip them.
        if is_bookkeeping(meta):
            continue
        entities = []
        for spelling in _parse_entities(meta.get("entities")):
            # The file this spelling names, not its basename: two files
            # sharing a name must not merge into one entity here either,
            # or one drawer naming both counts the same pair twice. Resolve
            # the raw names against the whole wing first: a drawer-local
            # canonical name such as Parser could hide its explicit Parser.c
            # when Parser is ambiguous with Parser.h elsewhere in the wing.
            canonical = file_keys.get(spelling)
            if canonical is None:
                continue  # an ambiguous name: it identifies no single file
            if canonical not in entities:
                entities.append(canonical)
        if len(entities) < 2:
            # Need at least 2 entities for a pair to exist.
            continue
        room = meta.get("room")
        room_str = room if isinstance(room, str) and room.strip() else None

        # Each unordered pair of distinct entities in this drawer is one
        # co-occurrence. itertools.combinations already gives unordered
        # pairs without repetition.
        for a, b in combinations(entities, 2):
            # Canonicalize order so (Aya, Lumi) and (Lumi, Aya) are the
            # same key. Skip self-pairs, including the same entity under two
            # spellings (``main.zig`` / ``src/main.zig``): the structural
            # extractor records both the path and the basename, and a
            # hallway between them is an entity paired with itself.
            if a == b:
                continue
            key = tuple(sorted([a, b]))
            pair_counts[key] += 1
            if room_str:
                pair_rooms[key].add(room_str)

    # No early return on an empty ``pair_counts``: the drawers were read, so
    # "no pairs" is this wing's real answer and must replace its old records.
    # Returning here left every stale hallway of the wing in place after a
    # rebuild that correctly produced none.

    # 3. Materialize hallway records for pairs above the threshold.
    #    Read this wing's stored rows first so we can PRESERVE L7 dynamics
    #    fields (strength, stability, last_activated, access_count) across
    #    recomputes. Without this preservation, every mine wipes the
    #    connection weights accumulated through use — defeating the
    #    living-connection layer entirely. Only this wing's rows are read:
    #    other wings' records are neither loaded nor rewritten.
    # Read → materialize → write in one transaction under the hallway lock,
    # so a concurrent writer cannot replace the wing from the same snapshot.
    with _hallway_file_lock(config), _writing(config) as conn:
        stored = conn.execute(
            "SELECT id, entity_a, entity_b, signature, record FROM hallways"
            " WHERE wing = ? ORDER BY rowid",
            (wing,),
        ).fetchall()
        stored_by_id = {row[0]: row for row in stored}
        existing_by_key: dict = {}
        for row in stored:
            # Canonicalize the lookup key by sorting the entity pair — must
            # match the symmetric ID generation in _hallway_id (which also
            # sorts). Without this, a persisted record with reversed entity
            # order would silently miss the lookup and lose its accumulated
            # dynamics on every recompute. The last record of a key wins.
            key = tuple(
                sorted(
                    [
                        file_keys.get(str(row[1]), str(row[1])),
                        file_keys.get(str(row[2]), str(row[2])),
                    ]
                )
            )
            existing_by_key[key] = row[4]

        def preserved_dynamics(key) -> dict:
            # Only copy the fields the dynamics layer cares about; everything
            # else is recomputed deterministically from the drawer set.
            raw = existing_by_key.get(key)
            if raw is None:
                return {}
            h = json.loads(raw)
            return {
                k: h[k]
                for k in ("strength", "stability", "last_activated", "access_count")
                if k in h
            }

        created: list[dict] = []
        changed: list[dict] = []
        created_at = utc_now_iso()
        for key in sorted(pair_counts.keys()):
            count = pair_counts[key]
            if count < min_count:
                continue
            entity_a, entity_b = key
            rooms = sorted(pair_rooms.get(key, set()))
            room_summary = ", ".join(rooms[:3]) if rooms else "(no room tags)"
            if len(rooms) > 3:
                room_summary += f", +{len(rooms) - 3} more"
            record = {
                "id": _hallway_id(wing, entity_a, entity_b),
                "wing": wing,
                "entity_a": entity_a,
                "entity_b": entity_b,
                "co_occurrence_count": count,
                "rooms": rooms,
                "label": f"{entity_a} ↔ {entity_b} (co-occur in {count} drawers across {len(rooms) or 'no'} room{'s' if len(rooms) != 1 else ''}: {room_summary})",
                "created_at": created_at,
                "created_by": "auto",
            }
            # A stored record this recompute would derive unchanged stays as
            # it is, created_at and dynamics included, and is not rewritten.
            same = stored_by_id.get(record["id"])
            if same is not None and same[3] == _signature(record):
                created.append(json.loads(same[4]))
                continue
            # Apply preserved dynamics if this entity pair existed in the
            # prior wing snapshot. Then initialize any still-missing fields
            # (the new-pair case + the legacy-record case both land cleanly).
            record.update(preserved_dynamics(key))
            initialize_dynamics_fields(record)
            created.append(record)
            changed.append(record)

        # 4. Persist — rewrite this wing's changed records and drop the ones
        #    the drawers no longer produce. Other wings' rows are untouched.
        kept_ids = {h["id"] for h in created}
        conn.executemany(
            "DELETE FROM hallways WHERE id = ?",
            [(row[0],) for row in stored if row[0] not in kept_ids],
        )
        conn.executemany(_UPSERT, [_row(h) for h in changed])

    return created


# ─────────────────────────────────────────────────────────────────────────────
# Query API — list_hallways, delete_hallway
# ─────────────────────────────────────────────────────────────────────────────


def list_hallways(wing: Optional[str] = None, config=None) -> list[dict]:
    """List hallway records. Filter by ``wing`` if specified."""
    conn = _reader(config)
    if conn is None:
        all_hallways = _load_hallways_json(config)
        if wing is None:
            return list(all_hallways)
        return [h for h in all_hallways if h.get("wing") == wing]
    with contextlib.closing(conn):
        if wing is None:
            return _records(conn.execute("SELECT record FROM hallways ORDER BY rowid"))
        return _records(
            conn.execute("SELECT record FROM hallways WHERE wing = ? ORDER BY rowid", (wing,))
        )


def list_hallways_page(
    wing: Optional[str] = None, limit: int = 100, offset: int = 0, config=None
) -> tuple[list[dict], int]:
    """One page of hallway records, strongest first, and how many there are.

    The order is :func:`list_hallways` stably sorted by descending
    ``co_occurrence_count`` (a missing count sorts as 0). Only the page is
    read, not every record of the palace.
    """
    conn = _reader(config)
    if conn is None:
        rows = sorted(
            list_hallways(wing, config=config),
            key=lambda h: -int(h.get("co_occurrence_count") or 0),
        )
        return rows[offset : offset + limit], len(rows)
    where, params = ("WHERE wing = ?", (wing,)) if wing is not None else ("", ())
    with contextlib.closing(conn):
        total = conn.execute(f"SELECT COUNT(*) FROM hallways {where}", params).fetchone()[0]
        page = _records(
            conn.execute(
                f"SELECT record FROM hallways {where}"
                " ORDER BY CAST(COALESCE(co_occurrence_count, 0) AS INTEGER) DESC, rowid"
                " LIMIT ? OFFSET ?",
                (*params, max(0, limit), max(0, offset)),
            )
        )
    return page, total


def entity_strength_records(config=None) -> list[dict]:
    """The hallway records entity tunnels are computed from, reduced to what they read.

    ``palace_graph.entity_tunnel_candidates`` reads, per wing, each entity's
    strongest ``co_occurrence_count`` and nothing else, so this returns one
    ``{"wing", "entity_a", "co_occurrence_count"}`` record per (wing, entity)
    rather than every hallway of the palace. An entity whose count is missing
    on some of its records also gets a record without a count, which the
    candidates read as the minimum, exactly as they read the full records.
    Before the first write has imported ``hallways.json``, the full records.
    """
    conn = _reader(config)
    if conn is None:
        return list_hallways(config=config)
    with contextlib.closing(conn):
        rows = conn.execute(
            "SELECT wing, entity, MAX(CAST(n AS INTEGER)), MAX(n IS NULL) FROM ("
            " SELECT wing, entity_a AS entity, co_occurrence_count AS n FROM hallways"
            " UNION ALL"
            " SELECT wing, entity_b, co_occurrence_count FROM hallways"
            ") GROUP BY wing, entity"
        ).fetchall()
    records: list[dict] = []
    for wing, entity, strongest, missing in rows:
        if strongest is not None:
            records.append({"wing": wing, "entity_a": entity, "co_occurrence_count": strongest})
        if missing:
            records.append({"wing": wing, "entity_a": entity})
    return records


def prune_spelling_hallways(config=None, apply: bool = False) -> dict:
    """Find (and with ``apply``) remove hallways that older mines wrote per spelling.

    Two defects, one cause: before :func:`canonical_entities` the miner paired
    raw spellings, so every code wing has ``main.zig ↔ src/main.zig``
    (an entity joined to itself) and four copies of ``ChatStore ↔ RootView``
    (one per spelling combination). Self-links are dropped; of each variant
    group the record with the highest co-occurrence count survives under
    its canonical spellings and the rest are dropped. Only the sidecar file
    is touched, never a drawer. ``removed`` is 0 on a dry run.
    """
    with _hallway_file_lock(config):
        return _prune_spelling_hallways_locked(config, apply)


def association_groups(records: list[dict]) -> list[list[dict]]:
    """Group hallway records that are one association, per wing.

    Each endpoint is mapped to the file it names using the wing's own
    spellings (:func:`_spelling_clusters`), so two files that share a
    basename are two entities, and a bare name that could be either of them
    belongs to no file. A record with such an ambiguous endpoint is a group
    of its own: comparing it pairwise let it match both files and bridge
    their records into one group, and the prune then deleted a real
    association. Self-links are expected to be filtered out first.

    Shared by ``--prune-spellings`` and ``mempalace audit`` so the audit never
    reports a duplicate the prune would keep.
    """
    by_wing: dict[str, list[dict]] = defaultdict(list)
    for h in records:
        if isinstance(h, dict):
            by_wing[str(h.get("wing") or "")].append(h)
    groups: list[list[dict]] = []
    for members in by_wing.values():
        spellings: dict[str, set[str]] = defaultdict(set)
        for h in members:
            for e in (str(h.get("entity_a")), str(h.get("entity_b"))):
                spellings[entity_spelling_key(e)].add(e)
        cluster_of: dict[str, tuple] = {}
        for key, names in spellings.items():
            for i, cluster in enumerate(_spelling_clusters(sorted(names))):
                for e in cluster:
                    cluster_of[e] = (key, i)
        buckets: dict[tuple, list[dict]] = {}
        for h in members:
            ca = cluster_of.get(str(h.get("entity_a")))
            cb = cluster_of.get(str(h.get("entity_b")))
            if ca is None or cb is None:
                groups.append([h])
                continue
            buckets.setdefault(tuple(sorted((ca, cb))), []).append(h)
        groups.extend(buckets.values())
    return groups


def _merge_variant_group(members: list[dict], kept: list[dict], duplicates: list[dict]) -> None:
    """Keep the strongest record of one association under its canonical spellings."""
    if len(members) == 1:
        kept.append(members[0])
        return
    members.sort(key=lambda h: -int(h.get("co_occurrence_count") or 0))
    survivor = dict(members[0])
    # Variants may arrive with reversed endpoints (``a ↔ b`` and ``b.py ↔ a``),
    # so canonicalize per entity across both columns rather than per column,
    # then keep the survivor's own orientation.
    # Canonicalize each side on its own, never through the basename: an
    # association between two files that share one (``src/user.py ↔
    # tests/user.py``) would otherwise get the same spelling on both sides
    # and turn into a self-link. Members may be written in either
    # orientation, so each is aligned to the survivor first.
    sa, sb = str(survivor["entity_a"]), str(survivor["entity_b"])
    side_a, side_b = [sa], [sb]
    for m in members[1:]:
        ma, mb = str(m.get("entity_a")), str(m.get("entity_b"))
        if same_file_spelling(ma, sa) and same_file_spelling(mb, sb):
            side_a.append(ma)
            side_b.append(mb)
        elif same_file_spelling(mb, sa) and same_file_spelling(ma, sb):
            side_a.append(mb)
            side_b.append(ma)
    survivor["entity_a"] = canonical_spelling(side_a)
    survivor["entity_b"] = canonical_spelling(side_b)
    survivor["id"] = _hallway_id(survivor["wing"], survivor["entity_a"], survivor["entity_b"])
    kept.append(survivor)
    duplicates.extend(members[1:])


def _prune_spelling_hallways_locked(config, apply: bool) -> dict:
    hallways = _load_hallways_for_write(config) if apply else _load_hallways(config)
    self_links = [h for h in hallways if is_self_link(h)]
    candidates = [h for h in hallways if isinstance(h, dict) and not is_self_link(h)]
    duplicates: list[dict] = []
    kept: list[dict] = []
    for members in association_groups(candidates):
        _merge_variant_group(members, kept, duplicates)

    by_wing: dict[str, int] = {}
    for h in self_links + duplicates:
        wing = str(h.get("wing") or "?")
        by_wing[wing] = by_wing.get(wing, 0) + 1
    self_links.sort(key=lambda h: -int(h.get("co_occurrence_count") or 0))
    duplicates.sort(key=lambda h: -int(h.get("co_occurrence_count") or 0))
    sample = [f"{h.get('entity_a')} ↔ {h.get('entity_b')}" for h in (self_links + duplicates)[:10]]
    doomed = len(self_links) + len(duplicates)
    removed = 0
    if apply and doomed:
        _save_hallways(kept, config)
        removed = doomed
    return {
        "total": len(hallways),
        "self_links": len(self_links),
        "duplicates": len(duplicates),
        "by_wing": dict(sorted(by_wing.items(), key=lambda kv: -kv[1])),
        "sample": sample,
        "removed": removed,
    }


def delete_hallway(hallway_id: str, config=None) -> bool:
    """Remove one hallway record by id. Returns True if a record was removed."""
    with _hallway_file_lock(config), _writing(config) as conn:
        return conn.execute("DELETE FROM hallways WHERE id = ?", (hallway_id,)).rowcount > 0


def drop_wing_hallways(wing: str, config=None) -> int:
    """Remove every hallway record of ``wing``. Returns how many were removed."""
    with _hallway_file_lock(config), _writing(config) as conn:
        return conn.execute("DELETE FROM hallways WHERE wing = ?", (wing,)).rowcount
