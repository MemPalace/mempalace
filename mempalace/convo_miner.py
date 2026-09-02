#!/usr/bin/env python3
"""
convo_miner.py — Mine conversations into the palace.

Ingests chat exports (Claude Code, ChatGPT, Slack, plain text transcripts).
Normalizes format, chunks by exchange pair (Q+A = one unit), files to palace.

Same palace as project mining. Different ingest strategy.
"""

import errno
import contextlib
import os
import sys
import json
import hashlib
import logging
import stat
import threading
from pathlib import Path
from datetime import datetime
from collections import defaultdict
from typing import Optional

from .backends import PalaceNotFoundError
from .collision_scan import assert_no_collisions
from .ids import (
    ID_RECIPE,
    make_convo_commit_id,
    make_convo_drawer_id,
    make_convo_generation_id,
    make_convo_sentinel_id,
    make_exchange_drawer_id,
)
from .normalize import normalize_conversations
from .entities import entities_metadata
from .palace import (
    NORMALIZE_VERSION,
    SKIP_DIRS,
    _metadata_matches_extract_mode,
    _validate_palace_fts5_after_mine,
    file_already_mined,
    get_collection,
    mine_lock,
    mine_palace_lock,
    prefetch_content_hashes,
    prefetch_mined_set,
)

logger = logging.getLogger("mempalace_mcp")
_mine_output_state = threading.local()


@contextlib.contextmanager
def mine_output_streams(stdout, stderr):
    """Route convo-miner progress for this thread without replacing sys streams."""
    missing = object()
    previous_stdout = getattr(_mine_output_state, "stdout", missing)
    previous_stderr = getattr(_mine_output_state, "stderr", missing)
    _mine_output_state.stdout = stdout
    _mine_output_state.stderr = stderr
    try:
        yield
    finally:
        if previous_stdout is missing:
            del _mine_output_state.stdout
        else:
            _mine_output_state.stdout = previous_stdout
        if previous_stderr is missing:
            del _mine_output_state.stderr
        else:
            _mine_output_state.stderr = previous_stderr


def _mine_print(*args, **kwargs):
    target = kwargs.get("file")
    if target is sys.stderr:
        kwargs["file"] = getattr(_mine_output_state, "stderr", sys.stderr)
    elif target is None:
        kwargs["file"] = getattr(_mine_output_state, "stdout", sys.stdout)
    print(*args, **kwargs)


def _access_read(access_gate):
    """Return the caller's shared backend gate, or a direct-mode no-op."""
    if access_gate is None:
        return contextlib.nullcontext()
    return access_gate.read_lock()


def _access_write(access_gate):
    """Return the caller's exclusive backend gate, or a direct-mode no-op."""
    if access_gate is None:
        return contextlib.nullcontext()
    return access_gate.write_lock()


# Cached hall keywords — avoids re-reading config per drawer
_HALL_KEYWORDS_CACHE = None


def _detect_hall_cached(content: str) -> str:
    """Route content to a hall using cached keywords. Same logic as miner.detect_hall."""
    global _HALL_KEYWORDS_CACHE
    if _HALL_KEYWORDS_CACHE is None:
        from .config import MempalaceConfig

        _HALL_KEYWORDS_CACHE = MempalaceConfig().hall_keywords
    content_lower = content[:3000].lower()
    scores = {}
    for hall, keywords in _HALL_KEYWORDS_CACHE.items():
        score = sum(1 for kw in keywords if kw in content_lower)
        if score > 0:
            scores[hall] = score
    return max(scores, key=scores.get) if scores else "general"


def file_conversation_exchange(
    collection,
    *,
    wing: str,
    room: str,
    text: str,
    source_file: str,
    agent: str,
    authored_at: Optional[str] = None,
    extra_metadata: Optional[dict] = None,
) -> Optional[str]:
    """File one verbatim conversation exchange as a single drawer.

    Canonical write path for live agent integrations (e.g. Hermes) and
    their backfills — both must route here so routing, normalization,
    and metadata conventions stay identical between live and historical
    ingest. Builds the same metadata the convo miner writes so hallway
    traversal, entity search, and since/before date filters see
    integration drawers exactly like mined ones.

    ``wing`` and ``room`` are validated with the same ``sanitize_name``
    rules the MCP write tools apply, but a failed name falls back
    (``wing_general`` / ``conversations``) instead of erroring: this
    path files *live* turns, and dropping a turn over a config typo
    would break the verbatim / 100%-recall promise. The fallback is
    logged at warning level so the misconfiguration is visible.

    ``extra_metadata`` lets callers append integration-specific fields
    (e.g. ``source`` / ``session_id``); keys that collide with the
    canonical fields are ignored, so it cannot be used to overwrite or
    drop them. Returns the drawer id, or None when ``text`` is empty
    after stripping.
    """
    from .config import sanitize_name

    text = (text or "").strip()
    if not text:
        return None
    try:
        wing = sanitize_name(wing, "wing")
    except ValueError:
        logger.warning(
            "file_conversation_exchange: invalid wing %r — filing under wing_general", wing
        )
        wing = "wing_general"
    try:
        room = sanitize_name(room, "room")
    except ValueError:
        logger.warning(
            "file_conversation_exchange: invalid room %r — filing under conversations", room
        )
        room = "conversations"
    filed_at = datetime.now().isoformat()
    drawer_id = make_exchange_drawer_id(wing, room, source_file, filed_at, text)
    metadata = {
        "wing": wing,
        "room": room,
        "hall": _detect_hall_cached(text),
        "source_file": source_file,
        "chunk_index": 0,
        "added_by": agent,
        "filed_at": filed_at,
        "entities": entities_metadata(text),
        "authored_at": authored_at if authored_at is not None else filed_at,
        "ingest_mode": "convos",
        "extract_mode": "exchange",
        "normalize_version": NORMALIZE_VERSION,
        "id_recipe": ID_RECIPE,
    }
    if extra_metadata:
        for key, value in extra_metadata.items():
            metadata.setdefault(key, value)
    collection.upsert(ids=[drawer_id], documents=[text], metadatas=[metadata])
    return drawer_id


# File types that might contain conversations
CONVO_EXTENSIONS = {
    ".txt",
    ".md",
    ".json",
    ".jsonl",
}

# Directories inside conversation sources that never hold conversations.
# ``tool-results``: Claude Code pages large tool outputs to
# ``<session>/tool-results/*.txt`` inside ``~/.claude/projects/<slug>/``.
# They are raw machine dumps referenced from the transcript JSONL — mining
# them stores megabytes of command output as "memories" (field measurement:
# 12.8k drawers from tool-results files on one palace; a single file
# produced 3.6k). Extends the generic SKIP_DIRS set for the convo scanner
# only — project mining semantics are unchanged.
CONVO_SKIP_DIRS = SKIP_DIRS | {"tool-results"}

MIN_CHUNK_SIZE = 30
CHUNK_SIZE = 800  # chars per drawer — align with miner.py
_LINE_GROUP_SIZE = 25  # lines per fallback group when no paragraph breaks
_LINE_FALLBACK_MIN_NEWLINES = 20  # trigger line-group fallback above this newline count
DRAWER_UPSERT_BATCH_SIZE = 1000
MAX_FILE_SIZE = 500 * 1024 * 1024  # 500 MB — skip files larger than this.
# Matches miner.py at 500 MB. Long Claude Code sessions, multi-year
# ChatGPT exports, and lifetime Slack dumps routinely exceed 10 MB; the
# cap at that level silently dropped them with `continue`. Per-drawer
# size is bounded by CHUNK_SIZE, but larger source files still produce
# more drawers and therefore more embedding/storage work — and content
# is normalized and loaded fully into memory before chunking, so memory
# use also scales with source size.


def _path_within_root(path: Path, root: Path) -> bool:
    try:
        path.expanduser().resolve().relative_to(root.expanduser().resolve())
        return True
    except (OSError, ValueError):
        return False


def _is_regular_source_file(filepath: Path, root: Path) -> bool:
    if not _path_within_root(filepath, root):
        return False
    # O_NONBLOCK keeps the S_ISREG verdict below reachable: a blocking open
    # of a FIFO waits in the kernel for a writer, so a named pipe called
    # ``session.jsonl`` would hang this check instead of failing it. See the
    # matching comment in ``miner._read_text_no_follow``, including why the
    # EAGAIN branch re-checks the type and then opens without the flag.
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = -1
    try:
        try:
            fd = os.open(filepath, flags)
        except OSError as exc:
            if exc.errno != errno.EAGAIN or not stat.S_ISREG(os.lstat(filepath).st_mode):
                raise
            fd = os.open(filepath, flags & ~getattr(os, "O_NONBLOCK", 0))
        st = os.fstat(fd)
        return stat.S_ISREG(st.st_mode) and st.st_size <= MAX_FILE_SIZE
    except OSError:
        return False
    finally:
        if fd != -1:
            try:
                os.close(fd)
            except OSError:
                pass


def _register_file(
    collection,
    source_file: str,
    wing: str,
    agent: str,
    extract_mode: str,
    content_hash: Optional[str] = None,
    access_gate=None,
):
    """Write a sentinel so file_already_mined() returns True for 0-chunk files.

    Without this, files that normalize to nothing or produce zero chunks are
    re-read and re-processed on every mine run because nothing was written to
    ChromaDB on the first pass.

    Stamps source_mtime like every real drawer does, so a file that later
    grows past the min-chunk-size floor (e.g. a short session that gets
    extended) is correctly detected as changed on the next mine instead of
    being skipped forever by this sentinel.

    Also used to register a file recognized as a content-duplicate of an
    already-mined transcript under a different path (see
    ``prefetch_content_hashes``) — stamping it here means the next run skips
    it via the cheap mtime check instead of re-normalizing and re-hashing it.
    """
    try:
        source_mtime = os.path.getmtime(source_file)
    except OSError:
        source_mtime = None
    sentinel_id = make_convo_sentinel_id(source_file, extract_mode)
    meta = {
        "wing": wing,
        "room": "_registry",
        "source_file": source_file,
        "added_by": agent,
        "filed_at": datetime.now().isoformat(),
        "ingest_mode": "registry",
        "extract_mode": extract_mode,
        "normalize_version": NORMALIZE_VERSION,
        "id_recipe": ID_RECIPE,
    }
    if source_mtime is not None:
        meta["source_mtime"] = source_mtime
    if content_hash is not None:
        meta["content_hash"] = content_hash
    with _access_write(access_gate):
        collection.upsert(
            documents=[f"[registry] {source_file}"],
            ids=[sentinel_id],
            metadatas=[meta],
        )


def _source_file_existing(collection, source_file: str, extract_mode: str) -> dict[str, dict]:
    """Map drawer_id -> stored metadata for one source file and extraction mode.

    Legacy conversation drawers did not carry extract_mode; treat those as
    exchange-mode rows so schema rebuilds can still clean them up without
    deleting newer general-mode drawers for the same transcript.
    """
    existing: dict[str, dict] = {}
    offset = 0
    while True:
        batch = collection.get(
            where={"source_file": source_file},
            limit=1000,
            offset=offset,
            include=["metadatas"],
        )
        batch_ids = batch.get("ids") or []
        metadatas = batch.get("metadatas") or []
        for drawer_id, meta in zip(batch_ids, metadatas):
            if (meta or {}).get("mine_commit_marker") is True:
                continue
            if _metadata_matches_extract_mode(meta or {}, extract_mode):
                existing[drawer_id] = meta or {}
        if not batch_ids:
            break
        offset += len(batch_ids)
    return existing


def _source_file_delete_ids(collection, source_file: str, extract_mode: str) -> list[str]:
    """Collect drawer IDs for one source file and extraction mode."""
    return list(_source_file_existing(collection, source_file, extract_mode))


# =============================================================================
# CHUNKING — exchange pairs for conversations
# =============================================================================


def chunk_exchanges(
    content: str,
    chunk_size: int = None,
    min_chunk_size: int = None,
) -> list:
    """
    Chunk by exchange pair: one > turn + AI response = one unit.
    Falls back to paragraph chunking if no > markers.

    Optional params override module-level defaults when provided.

    Raises ``ValueError`` if ``chunk_size`` is not a positive integer or
    ``min_chunk_size`` is negative. A non-positive ``chunk_size`` would
    cause ``_chunk_by_exchange`` below to loop forever — ``content[:0]``
    is empty, ``content[0:]`` is the whole string, and the remainder
    never shrinks.
    """
    if chunk_size is None:
        chunk_size = CHUNK_SIZE
    if min_chunk_size is None:
        min_chunk_size = MIN_CHUNK_SIZE

    if chunk_size <= 0:
        raise ValueError(f"chunk_size must be > 0, got {chunk_size}")
    if min_chunk_size < 0:
        raise ValueError(f"min_chunk_size must be >= 0, got {min_chunk_size}")

    lines = content.split("\n")
    quote_lines = sum(1 for line in lines if line.strip().startswith(">"))

    if quote_lines >= 3:
        return _chunk_by_exchange(lines, chunk_size, min_chunk_size)
    else:
        return _chunk_by_paragraph(content, chunk_size, min_chunk_size)


def _chunk_by_exchange(lines: list, chunk_size: int, min_chunk_size: int) -> list:
    """One user turn (>) + the AI response that follows = one or more chunks.

    The full AI response is preserved verbatim.  When the combined
    user-turn + response exceeds chunk_size the response is split across
    consecutive drawers so nothing is silently discarded.
    """
    chunks = []
    i = 0

    while i < len(lines):
        line = lines[i]
        if line.strip().startswith(">"):
            user_turn = line.strip()
            i += 1

            ai_lines = []
            while i < len(lines):
                next_line = lines[i]
                if next_line.strip().startswith(">") or next_line.strip().startswith("---"):
                    break
                # Preserve the line as-is — blank lines and indentation carry meaning
                # (paragraph breaks, list/code structure) and must survive verbatim.
                ai_lines.append(next_line)
                i += 1

            # Join on newline (not space) so line structure, blank lines, and
            # indentation reach the drawer unchanged. Trim only trailing blank
            # lines produced by the loop stopping at the next `>` turn.
            ai_response = "\n".join(ai_lines).rstrip("\n")
            content = f"{user_turn}\n{ai_response}" if ai_response else user_turn

            _emit_bounded(chunks, content, chunk_size, min_chunk_size)
        else:
            i += 1

    return chunks


def _emit_bounded(
    chunks: list,
    content: str,
    chunk_size: int,
    min_chunk_size: int,
) -> None:
    """Append ``content`` as one or more drawers, none exceeding ``chunk_size``.

    The ``min_chunk_size`` floor gates the WHOLE call (drops the input if
    its stripped length is at or below the floor, treated as noise). Once
    the input passes the floor, every slice is emitted verbatim so a
    small trailing remainder is preserved instead of silently dropped.
    The index-based loop avoids the O(N^2) repeated-substring allocation
    of a ``while content: content = content[chunk_size:]`` shape.
    """
    if len(content.strip()) <= min_chunk_size:
        return
    for i in range(0, len(content), chunk_size):
        chunks.append({"content": content[i : i + chunk_size], "chunk_index": len(chunks)})


def _chunk_by_paragraph(content: str, chunk_size: int, min_chunk_size: int) -> list:
    """Fallback: chunk by paragraph breaks."""
    chunks = []
    paragraphs = [p.strip() for p in content.split("\n\n") if p.strip()]

    # If no paragraph breaks and long content, chunk by line groups
    if len(paragraphs) <= 1 and content.count("\n") > _LINE_FALLBACK_MIN_NEWLINES:
        lines = content.split("\n")
        for i in range(0, len(lines), _LINE_GROUP_SIZE):
            group = "\n".join(lines[i : i + _LINE_GROUP_SIZE]).strip()
            _emit_bounded(chunks, group, chunk_size, min_chunk_size)
        return chunks

    for para in paragraphs:
        _emit_bounded(chunks, para, chunk_size, min_chunk_size)

    return chunks


# =============================================================================
# ROOM DETECTION — topic-based for conversations
# =============================================================================

TOPIC_KEYWORDS = {
    "technical": [
        "code",
        "python",
        "function",
        "bug",
        "error",
        "api",
        "database",
        "server",
        "deploy",
        "git",
        "test",
        "debug",
        "refactor",
    ],
    "architecture": [
        "architecture",
        "design",
        "pattern",
        "structure",
        "schema",
        "interface",
        "module",
        "component",
        "service",
        "layer",
    ],
    "planning": [
        "plan",
        "roadmap",
        "milestone",
        "deadline",
        "priority",
        "sprint",
        "backlog",
        "scope",
        "requirement",
        "spec",
    ],
    "decisions": [
        "decided",
        "chose",
        "picked",
        "switched",
        "migrated",
        "replaced",
        "trade-off",
        "alternative",
        "option",
        "approach",
    ],
    "problems": [
        "problem",
        "issue",
        "broken",
        "failed",
        "crash",
        "stuck",
        "workaround",
        "fix",
        "solved",
        "resolved",
    ],
}


def detect_convo_room(content: str) -> str:
    """Score conversation content against topic keywords."""
    content_lower = content[:3000].lower()
    scores = {}
    for room, keywords in TOPIC_KEYWORDS.items():
        score = sum(1 for kw in keywords if kw in content_lower)
        if score > 0:
            scores[room] = score
    if scores:
        return max(scores, key=scores.get)
    return "general"


# =============================================================================
# PALACE OPERATIONS
# =============================================================================


# =============================================================================
# SCAN FOR CONVERSATION FILES
# =============================================================================


def scan_convos(convo_dir: str, include_subagents: bool = False) -> list:
    """Find all potential conversation files.

    Skips symlinks and oversized files. Each skipped symlink is logged to
    ``sys.stderr`` with a ``  SKIP: <relative-path> (symlink)`` line so the
    caller can tell why an apparent conversation directory yielded no files.

    By default, directories named ``subagents`` are skipped: Claude Code
    records Explore/Plan/Grep subagent transcripts there, and on typical
    workspaces they outnumber main session files by one to two orders of
    magnitude. Pass ``include_subagents=True`` to mine them anyway.

    The match is case-insensitive on the directory name only (``subagents``
    or ``Subagents``), so directories like ``mysubagents`` or
    ``subagentsbackup`` are not affected.
    """
    # A direct conversation file is a valid source. For a file, feed only
    # its basename through the existing directory validation loop.
    requested_path = Path(convo_dir).expanduser()
    single_file = requested_path.is_file()
    convo_path = (requested_path.parent if single_file else requested_path).resolve()
    scan_entries = (
        [(str(convo_path), [], [requested_path.name])] if single_file else os.walk(convo_path)
    )
    files = []
    for root, dirs, filenames in scan_entries:
        dirs[:] = [
            d
            for d in dirs
            if d not in CONVO_SKIP_DIRS and (include_subagents or d.lower() != "subagents")
        ]
        for filename in filenames:
            if filename.endswith(".meta.json"):
                continue
            filepath = Path(root) / filename
            if filepath.suffix.lower() in CONVO_EXTENSIONS:
                # Skip symlinks and oversized files
                if filepath.is_symlink():
                    rel = filepath.relative_to(convo_path).as_posix()
                    try:
                        _mine_print(f"  SKIP: {rel} (symlink)", file=sys.stderr)
                    except OSError:
                        pass
                    continue
                # Skip files exceeding size limit, or those whose stat() raises
                # (permission denied, racing delete, broken symlink that
                # survived the earlier is_symlink check). Both branches log
                # to stderr to match the SKIP: (symlink) line above; silent
                # drops at this gate were the original #923 complaint.
                try:
                    file_stat = filepath.stat()
                    # Drop non-regular entries (FIFO, socket, device node)
                    # before any reader touches them — see the matching
                    # gate in ``miner.scan_project``.
                    if not stat.S_ISREG(file_stat.st_mode):
                        _mine_print(
                            f"  SKIP: {filepath.name} (not a regular file)",
                            file=sys.stderr,
                        )
                        continue
                    file_size = file_stat.st_size
                    if file_size > MAX_FILE_SIZE:
                        _mine_print(
                            f"  SKIP: {filepath.name} ({file_size / (1024 * 1024):.1f} MB)"
                            f" exceeds {MAX_FILE_SIZE // (1024 * 1024)} MB limit",
                            file=sys.stderr,
                        )
                        continue
                except OSError as exc:
                    # Prefer ``exc.strerror`` so the path isn't duplicated in
                    # the output (see the matching comment in
                    # ``miner.scan_project``).
                    _mine_print(
                        f"  SKIP: {filepath.name} (stat error: {exc.strerror or exc})",
                        file=sys.stderr,
                    )
                    continue
                if not _is_regular_source_file(filepath, convo_path):
                    continue
                files.append(filepath)
    return files


# =============================================================================
# MINE CONVERSATIONS
# =============================================================================


def _extract_authored_at(filepath):
    """Most-recent message timestamp in a transcript, used as the drawer's authored date.

    Both Claude Code and Codex JSONL transcripts carry a top-level ISO-8601
    ``timestamp`` on each line. We take the max so ``authored_at`` reflects when the
    content was actually written, independent of when it was mined (``filed_at``).
    This restores chronology: a session from days ago keeps its real date even when
    re-mined today, instead of every drawer collapsing to ingest time. Returns None
    for formats without per-line timestamps (e.g. plain ``.md``).
    """
    path = Path(filepath)
    if path.suffix != ".jsonl":
        return None
    latest = None
    try:
        with path.open(encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    ts = json.loads(line).get("timestamp")
                except (ValueError, TypeError, AttributeError):
                    continue
                # ISO-8601 timestamps are strings; guard against a non-string
                # ``timestamp`` so a malformed line can't raise TypeError on compare.
                if isinstance(ts, str) and (latest is None or ts > latest):
                    latest = ts
    except OSError:
        return None
    return latest


def _publish_changed_generations(
    collection,
    *,
    final_metadata,
    stale_ids,
    commit_id,
    commit_metadata,
    source_file,
    access_gate=None,
) -> bool:
    """Atomically publish a staged token, finalize rows, and retire old ones."""
    try:
        with _access_write(access_gate):
            # One marker upsert is the crash-atomic visibility switch. Before
            # it, every new generation is hidden. After it, the complete staged
            # token is searchable even if the process dies during metadata
            # finalization or stale-row cleanup. ``cleanup_pending`` keeps the
            # source retryable until every follow-up write succeeds.
            collection.upsert(
                ids=[commit_id],
                documents=[f"[conversation generation commit] {source_file}"],
                metadatas=[commit_metadata],
            )
            for batch_start in range(0, len(final_metadata), DRAWER_UPSERT_BATCH_SIZE):
                batch = final_metadata[batch_start : batch_start + DRAWER_UPSERT_BATCH_SIZE]
                collection.update(
                    ids=[drawer_id for drawer_id, _ in batch],
                    metadatas=[meta for _, meta in batch],
                )
            if stale_ids:
                collection.delete(ids=stale_ids)
            completed_marker = dict(commit_metadata)
            completed_marker["mine_cleanup_pending"] = False
            collection.update(ids=[commit_id], metadatas=[completed_marker])
        return True
    except Exception:
        logger.warning(
            "Failed to publish changed generations or delete %d orphaned "
            "convo drawers for %s; leaving the source incomplete so the "
            "next mine retries",
            len(stale_ids),
            source_file,
            exc_info=True,
        )
    return False


def _file_chunks_locked(
    collection,
    source_file,
    chunks,
    wing,
    room,
    agent,
    extract_mode,
    authored_at=None,
    content_hash=None,
    access_gate=None,
):
    """Lock the source file and file its chunks incrementally.

    Combines the per-file serialization that prevents concurrent agents from
    duplicating work (via mine_lock) with an incremental re-mine contract
    (#2403): logical drawer ids are deterministic over
    ``(source_file, extract_mode, chunk_index)``, so a re-mine of a changed
    source — a Claude Code session appends to its own transcript every turn,
    and /compact or /clear can rewrite one in place — only re-embeds the
    chunks whose content actually changed. A changed chunk is staged under a
    content-addressed physical generation instead of overwriting its old
    logical position. Unchanged chunks get a cheap metadata-only refresh
    (``source_mtime`` / ``chunk_total``). Existing generations are deleted
    only after every new batch succeeds, so a crash mid-operation leaves the
    prior verbatim set untouched; a crash after staging leaves a retryable
    incomplete group that reuses the staged embeddings (#2183).

    Returns (drawers_added, room_counts_delta, skipped) where drawers_added
    counts only new/changed drawers actually upserted this pass.
    """
    room_counts_delta: dict = defaultdict(int)
    drawers_added = 0
    with mine_lock(source_file):
        commit_id = make_convo_commit_id(source_file, extract_mode)
        # Re-check after lock — another agent may have just finished this file
        # at the current schema/mtime. A stale hit here returns False, so we
        # still fall through to the incremental path below.
        try:
            with _access_read(access_gate):
                marker_result = collection.get(ids=[commit_id], include=["metadatas"])
                pending_cleanup = any(
                    (meta or {}).get("mine_cleanup_pending") is True
                    for meta in (marker_result.get("metadatas") or [])
                )
                if not pending_cleanup and file_already_mined(
                    collection, source_file, check_mtime=True, extract_mode=extract_mode
                ):
                    return 0, room_counts_delta, True
        except Exception:
            logger.warning("Conversation mine re-check failed for %s", source_file, exc_info=True)
            return 0, room_counts_delta, True

        # Snapshot what the palace already holds for this source+mode. A
        # failed snapshot must abort this file's mine attempt rather than
        # fall through to a blind full upsert: without the snapshot the pass
        # cannot tell changed chunks from unchanged ones or find orphaned
        # ids, and would degrade to the very purge/rebuild churn this path
        # exists to avoid (#105 — convo_miner's own instance of the same
        # swallow already fixed for miner.py at #23). Returning here leaves
        # the old drawers' stored mtime untouched, so the next mine still
        # sees a mismatch and retries.
        try:
            with _access_read(access_gate):
                existing = _source_file_existing(collection, source_file, extract_mode)
        except Exception as exc:
            _mine_print(
                f"  ! [skip] existing-drawer snapshot failed for {source_file!r} "
                f"({exc!r}); leaving existing drawers untouched, will retry "
                f"on the next mine",
                file=sys.stderr,
            )
            logger.debug("Existing-drawer snapshot failed for %s", source_file, exc_info=True)
            return 0, room_counts_delta, True

        # One filed_at per source file so all transcript drawers of a pass
        # share an ingest timestamp.
        #
        # Every drawer of this pass carries ``chunk_total`` so
        # ``file_already_mined`` / ``prefetch_mined_set`` can tell a complete
        # multi-batch mine from one that crashed mid-file (#2183). Without it
        # a stable mtime + any surviving drawer permanently skips the file
        # and the missing exchanges never come back.
        filed_at = datetime.now().isoformat()
        try:
            source_mtime = os.path.getmtime(source_file)
        except OSError:
            source_mtime = None
        chunk_total = len(chunks)

        # Partition the target set: a chunk whose drawer already exists with
        # the same content hash at the current schema keeps its embedding and
        # only needs its completion metadata refreshed; everything else is
        # (re-)upserted. Drawers written before ``chunk_hash`` existed have no
        # hash to compare, count as changed once, and migrate themselves.
        # Physical ids can differ from their stable logical ids after a
        # changed-content re-mine. Group every existing generation by logical
        # position so a crash-left staging generation can be resumed without
        # re-embedding it, while legacy deterministic ids remain compatible.
        existing_by_logical: dict[str, list[tuple[str, dict]]] = defaultdict(list)
        for physical_id, stored_meta in existing.items():
            logical_id = stored_meta.get("logical_drawer_id")
            if not isinstance(logical_id, str) or not logical_id:
                logical_id = physical_id
            existing_by_logical[logical_id].append((physical_id, stored_meta))

        to_upsert: list = []  # (physical_id, content, final_meta)
        to_touch: list = []  # (physical_id, refreshed_meta)
        new_ids: set = set()
        for chunk in chunks:
            chunk_room = chunk.get("memory_type", room) if extract_mode == "general" else room
            if extract_mode == "general":
                room_counts_delta[chunk_room] += 1
            logical_drawer_id = make_convo_drawer_id(
                wing, chunk_room, source_file, extract_mode, chunk["chunk_index"]
            )
            chunk_hash = hashlib.sha256(chunk["content"].encode("utf-8")).hexdigest()
            candidates = existing_by_logical.get(logical_drawer_id, [])
            current = next(
                (
                    (physical_id, stored_meta)
                    for physical_id, stored_meta in candidates
                    if stored_meta.get("chunk_hash") == chunk_hash
                    and stored_meta.get("normalize_version", 1) >= NORMALIZE_VERSION
                ),
                None,
            )
            if current is not None:
                physical_id, prev = current
                new_ids.add(physical_id)
                touched = dict(prev)
                touched["logical_drawer_id"] = logical_drawer_id
                touched["mine_staged"] = False
                touched["chunk_total"] = chunk_total
                if source_mtime is not None:
                    touched["source_mtime"] = source_mtime
                else:
                    # A stale stored mtime with no current one to replace it
                    # would let an old group satisfy a future completion check.
                    touched.pop("source_mtime", None)
                # Refresh the chunk-0 conversation-hash stamp so cross-file
                # dedup keeps seeing conversations added since the last pass.
                if content_hash is not None and chunk.get("chunk_index", 0) == 0:
                    touched["content_hash"] = content_hash
                to_touch.append((physical_id, touched))
                continue
            physical_id = (
                make_convo_generation_id(logical_drawer_id, chunk_hash)
                if candidates
                else logical_drawer_id
            )
            new_ids.add(physical_id)
            meta = {
                "wing": wing,
                "room": chunk_room,
                "hall": _detect_hall_cached(chunk["content"]),
                "source_file": source_file,
                "chunk_index": chunk["chunk_index"],
                "added_by": agent,
                "filed_at": filed_at,
                "entities": entities_metadata(chunk["content"]),
                "authored_at": authored_at if authored_at is not None else filed_at,
                "ingest_mode": "convos",
                "extract_mode": extract_mode,
                "normalize_version": NORMALIZE_VERSION,
                "id_recipe": ID_RECIPE,
                "logical_drawer_id": logical_drawer_id,
                "mine_staged": False,
                "chunk_total": chunk_total,
                "chunk_hash": chunk_hash,
            }
            if source_mtime is not None:
                meta["source_mtime"] = source_mtime
            # Stamp content_hash only on chunk 0 so multi-conversation
            # privacy-export hashes are not O(N²)-duplicated across every
            # chunk row. ``prefetch_content_hashes`` still finds them —
            # it scans all drawers and splits comma-joined hash fields.
            if content_hash is not None and chunk.get("chunk_index", 0) == 0:
                meta["content_hash"] = content_hash
            to_upsert.append((physical_id, chunk["content"], meta))

        # A shrink/rewrite needs a two-phase completion marker. New/changed
        # rows are first written without the current source mtime; unchanged
        # rows keep their old mtime. Only after orphan cleanup succeeds do we
        # stamp the whole target set current. A transient delete failure then
        # remains visibly incomplete and retries even if the source never
        # changes again.
        stale_ids = [drawer_id for drawer_id in existing if drawer_id not in new_ids]
        generation_token = hashlib.sha256("\0".join(sorted(new_ids)).encode()).hexdigest()
        commit_metadata = {
            "wing": wing,
            "room": "_registry",
            "source_file": source_file,
            "extract_mode": extract_mode,
            "ingest_mode": "registry",
            "normalize_version": NORMALIZE_VERSION,
            "id_recipe": ID_RECIPE,
            "mine_staged": True,
            "mine_commit_marker": True,
            "mine_generation_commit": generation_token,
            "mine_cleanup_pending": True,
        }

        # Batch into bounded requests so large transcripts keep most of the
        # embedding speedup without one huge Chroma/SQLite request.
        #
        # No cleanup on failure: nothing was purged, so everything the palace
        # held before this pass is still there, and the partially written
        # pass leaves mixed mtime groups that each fall short of their
        # ``chunk_total`` — ``file_already_mined`` stays False and the next
        # mine retries (#2183 / #2403).
        for batch_start in range(0, len(to_upsert), DRAWER_UPSERT_BATCH_SIZE):
            batch = to_upsert[batch_start : batch_start + DRAWER_UPSERT_BATCH_SIZE]
            batch_ids = [drawer_id for drawer_id, _, _ in batch]
            batch_docs = [content for _, content, _ in batch]
            if stale_ids:
                batch_metas = []
                for _, _, final_meta in batch:
                    incomplete_meta = dict(final_meta)
                    incomplete_meta.pop("source_mtime", None)
                    incomplete_meta["mine_staged"] = True
                    incomplete_meta["mine_generation_token"] = generation_token
                    batch_metas.append(incomplete_meta)
            else:
                batch_metas = [meta for _, _, meta in batch]
            try:
                with _access_write(access_gate):
                    assert_no_collisions(list(zip(batch_ids, batch_metas)), collection)
                    collection.upsert(
                        documents=batch_docs,
                        ids=batch_ids,
                        metadatas=batch_metas,
                    )
                drawers_added += len(batch_docs)
            except Exception as e:
                if "already exists" not in str(e).lower():
                    raise
        if not stale_ids and not pending_cleanup:
            for batch_start in range(0, len(to_touch), DRAWER_UPSERT_BATCH_SIZE):
                batch = to_touch[batch_start : batch_start + DRAWER_UPSERT_BATCH_SIZE]
                with _access_write(access_gate):
                    collection.update(
                        ids=[drawer_id for drawer_id, _ in batch],
                        metadatas=[meta for _, meta in batch],
                    )

        # Delete orphaned drawers LAST — ids the current source no longer
        # produces (shrunk/rewritten file, id recipe migration). Deleting
        # them only after the full new set is in place means an interrupted
        # pass never leaves the palace with less than it had; the worst
        # crash window leaves transient duplicates. The current-mtime marker
        # is deliberately withheld until cleanup succeeds, so the next mine
        # retries even when the source itself stays unchanged.
        if stale_ids or pending_cleanup:
            final_metadata = []
            for drawer_id, meta in to_touch + [
                (item_id, item_meta) for item_id, _, item_meta in to_upsert
            ]:
                published_meta = dict(meta)
                published_meta["mine_staged"] = False
                published_meta["mine_generation_token"] = generation_token
                final_metadata.append((drawer_id, published_meta))
            if not _publish_changed_generations(
                collection,
                final_metadata=final_metadata,
                stale_ids=stale_ids,
                commit_id=commit_id,
                commit_metadata=commit_metadata,
                source_file=source_file,
                access_gate=access_gate,
            ):
                return drawers_added, room_counts_delta, True
    return drawers_added, room_counts_delta, False


def _is_ai_tool_path(path: Path) -> bool:
    """Return True when `path` lives inside a known AI-tool storage dir.

    Detected paths (exact-segment match — substrings like `.gemini-backup`
    or `.codex-archive` do NOT match):
      - any segment ``.codex`` (Codex CLI sessions / archives)
      - any segment ``.gemini`` (Gemini CLI sessions under ~/.gemini/tmp/...)
      - the consecutive segment pair ``.claude/projects`` (Claude Code).
        ``.claude`` alone is NOT matched — that is the settings/config dir,
        not a conversation source.

    Used by ``_resolve_wing`` to default the destination wing to
    ``wing_api`` when the user hasn't passed an explicit ``--wing``.
    """
    try:
        parts = path.resolve().parts
    except (OSError, RuntimeError):
        return False

    if ".codex" in parts:
        return True
    if ".gemini" in parts:
        return True
    for i in range(len(parts) - 1):
        if parts[i] == ".claude" and parts[i + 1] == "projects":
            return True
    return False


def _split_new_and_duplicate_conversations(
    conversations: list,
    wing: str,
    source_file: str,
    mined_content_hashes: dict,
) -> tuple:
    """Hash each conversation and split them into (new, duplicate) lists.

    A conversation is a duplicate when its hash is already registered under
    a *different* source_file in the same wing — mining the same transcript
    into a second wing is a deliberate re-file, not a repeat, so the lookup
    is scoped to (wing, hash). Returns ([(hash, text), ...] new, [(hash,
    dup_source_file), ...] duplicates).
    """
    new_items = []
    duplicates = []
    for conversation in conversations:
        content_hash = hashlib.sha256(conversation.strip().encode("utf-8")).hexdigest()
        dup_source = mined_content_hashes.get((wing, content_hash))
        if dup_source is None or dup_source == source_file:
            new_items.append((content_hash, conversation))
        else:
            duplicates.append((content_hash, dup_source))
    return new_items, duplicates


def _is_unchanged_since_last_mine(source_file: str, mined_mtimes: dict) -> bool:
    """True iff source_file was mined at the current schema AND its on-disk
    mtime still matches what was stored -- the mtime-aware replacement for
    "we've seen this source_file before" (transcripts are not immutable).

    False (re-mine) whenever the file isn't in mined_mtimes at all, its
    stored mtime is None (never recorded -- pre-mtime-tracking drawer, or
    getmtime failed when it was written), or getmtime fails right now
    (treat as changed rather than silently trusting stale data).
    """
    if source_file not in mined_mtimes:
        return False
    stored_mtime = mined_mtimes[source_file]
    if stored_mtime is None:
        return False
    try:
        current_mtime = os.path.getmtime(source_file)
    except OSError:
        return False
    return abs(stored_mtime - current_mtime) < 0.001


def _resolve_wing(convo_path: Path, wing: Optional[str]) -> str:
    """Determine the destination wing for ``mine_convos``.

    Precedence (first match wins):

      1. Explicit ``wing`` argument from the user — always wins, even on
         an AI-tool path. Empty string is treated as "no wing".
      2. AI-tool path detection — defaults to ``wing_api`` so Claude
         Code / Codex / Gemini conversations group under a single wing
         dedicated to API-sourced content.
      3. Basename fallback — sanitized via ``config.normalize_wing_name``
         (lowercase, spaces/hyphens collapsed to underscores). Shared
         single source of truth with ``cmd_init``,
         ``room_detector_local``, and ``miner.load_config`` so all
         wing-slug producers stay in sync (per #1194 consolidation).
    """
    from .config import normalize_wing_name

    if wing:
        return wing
    if _is_ai_tool_path(convo_path):
        return "wing_api"
    return normalize_wing_name(convo_path.name)


def mine_convos(
    convo_dir: str,
    palace_path: str,
    wing: str = None,
    agent: str = "mempalace",
    limit: int = 0,
    dry_run: bool = False,
    extract_mode: str = "exchange",
    include_subagents: bool = False,
    access_gate=None,
):
    """Mine a directory of conversation files into the palace.

    extract_mode:
        "exchange" — default exchange-pair chunking (Q+A = one unit)
        "general"  — general extractor: decisions, preferences, milestones, problems, emotions
    include_subagents:
        False (default) — skip Claude Code ``subagents/`` directories
        True            — also mine subagent transcripts

    The real work is in :func:`_mine_convos_impl`; this wrapper holds the
    per-palace flock around it so two concurrent ``mempalace mine --mode
    convos`` invocations against the same palace can't pile up. This
    mirrors the pattern in :func:`mempalace.miner.mine`. The lock is
    non-blocking: ``MineAlreadyRunning`` propagates to the CLI (which
    renders a holder-aware message and exits non-zero) or to in-process
    callers that expect to coexist with another writer.

    Dry-run skips the lock — it never writes to the palace and so cannot
    corrupt anything, and skipping the lock lets dry-run probes coexist
    with a live mine.

    ``access_gate`` is an optional Hub-supplied readers/writer gate. Direct
    CLI/library calls leave it unset. The HTTP Hub uses it to share Chroma
    reads with peer searches and to bound exclusive access to individual
    upsert/update/delete batches while a separate mutation lock preserves the
    one-writer lifecycle across the complete crash-safe re-mine (#2403).

    Chunking parameters (chunk_size, min_chunk_size) are read from
    MempalaceConfig inside :func:`_mine_convos_impl` so `config.json`
    governs both this path and the project-file miner in `miner.py`.
    """
    if dry_run:
        return _mine_convos_impl(
            convo_dir,
            palace_path,
            wing=wing,
            agent=agent,
            limit=limit,
            dry_run=dry_run,
            extract_mode=extract_mode,
            include_subagents=include_subagents,
            access_gate=access_gate,
        )

    with mine_palace_lock(palace_path):
        return _mine_convos_impl(
            convo_dir,
            palace_path,
            wing=wing,
            agent=agent,
            limit=limit,
            dry_run=dry_run,
            extract_mode=extract_mode,
            include_subagents=include_subagents,
            access_gate=access_gate,
        )


def _compute_hallways_for_wing_safe(wing, collection, drawers_filed, config=None):
    """Auto-populate the associative graph from the entities just mined.

    Best-effort: hallway computation must never fail an otherwise-good mine, and is
    skipped when nothing new was filed.
    """
    if drawers_filed <= 0:
        return
    try:
        from .hallways import compute_hallways_for_wing

        compute_hallways_for_wing(wing, col=collection, config=config)
    except Exception as exc:
        _mine_print(f"  (hallways skipped: {exc})")


def _normalize_convo_conversations(
    filepath: Path,
    source_file: str,
    cfg_min_chunk_size: int,
    collection,
    wing: str,
    agent: str,
    extract_mode: str,
    dry_run: bool,
    access_gate=None,
) -> Optional[list]:
    """Normalize a transcript file into its individual conversations,
    registering it as filed when there's nothing worth mining. Returns None
    when the caller should skip the file (normalize failed, or normalized
    content is too short to chunk).

    Kept as separate conversations rather than joined into one string so
    dedup can hash and skip per conversation — a Claude.ai privacy export
    bundles every conversation into a single file, and hashing the joined
    bundle means one new conversation added to a re-export changes the
    whole-file hash and hides the conversations that didn't change.
    """
    try:
        conversations = [c for c in normalize_conversations(str(filepath)) if c]
    except (OSError, ValueError):
        if not dry_run:
            _register_file(
                collection,
                source_file,
                wing,
                agent,
                extract_mode,
                access_gate=access_gate,
            )
        return None

    total_len = sum(len(c.strip()) for c in conversations)
    if not conversations or total_len < cfg_min_chunk_size:
        if not dry_run:
            _register_file(
                collection,
                source_file,
                wing,
                agent,
                extract_mode,
                access_gate=access_gate,
            )
        return None

    return conversations


def _open_convo_collection(
    palace_path: str,
    *,
    dry_run: bool,
    access_gate=None,
):
    """Open the conversation collection without creating it during dry-run."""
    if not dry_run:
        with _access_write(access_gate):
            return get_collection(palace_path)

    try:
        with _access_read(access_gate):
            return get_collection(
                palace_path,
                create=False,
                read_only=True,
            )
    except PalaceNotFoundError:
        # A missing palace or uninitialized collection represents empty
        # prior state to a dry-run. Do not create either one.
        return None


def _mine_convos_impl(
    convo_dir: str,
    palace_path: str,
    wing: str = None,
    agent: str = "mempalace",
    limit: int = 0,
    dry_run: bool = False,
    extract_mode: str = "exchange",
    include_subagents: bool = False,
    access_gate=None,
):
    from .config import MempalaceConfig

    palace_config = MempalaceConfig(palace_path=palace_path)
    cfg_chunk_size = palace_config.chunk_size
    # Only override convo_miner's MIN_CHUNK_SIZE when the user has set
    # min_chunk_size explicitly. min_chunk_size_explicit returns the
    # validated value or None — None keeps convo's lower 30-char floor
    # (more permissive than the 50-char project default, so short
    # exchanges aren't dropped). Using the validated accessor (not raw
    # _file_config) means a garbage/negative/bool config value can't
    # TypeError the length gate below or ValueError out of
    # chunk_exchanges and abort convo ingest.
    explicit_min = palace_config.min_chunk_size_explicit
    cfg_min_chunk_size = explicit_min if explicit_min is not None else MIN_CHUNK_SIZE

    convo_path = Path(convo_dir).expanduser().resolve()
    wing = _resolve_wing(convo_path, wing)

    files = scan_convos(convo_dir, include_subagents=include_subagents)

    _mine_print(f"\n{'=' * 55}")
    _mine_print("  MemPalace Mine -- Conversations")
    _mine_print(f"{'=' * 55}")
    _mine_print(f"  Wing:    {wing}")
    _mine_print(f"  Source:  {convo_path}")
    limit_suffix = f" (limit: {limit} new)" if limit > 0 else ""
    _mine_print(f"  Files:   {len(files)}{limit_suffix}")
    _mine_print(f"  Palace:  {palace_path}")
    if dry_run:
        _mine_print("  DRY RUN -- nothing will be filed")
    _mine_print(f"{'-' * 55}\n")

    collection = _open_convo_collection(
        palace_path,
        dry_run=dry_run,
        access_gate=access_gate,
    )

    # Bulk pre-fetch already-mined source_file -> stored mtime in one
    # paginated pass instead of `len(files)` separate WHERE-source_file
    # queries. On a 150k-drawer palace each per-file query costs ~2s, so a
    # 2000-file sweep used to spend >1h just deciding to skip.
    # prefetch_mined_set() does the same decisions in a single scan; loop
    # body becomes an O(1) dict lookup + a cheap local mtime comparison.
    with _access_read(access_gate):
        mined_mtimes: dict = (
            prefetch_mined_set(collection, extract_mode=extract_mode)
            if collection is not None
            else {}
        )
        # content_hash -> source_file for transcripts already filed. Repeated
        # exports from Claude/ChatGPT commonly land under a new filename each
        # run even when the conversation itself is unchanged, so the
        # source_file-keyed skip above ("mined_mtimes") never recognizes them —
        # this catches the same conversation reappearing at a new path.
        mined_content_hashes: dict = (
            prefetch_content_hashes(collection, extract_mode=extract_mode)
            if collection is not None
            else {}
        )

    total_drawers = 0
    files_mined = 0
    files_skipped = 0
    files_processed = 0
    room_counts = defaultdict(int)

    for i, filepath in enumerate(files, 1):
        files_processed = i
        source_file = str(filepath)

        # Skip only if already filed at the current NORMALIZE_VERSION AND
        # unchanged on disk since. Transcripts are NOT assumed immutable:
        # a Claude Code session keeps appending to the same file while
        # active, and /compact or /clear can rewrite one in place -- so
        # "we've seen this source_file before" alone is not sufficient.
        # Falling through re-mines: _file_chunks_locked purges this
        # source_file's stale drawers before inserting fresh ones, so this
        # never leaves duplicates behind.
        if _is_unchanged_since_last_mine(source_file, mined_mtimes):
            files_skipped += 1
            continue

        if not _is_regular_source_file(filepath, Path(convo_dir).expanduser().resolve()):
            files_skipped += 1
            continue

        conversations = _normalize_convo_conversations(
            filepath,
            source_file,
            cfg_min_chunk_size,
            collection,
            wing,
            agent,
            extract_mode,
            dry_run,
            access_gate=access_gate,
        )
        if conversations is None:
            continue

        # Hash and dedup per conversation, not per file: a Claude/ChatGPT
        # privacy export bundles every conversation into one file, so a
        # re-export that adds one new conversation changes the whole-file
        # hash and would hide the conversations that didn't change if we
        # hashed the joined bundle. Conversations whose hash is already
        # filed under a different source_file in this wing are dropped;
        # the rest are re-joined and mined as usual.
        new_items, duplicates = _split_new_and_duplicate_conversations(
            conversations, wing, source_file, mined_content_hashes
        )
        if not new_items:
            if not dry_run:
                _register_file(
                    collection,
                    source_file,
                    wing,
                    agent,
                    extract_mode,
                    access_gate=access_gate,
                )
            dup_source = duplicates[0][1]
            _mine_print(
                f"  = [{i:4}/{len(files)}] {filepath.name[:50]:50} "
                f"duplicate of {Path(dup_source).name}"
            )
            files_skipped += 1
            continue

        content = "\n\n".join(text for _, text in new_items)
        content_hash = ",".join(h for h, _ in new_items)

        # Chunk — either exchange pairs or general extraction
        if extract_mode == "general":
            from .general_extractor import extract_memories

            chunks = extract_memories(content, chunk_size=cfg_chunk_size)
            # Each chunk already has memory_type; use it as the room name
        else:
            chunks = chunk_exchanges(
                content,
                chunk_size=cfg_chunk_size,
                min_chunk_size=cfg_min_chunk_size,
            )

        if not chunks:
            if not dry_run:
                _register_file(
                    collection,
                    source_file,
                    wing,
                    agent,
                    extract_mode,
                    access_gate=access_gate,
                )
            continue

        # Detect room from content (general mode uses memory_type instead)
        if extract_mode != "general":
            room = detect_convo_room(content)
        else:
            room = None  # set per-chunk below

        if dry_run:
            if extract_mode == "general":
                from collections import Counter

                type_counts = Counter(c.get("memory_type", "general") for c in chunks)
                types_str = ", ".join(f"{t}:{n}" for t, n in type_counts.most_common())
                _mine_print(
                    f"    [DRY RUN] {filepath.name} -> {len(chunks)} memories ({types_str})"
                )
            else:
                _mine_print(f"    [DRY RUN] {filepath.name} -> room:{room} ({len(chunks)} drawers)")
            total_drawers += len(chunks)
            # Track room counts
            if extract_mode == "general":
                for c in chunks:
                    room_counts[c.get("memory_type", "general")] += 1
            else:
                room_counts[room] += 1
            files_mined += 1
            if limit > 0 and files_mined >= limit:
                break
            continue

        if extract_mode != "general":
            room_counts[room] += 1

        # Lock + purge stale + file fresh chunks. Lock serializes concurrent
        # agents; purge removes pre-v2 drawers so the schema bump applies.
        drawers_added, room_delta, skipped = _file_chunks_locked(
            collection,
            source_file,
            chunks,
            wing,
            room,
            agent,
            extract_mode,
            authored_at=_extract_authored_at(filepath),
            content_hash=content_hash,
            access_gate=access_gate,
        )
        if skipped:
            files_skipped += 1
            continue
        for r, n in room_delta.items():
            room_counts[r] += n

        for h, _ in new_items:
            mined_content_hashes[(wing, h)] = source_file
        total_drawers += drawers_added
        files_mined += 1
        _mine_print(f"  + [{i:4}/{len(files)}] {filepath.name[:50]:50} +{drawers_added}")
        if limit > 0 and files_mined >= limit:
            break

    if not dry_run:
        # Compute hallways before the FTS5 validation: the latter opens a direct sqlite
        # connection to the Chroma DB, which can invalidate the live collection handle on
        # some Chroma builds and make the hallway fetch fail.
        with _access_read(access_gate):
            _compute_hallways_for_wing_safe(wing, collection, total_drawers, config=palace_config)
        # Validation may close cached Chroma handles and rebuild the FTS index,
        # so it is a mutation even when the common healthy path is read-only.
        # Keep peer searches out until that maintenance work is complete.
        with _access_write(access_gate):
            _validate_palace_fts5_after_mine(palace_path)

    _mine_print(f"\n{'=' * 55}")
    _mine_print("  Done.")
    _mine_print(f"  Files processed: {files_processed - files_skipped}")
    _mine_print(f"  Files skipped (already filed): {files_skipped}")
    _mine_print(f"  Drawers filed: {total_drawers}")
    if room_counts:
        _mine_print("\n  By room:")
        for room, count in sorted(room_counts.items(), key=lambda x: x[1], reverse=True):
            _mine_print(f"    {room:20} {count} files")
    _mine_print('\n  Next: mempalace search "what you\'re looking for"')
    _mine_print(f"{'=' * 55}\n")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        _mine_print(
            "Usage: python convo_miner.py <convo_dir> [--palace PATH] [--limit N] [--dry-run]"
        )
        sys.exit(1)
    from .config import MempalaceConfig

    mine_convos(sys.argv[1], palace_path=MempalaceConfig().palace_path)
