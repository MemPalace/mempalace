#!/usr/bin/env python3
"""
convo_miner.py — Mine conversations into the palace.

Ingests chat exports (Claude Code, ChatGPT, Slack, plain text transcripts).
Normalizes format, chunks by exchange pair (Q+A = one unit), files to palace.

Same palace as project mining. Different ingest strategy.
"""

import hashlib
import json
import logging
import os
import stat
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Optional

from .backends import PalaceNotFoundError
from .entities import entities_metadata
from .fsio import encoding_metadata, open_source_file
from .ids import (
    ID_RECIPE,
    make_convo_drawer_id,
    make_convo_sentinel_id,
    make_exchange_drawer_id,
)
from .normalize import (
    UnparsedCodexTranscriptError,
    normalize_conversations_with_mtime,
    recording_transforms,
)
from .palace import (
    CHUNKER_VERSIONS,
    CONVO_CHUNKER_VERSION,
    DRAWER_UPSERT_BATCH_SIZE,
    MINE_ABORTS,
    NORMALIZE_VERSION,
    SKIP_DIRS,
    _metadata_matches_extract_mode,
    _validate_palace_fts5_after_mine,
    assert_no_collisions,
    file_already_mined,
    get_collection,
    mine_lock,
    mine_palace_lock,
    mine_yield_point,
    open_aaak_collection,
    palace_write_serial,
    prefetch_mined_set,
    print_files_failed,
    raise_if_files_failed,
    write_file_drawers,
)
from .source_identity import identity_metadata, source_directory_identity
from .timeutil import utc_now_iso

logger = logging.getLogger(__name__)

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
    filed_at = utc_now_iso()
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
        # Not produced by the chunker. Stamped so the version checks treat a
        # live row exactly as they did before the field existed; the separate
        # source_mtime rule is unchanged.
        "convo_chunker_version": CONVO_CHUNKER_VERSION,
        "id_recipe": ID_RECIPE,
        # Same directory identity a mined drawer carries, so ``sync`` decides
        # a live exchange and a historical one by the same rule.
        **identity_metadata(source_file),
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
    try:
        fd, st = open_source_file(filepath)
    except OSError:
        return False
    os.close(fd)
    return st.st_size <= MAX_FILE_SIZE


def _register_file(
    collection,
    source_file: str,
    wing: str,
    agent: str,
    extract_mode: str,
    source_mtime: Optional[float] = None,
):
    """Write a sentinel so file_already_mined() returns True for 0-chunk files.

    Without this, files that normalize to nothing or produce zero chunks are
    re-read and re-processed on every mine run because nothing was written to
    ChromaDB on the first pass.

    Stamps source_mtime like every real drawer does, so a file that later
    grows past the min-chunk-size floor (e.g. a short session that gets
    extended) is correctly detected as changed on the next mine instead of
    being skipped forever by this sentinel.
    """
    if source_mtime is None:
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
        "filed_at": utc_now_iso(),
        "ingest_mode": "registry",
        "extract_mode": extract_mode,
        "normalize_version": NORMALIZE_VERSION,
        "id_recipe": ID_RECIPE,
    }
    if extract_mode in CHUNKER_VERSIONS:
        key, version = CHUNKER_VERSIONS[extract_mode]
        meta[key] = version
    if source_mtime is not None:
        meta["source_mtime"] = source_mtime
    write_file_drawers(
        collection,
        source_file,
        [(sentinel_id, f"[registry] {source_file}", meta)],
        extract_mode=extract_mode,
        prune=False,
    )


def _source_file_delete_ids(collection, source_file: str, extract_mode: str) -> list[str]:
    """Collect drawer IDs for one source file and extraction mode.

    Legacy conversation drawers did not carry extract_mode; treat those as
    exchange-mode rows so schema rebuilds can still clean them up without
    deleting newer general-mode drawers for the same transcript.
    """
    ids: list[str] = []
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
            if _metadata_matches_extract_mode(meta or {}, extract_mode):
                ids.append(drawer_id)
        if not batch_ids:
            break
        offset += len(batch_ids)
    return ids


def _source_file_stored_drawers(collection, source_file: str, extract_mode: str) -> dict:
    """Fetch ``{drawer_id: (document, metadata)}`` for one source file + mode.

    Same row scoping as ``_source_file_delete_ids`` (which decides which
    drawers belong to this convo-miner pass), but also returns the documents
    so an incremental re-mine can byte-compare each stored chunk against the
    re-derived one and skip the identical ones. Raises on backend
    errors so the caller can fall back to the legacy full-purge path.
    """
    stored: dict = {}
    offset = 0
    while True:
        batch = collection.get(
            where={"source_file": source_file},
            limit=1000,
            offset=offset,
            include=["metadatas", "documents"],
        )
        batch_ids = batch.get("ids") or []
        metadatas = list(batch.get("metadatas") or [])
        documents = list(batch.get("documents") or [])
        for idx, drawer_id in enumerate(batch_ids):
            meta = metadatas[idx] if idx < len(metadatas) else None
            if not _metadata_matches_extract_mode(meta or {}, extract_mode):
                continue
            doc = documents[idx] if idx < len(documents) else None
            stored[drawer_id] = (doc, meta or {})
        if not batch_ids:
            break
        offset += len(batch_ids)
    return stored


def _plan_pass_drawers(
    chunks,
    wing,
    room,
    source_file,
    agent,
    extract_mode,
    authored_at,
    filed_at,
    source_mtime,
    chunk_total,
    content_hash,
    source_dir_ino=None,
    transforms: str = "",
    source_encoding: str = "utf-8",
):
    """Build this pass's ``(docs, ids, metas)`` plus per-room counters.

    Pure construction — the collection is never touched — so the legacy
    full-purge path and the incremental path derive identical metadata from
    the same code. The incremental path additionally byte-compares these
    documents against the stored rows before upserting.
    """
    room_counts_delta: dict = defaultdict(int)
    docs: list = []
    ids: list = []
    metas: list = []
    for chunk in chunks:
        chunk_room = chunk.get("memory_type", room) if extract_mode == "general" else room
        if extract_mode == "general":
            room_counts_delta[chunk_room] += 1
        drawer_id = make_convo_drawer_id(
            wing, chunk_room, source_file, extract_mode, chunk["chunk_index"]
        )
        docs.append(chunk["content"])
        ids.append(drawer_id)
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
            "chunk_total": chunk_total,
        }
        if source_mtime is not None:
            meta["source_mtime"] = source_mtime
        if source_dir_ino:
            # Which directory this transcript was read from, so
            # ``sync`` can tell a neighbour in the same directory
            # from one on a volume mounted to that directory.
            meta["source_dir_ino"] = source_dir_ino
        if transforms:
            # Every drawer of a file whose parse changed its text: the
            # RFC 002 transformations it applied.
            meta["transforms"] = transforms
        meta.update(encoding_metadata(source_encoding))
        if extract_mode in CHUNKER_VERSIONS:
            # Conversation rows carry the chunker version so the mined-set
            # checks treat an older/missing row as stale and rebuild it once.
            # Stamped in the single place this pass's metadata
            # is built, so the legacy and incremental paths stay byte-identical.
            key, version = CHUNKER_VERSIONS[extract_mode]
            meta[key] = version
        # Stamp content_hash only on chunk 0 so multi-conversation
        # privacy-export hashes are not O(N²)-duplicated across every
        # chunk row. ``prefetch_content_hashes`` still finds them —
        # it scans all drawers and splits comma-joined hash fields.
        if content_hash is not None and chunk.get("chunk_index", 0) == 0:
            meta["content_hash"] = content_hash
        metas.append(meta)
    return docs, ids, metas, room_counts_delta


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
    return _chunk_by_paragraph(content, chunk_size, min_chunk_size)


def _chunk_by_exchange(lines: list, chunk_size: int, min_chunk_size: int) -> list:
    """One user turn (>) + the AI response that follows = one or more chunks.

    Every non-blank line lands in a chunk. A response runs until the next
    user turn — a ``---`` rule inside it is part of the response — and any
    text before the first user turn is filed as its own unit. When a unit
    exceeds chunk_size it is split across consecutive drawers.
    """
    chunks = []
    i = 0
    # Separator between the previous unit and the next one as it stood in the
    # source: the newline ending that unit plus the blank lines trimmed off it.
    # A unit small enough to join the previous drawer is joined with it.
    gap = "\n"

    preamble = []
    while i < len(lines) and not lines[i].strip().startswith(">"):
        preamble.append(lines[i])
        i += 1
    raw_preamble = "\n".join(preamble)
    _emit_bounded(chunks, raw_preamble.strip("\n"), chunk_size, min_chunk_size)
    gap = "\n" * (len(raw_preamble) - len(raw_preamble.rstrip("\n")) + 1)

    while i < len(lines):
        user_turn = lines[i].strip()
        i += 1

        ai_lines = []
        while i < len(lines) and not lines[i].strip().startswith(">"):
            # Preserve the line as-is — blank lines and indentation carry meaning
            # (paragraph breaks, list/code structure) and must survive verbatim.
            ai_lines.append(lines[i])
            i += 1

        # Join on newline (not space) so line structure, blank lines, and
        # indentation reach the drawer unchanged. Trim only trailing blank
        # lines produced by the loop stopping at the next `>` turn.
        raw_response = "\n".join(ai_lines)
        ai_response = raw_response.rstrip("\n")
        content = f"{user_turn}\n{ai_response}" if ai_response else user_turn

        _emit_bounded(chunks, content, chunk_size, min_chunk_size, joiner=gap)
        gap = "\n" * (len(raw_response) - len(ai_response) + 1)

    return chunks


def _emit_bounded(
    chunks: list,
    content: str,
    chunk_size: int,
    min_chunk_size: int,
    joiner: str = "\n",
) -> None:
    """Append ``content`` as one or more drawers, none exceeding ``chunk_size``.

    Nothing but whitespace is ever dropped. A unit whose stripped length is
    at or below ``min_chunk_size`` is too small to be a useful drawer on its
    own, so it is appended to the previous drawer (joined by ``joiner``,
    which callers pass as the separator that stood between the two units in
    the source) when that still fits in ``chunk_size``, and emitted as its
    own drawer otherwise.
    """
    if not content.strip():
        return
    if len(content.strip()) <= min_chunk_size and chunks:
        merged = chunks[-1]["content"] + joiner + content
        if len(merged) <= chunk_size:
            chunks[-1]["content"] = merged
            return
    for piece in _split_bounded(content, chunk_size):
        chunks.append({"content": piece, "chunk_index": len(chunks)})


def _split_bounded(content: str, chunk_size: int) -> list:
    """Split ``content`` into slices of at most ``chunk_size`` characters.

    The slices concatenate back to exactly ``content``. Each cut lands just
    after the last whitespace in the back half of its window, so words are
    not cut in two; a window with no whitespace there is cut at
    ``chunk_size``. Scanning one window at a time keeps this linear.
    """
    pieces = []
    start = 0
    while len(content) - start > chunk_size:
        limit = start + chunk_size
        floor = start + chunk_size // 2
        cut = max(content.rfind(ws, floor, limit) for ws in (" ", "\n", "\t"))
        cut = cut + 1 if cut != -1 else limit
        pieces.append(content[start:cut])
        start = cut
    pieces.append(content[start:])
    return pieces


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
        _emit_bounded(chunks, para, chunk_size, min_chunk_size, joiner="\n\n")

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
                        print(f"  SKIP: {rel} (symlink)", file=sys.stderr)
                    except OSError:
                        pass
                    continue
                # Skip files exceeding size limit, or those whose stat() raises
                # (permission denied, racing delete, broken symlink that
                # survived the earlier is_symlink check). Both branches log
                # to stderr to match the SKIP: (symlink) line above, so no
                # file is dropped here without a word.
                try:
                    file_stat = filepath.stat()
                    # Drop non-regular entries (FIFO, socket, device node)
                    # before any reader touches them — see the matching
                    # gate in ``miner.scan_project``.
                    if not stat.S_ISREG(file_stat.st_mode):
                        print(
                            f"  SKIP: {filepath.name} (not a regular file)",
                            file=sys.stderr,
                        )
                        continue
                    file_size = file_stat.st_size
                    if file_size > MAX_FILE_SIZE:
                        print(
                            f"  SKIP: {filepath.name} ({file_size / (1024 * 1024):.1f} MB)"
                            f" exceeds {MAX_FILE_SIZE // (1024 * 1024)} MB limit",
                            file=sys.stderr,
                        )
                        continue
                except OSError as exc:
                    # Prefer ``exc.strerror`` so the path isn't duplicated in
                    # the output (see the matching comment in
                    # ``miner.scan_project``).
                    print(
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


# Internal marker set on the exception an incremental upsert-failure rolls
# back with (see ``_apply_incremental_pass``). The incremental call site sits
# inside ``_file_chunks_locked``'s outer ``try`` (the legacy rebuild handler);
# without the marker that handler would run the source-wide partial cleanup a
# SECOND time, deleting the byte-identical drawers the incremental path was
# preserving — the fail-safe requirement that a crash must leave the
# existing palace untouched.
# We keep the attribute (rather than a sentinel type) so the operator still
# sees the backend's real error type + message.
_INCREMENTAL_ROLLED_BACK = "_convo_incremental_rolled_back"


def _apply_incremental_pass(
    collection,
    source_file,
    extract_mode,
    incremental,
    docs,
    ids,
    metas,
    room_counts_delta,
) -> tuple:
    """Execute the incremental re-mine (bounded slice).

    Purges only the orphans this pass will not rewrite, upserts only the
    changed / new drawers (batched), and refreshes the bookkeeping
    metadata (``source_mtime`` / ``chunk_total`` / ``content_hash``) on
    the skipped byte-identical rows so ``file_already_mined`` /
    ``prefetch_mined_set`` still see one complete, current group.
    Returns ``(drawers_added, room_counts_delta)`` — the caller combines
    that with ``skipped=False``. On an upsert failure after the orphan
    purge already ran, performs a *targeted* rollback: deletes only the
    chunks that were successfully upserted in this pass before the later
    batch failed, and leaves the unchanged byte-identical drawers (which
    were never in the upsert set) in place. The source-wide partial
    cleanup belongs to the legacy rebuild only — that path deletes the
    full set before upserting, so a mid-file crash there is lossy
    regardless (fail-safe: re-file from scratch).
    The metadata-only ``update()`` failure is fail-safe only (wasteful,
    never lossy) and does NOT fall into any cleanup.
    """
    # Purge only orphans: stored ids this pass does not rewrite
    # (the file shrank, or /compact re-chunked the head so
    # position N no longer maps to the old chunk N). Identical
    # and changed chunks are both rewritten in place by the
    # upsert below — no delete needed for id correctness
    # (drawer ids are positional, as this re-file rewrites by index),
    # and upsert-overwrite
    # re-embeds the changed content exactly like a rebuild.
    keep_ids = set(ids)
    orphan_ids = [drawer_id for drawer_id in incremental if drawer_id not in keep_ids]
    if orphan_ids:
        collection.delete(ids=orphan_ids)
    # Split into skipped (byte-identical) and changed, and
    # build the metadata-only refresh for the skipped rows:
    # source_mtime + chunk_total + (content_hash on the
    # chunk-0 row) must track THIS pass, or
    # file_already_mined / prefetch_mined_set keep grouping
    # them under the file's old mtime with the old chunk_total
    # and the file is never "complete" at the new mtime again.
    skip_ids: list = []
    skip_refresh_metas: dict = {}
    upsert_docs: list = []
    upsert_ids: list = []
    upsert_metas: list = []
    for doc, drawer_id, meta in zip(docs, ids, metas):
        stored_doc = incremental.get(drawer_id)
        if stored_doc is not None and stored_doc[0] == doc:
            skip_ids.append(drawer_id)
            refresh = {
                "source_mtime": meta.get("source_mtime"),
                "chunk_total": meta["chunk_total"],
            }
            # Re-stamp the chunk-0 hash when the pass stamps
            # one AND the stored row already carries one.
            # When the pass has no mtime, ``update()`` uses None to
            # *remove* keys, so leave the hash alone in that case
            # rather than clobbering a valid stored hash.
            if (
                meta.get("source_mtime") is not None
                and incremental[drawer_id][1].get("content_hash") is not None
                and meta.get("content_hash") is not None
            ):
                refresh["content_hash"] = meta["content_hash"]
            skip_refresh_metas[drawer_id] = refresh
        else:
            upsert_docs.append(doc)
            upsert_ids.append(drawer_id)
            upsert_metas.append(meta)
    upserted_ok_ids: list = []
    try:
        if upsert_ids:
            # Batch bounded upserts so large transcripts keep
            # most of the embedding speedup without one
            # huge Chroma/SQLite request.
            for batch_start in range(0, len(upsert_ids), DRAWER_UPSERT_BATCH_SIZE):
                slice_end = min(batch_start + DRAWER_UPSERT_BATCH_SIZE, len(upsert_ids))
                batch_ids = upsert_ids[batch_start:slice_end]
                assert_no_collisions(
                    list(zip(batch_ids, upsert_metas[batch_start:slice_end])),
                    collection,
                )
                try:
                    collection.upsert(
                        documents=upsert_docs[batch_start:slice_end],
                        ids=batch_ids,
                        metadatas=upsert_metas[batch_start:slice_end],
                    )
                except Exception as e:
                    if "already exists" not in str(e).lower():
                        raise
                upserted_ok_ids.extend(batch_ids)
        drawers_added = len(upsert_ids)
    except Exception as exc:
        # Targeted rollback: delete only the chunks this
        # pass successfully upserted before the later batch failed. The
        # unchanged, byte-identical drawers are NOT in upsert_ids (they
        # were routed to skip_ids), so they survive the crash — the
        # documented requirement that a mid-file crash leaves the
        # existing palace untouched (fail-safe).
        # The source-wide ``cleanup_partial()`` is NOT invoked here;
        # that semantic belongs to the legacy rebuild only, where the
        # full set was already deleted before the upsert started.
        if upserted_ok_ids:
            try:
                collection.delete(ids=upserted_ok_ids)
            except Exception:
                logger.warning(
                    "Failed to roll back partially-upserted convo drawers for %s",
                    source_file,
                    exc_info=True,
                )
        # Flag the exception so the outer handler in
        # _file_chunks_locked skips its redundant source-wide
        # cleanup — the incremental path already rolled back
        # its own upserts above.
        setattr(exc, _INCREMENTAL_ROLLED_BACK, True)
        raise
    else:
        # Metadata-only: no documents, no embeddings — a
        # backend that merges metadata leaves entities,
        # novelty tags and the stored vectors intact. A None
        # value removes the key (ChromaDB update() semantics),
        # which is exactly right when the pass has no mtime.
        # If this fails the skipped rows simply keep their old
        # mtime and the file is re-mined next pass: wasteful,
        # never lossy (fail-safe) — so it must NOT fall
        # through to the full-rebuild cleanup, where the
        # already-upserted changed chunks would be deleted and
        # re-filed needlessly.
        if skip_ids:
            grouped: dict = {}
            for drawer_id in skip_ids:
                refresh = skip_refresh_metas[drawer_id]
                grouped.setdefault(tuple(sorted(refresh.items())), []).append(drawer_id)
            # Chroma update() takes parallel lists; group by
            # identical refresh shape — in practice one group,
            # since the refresh is identical across the pass —
            # so one call per shape.
            try:
                for group_ids in grouped.values():
                    collection.update(
                        ids=group_ids,
                        metadatas=[skip_refresh_metas[d] for d in group_ids],
                    )
            except Exception as exc:
                logger.warning(
                    "Failed to refresh metadata on %d unchanged "
                    "convo drawers for %s: %r; the file will be "
                    "re-mined on the next pass",
                    len(skip_ids),
                    source_file,
                    exc,
                )
    return drawers_added, room_counts_delta


def _file_chunks_locked(
    collection,
    source_file,
    chunks,
    wing,
    room,
    agent,
    extract_mode,
    authored_at=None,
    source_dir_ino=None,
    source_mtime: Optional[float] = None,
    transforms: str = "",
    source_encoding: str = "utf-8",
    aaak_col=None,
):
    """Lock the source file and file this pass's drawers.

    Two paths share this contract:

    * **Incremental** (preferred): the file was already mined at the
      current schema, so only the delta is written. Stored drawers whose
      re-derived document is byte-identical are left exactly as stored —
      not re-embedded, not re-filed — and only stored ids this pass will
      not rewrite are purged (the orphans a rewrite/compaction left
      behind). Everything this pass does file is upserted as before. The
      pass's bookkeeping fields (``source_mtime`` / ``chunk_total``) are
      refreshed on the skipped rows with a metadata-only ``update()`` so
      ``file_already_mined`` / ``prefetch_mined_set`` — which group by
      ``(source_file, source_mtime)`` and require a group to reach
      ``chunk_total`` — still see one complete, current group.

    * **Legacy full rebuild** (fallback): any case where the incremental
      path is not provably applicable — first mine of the file, a
      normalize-schema bump, an interrupted prior pass, or any backend
      error during the prefetch — purges every drawer for the source file
      (``_source_file_delete_ids``) and upserts the whole pass, exactly as
      before. A failed purge must abort this file's mine attempt rather
      than fall through to upsert: proceeding on top of an unpurged (or
      partially purged) set produces duplicate/stale drawers, with no
      operator-visible signal beyond a debug log (the pre-existing
      own instance of the same swallow already fixed for miner.py at
      #23). Returning here leaves the old drawers' stored mtime
      untouched, so the next mine still sees a mismatch and retries.

    Combines the per-file serialization that prevents concurrent agents
    from duplicating work (via mine_lock). Transcripts are not assumed
    immutable: an interactive coding session keeps appending to its own file
    while active, and /compact or /clear can rewrite one in place — which
    is exactly what the incremental path absorbs and what a schema bump
    escalates to the full rebuild.

    Reliability of an interrupted re-mine (data loss between the purge
    and the re-file of changed chunks) is tracked separately;
    both paths retain the ``chunk_total`` completion guard so
    a mid-file crash is still detectable on the next pass.

    Returns (drawers_added, room_counts_delta, skipped). Raises
    ``StaleDrawerPurgeFailed`` when the stale drawers could not be removed,
    which the mine counts as an error.
    """
    room_counts_delta: dict = defaultdict(int)
    with mine_lock(source_file):
        # Re-check after lock — another agent may have just finished this file
        # at the current schema/mtime. A stale hit here returns False, so we
        # still fall through to the incremental / legacy paths below.
        if file_already_mined(collection, source_file, check_mtime=True, extract_mode=extract_mode):
            return 0, room_counts_delta, True

        # The write-path decision (incremental vs legacy rebuild) and the
        # legacy purge must run in that order — purging first would destroy
        # the stored rows the incremental path needs to byte-compare, and a
        # "purge-then-rebuild" failure would wipe the source-wide set even
        # though the incremental path had not touched it (crash-safety:
        # the unchanged drawers must survive). The legacy full-rebuild
        # path below owns the purge and returns PURGE_FAILED if it
        # raises (the same swallow that was already fixed for miner.py).

        # Batch chunks into bounded upserts so large transcripts keep most of
        # the embedding speedup without one huge Chroma/SQLite request. Keep
        # one filed_at per source file so all transcript drawers share an
        # ingest timestamp.
        #
        # Every drawer of this pass carries ``chunk_total`` so
        # ``file_already_mined`` / ``prefetch_mined_set`` can tell a complete
        # multi-batch mine from one that crashed mid-file. Without it
        # a stable mtime + any surviving drawer permanently skips the file
        # and the missing exchanges never come back.
        filed_at = datetime.now().isoformat()
        # Prefer the mtime captured with the read (fstat at open time): an
        # interactive session can append to its own transcript between that
        # read and the moment we stat again, so a fresh getmtime would record
        # a time this pass did not actually ingest. Fall back to a live stat
        # only when the read did not yield one.
        if source_mtime is None:
            try:
                source_mtime = os.path.getmtime(source_file)
            except OSError:
                source_mtime = None
        # Stable per-file content hash, stamped only on chunk 0 so
        # multi-conversation files don't O(N²)-duplicate it; it lets
        # prefetch_content_hashes and the incremental byte-compare tell a
        # content change from a pure re-file (diary_ingest recipe).
        first_content = chunks[0].get("content", "") if chunks else ""
        content_hash = hashlib.sha256(first_content.encode("utf-8", "surrogateescape")).hexdigest()
        chunk_total = len(chunks)
        docs, ids, metas, room_counts_delta = _plan_pass_drawers(
            chunks,
            wing,
            room,
            source_file,
            agent,
            extract_mode,
            authored_at,
            filed_at,
            source_mtime,
            chunk_total,
            content_hash,
            source_dir_ino=source_dir_ino,
            transforms=transforms,
            source_encoding=source_encoding,
        )

        drawers_added = 0
        try:
            # Decide the write path. The incremental path is safe only when
            # every stored row for this file was filed at the current
            # normalize schema (otherwise rows are stale by design and a
            # full rebuild is the only correct answer), a single
            # mtime-group exists (several groups mean an interrupted or
            # mixed pass — same rebuild), and the backend returned the
            # stored documents for the comparison. Anything else — first
            # mine, no stored rows, prefetch trouble — falls back to the
            # legacy full rebuild below.
            incremental = None  # type: Optional[dict]
            if chunks:
                try:
                    stored = _source_file_stored_drawers(collection, source_file, extract_mode)
                except Exception as exc:
                    logger.debug(
                        "Incremental re-mine prefetch failed for %s: %r — "
                        "falling back to full rebuild",
                        source_file,
                        exc,
                    )
                    stored = None
                if stored:
                    same_schema = all(
                        (meta or {}).get("normalize_version", 1) >= NORMALIZE_VERSION
                        for _doc, meta in stored.values()
                    )
                    mtime_groups = {
                        (meta or {}).get("source_mtime") for _doc, meta in stored.values()
                    }
                    if same_schema and len(mtime_groups) == 1 and None not in mtime_groups:
                        incremental = stored
            if incremental is not None:
                drawers_added, room_counts_delta = _apply_incremental_pass(
                    collection,
                    source_file,
                    extract_mode,
                    incremental,
                    docs,
                    ids,
                    metas,
                    room_counts_delta,
                )
                return drawers_added, room_counts_delta, False

            # Legacy full rebuild: delegate to the canonical contract so
            # the purge order, bounded batching (read from palace globals),
            # the one-high chunk_total marker, and the
            # StaleDrawerPurgeFailed / partial-keep semantics all match
            # what the rest of the palace does and what the unit tests pin.
            filed = write_file_drawers(
                collection,
                source_file,
                list(zip(ids, docs, metas)),
                extract_mode=extract_mode,
                prune=True,
            )
            drawers_added = int(filed)
            return drawers_added, room_counts_delta, False
        except Exception:
            # Self-managing write paths: write_file_drawers keeps the new
            # drawers marked unfinished on a purge or batch failure, and
            # the incremental pass rolls back its own partial upserts, so a
            # plain re-raise is all that is left to do here.
            raise
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
         wing-slug producers stay in sync.
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
        print(f"  (hallways skipped: {exc})")


def _normalize_convo_conversations(
    filepath: Path,
    source_file: str,
    collection,
    wing: str,
    agent: str,
    extract_mode: str,
    dry_run: bool,
) -> Optional[tuple[list, float]]:
    """Normalize a transcript file into its individual conversations,
    registering it as filed when there's nothing worth mining. Returns None
    when the caller should skip the file (normalize failed, or the normalized
    content is only whitespace). A short transcript is still mined: the
    chunker keeps text below the min chunk size rather than dropping it.

    On success the second item is the mtime from the same ``fstat`` as the
    read. Callers stamp that value; they must not stat the file again.
    """
    try:
        conversations, source_mtime = normalize_conversations_with_mtime(str(filepath))
        conversations = [c for c in conversations if c]
    except UnparsedCodexTranscriptError as exc:
        logger.warning("Skipping %s: %s; source remains eligible for retry", filepath, exc)
        return None
    except (OSError, ValueError):
        if not dry_run:
            _register_file(collection, source_file, wing, agent, extract_mode)
        return None

    if not any(c.strip() for c in conversations):
        if not dry_run:
            _register_file(
                collection, source_file, wing, agent, extract_mode, source_mtime=source_mtime
            )
        return None

    return conversations, source_mtime


def _print_room_counts(room_counts) -> None:
    if room_counts:
        print("\n  By room:")
        for room, count in sorted(room_counts.items(), key=lambda x: x[1], reverse=True):
            print(f"    {room:20} {count} files")


def _yield_and_reopen(palace_path: str, collection, dry_run: bool):
    """Pass a file boundary, reopening the collection if requests ran in it.

    A request in the hub's handoff may close the backend under this handle (a
    search resets the Chroma caches after a transient index error). Reopening
    returns the cached collection when nothing closed it.
    """
    if not mine_yield_point() or dry_run:
        return collection
    return _open_convo_collection(palace_path, dry_run=False)


def _open_convo_collection(
    palace_path: str,
    *,
    dry_run: bool,
):
    """Open the conversation collection without creating it during dry-run."""
    if not dry_run:
        return get_collection(palace_path)

    try:
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
):
    from .config import MempalaceConfig

    writes_at_start = palace_write_serial()
    palace_config = MempalaceConfig(palace_path=palace_path)
    cfg_chunk_size = palace_config.chunk_size
    spellcheck = palace_config.convo_spellcheck
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

    print(f"\n{'=' * 55}")
    print("  MemPalace Mine -- Conversations")
    print(f"{'=' * 55}")
    print(f"  Wing:    {wing}")
    print(f"  Source:  {convo_path}")
    limit_suffix = f" (limit: {limit} new)" if limit > 0 else ""
    print(f"  Files:   {len(files)}{limit_suffix}")
    print(f"  Palace:  {palace_path}")
    if dry_run:
        print("  DRY RUN -- nothing will be filed")
    print(f"{'-' * 55}\n")

    collection = _open_convo_collection(
        palace_path,
        dry_run=dry_run,
    )
    aaak_col = None if dry_run else open_aaak_collection(palace_path)

    # Bulk pre-fetch already-mined source_file -> stored mtime in one
    # paginated pass instead of `len(files)` separate WHERE-source_file
    # queries. On a 150k-drawer palace each per-file query costs ~2s, so a
    # 2000-file sweep used to spend >1h just deciding to skip.
    # prefetch_mined_set() does the same decisions in a single scan; loop
    # body becomes an O(1) dict lookup + a cheap local mtime comparison.
    candidate_source_files = [str(f) for f in files]
    mined_mtimes: dict = (
        prefetch_mined_set(
            collection, extract_mode=extract_mode, source_files=candidate_source_files
        )
        if collection is not None
        else {}
    )

    total_drawers = 0
    files_mined = 0
    files_skipped = 0
    files_failed: list = []
    files_processed = 0
    room_counts = defaultdict(int)

    for i, filepath in enumerate(files, 1):
        collection = _yield_and_reopen(palace_path, collection, dry_run)
        files_processed = i
        source_file = str(filepath)

        try:
            # Skip only if already filed at the current NORMALIZE_VERSION AND
            # unchanged on disk since. Transcripts are NOT assumed immutable:
            # a Claude Code session keeps appending to the same file while
            # active, and /compact or /clear can rewrite one in place -- so
            # "we've seen this source_file before" alone is not sufficient.
            # Falling through re-mines: _file_chunks_locked drops this
            # source_file's stale drawers once the fresh ones are filed, so
            # this never leaves duplicates behind.
            if _is_unchanged_since_last_mine(source_file, mined_mtimes):
                files_skipped += 1
                continue

            if not _is_regular_source_file(filepath, Path(convo_dir).expanduser().resolve()):
                files_skipped += 1
                continue

            with recording_transforms(spellcheck=spellcheck) as normalize_run:
                normalized = _normalize_convo_conversations(
                    filepath,
                    source_file,
                    collection,
                    wing,
                    agent,
                    extract_mode,
                    dry_run,
                )
            if normalized is None:
                continue
            conversations, source_mtime = normalized

            # Each file keeps its own drawers even when another file holds the
            # same conversation: dropping the copy would hide it from anything
            # scoped to this source.
            content = "\n\n".join(conversations)

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
                        source_mtime=source_mtime,
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
                    print(f"    [DRY RUN] {filepath.name} -> {len(chunks)} memories ({types_str})")
                    for t, n in type_counts.items():
                        room_counts[t] += n
                else:
                    print(f"    [DRY RUN] {filepath.name} -> room:{room} ({len(chunks)} drawers)")
                    room_counts[room] += 1
                total_drawers += len(chunks)
                files_mined += 1
                if limit > 0 and files_mined >= limit:
                    break
                continue

            if extract_mode != "general":
                room_counts[room] += 1

            # Lock + file fresh chunks + drop stale ones. Lock serializes
            # concurrent agents; the prune removes pre-v2 drawers so the schema
            # bump applies. A failed prune raises into the per-file handler.
            drawers_added, room_delta, skipped = _file_chunks_locked(
                collection,
                source_file,
                chunks,
                wing,
                room,
                agent,
                extract_mode,
                authored_at=_extract_authored_at(filepath),
                source_dir_ino=source_directory_identity(filepath),
                source_mtime=source_mtime,
                transforms=normalize_run.transforms,
                source_encoding=normalize_run.encoding,
                aaak_col=aaak_col,
            )
            if skipped:
                files_skipped += 1
                continue
            for r, n in room_delta.items():
                room_counts[r] += n

            total_drawers += drawers_added
            files_mined += 1
            print(f"  + [{i:4}/{len(files)}] {filepath.name[:50]:50} +{drawers_added}")
            if limit > 0 and files_mined >= limit:
                break
        except MINE_ABORTS:
            raise
        except Exception as exc:
            files_failed.append(source_file)
            print(f"  ! [error] {filepath.name}: {type(exc).__name__}: {exc}", file=sys.stderr)
            continue

    if not dry_run:
        # Compute hallways before the FTS5 validation: the latter opens a direct sqlite
        # connection to the Chroma DB, which can invalidate the live collection handle on
        # some Chroma builds and make the hallway fetch fail.
        _compute_hallways_for_wing_safe(wing, collection, total_drawers, config=palace_config)
        _validate_palace_fts5_after_mine(palace_path, writes_since=writes_at_start)

    print(f"\n{'=' * 55}")
    print("  Done.")
    print(f"  Files processed: {files_processed - files_skipped - len(files_failed)}")
    print_files_failed(files_failed)
    print(f"  Files skipped (already filed): {files_skipped}")
    print(f"  Drawers filed: {total_drawers}")
    _print_room_counts(room_counts)
    print('\n  Next: mempalace search "what you\'re looking for"')
    print(f"{'=' * 55}\n")
    # After the summary, so the operator still sees what was filed.
    raise_if_files_failed(files_failed, files_processed)
