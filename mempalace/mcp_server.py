#!/usr/bin/env python3
"""
MemPalace MCP Server — read/write palace access for Claude Code
================================================================
Install: claude mcp add mempalace -- mempalace-mcp [--palace /path/to/palace]

Tools (read):
  mempalace_status          — total drawers, wing/room breakdown
  mempalace_list_wings      — all wings with drawer counts
  mempalace_list_rooms      — rooms within a wing
  mempalace_get_taxonomy    — full wing → room → count tree
  mempalace_search          — semantic search, optional wing/room filter
  mempalace_check_duplicate — check if content already exists before filing

Tools (write):
  mempalace_add_drawer      — file verbatim content into a wing/room
  mempalace_delete_drawer   — remove a drawer by ID

Tools (maintenance):
  mempalace_reconnect       — force cache invalidation and reconnect after external writes
"""

import os
import sys

# --- MCP stdio protection (issue #225) -----------------------------------
# The MCP protocol multiplexes JSON-RPC over stdio: stdout MUST carry only
# valid JSON-RPC messages, stderr is for human-readable logs. Some
# transitive dependencies (chromadb → onnxruntime, posthog telemetry) print
# banners and error messages directly to stdout — sometimes at C level —
# which breaks Claude Desktop's JSON parser. Redirect stdout → stderr at
# both the Python and file-descriptor level before heavy imports, then
# restore the real stdout in main() before entering the protocol loop.
_REAL_STDOUT = sys.stdout
_REAL_STDOUT_FD = None
try:
    _REAL_STDOUT_FD = os.dup(1)
    os.dup2(2, 1)
except (OSError, AttributeError):
    # Environments without fd-level stdio (embedded interpreters, some test
    # harnesses). The Python-level redirect below still applies.
    pass
sys.stdout = sys.stderr

import argparse  # noqa: E402  (deferred until after stdio protection above)
import json  # noqa: E402
import logging  # noqa: E402
import hashlib  # noqa: E402
import sqlite3  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
from datetime import date, datetime  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Optional  # noqa: E402

from .config import (  # noqa: E402
    MempalaceConfig,
    sanitize_kg_value,
    sanitize_name,
    sanitize_content,
    sanitize_iso_temporal,
)
from .version import __version__  # noqa: E402
from chromadb.errors import NotFoundError as _ChromaNotFoundError  # noqa: E402

from .backends.chroma import (  # noqa: E402
    ChromaBackend,
    ChromaCollection,
    _HNSW_BLOAT_GUARD,
    _pin_hnsw_threads,
    hnsw_capacity_status,
)
from .query_sanitizer import sanitize_query  # noqa: E402
from .searcher import search_memories  # noqa: E402
from .palace_graph import (  # noqa: E402
    traverse,
    find_tunnels,
    graph_stats,
    create_tunnel,
    list_tunnels,
    delete_tunnel,
    follow_tunnels,
)

from .knowledge_graph import KnowledgeGraph, DEFAULT_KG_PATH  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
logger = logging.getLogger("mempalace_mcp")


def _parse_args():
    parser = argparse.ArgumentParser(description="MemPalace MCP Server")
    parser.add_argument(
        "--palace",
        metavar="PATH",
        help="Path to the palace directory (overrides config file and env var)",
    )
    args, unknown = parser.parse_known_args()
    if unknown:
        logger.debug("Ignoring unknown args: %s", unknown)
    return args


_args = _parse_args()

if _args.palace:
    os.environ["MEMPALACE_PALACE_PATH"] = os.path.abspath(_args.palace)

_config = MempalaceConfig()

_kg_by_path: dict[str, KnowledgeGraph] = {}
_kg_cache_lock = threading.Lock()
_palace_flag_given: bool = bool(_args.palace)


def _resolve_kg_path() -> str:
    if _palace_flag_given:
        return os.path.join(_config.palace_path, "knowledge_graph.sqlite3")
    return DEFAULT_KG_PATH


def _get_kg() -> KnowledgeGraph:
    path = os.path.abspath(_resolve_kg_path())
    kg = _kg_by_path.get(path)
    if kg is not None:
        return kg
    with _kg_cache_lock:
        kg = _kg_by_path.get(path)
        if kg is None:
            kg = KnowledgeGraph(db_path=path)
            _kg_by_path[path] = kg
    return kg


def _call_kg(op):
    """Run ``op(kg)`` against the cached KG with one-shot retry on close.

    Race we're guarding against: a handler grabs ``kg = _get_kg()`` and is
    about to call ``kg.add_triple(...)`` when ``tool_reconnect`` fires on
    another thread, drains ``_kg_by_path``, and closes the underlying
    sqlite3.Connection. The handler's call then raises
    ``sqlite3.ProgrammingError: Cannot operate on a closed database`` and
    bubbles up as a -32000 to the MCP client even though the user just
    asked for a reconnect.

    Catch that single class of error, evict the stale entry from the
    cache (only if it still points at the closed instance — another
    thread may have already replaced it), and try once more with a fresh
    KG. Beyond one retry give up: a second close means we're losing a
    sustained race we won't win in this loop, and a hung loop is worse
    than a clear failure surface.
    """
    for attempt in range(2):
        kg = _get_kg()
        try:
            return op(kg)
        except sqlite3.ProgrammingError:
            if attempt == 0:
                path = os.path.abspath(_resolve_kg_path())
                with _kg_cache_lock:
                    if _kg_by_path.get(path) is kg:
                        _kg_by_path.pop(path, None)
                continue
            raise


_client_cache = None
_collection_cache = None
_palace_db_inode = 0  # inode of chroma.sqlite3 at cache time
_palace_db_mtime = 0.0  # mtime of chroma.sqlite3 at cache time


def _is_transient_index_error(result) -> bool:
    # Chroma can return "Internal error: Error finding id" during the
    # HNSW flush window after a bulk CLI mine — SQLite rows are
    # committed but the binary segment metadata isn't flushed yet.
    # Self-heals once the flush completes (~30-60s). See issue #1315.
    if not isinstance(result, dict):
        return False
    err = result.get("error", "")
    return isinstance(err, str) and ("Error finding id" in err or "Internal error" in err)


def _force_chroma_cache_reset() -> None:
    # Drop both the MCP-local client cache and the shared backend's
    # per-palace cache so the next call rebuilds against the post-flush
    # state. Without clearing _DEFAULT_BACKEND._clients the retry
    # would just hit the same stale handle, since tool_search routes
    # via search_memories -> palace.get_collection -> backend cache.
    global \
        _client_cache, \
        _collection_cache, \
        _palace_db_inode, \
        _palace_db_mtime, \
        _metadata_cache, \
        _metadata_cache_time
    _client_cache = None
    _collection_cache = None
    _palace_db_inode = 0
    _palace_db_mtime = 0.0
    _metadata_cache = None
    _metadata_cache_time = 0
    try:
        from .palace import _DEFAULT_BACKEND

        _DEFAULT_BACKEND._clients.pop(_config.palace_path, None)
        _DEFAULT_BACKEND._freshness.pop(_config.palace_path, None)
    except Exception:
        pass


# ── Vector-search disabled flag (#1222) ──────────────────────────────────
# Set when ``hnsw_capacity_status`` reports a divergence between sqlite
# and the HNSW segment large enough that chromadb would segfault on
# segment load. While this is set, vector-shaped tools (``search``,
# ``check_duplicate``) route to the sqlite-only BM25 fallback in
# :func:`mempalace.searcher._bm25_only_via_sqlite`. Cleared after a
# successful repair via :func:`tool_reconnect` (which re-runs the probe).
_vector_disabled = False
_vector_disabled_reason = ""
# Optional[dict] (not ``dict | None``) keeps Python 3.9 import-time
# parsing happy — PEP 604 unions in annotations only became unconditional
# at module-eval time in 3.10.
_vector_capacity_status: Optional[dict] = None


def _refresh_vector_disabled_flag() -> None:
    """Re-run the HNSW capacity probe and update the module-level flag.

    Called from :func:`_get_client` whenever the client cache is rebuilt
    (first open or palace replacement). Cheap — pure sqlite + pickle
    read, no chromadb interaction. Never raises: a probe that crashes
    would defeat the point.
    """
    global _vector_disabled, _vector_disabled_reason, _vector_capacity_status
    try:
        info = hnsw_capacity_status(_config.palace_path, _config.collection_name)
    except Exception:
        logger.debug("HNSW capacity probe raised", exc_info=True)
        return
    _vector_capacity_status = info
    if info.get("diverged"):
        if not _vector_disabled:
            logger.warning(
                "HNSW capacity divergence detected (%s) — routing search to "
                "BM25-only sqlite fallback. Run `mempalace repair` to restore "
                "vector search.",
                info.get("message", "unknown"),
            )
        _vector_disabled = True
        _vector_disabled_reason = info.get("message", "")
    else:
        if _vector_disabled:
            logger.info(
                "HNSW capacity within tolerance (%s) — vector search re-enabled",
                info.get("message", ""),
            )
        _vector_disabled = False
        _vector_disabled_reason = ""


# ==================== WRITE-AHEAD LOG ====================
# Every write operation is logged to a JSONL file before execution.
# This provides an audit trail for detecting memory poisoning and
# enables review/rollback of writes from external or untrusted sources.

_WAL_DIR = Path(os.path.expanduser("~/.mempalace/wal"))
_WAL_DIR.mkdir(parents=True, exist_ok=True)
try:
    _WAL_DIR.chmod(0o700)
except (OSError, NotImplementedError):
    pass
_WAL_FILE = _WAL_DIR / "write_log.jsonl"
# Atomically create WAL file with restricted permissions (no TOCTOU race).
# os.open with O_CREAT|O_WRONLY and mode 0o600 creates the file if absent
# or opens it if present, both in a single syscall.
try:
    _fd = os.open(str(_WAL_FILE), os.O_CREAT | os.O_WRONLY, 0o600)
    os.close(_fd)
except (OSError, NotImplementedError):
    pass

# Keys whose values should be redacted in WAL entries to avoid logging sensitive content
_WAL_REDACT_KEYS = frozenset(
    {"content", "content_preview", "document", "entry", "entry_preview", "query", "text"}
)


def _wal_log(operation: str, params: dict, result: dict = None):
    """Append a write operation to the write-ahead log."""
    # Redact sensitive content from params before logging
    safe_params = {}
    for k, v in params.items():
        if k in _WAL_REDACT_KEYS:
            safe_params[k] = f"[REDACTED {len(v)} chars]" if isinstance(v, str) else "[REDACTED]"
        else:
            safe_params[k] = v
    entry = {
        "timestamp": datetime.now().isoformat(),
        "operation": operation,
        "params": safe_params,
        "result": result,
    }
    try:
        fd = os.open(str(_WAL_FILE), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except Exception as e:
        logger.error(f"WAL write failed: {e}")


def _get_client():
    """Return a ChromaDB PersistentClient, reconnecting if the database changed on disk.

    Detects palace rebuilds (repair/nuke/purge) by checking the inode of
    chroma.sqlite3.  A full rebuild replaces the file, changing the inode.
    Also detects external writes (scripts, CLI) via mtime changes — the
    inode check alone misses in-place modifications that invalidate the
    in-memory HNSW index.

    Note: FAT/exFAT may return 0 for st_ino — the ``current_inode != 0``
    guard skips reconnect detection on those filesystems (safe fallback).
    """
    global \
        _client_cache, \
        _collection_cache, \
        _palace_db_inode, \
        _palace_db_mtime, \
        _metadata_cache, \
        _metadata_cache_time
    db_path = os.path.join(_config.palace_path, "chroma.sqlite3")
    try:
        st = os.stat(db_path)
        current_inode = st.st_ino
        current_mtime = st.st_mtime
    except OSError:
        current_inode = 0
        current_mtime = 0.0

    # If the DB file disappeared (e.g. during rebuild) but we have a cached
    # collection, invalidate so we don't serve stale data.  Without this,
    # both stored and current values are 0 on the first call after deletion,
    # making inode_changed and mtime_changed both False.
    if not os.path.isfile(db_path) and _collection_cache is not None:
        _client_cache = None
        _collection_cache = None
        _palace_db_inode = 0
        _palace_db_mtime = 0.0
        # Fall through to normal reconnect which will handle missing DB

    inode_changed = current_inode != 0 and current_inode != _palace_db_inode
    mtime_changed = current_mtime != 0.0 and abs(current_mtime - _palace_db_mtime) > 0.01

    if _client_cache is None or inode_changed or mtime_changed:
        # Run the HNSW capacity probe BEFORE chromadb opens the segment —
        # if the index is severely undersized, segment load can segfault
        # the whole MCP server (#1222). The probe is pure sqlite +
        # metadata-pickle read; never touches the HNSW binary files.
        _refresh_vector_disabled_flag()
        _client_cache = ChromaBackend.make_client(_config.palace_path)
        _collection_cache = None
        _metadata_cache = None
        _metadata_cache_time = 0
        _palace_db_inode = current_inode
        _palace_db_mtime = current_mtime
    return _client_cache


def _get_collection(create=False):
    """Return the ChromaDB collection, caching the client between calls.

    On failure, log the exception and retry once after clearing the client
    and collection caches. Tools were silently returning ``None`` when a
    cached client/collection went stale — typically after the chromadb
    rust bindings invalidated a handle following an out-of-band write —
    leaving the LLM with no diagnostic and no recovery path. The retry
    forces ``_get_client()`` to rebuild from scratch (which re-runs
    ``quarantine_stale_hnsw`` per #1322), so the second attempt heals the
    common stale-handle / stale-HNSW case automatically.
    """
    global _client_cache, _collection_cache, _metadata_cache, _metadata_cache_time
    for attempt in range(2):
        try:
            client = _get_client()
            # ChromaDB 1.x persists the EF *identity* (its ``name()``) with the
            # collection but not the EF *instance/configuration*. So a reader or
            # writer that omits ``embedding_function=`` silently gets chromadb's
            # built-in ``DefaultEmbeddingFunction`` — its ``name()`` matches the
            # one we spoof in ``mempalace.embedding`` (both report ``"default"``,
            # the identity check passes), but the *provider list* is chromadb's
            # default rather than the user's resolved device. On bleeding-edge
            # interpreters (#1299: python 3.14 + chromadb 1.5.x on Apple Silicon)
            # that default provider selection can SIGSEGV the host process on
            # first ``col.add()``. The miner / Stop hook ingest path avoids this
            # because it routes through ``ChromaBackend.get_collection``, which
            # resolves the EF via ``ChromaBackend._resolve_embedding_function``;
            # the MCP server bypassed that abstraction. Resolve the EF inside the
            # branches that actually open a collection so warm-cache reads stay
            # zero-cost. Reuse the backend helper so the two call sites can't
            # drift on logging or fallback semantics.
            if create:
                ef = ChromaBackend._resolve_embedding_function()
                ef_kwargs = {"embedding_function": ef} if ef is not None else {}
                # hnsw:num_threads=1 disables ChromaDB's multi-threaded ParallelFor
                # HNSW insert path, which has a race in repairConnectionsForUpdate /
                # addPoint (see issues #974, #965). Set via metadata on fresh
                # collections and re-applied via _pin_hnsw_threads() for legacy
                # palaces whose collections were created before this fix (the
                # runtime config does not persist cross-process in chromadb 1.5.x,
                # so the retrofit runs every time _get_collection opens a cache).
                #
                # ChromaDB 1.5.x's Rust binding SIGSEGVs when get_or_create_collection
                # is called with metadata that differs from what's stored. The split
                # below skips the metadata-comparison codepath for existing
                # collections, mirroring the backend-layer fix from #1262.
                try:
                    raw = client.get_collection(_config.collection_name, **ef_kwargs)
                except _ChromaNotFoundError:
                    raw = client.create_collection(
                        _config.collection_name,
                        metadata={
                            "hnsw:space": "cosine",
                            "hnsw:num_threads": 1,
                            **_HNSW_BLOAT_GUARD,
                        },
                        **ef_kwargs,
                    )
                _pin_hnsw_threads(raw)
                _collection_cache = ChromaCollection(raw, palace_path=_config.palace_path)
                _metadata_cache = None
                _metadata_cache_time = 0
            elif _collection_cache is None:
                ef = ChromaBackend._resolve_embedding_function()
                ef_kwargs = {"embedding_function": ef} if ef is not None else {}
                raw = client.get_collection(_config.collection_name, **ef_kwargs)
                _pin_hnsw_threads(raw)
                _collection_cache = ChromaCollection(raw, palace_path=_config.palace_path)
                _metadata_cache = None
                _metadata_cache_time = 0
            return _collection_cache
        except Exception:
            logger.exception(
                "_get_collection attempt %d/2 failed (palace=%s, create=%s)",
                attempt + 1,
                _config.palace_path,
                create,
            )
            if attempt == 0:
                # Reset all caches so the next attempt forces _get_client()
                # to rebuild the chromadb client from scratch — that path
                # re-runs quarantine_stale_hnsw (#1322) and reopens the
                # collection cleanly, healing the common stale-handle case.
                _client_cache = None
                _collection_cache = None
                _metadata_cache = None
                _metadata_cache_time = 0
    return None


def _no_palace():
    return {
        "error": "No palace found",
        "hint": "Run: mempalace init <dir> && mempalace mine <dir>",
    }


# ==================== HELPERS ====================


def _fetch_all_metadata(col, where=None):
    """Paginate col.get() to avoid the 10K silent truncation limit."""
    total = col.count()
    all_meta = []
    offset = 0
    while offset < total:
        kwargs = {"include": ["metadatas"], "limit": 1000, "offset": offset}
        if where:
            kwargs["where"] = where
        batch = col.get(**kwargs)
        if not batch["metadatas"]:
            break
        all_meta.extend(batch["metadatas"])
        offset += len(batch["metadatas"])
    return all_meta


_metadata_cache = None
_metadata_cache_time = 0
_METADATA_CACHE_TTL = 5.0  # seconds
_MAX_RESULTS = 100  # upper bound for search/list limit params


def _get_cached_metadata(col, where=None):
    """Return cached metadata if fresh, else fetch and cache."""
    global _metadata_cache, _metadata_cache_time
    now = time.time()
    if (
        where is None
        and _metadata_cache is not None
        and (now - _metadata_cache_time) < _METADATA_CACHE_TTL
    ):
        return _metadata_cache
    result = _fetch_all_metadata(col, where=where)
    if where is None:
        _metadata_cache = result
        _metadata_cache_time = now
    return result


def _sanitize_optional_name(value: str = None, field_name: str = "name") -> str:
    """Validate optional wing/room-style filters."""
    if value is None or not value.strip():
        return None
    return sanitize_name(value, field_name)


# ==================== READ TOOLS ====================


def _tool_status_via_sqlite() -> dict:
    """Pure-sqlite status reader for the #1222 fallback path.

    When the HNSW capacity probe detects divergence, opening the chromadb
    persistent client can segfault. This reader pulls the same wing/room
    breakdown directly from ``embedding_metadata`` so the operator still
    gets a working status response — and crucially the
    ``vector_disabled`` flag — without us touching the vector segment.
    """
    import sqlite3 as _sqlite3

    db_path = os.path.join(_config.palace_path, "chroma.sqlite3")
    if not os.path.isfile(db_path):
        return _no_palace()
    collection_name = _config.collection_name

    wings: dict = {}
    rooms: dict = {}
    total = 0
    try:
        conn = _sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            row = conn.execute(
                """
                SELECT COUNT(*)
                FROM embeddings e
                JOIN segments s ON e.segment_id = s.id
                JOIN collections c ON s.collection = c.id
                WHERE c.name = ?
                """,
                (collection_name,),
            ).fetchone()
            total = int(row[0]) if row and row[0] is not None else 0
            for key, target in (("wing", wings), ("room", rooms)):
                for value, count in conn.execute(
                    """
                    SELECT em.string_value, COUNT(*)
                    FROM embedding_metadata em
                    JOIN embeddings e ON em.id = e.id
                    JOIN segments s ON e.segment_id = s.id
                    JOIN collections c ON s.collection = c.id
                    WHERE c.name = ?
                      AND em.key = ?
                      AND em.string_value IS NOT NULL
                    GROUP BY em.string_value
                    """,
                    (collection_name, key),
                ):
                    target[value] = count
        finally:
            conn.close()
    except _sqlite3.Error:
        logger.exception("tool_status sqlite fallback read failed")

    result = {
        "total_drawers": total,
        "wings": wings,
        "rooms": rooms,
        "protocol": PALACE_PROTOCOL,
        "aaak_dialect": AAAK_SPEC,
        "vector_disabled": True,
        "vector_disabled_reason": _vector_disabled_reason,
    }
    if _vector_capacity_status:
        result["hnsw_capacity"] = {
            "sqlite_count": _vector_capacity_status.get("sqlite_count"),
            "hnsw_count": _vector_capacity_status.get("hnsw_count"),
            "divergence": _vector_capacity_status.get("divergence"),
        }
    return result


def tool_status():
    # Run the safe sqlite/pickle probe before we touch chromadb. In the
    # #1222 failure mode, opening the persistent client to call .count()
    # can segfault — short-circuit to a pure-sqlite path when divergence
    # is detected so status stays reachable.
    db_exists = os.path.isfile(os.path.join(_config.palace_path, "chroma.sqlite3"))
    _refresh_vector_disabled_flag()

    if _vector_disabled:
        return _tool_status_via_sqlite()

    # Use create=True only when a palace DB already exists on disk -- this
    # bootstraps the ChromaDB collection on a valid-but-empty palace without
    # accidentally creating a palace in a non-existent directory (#830).
    col = _get_collection(create=db_exists)
    if not col:
        return _no_palace()
    count = col.count()
    wings = {}
    rooms = {}
    result = {
        "total_drawers": count,
        "wings": wings,
        "rooms": rooms,
        "protocol": PALACE_PROTOCOL,
        "aaak_dialect": AAAK_SPEC,
    }
    try:
        all_meta = _get_cached_metadata(col)
        for m in all_meta:
            m = m or {}
            w = m.get("wing", "unknown")
            r = m.get("room", "unknown")
            wings[w] = wings.get(w, 0) + 1
            rooms[r] = rooms.get(r, 0) + 1
    except Exception as e:
        logger.exception("tool_status metadata fetch failed")
        result["error"] = str(e)
        result["partial"] = True
    return result


# ── AAAK Dialect Spec ─────────────────────────────────────────────────────────
# Included in status response so the AI learns it on first wake-up call.
# Also available via mempalace_get_aaak_spec tool.

PALACE_PROTOCOL = """IMPORTANT — MemPalace Memory Protocol:
1. ON WAKE-UP: Call mempalace_status to load palace overview + AAAK spec.
2. BEFORE RESPONDING about any person, project, or past event: call mempalace_kg_query or mempalace_search FIRST. Never guess — verify.
3. IF UNSURE about a fact (name, gender, age, relationship): say "let me check" and query the palace. Wrong is worse than slow.
4. AFTER EACH SESSION: call mempalace_diary_write to record what happened, what you learned, what matters.
5. WHEN FACTS CHANGE: call mempalace_kg_invalidate on the old fact, mempalace_kg_add for the new one.

This protocol ensures the AI KNOWS before it speaks. Storage is not memory — but storage + this protocol = memory."""

AAAK_SPEC = """AAAK is a compressed memory dialect that MemPalace uses for efficient storage.
It is designed to be readable by both humans and LLMs without decoding.

FORMAT:
  ENTITIES: 3-letter uppercase codes. ALC=Alice, JOR=Jordan, RIL=Riley, MAX=Max, BEN=Ben.
  EMOTIONS: *action markers* before/during text. *warm*=joy, *fierce*=determined, *raw*=vulnerable, *bloom*=tenderness.
  STRUCTURE: Pipe-separated fields. FAM: family | PROJ: projects | ⚠: warnings/reminders.
  DATES: ISO format (2026-03-31). COUNTS: Nx = N mentions (e.g., 570x).
  IMPORTANCE: ★ to ★★★★★ (1-5 scale).
  HALLS: hall_facts, hall_events, hall_discoveries, hall_preferences, hall_advice.
  WINGS: wing_user, wing_agent, wing_team, wing_code, wing_myproject, wing_hardware, wing_ue5, wing_ai_research.
  ROOMS: Hyphenated slugs representing named ideas (e.g., chromadb-setup, gpu-pricing).

EXAMPLE:
  FAM: ALC→♡JOR | 2D(kids): RIL(18,sports) MAX(11,chess+swimming) | BEN(contributor)

Read AAAK naturally — expand codes mentally, treat *markers* as emotional context.
When WRITING AAAK: use entity codes, mark emotions, keep structure tight."""


def tool_list_wings():
    col = _get_collection()
    if not col:
        return _no_palace()
    wings = {}
    result = {"wings": wings}
    try:
        all_meta = _get_cached_metadata(col)
        for m in all_meta:
            m = m or {}
            w = m.get("wing", "unknown")
            wings[w] = wings.get(w, 0) + 1
    except Exception as e:
        logger.exception("tool_list_wings metadata fetch failed")
        result["error"] = str(e)
        result["partial"] = True
    return result


def tool_list_rooms(wing: str = None):
    try:
        wing = _sanitize_optional_name(wing, "wing")
    except ValueError as e:
        return {"error": str(e)}
    col = _get_collection()
    if not col:
        return _no_palace()
    rooms = {}
    result = {"wing": wing or "all", "rooms": rooms}
    try:
        where = {"wing": wing} if wing else None
        all_meta = _fetch_all_metadata(col, where=where)
        for m in all_meta:
            m = m or {}
            r = m.get("room", "unknown")
            rooms[r] = rooms.get(r, 0) + 1
    except Exception as e:
        logger.exception("tool_list_rooms metadata fetch failed")
        result["error"] = str(e)
        result["partial"] = True
    return result


def tool_get_taxonomy():
    col = _get_collection()
    if not col:
        return _no_palace()
    taxonomy = {}
    result = {"taxonomy": taxonomy}
    try:
        all_meta = _get_cached_metadata(col)
        for m in all_meta:
            m = m or {}
            w = m.get("wing", "unknown")
            r = m.get("room", "unknown")
            if w not in taxonomy:
                taxonomy[w] = {}
            taxonomy[w][r] = taxonomy[w].get(r, 0) + 1
    except Exception as e:
        logger.exception("tool_get_taxonomy metadata fetch failed")
        result["error"] = str(e)
        result["partial"] = True
    return result


def tool_search(
    query: str,
    limit: int = 5,
    wing: str = None,
    room: str = None,
    max_distance: float = 1.5,
    min_similarity: float = None,
    context: str = None,
):
    limit = max(1, min(limit, _MAX_RESULTS))
    try:
        wing = _sanitize_optional_name(wing, "wing")
        room = _sanitize_optional_name(room, "room")
    except ValueError as e:
        return {"error": str(e)}
    # Backwards compat: accept old name
    # Backwards compat: convert old similarity scale (higher=stricter) to
    # distance scale (lower=stricter). Similarity 0.8 → distance 0.2.
    dist = (1.0 - min_similarity) if min_similarity is not None else max_distance
    # Mitigate system prompt contamination (Issue #333)
    sanitized = sanitize_query(query)
    # Ensure the vector-disabled probe has been run via the safe
    # sqlite/pickle path before we touch chromadb. Calling _get_client()
    # here would defeat the fallback — it constructs a PersistentClient
    # which can segfault on segment load in the #1222 failure mode.
    _refresh_vector_disabled_flag()
    result = search_memories(
        sanitized["clean_query"],
        palace_path=_config.palace_path,
        wing=wing,
        room=room,
        n_results=limit,
        max_distance=dist,
        vector_disabled=_vector_disabled,
        collection_name=_config.collection_name,
    )
    if _is_transient_index_error(result):
        # Post-bulk-write HNSW flush window (#1315): drop caches, give
        # the segment a moment to settle, retry once. Caller never sees
        # the transient unless the second attempt also fails.
        _force_chroma_cache_reset()
        time.sleep(2)
        _refresh_vector_disabled_flag()
        result = search_memories(
            sanitized["clean_query"],
            palace_path=_config.palace_path,
            wing=wing,
            room=room,
            n_results=limit,
            max_distance=dist,
            vector_disabled=_vector_disabled,
        )
        if not _is_transient_index_error(result):
            result["index_recovered"] = True
    if _vector_disabled:
        result["vector_disabled"] = True
        result["vector_disabled_reason"] = _vector_disabled_reason
    # Attach sanitizer metadata for transparency
    if sanitized["was_sanitized"]:
        result["query_sanitized"] = True
        result["sanitizer"] = {
            "method": sanitized["method"],
            "original_length": sanitized["original_length"],
            "clean_length": sanitized["clean_length"],
            "clean_query": sanitized["clean_query"],
        }
    if context:
        result["context_received"] = True
    return result


def tool_check_duplicate(content: str, threshold: float = 0.9):
    _refresh_vector_disabled_flag()
    if _vector_disabled:
        # Without a usable HNSW we can't compute cosine similarity for
        # near-duplicate detection. Report the limitation rather than
        # silently returning "not a duplicate" — false negatives here
        # would let the AI re-file content the palace already holds.
        return {
            "is_duplicate": False,
            "matches": [],
            "vector_disabled": True,
            "vector_disabled_reason": _vector_disabled_reason,
            "hint": (
                "duplicate detection requires vector search; run `mempalace repair` to restore"
            ),
        }
    col = _get_collection()
    if not col:
        return _no_palace()
    try:
        results = col.query(
            query_texts=[content],
            n_results=5,
            include=["metadatas", "documents", "distances"],
        )
        duplicates = []
        if results["ids"] and results["ids"][0]:
            for i, drawer_id in enumerate(results["ids"][0]):
                dist = results["distances"][0][i]
                similarity = round(max(0.0, 1 - dist), 3)
                if similarity >= threshold:
                    # Chroma 1.5.x can return None for partially-flushed rows;
                    # coerce to empty sentinels so downstream .get() is safe.
                    meta = results["metadatas"][0][i] or {}
                    doc = results["documents"][0][i] or ""
                    duplicates.append(
                        {
                            "id": drawer_id,
                            "wing": meta.get("wing", "?"),
                            "room": meta.get("room", "?"),
                            "similarity": similarity,
                            "content": doc[:200] + "..." if len(doc) > 200 else doc,
                        }
                    )
        return {
            "is_duplicate": len(duplicates) > 0,
            "matches": duplicates,
        }
    except Exception:
        logger.exception("check_duplicate failed")
        return {"error": "Duplicate check failed"}


def tool_get_aaak_spec():
    """Return the AAAK dialect specification."""
    return {"aaak_spec": AAAK_SPEC}


def tool_traverse_graph(start_room: str, max_hops: int = 2):
    """Walk the palace graph from a room. Find connected ideas across wings."""
    max_hops = max(1, min(max_hops, 10))
    col = _get_collection()
    if not col:
        return _no_palace()
    return traverse(start_room, col=col, max_hops=max_hops)


def tool_find_tunnels(wing_a: str = None, wing_b: str = None):
    """Find rooms that bridge two wings — the hallways connecting domains."""
    try:
        wing_a = _sanitize_optional_name(wing_a, "wing_a")
        wing_b = _sanitize_optional_name(wing_b, "wing_b")
    except ValueError as e:
        return {"error": str(e)}
    col = _get_collection()
    if not col:
        return _no_palace()
    return find_tunnels(wing_a, wing_b, col=col)


def tool_graph_stats():
    """Palace graph overview: nodes, tunnels, edges, connectivity."""
    col = _get_collection()
    if not col:
        return _no_palace()
    return graph_stats(col=col)


def tool_create_tunnel(
    source_wing: str,
    source_room: str,
    target_wing: str,
    target_room: str,
    label: str = "",
    source_drawer_id: str = None,
    target_drawer_id: str = None,
):
    """Create an explicit cross-wing tunnel between two palace locations.

    Use when you notice content in one project relates to another project.
    Example: an API design discussion in project_api connects to the
    database schema in project_database.
    """
    try:
        source_wing = sanitize_name(source_wing, "source_wing")
        source_room = sanitize_name(source_room, "source_room")
        target_wing = sanitize_name(target_wing, "target_wing")
        target_room = sanitize_name(target_room, "target_room")
    except ValueError as e:
        return {"error": str(e)}
    return create_tunnel(
        source_wing,
        source_room,
        target_wing,
        target_room,
        label=label,
        source_drawer_id=source_drawer_id,
        target_drawer_id=target_drawer_id,
    )


def tool_list_tunnels(wing: str = None):
    """List all explicit cross-wing tunnels, optionally filtered by wing."""
    try:
        wing = _sanitize_optional_name(wing, "wing")
    except ValueError as e:
        return {"error": str(e)}
    return list_tunnels(wing)


def tool_delete_tunnel(tunnel_id: str):
    """Delete an explicit tunnel by its ID."""
    if not tunnel_id or not isinstance(tunnel_id, str):
        return {"error": "tunnel_id is required"}
    return delete_tunnel(tunnel_id)


def tool_follow_tunnels(wing: str, room: str):
    """Follow explicit tunnels from a room to see connected drawers in other wings."""
    try:
        wing = sanitize_name(wing, "wing")
        room = sanitize_name(room, "room")
    except ValueError as e:
        return {"error": str(e)}
    col = _get_collection()
    return follow_tunnels(wing, room, col=col)


# ==================== WRITE TOOLS ====================


def tool_add_drawer(
    wing: str, room: str, content: str, source_file: str = None, added_by: str = "mcp"
):
    """File verbatim content into a wing/room. Checks for duplicates first."""
    global _metadata_cache
    try:
        wing = sanitize_name(wing, "wing")
        room = sanitize_name(room, "room")
        content = sanitize_content(content)
    except ValueError as e:
        return {"success": False, "error": str(e)}

    col = _get_collection(create=True)
    if not col:
        return _no_palace()

    drawer_id = (
        f"drawer_{wing}_{room}_{hashlib.sha256((wing + room + content).encode()).hexdigest()[:24]}"
    )

    _wal_log(
        "add_drawer",
        {
            "drawer_id": drawer_id,
            "wing": wing,
            "room": room,
            "added_by": added_by,
            "content_length": len(content),
            "content_preview": content[:200],
        },
    )

    # Idempotency: if the deterministic ID already exists, return success as a no-op.
    try:
        existing = col.get(ids=[drawer_id], include=[])
        if existing.ids:
            return {"success": True, "reason": "already_exists", "drawer_id": drawer_id}
    except Exception:
        logger.debug("Idempotency pre-check failed for %s", drawer_id, exc_info=True)

    try:
        col.upsert(
            ids=[drawer_id],
            documents=[content],
            metadatas=[
                {
                    "wing": wing,
                    "room": room,
                    "source_file": source_file or "",
                    "chunk_index": 0,
                    "added_by": added_by,
                    "filed_at": datetime.now().isoformat(),
                }
            ],
        )
        inserted = col.get(ids=[drawer_id], include=[])
        if not inserted.ids:
            raise RuntimeError(
                "Drawer write was acknowledged but the new ID is not readable. "
                "The palace index may be stale; run reconnect or repair."
            )
        _metadata_cache = None
        logger.info(f"Filed drawer: {drawer_id} → {wing}/{room}")
        return {"success": True, "drawer_id": drawer_id, "wing": wing, "room": room}
    except Exception as e:
        return {"success": False, "error": str(e)}


def tool_delete_drawer(drawer_id: str):
    """Delete a single drawer by ID."""
    global _metadata_cache
    col = _get_collection()
    if not col:
        return _no_palace()
    existing = col.get(ids=[drawer_id])
    if not existing["ids"]:
        return {"success": False, "error": f"Drawer not found: {drawer_id}"}

    # Log the deletion with the content being removed for audit trail
    deleted_content = existing.get("documents", [""])[0] if existing.get("documents") else ""
    deleted_meta = existing.get("metadatas", [{}])[0] if existing.get("metadatas") else {}
    _wal_log(
        "delete_drawer",
        {
            "drawer_id": drawer_id,
            "deleted_meta": deleted_meta,
            "content_preview": deleted_content[:200],
        },
    )

    try:
        col.delete(ids=[drawer_id])
        _metadata_cache = None
        logger.info(f"Deleted drawer: {drawer_id}")
        return {"success": True, "drawer_id": drawer_id}
    except Exception as e:
        return {"success": False, "error": str(e)}


def tool_sync(project_dir: str = None, wing: str = None, apply: bool = False):
    """Prune drawers whose source files are gitignored, missing, or moved (#1252)."""
    global _metadata_cache
    from .palace import MineAlreadyRunning
    from .sync import sync_palace

    if not _config.palace_path:
        np = _no_palace()
        return {"success": False, "error": np.get("error", "no palace"), "hint": np.get("hint")}
    project_dirs = [project_dir] if project_dir else None
    try:
        try:
            report = sync_palace(
                palace_path=_config.palace_path,
                project_dirs=project_dirs,
                wing=wing,
                dry_run=not apply,
                wal_log=_wal_log,
            )
            return {"success": True, **report}
        # Order matters: typed handlers must precede the bare Exception
        # below, otherwise MineAlreadyRunning and ValueError fall into the
        # generic "sync failed" branch and break the structured-error tests.
        except MineAlreadyRunning as exc:
            return {"success": False, "error": f"another mine is in progress: {exc}"}
        except ValueError as exc:
            return {"success": False, "error": str(exc)}
        except Exception as exc:
            return {"success": False, "error": f"sync failed: {exc}"}
    finally:
        if apply:
            _metadata_cache = None


def tool_get_drawer(drawer_id: str):
    """Fetch a single drawer by ID. Returns full content and metadata."""
    col = _get_collection()
    if not col:
        return _no_palace()
    try:
        result = col.get(ids=[drawer_id], include=["documents", "metadatas"])
        if not result["ids"]:
            return {"error": f"Drawer not found: {drawer_id}"}
        meta = result["metadatas"][0]
        doc = result["documents"][0]
        # source_file is the absolute filesystem path written by the
        # miners. Reduce to its basename before handing it to the MCP
        # client — same threat model as the palace_path leak fix:
        # nested-agent / multi-server topologies treat the client as a
        # separate trust domain. Basename preserves citation utility.
        # Mirrors the searcher.search_memories() return shape.
        safe_meta = dict(meta) if meta else {}
        if safe_meta.get("source_file"):
            safe_meta["source_file"] = Path(safe_meta["source_file"]).name
        return {
            "drawer_id": drawer_id,
            "content": doc,
            "wing": safe_meta.get("wing", ""),
            "room": safe_meta.get("room", ""),
            "metadata": safe_meta,
        }
    except Exception as e:
        return {"error": str(e)}


def tool_list_drawers(wing: str = None, room: str = None, limit: int = 20, offset: int = 0):
    """List drawers with pagination. Optional wing/room filter."""
    limit = max(1, min(limit, _MAX_RESULTS))
    offset = max(0, offset)
    try:
        wing = _sanitize_optional_name(wing, "wing")
        room = _sanitize_optional_name(room, "room")
    except ValueError as e:
        return {"error": str(e)}
    col = _get_collection()
    if not col:
        return _no_palace()
    try:
        where = None
        conditions = []
        if wing:
            conditions.append({"wing": wing})
        if room:
            conditions.append({"room": room})
        if len(conditions) == 1:
            where = conditions[0]
        elif len(conditions) > 1:
            where = {"$and": conditions}

        kwargs = {"include": ["documents", "metadatas"], "limit": limit, "offset": offset}
        if where:
            kwargs["where"] = where
        result = col.get(**kwargs)

        # Compute total matching drawers for pagination.
        if where:
            total_result = col.get(where=where, include=[])
            total = len(total_result["ids"])
        else:
            total = col.count()

        drawers = []
        for i, did in enumerate(result["ids"]):
            meta = result["metadatas"][i]
            doc = result["documents"][i]
            drawers.append(
                {
                    "drawer_id": did,
                    "wing": meta.get("wing", ""),
                    "room": meta.get("room", ""),
                    "content_preview": doc[:200] + "..." if len(doc) > 200 else doc,
                }
            )
        return {
            "drawers": drawers,
            "total": total,
            "count": len(drawers),
            "offset": offset,
            "limit": limit,
        }
    except Exception as e:
        return {"error": str(e)}


def tool_update_drawer(drawer_id: str, content: str = None, wing: str = None, room: str = None):
    """Update an existing drawer's content and/or metadata."""
    global _metadata_cache

    if content is None and wing is None and room is None:
        return {"success": True, "drawer_id": drawer_id, "noop": True}

    col = _get_collection()
    if not col:
        return _no_palace()
    try:
        existing = col.get(ids=[drawer_id], include=["documents", "metadatas"])
        if not existing["ids"]:
            return {"success": False, "error": f"Drawer not found: {drawer_id}"}

        old_meta = existing["metadatas"][0]
        old_doc = existing["documents"][0]

        new_doc = old_doc
        if content is not None:
            try:
                new_doc = sanitize_content(content)
            except ValueError as e:
                return {"success": False, "error": str(e)}

        new_meta = dict(old_meta)
        if wing is not None:
            try:
                new_meta["wing"] = sanitize_name(wing, "wing")
            except ValueError as e:
                return {"success": False, "error": str(e)}
        if room is not None:
            try:
                new_meta["room"] = sanitize_name(room, "room")
            except ValueError as e:
                return {"success": False, "error": str(e)}

        _wal_log(
            "update_drawer",
            {
                "drawer_id": drawer_id,
                "old_wing": old_meta.get("wing", ""),
                "old_room": old_meta.get("room", ""),
                "new_wing": new_meta.get("wing", ""),
                "new_room": new_meta.get("room", ""),
                "content_changed": content is not None,
                "content_preview": new_doc[:200] if content is not None else None,
            },
        )

        update_kwargs = {"ids": [drawer_id]}
        if content is not None:
            update_kwargs["documents"] = [new_doc]
        update_kwargs["metadatas"] = [new_meta]
        col.update(**update_kwargs)

        _metadata_cache = None

        logger.info(f"Updated drawer: {drawer_id}")
        return {
            "success": True,
            "drawer_id": drawer_id,
            "wing": new_meta.get("wing", ""),
            "room": new_meta.get("room", ""),
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


# ==================== KNOWLEDGE GRAPH ====================


def tool_kg_query(entity: str, as_of: str = None, direction: str = "both"):
    """Query the knowledge graph for an entity's relationships."""
    try:
        entity = sanitize_kg_value(entity, "entity")
        as_of = sanitize_iso_temporal(as_of, "as_of")
    except ValueError as e:
        return {"error": str(e)}

    if direction not in ("outgoing", "incoming", "both"):
        return {"error": "direction must be 'outgoing', 'incoming', or 'both'"}

    results = _call_kg(lambda kg: kg.query_entity(entity, as_of=as_of, direction=direction))
    return {"entity": entity, "as_of": as_of, "facts": results, "count": len(results)}


def tool_kg_add(
    subject: str,
    predicate: str,
    object: str,
    valid_from: str = None,
    valid_to: str = None,
    source_closet: str = None,
    source_file: str = None,
    source_drawer_id: str = None,
):
    """Add a relationship to the knowledge graph.

    All temporal and provenance fields are optional. ``valid_to`` lets callers
    backfill historical facts with a known end date/time in a single call
    instead of a separate ``kg_invalidate`` call.

    Temporal values accept either ``YYYY-MM-DD`` or canonical UTC datetimes in
    the form ``YYYY-MM-DDTHH:MM:SSZ``.
    """
    try:
        subject = sanitize_kg_value(subject, "subject")
        predicate = sanitize_name(predicate, "predicate")
        object = sanitize_kg_value(object, "object")
        valid_from = sanitize_iso_temporal(valid_from, "valid_from")
        valid_to = sanitize_iso_temporal(valid_to, "valid_to")
    except ValueError as e:
        return {"success": False, "error": str(e)}

    _wal_log(
        "kg_add",
        {
            "subject": subject,
            "predicate": predicate,
            "object": object,
            "valid_from": valid_from,
            "valid_to": valid_to,
            "source_closet": source_closet,
            "source_file": source_file,
            "source_drawer_id": source_drawer_id,
        },
    )

    triple_id = _call_kg(
        lambda kg: kg.add_triple(
            subject,
            predicate,
            object,
            valid_from=valid_from,
            valid_to=valid_to,
            source_closet=source_closet,
            source_file=source_file,
            source_drawer_id=source_drawer_id,
        )
    )
    return {"success": True, "triple_id": triple_id, "fact": f"{subject} → {predicate} → {object}"}


def tool_kg_invalidate(subject: str, predicate: str, object: str, ended: str = None):
    """Mark a fact as no longer true.

    Returns the actual ``ended`` date/time that was stored. When the caller
    omits ``ended``, the underlying graph stamps ``date.today()`` and the
    response reflects that resolved value.

    Temporal values accept either ``YYYY-MM-DD`` or canonical UTC datetimes in
    the form ``YYYY-MM-DDTHH:MM:SSZ``.
    """
    try:
        subject = sanitize_kg_value(subject, "subject")
        predicate = sanitize_name(predicate, "predicate")
        object = sanitize_kg_value(object, "object")
        ended = sanitize_iso_temporal(ended, "ended")
    except ValueError as e:
        return {"success": False, "error": str(e)}

    resolved_ended = ended or date.today().isoformat()

    _wal_log(
        "kg_invalidate",
        {
            "subject": subject,
            "predicate": predicate,
            "object": object,
            "ended": resolved_ended,
        },
    )

    _call_kg(lambda kg: kg.invalidate(subject, predicate, object, ended=resolved_ended))
    return {
        "success": True,
        "fact": f"{subject} → {predicate} → {object}",
        "ended": resolved_ended,
    }


def tool_kg_timeline(entity: str = None):
    """Get chronological timeline of facts, optionally for one entity."""
    if entity is not None:
        try:
            entity = sanitize_kg_value(entity, "entity")
        except ValueError as e:
            return {"error": str(e)}
    results = _call_kg(lambda kg: kg.timeline(entity))
    return {"entity": entity or "all", "timeline": results, "count": len(results)}


def tool_kg_stats():
    """Knowledge graph overview: entities, triples, relationship types."""
    return _call_kg(lambda kg: kg.stats())


# ==================== AGENT DIARY ====================


def tool_diary_write(agent_name: str, entry: str, topic: str = "general", wing: str = ""):
    """
    Write a diary entry for this agent. Entries are timestamped and
    accumulate over time in a diary room.

    This is the agent's personal journal — observations, thoughts,
    what it worked on, what it noticed, what it thinks matters.

    Note: ``agent_name`` is normalized to lowercase before storage so
    that diary reads are case-insensitive (see #1243). "Claude",
    "claude", and "CLAUDE" all resolve to the same agent.
    """
    try:
        agent_name = sanitize_name(agent_name, "agent_name").lower()
        entry = sanitize_content(entry)
        topic = sanitize_name(topic, "topic")
    except ValueError as e:
        return {"success": False, "error": str(e)}

    if wing:
        wing = sanitize_name(wing)
    else:
        wing = f"wing_{agent_name.replace(' ', '_')}"
    room = "diary"
    col = _get_collection(create=True)
    if not col:
        return _no_palace()

    now = datetime.now()
    entry_id = (
        f"diary_{wing}_{now.strftime('%Y%m%d_%H%M%S%f')}_"
        f"{hashlib.sha256(entry.encode()).hexdigest()[:12]}"
    )

    _wal_log(
        "diary_write",
        {
            "agent_name": agent_name,
            "topic": topic,
            "entry_id": entry_id,
            "entry_preview": entry[:200],
        },
    )

    try:
        # TODO: Future versions should expand AAAK before embedding to improve
        # semantic search quality. For now, store raw AAAK in metadata so it's
        # preserved, and keep the document as-is for embedding (even though
        # compressed AAAK degrades embedding quality).
        col.add(
            ids=[entry_id],
            documents=[entry],
            metadatas=[
                {
                    "wing": wing,
                    "room": room,
                    "hall": "hall_diary",
                    "topic": topic,
                    "type": "diary_entry",
                    "agent": agent_name,
                    "filed_at": now.isoformat(),
                    "date": now.strftime("%Y-%m-%d"),
                }
            ],
        )
        logger.info(f"Diary entry: {entry_id} → {wing}/diary/{topic}")
        return {
            "success": True,
            "entry_id": entry_id,
            "agent": agent_name,
            "topic": topic,
            "timestamp": now.isoformat(),
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


def tool_diary_read(agent_name: str, last_n: int = 10, wing: str = ""):
    """
    Read an agent's recent diary entries. Returns the last N entries
    in chronological order — the agent's personal journal.

    When ``wing`` is provided, reads only from that wing. When ``wing``
    is empty or omitted, returns entries from every wing this agent has
    written to. Diary writes from hooks land in project-derived wings
    (``wing_<project>``), so requiring a specific wing on read would
    silo those entries from agent-initiated reads.

    Note: ``agent_name`` is normalized to lowercase before filtering so
    that reads are case-insensitive (see #1243). Entries written under
    pre-fix mixed-case agent names will not match the lowercase filter;
    use ``mempalace repair`` to migrate legacy data if needed.
    """
    try:
        agent_name = sanitize_name(agent_name, "agent_name").lower()
        if wing:
            wing = sanitize_name(wing)
    except ValueError as e:
        return {"error": str(e)}
    last_n = max(1, min(last_n, 100))
    col = _get_collection()
    if not col:
        return _no_palace()

    # Build filter: always scope by agent + room=diary. Wing is optional —
    # when empty, return entries across all wings for this agent (matches
    # the #1097 empty-string-as-no-filter convention for LLM ergonomics).
    conditions = [{"room": "diary"}, {"agent": agent_name}]
    if wing:
        conditions.insert(0, {"wing": wing})

    try:
        results = col.get(
            where={"$and": conditions},
            include=["documents", "metadatas"],
            limit=10000,
        )

        if not results["ids"]:
            return {"agent": agent_name, "entries": [], "message": "No diary entries yet."}

        # Combine and sort by timestamp
        entries = []
        for doc, meta in zip(results["documents"], results["metadatas"]):
            entries.append(
                {
                    "date": meta.get("date", ""),
                    "timestamp": meta.get("filed_at", ""),
                    "topic": meta.get("topic", ""),
                    "content": doc,
                }
            )

        entries.sort(key=lambda x: x["timestamp"], reverse=True)
        entries = entries[:last_n]

        return {
            "agent": agent_name,
            "entries": entries,
            "total": len(results["ids"]),
            "showing": len(entries),
        }
    except Exception:
        logger.exception("diary_read failed")
        return {"error": "Failed to read diary entries"}


def tool_hook_settings(silent_save: bool = None, desktop_toast: bool = None):
    """
    Get or set hook behavior settings.

    - silent_save: True = stop hook saves directly (no MCP clutter),
      False = legacy blocking MCP calls. Default: True.
    - desktop_toast: True = show notify-send desktop toast on save,
      False = terminal-only notification. Default: False.

    Call with no arguments to see current settings.
    """
    from .config import MempalaceConfig

    try:
        config = MempalaceConfig()
    except Exception as e:
        return {"success": False, "error": str(e)}

    changed = []
    if silent_save is not None:
        config.set_hook_setting("silent_save", silent_save)
        changed.append(f"silent_save → {silent_save}")
    if desktop_toast is not None:
        config.set_hook_setting("desktop_toast", desktop_toast)
        changed.append(f"desktop_toast → {desktop_toast}")

    # Re-read to return current state
    try:
        config = MempalaceConfig()
    except Exception:
        logger.debug("Could not re-read config after update", exc_info=True)

    result = {
        "success": True,
        "settings": {
            "silent_save": config.hook_silent_save,
            "desktop_toast": config.hook_desktop_toast,
        },
    }
    if changed:
        result["updated"] = changed
    return result


def tool_memories_filed_away():
    """Acknowledge the latest silent checkpoint. Returns a short summary."""
    state_dir = Path.home() / ".mempalace" / "hook_state"
    ack_file = state_dir / "last_checkpoint"
    if not ack_file.is_file():
        return {
            "status": "quiet",
            "message": "No recent journal entry",
            "count": 0,
            "timestamp": None,
        }
    try:
        data = json.loads(ack_file.read_text(encoding="utf-8"))
        ack_file.unlink(missing_ok=True)
        msgs = data.get("msgs", 0)
        return {
            "status": "ok",
            "message": f"\u2726 {msgs} messages tucked into drawers",
            "count": msgs,
            "timestamp": data.get("ts", None),
        }
    except (json.JSONDecodeError, OSError):
        ack_file.unlink(missing_ok=True)
        return {
            "status": "error",
            "message": "\u2726 Journal entry filed in the palace",
            "count": 0,
            "timestamp": None,
        }


# ==================== SETTINGS TOOLS ====================


def tool_reconnect():
    """Force the MCP server to drop cached ChromaDB + KnowledgeGraph state.

    Use after external scripts or CLI commands modify the palace database
    or replace ``knowledge_graph.sqlite3`` directly, which can leave the
    in-memory HNSW index stale or pin a closed-on-disk SQLite connection.
    """
    global \
        _client_cache, \
        _collection_cache, \
        _palace_db_inode, \
        _palace_db_mtime, \
        _vector_disabled, \
        _vector_disabled_reason
    from . import palace as palace_module

    close_errors = []
    try:
        palace_module._DEFAULT_BACKEND.close_palace(_config.palace_path)
    except Exception as exc:
        logger.debug("Failed to close shared palace backend during reconnect", exc_info=True)
        close_errors.append(f"backend close_palace failed: {exc}")
    try:
        from chromadb.api.client import SharedSystemClient

        clear_system_cache = getattr(SharedSystemClient, "clear_system_cache", None)
        if callable(clear_system_cache):
            clear_system_cache()
        else:
            logger.debug(
                "SharedSystemClient.clear_system_cache is unavailable; skipping shared Chroma cache clear during reconnect"
            )
    except Exception as exc:
        logger.debug(
            "Failed to clear Chroma shared system cache during reconnect",
            exc_info=True,
        )
        close_errors.append(f"shared Chroma cache clear failed: {exc}")
    _client_cache = None
    _collection_cache = None
    _palace_db_inode = 0
    _palace_db_mtime = 0.0
    # Force probe re-run on next _get_client by clearing the flag now;
    # _refresh_vector_disabled_flag will re-set it if the divergence
    # still applies after the reconnect.
    _vector_disabled = False
    _vector_disabled_reason = ""
    # Drain the per-path KnowledgeGraph cache so a replaced sqlite file is
    # reopened on the next tool call rather than served from a stale handle.
    with _kg_cache_lock:
        for kg in _kg_by_path.values():
            try:
                kg.close()
            except Exception:
                pass
        _kg_by_path.clear()
    try:
        col = _get_collection()
        if col is None:
            result = {
                "success": False,
                "message": "No palace found after reconnect",
                "drawers": 0,
                "vector_disabled": _vector_disabled,
            }
            if close_errors:
                result["error"] = "; ".join(close_errors)
            return result
        if close_errors:
            return {
                "success": False,
                "message": "Reconnect reopened the palace but failed to fully reset cached handles",
                "drawers": col.count(),
                "vector_disabled": _vector_disabled,
                "vector_disabled_reason": _vector_disabled_reason,
                "error": "; ".join(close_errors),
            }
        return {
            "success": True,
            "message": "Reconnected to palace",
            "drawers": col.count(),
            "vector_disabled": _vector_disabled,
            "vector_disabled_reason": _vector_disabled_reason,
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


# ==================== MCP PROTOCOL ====================

TOOLS = {
    "mempalace_status": {
        "description": "Palace overview — total drawers, wing and room counts",
        "input_schema": {"type": "object", "properties": {}},
        "handler": tool_status,
    },
    "mempalace_list_wings": {
        "description": "List all wings with drawer counts",
        "input_schema": {"type": "object", "properties": {}},
        "handler": tool_list_wings,
    },
    "mempalace_list_rooms": {
        "description": "List rooms within a wing (or all rooms if no wing given)",
        "input_schema": {
            "type": "object",
            "properties": {
                "wing": {"type": "string", "description": "Wing to list rooms for (optional)"},
            },
        },
        "handler": tool_list_rooms,
    },
    "mempalace_get_taxonomy": {
        "description": "Full taxonomy: wing → room → drawer count",
        "input_schema": {"type": "object", "properties": {}},
        "handler": tool_get_taxonomy,
    },
    "mempalace_get_aaak_spec": {
        "description": "Get the AAAK dialect specification — the compressed memory format MemPalace uses. Call this if you need to read or write AAAK-compressed memories.",
        "input_schema": {"type": "object", "properties": {}},
        "handler": tool_get_aaak_spec,
    },
    "mempalace_kg_query": {
        "description": "Query the knowledge graph for an entity's relationships. Returns typed facts with temporal validity. E.g. 'Max' → child_of Alice, loves chess, does swimming. Filter by date with as_of to see what was true at a point in time.",
        "input_schema": {
            "type": "object",
            "properties": {
                "entity": {
                    "type": "string",
                    "description": "Entity to query (e.g. 'Max', 'MyProject', 'Alice')",
                },
                "as_of": {
                    "type": "string",
                    "description": "Date/datetime filter — only facts valid at this time (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SSZ, optional)",
                },
                "direction": {
                    "type": "string",
                    "description": "outgoing (entity→?), incoming (?→entity), or both (default: both)",
                },
            },
            "required": ["entity"],
        },
        "handler": tool_kg_query,
    },
    "mempalace_kg_add": {
        "description": "Add a fact to the knowledge graph. Subject → predicate → object with optional time window. E.g. ('Max', 'started_school', 'Year 7', valid_from='2026-09-01'). Pass valid_to to backfill an already-ended historical fact in a single call.",
        "input_schema": {
            "type": "object",
            "properties": {
                "subject": {"type": "string", "description": "The entity doing/being something"},
                "predicate": {
                    "type": "string",
                    "description": "The relationship type (e.g. 'loves', 'works_on', 'daughter_of')",
                },
                "object": {"type": "string", "description": "The entity being connected to"},
                "valid_from": {
                    "type": "string",
                    "description": "When this became true (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SSZ, optional)",
                },
                "valid_to": {
                    "type": "string",
                    "description": "When this stopped being true (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SSZ, optional). Use for backfilling already-ended historical facts.",
                },
                "source_closet": {
                    "type": "string",
                    "description": "Closet ID where this fact appears (optional)",
                },
                "source_file": {
                    "type": "string",
                    "description": "Source file path the fact was extracted from (optional)",
                },
                "source_drawer_id": {
                    "type": "string",
                    "description": "Drawer ID the fact was extracted from (optional, RFC 002 provenance)",
                },
            },
            "required": ["subject", "predicate", "object"],
        },
        "handler": tool_kg_add,
    },
    "mempalace_kg_invalidate": {
        "description": "Mark a fact as no longer true. E.g. ankle injury resolved, job ended, moved house.",
        "input_schema": {
            "type": "object",
            "properties": {
                "subject": {"type": "string", "description": "Entity"},
                "predicate": {"type": "string", "description": "Relationship"},
                "object": {"type": "string", "description": "Connected entity"},
                "ended": {
                    "type": "string",
                    "description": "When it stopped being true (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SSZ, default: today)",
                },
            },
            "required": ["subject", "predicate", "object"],
        },
        "handler": tool_kg_invalidate,
    },
    "mempalace_kg_timeline": {
        "description": "Chronological timeline of facts. Shows the story of an entity (or everything) in order.",
        "input_schema": {
            "type": "object",
            "properties": {
                "entity": {
                    "type": "string",
                    "description": "Entity to get timeline for (optional — omit for full timeline)",
                },
            },
        },
        "handler": tool_kg_timeline,
    },
    "mempalace_kg_stats": {
        "description": "Knowledge graph overview: entities, triples, current vs expired facts, relationship types.",
        "input_schema": {"type": "object", "properties": {}},
        "handler": tool_kg_stats,
    },
    "mempalace_traverse": {
        "description": "Walk the palace graph from a room. Shows connected ideas across wings — the tunnels. Like following a thread through the palace: start at 'chromadb-setup' in wing_code, discover it connects to wing_myproject (planning) and wing_user (feelings about it).",
        "input_schema": {
            "type": "object",
            "properties": {
                "start_room": {
                    "type": "string",
                    "description": "Room to start from (e.g. 'chromadb-setup', 'riley-school')",
                },
                "max_hops": {
                    "type": "integer",
                    "description": "How many connections to follow (default: 2)",
                },
            },
            "required": ["start_room"],
        },
        "handler": tool_traverse_graph,
    },
    "mempalace_find_tunnels": {
        "description": "Find rooms that bridge two wings — the hallways connecting different domains. E.g. what topics connect wing_code to wing_team?",
        "input_schema": {
            "type": "object",
            "properties": {
                "wing_a": {"type": "string", "description": "First wing (optional)"},
                "wing_b": {"type": "string", "description": "Second wing (optional)"},
            },
        },
        "handler": tool_find_tunnels,
    },
    "mempalace_graph_stats": {
        "description": "Palace graph overview: total rooms, tunnel connections, edges between wings.",
        "input_schema": {"type": "object", "properties": {}},
        "handler": tool_graph_stats,
    },
    "mempalace_create_tunnel": {
        "description": "Create a cross-wing tunnel linking two palace locations. Use when content in one project relates to another — e.g., an API design in project_api connects to a database schema in project_database.",
        "input_schema": {
            "type": "object",
            "properties": {
                "source_wing": {"type": "string", "description": "Wing of the source"},
                "source_room": {"type": "string", "description": "Room in the source wing"},
                "target_wing": {"type": "string", "description": "Wing of the target"},
                "target_room": {"type": "string", "description": "Room in the target wing"},
                "label": {"type": "string", "description": "Description of the connection"},
                "source_drawer_id": {
                    "type": "string",
                    "description": "Optional specific drawer ID",
                },
                "target_drawer_id": {
                    "type": "string",
                    "description": "Optional specific drawer ID",
                },
            },
            "required": ["source_wing", "source_room", "target_wing", "target_room"],
        },
        "handler": tool_create_tunnel,
    },
    "mempalace_list_tunnels": {
        "description": "List all explicit cross-wing tunnels. Optionally filter by wing.",
        "input_schema": {
            "type": "object",
            "properties": {
                "wing": {
                    "type": "string",
                    "description": "Filter tunnels by wing (shows tunnels where wing is source or target)",
                },
            },
        },
        "handler": tool_list_tunnels,
    },
    "mempalace_delete_tunnel": {
        "description": "Delete an explicit tunnel by its ID.",
        "input_schema": {
            "type": "object",
            "properties": {
                "tunnel_id": {"type": "string", "description": "Tunnel ID to delete"},
            },
            "required": ["tunnel_id"],
        },
        "handler": tool_delete_tunnel,
    },
    "mempalace_follow_tunnels": {
        "description": "Follow tunnels from a room to see what it connects to in other wings. Returns connected rooms with drawer previews.",
        "input_schema": {
            "type": "object",
            "properties": {
                "wing": {"type": "string", "description": "Wing to start from"},
                "room": {"type": "string", "description": "Room to follow tunnels from"},
            },
            "required": ["wing", "room"],
        },
        "handler": tool_follow_tunnels,
    },
    "mempalace_search": {
        "description": "Semantic search. Returns verbatim drawer content with similarity scores. IMPORTANT: 'query' must contain ONLY search keywords. Use 'context' for background. Results with cosine distance > max_distance are filtered out.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Short search query ONLY — keywords or a question. Max 250 chars.",
                    "maxLength": 250,
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results (default 5)",
                    "minimum": 1,
                    "maximum": 100,
                },
                "wing": {"type": "string", "description": "Filter by wing (optional)"},
                "room": {"type": "string", "description": "Filter by room (optional)"},
                "max_distance": {
                    "type": "number",
                    "description": "Max cosine distance threshold (0=identical, 2=opposite). Results further than this are dropped. Lower = stricter. Default 1.5. Set to 0 to disable.",
                },
                "context": {
                    "type": "string",
                    "description": "Background context for the search (optional). NOT used for embedding — only for future re-ranking.",
                },
            },
            "required": ["query"],
        },
        "handler": tool_search,
    },
    "mempalace_check_duplicate": {
        "description": "Check if content already exists in the palace before filing",
        "input_schema": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "Content to check"},
                "threshold": {
                    "type": "number",
                    "description": "Similarity threshold 0-1 (default 0.9)",
                },
            },
            "required": ["content"],
        },
        "handler": tool_check_duplicate,
    },
    "mempalace_add_drawer": {
        "description": "File verbatim content into the palace. Checks for duplicates first.",
        "input_schema": {
            "type": "object",
            "properties": {
                "wing": {"type": "string", "description": "Wing (project name)"},
                "room": {
                    "type": "string",
                    "description": "Room (aspect: backend, decisions, meetings...)",
                },
                "content": {
                    "type": "string",
                    "description": "Verbatim content to store — exact words, never summarized",
                },
                "source_file": {"type": "string", "description": "Where this came from (optional)"},
                "added_by": {"type": "string", "description": "Who is filing this (default: mcp)"},
            },
            "required": ["wing", "room", "content"],
        },
        "handler": tool_add_drawer,
    },
    "mempalace_delete_drawer": {
        "description": "Delete a drawer by ID. Irreversible.",
        "input_schema": {
            "type": "object",
            "properties": {
                "drawer_id": {"type": "string", "description": "ID of the drawer to delete"},
            },
            "required": ["drawer_id"],
        },
        "handler": tool_delete_drawer,
    },
    "mempalace_sync": {
        "description": "Prune drawers whose source files are gitignored, deleted, or moved. Returns dry-run report by default; pass apply=true to commit deletions.",
        "input_schema": {
            "type": "object",
            "properties": {
                "project_dir": {
                    "type": "string",
                    "description": "Project root to scope the sync (optional; auto-detected from drawer metadata if omitted)",
                },
                "wing": {"type": "string", "description": "Limit to one wing (optional)"},
                "apply": {
                    "type": "boolean",
                    "description": "Actually delete drawers; default is dry-run preview",
                },
            },
        },
        "handler": tool_sync,
    },
    "mempalace_get_drawer": {
        "description": "Fetch a single drawer by ID — returns full content and metadata.",
        "input_schema": {
            "type": "object",
            "properties": {
                "drawer_id": {"type": "string", "description": "ID of the drawer to fetch"},
            },
            "required": ["drawer_id"],
        },
        "handler": tool_get_drawer,
    },
    "mempalace_list_drawers": {
        "description": "List drawers with pagination. Optional wing/room filter. Returns IDs, wings, rooms, content previews, and total matching count for pagination.",
        "input_schema": {
            "type": "object",
            "properties": {
                "wing": {"type": "string", "description": "Filter by wing (optional)"},
                "room": {"type": "string", "description": "Filter by room (optional)"},
                "limit": {
                    "type": "integer",
                    "description": "Max results per page (default 20, max 100)",
                    "minimum": 1,
                    "maximum": 100,
                },
                "offset": {
                    "type": "integer",
                    "description": "Offset for pagination (default 0)",
                    "minimum": 0,
                },
            },
        },
        "handler": tool_list_drawers,
    },
    "mempalace_update_drawer": {
        "description": "Update an existing drawer's content and/or metadata (wing, room). Fetches existing drawer first; returns error if not found.",
        "input_schema": {
            "type": "object",
            "properties": {
                "drawer_id": {"type": "string", "description": "ID of the drawer to update"},
                "content": {
                    "type": "string",
                    "description": "New content (optional — omit to keep existing)",
                },
                "wing": {
                    "type": "string",
                    "description": "New wing (optional — omit to keep existing)",
                },
                "room": {
                    "type": "string",
                    "description": "New room (optional — omit to keep existing)",
                },
            },
            "required": ["drawer_id"],
        },
        "handler": tool_update_drawer,
    },
    "mempalace_diary_write": {
        "description": "Write to your personal agent diary in AAAK format. Your observations, thoughts, what you worked on, what matters. Each agent has their own diary with full history. Write in AAAK for compression — e.g. 'SESSION:2026-04-04|built.palace.graph+diary.tools|ALC.req:agent.diaries.in.aaak|★★★'. Use entity codes from the AAAK spec.",
        "input_schema": {
            "type": "object",
            "properties": {
                "agent_name": {
                    "type": "string",
                    "description": "Your name — each agent gets their own diary wing",
                },
                "entry": {
                    "type": "string",
                    "description": "Your diary entry in AAAK format — compressed, entity-coded, emotion-marked",
                },
                "topic": {
                    "type": "string",
                    "description": "Topic tag (optional, default: general)",
                },
                "wing": {
                    "type": "string",
                    "description": "Target wing for this diary entry (optional). If omitted, uses wing_{agent_name}. Use this to write diary entries to a project wing instead of an agent-specific wing.",
                },
            },
            "required": ["agent_name", "entry"],
        },
        "handler": tool_diary_write,
    },
    "mempalace_diary_read": {
        "description": "Read your recent diary entries (in AAAK). See what past versions of yourself recorded — your journal across sessions.",
        "input_schema": {
            "type": "object",
            "properties": {
                "agent_name": {
                    "type": "string",
                    "description": "Your name — each agent gets their own diary wing",
                },
                "last_n": {
                    "type": "integer",
                    "description": "Number of recent entries to read (default: 10)",
                },
                "wing": {
                    "type": "string",
                    "description": "Wing to read diary entries from (optional). If omitted, reads from wing_{agent_name}.",
                },
            },
            "required": ["agent_name"],
        },
        "handler": tool_diary_read,
    },
    "mempalace_hook_settings": {
        "description": (
            "Get or set hook behavior. silent_save: True = save directly "
            "(no MCP clutter), False = legacy blocking. desktop_toast: "
            "True = show desktop notification. Call with no args to view."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "silent_save": {
                    "type": "boolean",
                    "description": "True = silent direct save, False = blocking MCP calls",
                },
                "desktop_toast": {
                    "type": "boolean",
                    "description": "True = show desktop toast via notify-send",
                },
            },
        },
        "handler": tool_hook_settings,
    },
    "mempalace_memories_filed_away": {
        "description": "Check if a recent palace checkpoint was saved. Returns message count and timestamp.",
        "input_schema": {"type": "object", "properties": {}},
        "handler": tool_memories_filed_away,
    },
    "mempalace_reconnect": {
        "description": (
            "Force reconnect to the palace database. Use after external scripts or CLI commands"
            " modified the palace directly, which can leave the in-memory HNSW index stale."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
        "handler": tool_reconnect,
    },
}


SUPPORTED_PROTOCOL_VERSIONS = [
    "2025-11-25",
    "2025-06-18",
    "2025-03-26",
    "2024-11-05",
]


def handle_request(request):
    if not isinstance(request, dict):
        return {
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": -32600, "message": "Invalid Request"},
        }
    method = request.get("method") or ""
    params = request.get("params") or {}
    req_id = request.get("id")

    if method == "initialize":
        client_version = params.get("protocolVersion", SUPPORTED_PROTOCOL_VERSIONS[-1])
        negotiated = (
            client_version
            if client_version in SUPPORTED_PROTOCOL_VERSIONS
            else SUPPORTED_PROTOCOL_VERSIONS[0]
        )
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": negotiated,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "mempalace", "version": __version__},
            },
        }
    elif method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}
    elif method.startswith("notifications/"):
        # Notifications (no id) never get a response per JSON-RPC spec
        return None
    elif method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "tools": [
                    {"name": n, "description": t["description"], "inputSchema": t["input_schema"]}
                    for n, t in TOOLS.items()
                ]
            },
        }
    elif method == "tools/call":
        if not isinstance(params, dict) or "name" not in params:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {
                    "code": -32602,
                    "message": "Invalid params: 'name' is required for tools/call",
                },
            }
        tool_name = params.get("name")
        tool_args = params.get("arguments") or {}
        if tool_name not in TOOLS:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Unknown tool: {tool_name}"},
            }
        # Whitelist arguments to declared schema properties only.
        # Prevents callers from spoofing internal params like added_by/source_file.
        # Skip filtering if handler explicitly accepts **kwargs (pass-through).
        # Default to filtering on inspect failure (safe fallback).
        import inspect

        schema_props = TOOLS[tool_name]["input_schema"].get("properties", {})
        try:
            handler = TOOLS[tool_name]["handler"]
            sig = inspect.signature(handler)
            accepts_var_keyword = any(
                p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
            )
        except (ValueError, TypeError):
            accepts_var_keyword = False
        if not accepts_var_keyword:
            tool_args = {k: v for k, v in tool_args.items() if k in schema_props}
        # Coerce argument types based on input_schema.
        # MCP JSON transport may deliver integers as floats or strings;
        # ChromaDB and Python slicing require native int.
        for key, value in list(tool_args.items()):
            prop_schema = schema_props.get(key, {})
            declared_type = prop_schema.get("type")
            try:
                if declared_type == "integer" and not isinstance(value, int):
                    tool_args[key] = int(value)
                elif declared_type == "number" and not isinstance(value, (int, float)):
                    tool_args[key] = float(value)
            except (ValueError, TypeError):
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32602, "message": f"Invalid value for parameter '{key}'"},
                }
        try:
            tool_args.pop("wait_for_previous", None)
            result = TOOLS[tool_name]["handler"](**tool_args)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [
                        {"type": "text", "text": json.dumps(result, indent=2, ensure_ascii=False)}
                    ]
                },
            }
        except Exception:
            logger.exception(f"Tool error in {tool_name}")
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32000, "message": "Internal tool error"},
            }

    # Notifications (missing id) must never get a response
    if req_id is None:
        return None
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Unknown method: {method}"},
    }


def _restore_stdout():
    """Restore real stdout for MCP JSON-RPC output (see issue #225)."""
    global _REAL_STDOUT, _REAL_STDOUT_FD
    if _REAL_STDOUT_FD is not None:
        try:
            os.dup2(_REAL_STDOUT_FD, 1)
            os.close(_REAL_STDOUT_FD)
        except OSError:
            pass
        _REAL_STDOUT_FD = None
    sys.stdout = _REAL_STDOUT


def main():
    _restore_stdout()
    # Force UTF-8 on stdio. MCP JSON-RPC is UTF-8, but Python on Windows
    # defaults stdin/stdout to the system codepage (e.g. cp1251), which
    # corrupts non-ASCII payloads and surfaces as generic -32000 errors on
    # Cyrillic/CJK content. See PEP 540.
    for stream in (sys.stdin, sys.stdout):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, OSError):
                pass
    logger.info("MemPalace MCP Server starting...")
    # Pre-flight: probe HNSW capacity before any tool call so the warning
    # is visible at startup rather than on first use (#1222). Pure
    # filesystem read; never opens a chromadb client.
    _refresh_vector_disabled_flag()
    while True:
        try:
            line = sys.stdin.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            request = json.loads(line)
            response = handle_request(request)
            if response is not None:
                sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
                sys.stdout.flush()
        except KeyboardInterrupt:
            break
        except Exception as e:
            logger.error(f"Server error: {e}")


if __name__ == "__main__":
    main()
