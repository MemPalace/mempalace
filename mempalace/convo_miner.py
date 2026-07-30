#!/usr/bin/env python3
"""
convo_miner.py — Mine conversations into the palace.

Ingests chat exports (Claude Code, ChatGPT, Slack, plain text transcripts).
Normalizes format, chunks by exchange pair (Q+A = one unit), files to palace.

Same palace as project mining. Different ingest strategy.
"""

import os
import sys
import json
import logging
import stat
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from itertools import islice
from pathlib import Path
from typing import Optional

from .collision_scan import assert_no_collisions
from .ids import ID_RECIPE, make_convo_drawer_id, make_convo_sentinel_id
from .normalize import normalize
from .normalized_conversations import (
    SCHEMA as NORMALIZED_CONVERSATION_SCHEMA,
    NormalizedConversation,
    NormalizedConversationProbe,
    count_normalized_conversation_chunks,
    has_normalized_sidecar,
    iter_normalized_conversation_chunks,
    load_normalized_conversation,
    probe_normalized_conversation,
)
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
    prefetch_mined_set,
)

logger = logging.getLogger("mempalace_mcp")


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
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = -1
    try:
        fd = os.open(filepath, flags)
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


def _register_file(collection, source_file: str, wing: str, agent: str, extract_mode: str):
    """Write a sentinel so file_already_mined() returns True for 0-chunk files.

    Without this, files that normalize to nothing or produce zero chunks are
    re-read and re-processed on every mine run because nothing was written to
    ChromaDB on the first pass.

    Stamps source_mtime like every real drawer does, so a file that later
    grows past the min-chunk-size floor (e.g. a short session that gets
    extended) is correctly detected as changed on the next mine instead of
    being skipped forever by this sentinel.
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
    collection.upsert(
        documents=[f"[registry] {source_file}"],
        ids=[sentinel_id],
        metadatas=[meta],
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


def _normalized_generation_key(source_version: str, chunk_size: int) -> tuple:
    return (source_version, chunk_size, NORMALIZE_VERSION, ID_RECIPE)


@dataclass
class NormalizedSourceState:
    """Compact, wing-local completeness state for one normalized source."""

    actual_count: int = 0
    generations: set = field(default_factory=set)
    expected_counts: set = field(default_factory=set)
    ids_by_generation: dict = field(default_factory=lambda: defaultdict(list))
    counts_by_generation: dict = field(default_factory=lambda: defaultdict(int))
    expected_by_generation: dict = field(default_factory=lambda: defaultdict(set))

    def record(self, metadata: dict, drawer_id: Optional[str] = None) -> None:
        version = metadata.get("source_version")
        chunk_size = metadata.get("source_chunk_size")
        generation = (
            version,
            chunk_size,
            metadata.get("normalize_version"),
            metadata.get("id_recipe"),
        )
        expected = metadata.get("source_chunk_count")
        self.actual_count += 1
        self.generations.add(generation)
        self.expected_counts.add(expected)
        self.counts_by_generation[generation] += 1
        self.expected_by_generation[generation].add(expected)
        if drawer_id is not None:
            self.ids_by_generation[generation].append(drawer_id)

    def record_count(
        self,
        source_version,
        chunk_size,
        normalize_version,
        id_recipe,
        expected_count,
        count: int,
    ) -> None:
        generation = (source_version, chunk_size, normalize_version, id_recipe)
        self.actual_count += count
        self.generations.add(generation)
        self.expected_counts.add(expected_count)
        self.counts_by_generation[generation] += count
        self.expected_by_generation[generation].add(expected_count)

    def generation_is_complete(
        self, source_version: str, chunk_size: int, expected_count: int
    ) -> bool:
        generation = _normalized_generation_key(source_version, chunk_size)
        return (
            expected_count > 0
            and self.counts_by_generation.get(generation, 0) == expected_count
            and self.expected_by_generation.get(generation, set()) == {expected_count}
        )

    def is_only_complete(self, source_version: str, chunk_size: int) -> bool:
        return (
            self.actual_count > 0
            and self.generations == {_normalized_generation_key(source_version, chunk_size)}
            and len(self.expected_counts) == 1
            and next(iter(self.expected_counts), None) == self.actual_count
        )


def _normalized_source_state(
    collection,
    source_file: str,
    wing: str,
    extract_mode: str,
    *,
    collect_ids: bool = False,
) -> NormalizedSourceState:
    state = NormalizedSourceState()
    offset = 0
    while True:
        batch = collection.get(
            where={"$and": [{"source_file": source_file}, {"wing": wing}]},
            limit=1000,
            offset=offset,
            include=["metadatas"],
        )
        batch_ids = batch.get("ids") or []
        for drawer_id, meta in zip(batch_ids, batch.get("metadatas") or []):
            meta = meta or {}
            if not _metadata_matches_extract_mode(meta, extract_mode):
                continue
            state.record(meta, drawer_id if collect_ids else None)
        if not batch_ids:
            break
        offset += len(batch_ids)
    return state


def _prefetch_normalized_sources(
    collection, wing: str, extract_mode: str
) -> dict[str, NormalizedSourceState]:
    """Fetch compact completeness state for all normalized sources once."""

    states: dict[str, NormalizedSourceState] = {}
    offset = 0
    while True:
        batch = collection.get(
            where={
                "$and": [
                    {"normalized_schema": NORMALIZED_CONVERSATION_SCHEMA},
                    {"wing": wing},
                ]
            },
            limit=1000,
            offset=offset,
            include=["metadatas"],
        )
        batch_ids = batch.get("ids") or []
        for meta in batch.get("metadatas") or []:
            meta = meta or {}
            source_file = meta.get("source_file")
            if not source_file or not _metadata_matches_extract_mode(meta, extract_mode):
                continue
            states.setdefault(source_file, NormalizedSourceState()).record(meta)
        if not batch_ids:
            break
        offset += len(batch_ids)
    return states


def normalized_conversation_delta(
    convo_dir: str,
    palace_path: str,
    *,
    wing: str,
    extract_mode: str = "exchange",
) -> dict:
    """Report normalized source reconciliation without writing palace state."""

    import sqlite3

    from .config import MempalaceConfig, sqlite_read_uri

    if extract_mode != "exchange":
        raise ValueError("normalized conversation delta requires extract_mode=exchange")
    source_root = Path(convo_dir).expanduser().resolve(strict=True)
    stored: dict[str, NormalizedSourceState] = {}
    palace_config = MempalaceConfig(palace_path=palace_path)
    db_path = Path(palace_path) / "chroma.sqlite3"
    if not db_path.is_file():
        raise ValueError("read-only delta reporting currently requires an existing Chroma palace")
    connection = sqlite3.connect(sqlite_read_uri(str(db_path)), uri=True)
    try:
        rows = connection.execute(
            """
            SELECT sf.string_value,
                   sv.string_value,
                   scs.int_value,
                   nv.int_value,
                   ir.string_value,
                   scc.int_value,
                   em.string_value,
                   COUNT(*)
              FROM embeddings e
              JOIN segments s
                ON e.segment_id = s.id AND s.scope = 'METADATA'
              JOIN collections c
                ON s.collection = c.id
              JOIN embedding_metadata sf
                ON sf.id = e.id AND sf.key = 'source_file'
              JOIN embedding_metadata w
                ON w.id = e.id AND w.key = 'wing'
              JOIN embedding_metadata ns
                ON ns.id = e.id AND ns.key = 'normalized_schema'
              LEFT JOIN embedding_metadata sv
                ON sv.id = e.id AND sv.key = 'source_version'
              LEFT JOIN embedding_metadata scs
                ON scs.id = e.id AND scs.key = 'source_chunk_size'
              LEFT JOIN embedding_metadata nv
                ON nv.id = e.id AND nv.key = 'normalize_version'
              LEFT JOIN embedding_metadata ir
                ON ir.id = e.id AND ir.key = 'id_recipe'
              LEFT JOIN embedding_metadata scc
                ON scc.id = e.id AND scc.key = 'source_chunk_count'
              LEFT JOIN embedding_metadata em
                ON em.id = e.id AND em.key = 'extract_mode'
             WHERE c.name = ?
               AND w.string_value = ?
               AND ns.string_value = ?
             GROUP BY sf.string_value, sv.string_value, scs.int_value,
                      nv.int_value, ir.string_value, scc.int_value,
                      em.string_value
            """,
            (palace_config.collection_name, wing, NORMALIZED_CONVERSATION_SCHEMA),
        )
        for (
            source_file,
            source_version,
            source_chunk_size,
            normalize_version,
            id_recipe,
            source_chunk_count,
            stored_mode,
            count,
        ) in rows:
            if not source_file or not _metadata_matches_extract_mode(
                {"extract_mode": stored_mode}, extract_mode
            ):
                continue
            stored.setdefault(source_file, NormalizedSourceState()).record_count(
                source_version,
                source_chunk_size,
                normalize_version,
                id_recipe,
                source_chunk_count,
                int(count),
            )
    finally:
        connection.close()
    stored = {
        source_file: state
        for source_file, state in stored.items()
        if _path_within_root(Path(source_file), source_root)
    }

    current_paths: set[str] = set()
    new: list[str] = []
    changed: list[str] = []
    unchanged: list[str] = []
    projected_counts: dict[str, int] = {}
    for path in scan_convos(str(source_root)):
        if not has_normalized_sidecar(path):
            continue
        conversation = load_normalized_conversation(
            path,
            source_root,
            extract_mode=extract_mode,
        )
        source_file = str(path)
        current_paths.add(source_file)
        projected_counts[source_file] = count_normalized_conversation_chunks(
            conversation,
            chunk_size=palace_config.chunk_size,
        )
        source_state = stored.get(source_file, NormalizedSourceState())
        complete = source_state.is_only_complete(
            conversation.source_version,
            palace_config.chunk_size,
        )
        if complete:
            unchanged.append(source_file)
        elif source_state.actual_count:
            changed.append(source_file)
        else:
            new.append(source_file)

    removed = sorted(set(stored) - current_paths)
    old_changed = sum(stored[path].actual_count for path in changed)
    removed_drawers = sum(stored[path].actual_count for path in removed)
    new_drawers = sum(projected_counts[path] for path in new)
    changed_drawers = sum(projected_counts[path] for path in changed)
    return {
        "new": sorted(new),
        "changed": sorted(changed),
        "unchanged": sorted(unchanged),
        "removed": removed,
        "new_drawers": new_drawers,
        "replacement_drawers": old_changed,
        "changed_drawers": changed_drawers,
        "removed_drawers": removed_drawers,
        "net_drawers": new_drawers + changed_drawers - old_changed - removed_drawers,
    }


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


def scan_convos(convo_dir: str) -> list:
    """Find all potential conversation files.

    Skips symlinks and oversized files. Each skipped symlink is logged to
    ``sys.stderr`` with a ``  SKIP: <relative-path> (symlink)`` line so the
    caller can tell why an apparent conversation directory yielded no files.
    """
    convo_path = Path(convo_dir).expanduser().resolve()
    files = []
    for root, dirs, filenames in os.walk(convo_path):
        dirs[:] = [d for d in dirs if d not in CONVO_SKIP_DIRS]
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
                # to stderr to match the SKIP: (symlink) line above; silent
                # drops at this gate were the original #923 complaint.
                try:
                    file_size = filepath.stat().st_size
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


def _file_chunks_locked(
    collection,
    source_file,
    chunks,
    wing,
    room,
    agent,
    extract_mode,
    authored_at=None,
    normalized: Optional[NormalizedConversation] = None,
    chunk_count: Optional[int] = None,
    source_chunk_size: Optional[int] = None,
):
    """Lock one source and replace it without removing the last complete generation.

    Combines the per-file serialization that prevents concurrent agents from
    duplicating work with two replacement contracts. Generic sources retain
    their established purge-before-upsert behavior. Normalized sources use
    source-versioned IDs: remove only an incomplete target generation, stage
    and verify the complete target, then retire older generations. A failed
    stage therefore leaves the prior complete generation available.

    Returns (drawers_added, room_counts_delta, skipped).
    """
    room_counts_delta: dict = defaultdict(int)
    drawers_added = 0
    expected_chunk_count = len(chunks) if chunk_count is None else chunk_count
    normalized_chunk_size = source_chunk_size if source_chunk_size is not None else 0
    with mine_lock(source_file):
        # Re-check after lock — another agent may have just finished this file
        # at the current schema/mtime. A stale hit here returns False, so we
        # still fall through to the purge+rebuild path below.
        if normalized is not None:
            source_state = _normalized_source_state(
                collection,
                source_file,
                wing,
                extract_mode,
                collect_ids=True,
            )
            already_mined = source_state.is_only_complete(
                normalized.source_version,
                normalized_chunk_size,
            )
        else:
            source_state = None
            already_mined = file_already_mined(
                collection,
                source_file,
                check_mtime=True,
                extract_mode=extract_mode,
            )
        if already_mined:
            return 0, room_counts_delta, True

        if normalized is None:
            try:
                delete_ids = _source_file_delete_ids(collection, source_file, extract_mode)
                if delete_ids:
                    collection.delete(ids=delete_ids)
            except Exception:
                logger.debug("Stale-drawer purge failed for %s", source_file, exc_info=True)
        elif not source_state.generation_is_complete(
            normalized.source_version,
            normalized_chunk_size,
            expected_chunk_count,
        ):
            incomplete_target_ids = source_state.ids_by_generation.get(
                _normalized_generation_key(normalized.source_version, normalized_chunk_size),
                [],
            )
            if incomplete_target_ids:
                collection.delete(ids=incomplete_target_ids)
                cleared = _normalized_source_state(
                    collection,
                    source_file,
                    wing,
                    extract_mode,
                )
                if cleared.counts_by_generation.get(
                    _normalized_generation_key(normalized.source_version, normalized_chunk_size),
                    0,
                ):
                    raise RuntimeError("normalized target-generation purge left stale drawer(s)")

        # Batch chunks into bounded upserts so large transcripts keep most of
        # the embedding speedup without one huge Chroma/SQLite request. Keep
        # one filed_at per source file so all transcript drawers share an
        # ingest timestamp.
        filed_at = datetime.now().isoformat()
        try:
            source_mtime = os.path.getmtime(source_file)
        except OSError:
            source_mtime = None
        target_needs_upsert = normalized is None or not source_state.generation_is_complete(
            normalized.source_version,
            normalized_chunk_size,
            expected_chunk_count,
        )
        chunk_iterator = iter(chunks) if target_needs_upsert else iter(())
        while batch := list(islice(chunk_iterator, DRAWER_UPSERT_BATCH_SIZE)):
            batch_docs: list = []
            batch_ids: list = []
            batch_metas: list = []
            for chunk in batch:
                chunk_room = chunk.get("memory_type", room) if extract_mode == "general" else room
                if extract_mode == "general":
                    room_counts_delta[chunk_room] += 1
                drawer_id = make_convo_drawer_id(
                    wing,
                    chunk_room,
                    source_file,
                    extract_mode,
                    chunk["chunk_index"],
                    source_version=(normalized.source_version if normalized is not None else None),
                    source_chunk_size=normalized_chunk_size,
                    normalize_version=NORMALIZE_VERSION,
                    id_recipe=ID_RECIPE,
                )
                batch_docs.append(chunk["content"])
                batch_ids.append(drawer_id)
                meta = {
                    "wing": wing,
                    "room": chunk_room,
                    "hall": _detect_hall_cached(chunk["content"]),
                    "source_file": source_file,
                    "chunk_index": chunk["chunk_index"],
                    "added_by": agent,
                    "filed_at": filed_at,
                    "entities": entities_metadata(chunk["content"]),
                    "authored_at": (
                        chunk.get("authored_to")
                        if normalized is not None
                        else authored_at
                        if authored_at is not None
                        else filed_at
                    ),
                    "ingest_mode": "convos",
                    "extract_mode": extract_mode,
                    "normalize_version": NORMALIZE_VERSION,
                    "id_recipe": ID_RECIPE,
                }
                if source_mtime is not None:
                    meta["source_mtime"] = source_mtime
                if normalized is not None:
                    meta.update(
                        {
                            "authored_from": chunk["authored_from"],
                            "authored_to": chunk["authored_to"],
                            "message_from": chunk["message_from"],
                            "message_to": chunk["message_to"],
                            "message_ids": chunk["message_ids"],
                            "message_count": chunk["message_count"],
                            "source_version": normalized.source_version,
                            "source_chunk_size": normalized_chunk_size,
                            "source_chunk_count": expected_chunk_count,
                            "source_fingerprint": normalized.metadata.source_fingerprint,
                            "transformations": normalized.metadata.transformations,
                            "exporter_version": normalized.metadata.exporter_version,
                            "hermes_profile": normalized.metadata.hermes_profile,
                            "hermes_session_id": normalized.metadata.hermes_session_id,
                            "hermes_source": normalized.metadata.hermes_source,
                            "normalized_schema": normalized.metadata.schema,
                        }
                    )
                batch_metas.append(meta)
            assert_no_collisions(list(zip(batch_ids, batch_metas)), collection)
            try:
                collection.upsert(
                    documents=batch_docs,
                    ids=batch_ids,
                    metadatas=batch_metas,
                )
                drawers_added += len(batch_docs)
            except Exception as e:
                if "already exists" not in str(e).lower():
                    raise

        if normalized is not None:
            staged = _normalized_source_state(
                collection,
                source_file,
                wing,
                extract_mode,
                collect_ids=True,
            )
            if not staged.generation_is_complete(
                normalized.source_version,
                normalized_chunk_size,
                expected_chunk_count,
            ):
                raise RuntimeError("normalized target generation is incomplete after staging")
            stale_ids = [
                drawer_id
                for generation, version_ids in staged.ids_by_generation.items()
                if generation
                != _normalized_generation_key(normalized.source_version, normalized_chunk_size)
                for drawer_id in version_ids
            ]
            if stale_ids:
                collection.delete(ids=stale_ids)
            committed = _normalized_source_state(
                collection,
                source_file,
                wing,
                extract_mode,
            )
            if not committed.is_only_complete(
                normalized.source_version,
                normalized_chunk_size,
            ):
                raise RuntimeError("normalized source replacement left stale drawer(s)")
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
):
    """Mine a directory of conversation files into the palace.

    extract_mode:
        "exchange" — default exchange-pair chunking (Q+A = one unit)
        "general"  — general extractor: decisions, preferences, milestones, problems, emotions

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


def _probe_normalized_conversation(filepath: Path, source_root: Path, extract_mode: str):
    if has_normalized_sidecar(filepath):
        return probe_normalized_conversation(
            filepath,
            source_root,
            extract_mode=extract_mode,
        )
    return None


def _conversation_source_is_unchanged(
    source_file: str,
    probe: Optional[NormalizedConversationProbe],
    chunk_size: int,
    mined_mtimes: dict,
    normalized_states: dict,
) -> bool:
    if probe is not None:
        state = normalized_states.get(source_file, NormalizedSourceState())
        return state.is_only_complete(probe.source_version, chunk_size)
    return _is_unchanged_since_last_mine(source_file, mined_mtimes)


def _chunk_conversation_content(
    content: str,
    normalized: Optional[NormalizedConversation],
    extract_mode: str,
    chunk_size: int,
    min_chunk_size: int,
) -> list:
    if normalized is not None:
        return iter_normalized_conversation_chunks(normalized, chunk_size=chunk_size)
    if extract_mode == "general":
        from .general_extractor import extract_memories

        return extract_memories(content, chunk_size=chunk_size)
    return chunk_exchanges(
        content,
        chunk_size=chunk_size,
        min_chunk_size=min_chunk_size,
    )


def _conversation_room(
    content: str,
    normalized: Optional[NormalizedConversation],
    extract_mode: str,
) -> Optional[str]:
    if normalized is not None:
        return normalized.metadata.room
    if extract_mode != "general":
        return detect_convo_room(content)
    return None


def _prefetch_conversation_states(files, collection, dry_run: bool, wing: str, extract_mode: str):
    if dry_run:
        return {}, {}
    normalized_files = {path for path in files if has_normalized_sidecar(path)}
    mined_mtimes = {}
    if len(normalized_files) != len(files):
        mined_mtimes = prefetch_mined_set(collection, extract_mode=extract_mode)
    normalized_states = {}
    if normalized_files:
        normalized_states = _prefetch_normalized_sources(collection, wing, extract_mode)
    return mined_mtimes, normalized_states


def _mine_convos_impl(
    convo_dir: str,
    palace_path: str,
    wing: str = None,
    agent: str = "mempalace",
    limit: int = 0,
    dry_run: bool = False,
    extract_mode: str = "exchange",
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

    files = scan_convos(convo_dir)

    print(f"\n{'=' * 55}")
    print("  MemPalace Mine — Conversations")
    print(f"{'=' * 55}")
    print(f"  Wing:    {wing}")
    print(f"  Source:  {convo_path}")
    limit_suffix = f" (limit: {limit} new)" if limit > 0 else ""
    print(f"  Files:   {len(files)}{limit_suffix}")
    print(f"  Palace:  {palace_path}")
    if dry_run:
        print("  DRY RUN — nothing will be filed")
    print(f"{'-' * 55}\n")

    collection = get_collection(palace_path) if not dry_run else None

    # Bulk pre-fetch already-mined source_file -> stored mtime in one
    # paginated pass instead of `len(files)` separate WHERE-source_file
    # queries. On a 150k-drawer palace each per-file query costs ~2s, so a
    # 2000-file sweep used to spend >1h just deciding to skip.
    # prefetch_mined_set() does the same decisions in a single scan; loop
    # body becomes an O(1) dict lookup + a cheap local mtime comparison.
    mined_mtimes, normalized_states = _prefetch_conversation_states(
        files,
        collection,
        dry_run,
        wing,
        extract_mode,
    )

    total_drawers = 0
    files_mined = 0
    files_skipped = 0
    files_processed = 0
    room_counts = defaultdict(int)

    for i, filepath in enumerate(files, 1):
        files_processed = i
        source_file = str(filepath)
        probe = _probe_normalized_conversation(
            filepath,
            convo_path,
            extract_mode,
        )

        # Skip only if already filed at the current NORMALIZE_VERSION AND
        # unchanged on disk since. Transcripts are NOT assumed immutable:
        # a Claude Code session keeps appending to the same file while
        # active, and /compact or /clear can rewrite one in place -- so
        # "we've seen this source_file before" alone is not sufficient.
        # Falling through re-mines. Normalized sources stage and verify a new
        # source-versioned generation before retiring stale drawers; generic
        # sources retain their established purge-and-upsert path.
        if not dry_run and _conversation_source_is_unchanged(
            source_file,
            probe,
            cfg_chunk_size,
            mined_mtimes,
            normalized_states,
        ):
            files_skipped += 1
            continue

        if not _is_regular_source_file(filepath, Path(convo_dir).expanduser().resolve()):
            files_skipped += 1
            continue

        normalized = None
        content = None
        if probe is not None:
            normalized = load_normalized_conversation(
                filepath,
                convo_path,
                extract_mode=extract_mode,
            )
            content = normalized.transcript

        # A validated normalized export is already canonical and must remain
        # byte-for-byte intact. Every other source keeps the generic format
        # detection, noise filtering, and optional spellcheck behavior.
        if normalized is None:
            try:
                content = normalize(str(filepath))
            except (OSError, ValueError):
                if not dry_run:
                    _register_file(collection, source_file, wing, agent, extract_mode)
                continue
        if not content or (normalized is None and len(content.strip()) < cfg_min_chunk_size):
            if not dry_run:
                _register_file(collection, source_file, wing, agent, extract_mode)
            continue

        # Chunk — either exchange pairs or general extraction
        chunks = _chunk_conversation_content(
            content,
            normalized,
            extract_mode,
            cfg_chunk_size,
            cfg_min_chunk_size,
        )
        chunk_count = (
            count_normalized_conversation_chunks(normalized, chunk_size=cfg_chunk_size)
            if normalized is not None
            else len(chunks)
        )

        if chunk_count == 0:
            if not dry_run:
                _register_file(collection, source_file, wing, agent, extract_mode)
            continue

        # Detect room from content (general mode uses memory_type instead)
        room = _conversation_room(content, normalized, extract_mode)

        if dry_run:
            if extract_mode == "general":
                from collections import Counter

                type_counts = Counter(c.get("memory_type", "general") for c in chunks)
                types_str = ", ".join(f"{t}:{n}" for t, n in type_counts.most_common())
                print(f"    [DRY RUN] {filepath.name} → {len(chunks)} memories ({types_str})")
            else:
                print(f"    [DRY RUN] {filepath.name} → room:{room} ({chunk_count} drawers)")
            total_drawers += chunk_count
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

        # Lock + reconcile stale + file fresh chunks. The lock serializes
        # concurrent agents for this source.
        drawers_added, room_delta, skipped = _file_chunks_locked(
            collection,
            source_file,
            chunks,
            wing,
            room,
            agent,
            extract_mode,
            authored_at=_extract_authored_at(filepath),
            normalized=normalized,
            chunk_count=chunk_count,
            source_chunk_size=(cfg_chunk_size if normalized is not None else None),
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

    if not dry_run:
        # Compute hallways before the FTS5 validation: the latter opens a direct sqlite
        # connection to the Chroma DB, which can invalidate the live collection handle on
        # some Chroma builds and make the hallway fetch fail.
        _compute_hallways_for_wing_safe(wing, collection, total_drawers, config=palace_config)
        _validate_palace_fts5_after_mine(palace_path)

    print(f"\n{'=' * 55}")
    print("  Done.")
    print(f"  Files processed: {files_processed - files_skipped}")
    print(f"  Files skipped (already filed): {files_skipped}")
    print(f"  Drawers filed: {total_drawers}")
    if room_counts:
        print("\n  By room:")
        for room, count in sorted(room_counts.items(), key=lambda x: x[1], reverse=True):
            print(f"    {room:20} {count} files")
    print('\n  Next: mempalace search "what you\'re looking for"')
    print(f"{'=' * 55}\n")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python convo_miner.py <convo_dir> [--palace PATH] [--limit N] [--dry-run]")
        sys.exit(1)
    from .config import MempalaceConfig

    mine_convos(sys.argv[1], palace_path=MempalaceConfig().palace_path)
