#!/usr/bin/env python3
"""
convo_miner.py — Mine conversations into the palace.

Ingests chat exports (Claude Code, ChatGPT, Slack, plain text transcripts).
Normalizes format, chunks by exchange pair (Q+A = one unit), files to palace.

Same palace as project mining. Different ingest strategy.
"""

import hashlib
import os
import sys
import json
import logging
import stat
from pathlib import Path
from datetime import datetime
from collections import defaultdict
from typing import Optional

from .collision_scan import assert_no_collisions
from .ids import ID_RECIPE, make_convo_drawer_id, make_convo_sentinel_id
from .normalize import _read_source_file, normalize
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
WING_RESOLUTION_VERSION = 1
# Bumping this forces a ONE-TIME full re-mine of every already-filed convo
# drawer (via file_already_mined/prefetch_mined_set's
# min_wing_resolution_version parameter, which they take specifically for
# this), the same way NORMALIZE_VERSION forces a rebuild after a
# normalization-schema change -- except scoped to convo mining only,
# never touching the shared project/format miners (they never pass this
# parameter and see no behavior change). Introduced alongside per-file
# wing resolution (_resolve_wing_for_file) so every file already mined
# under the old single-wing-per-sweep behavior gets reclassified into
# its correct per-project wing exactly once, rather than staying
# collapsed under wing_api until its content happens to change again.
# Bump again only if the wing-resolution ALGORITHM itself changes in a
# way that should force another reclassification pass.
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

    ``source_mtime``, when given, is used directly (captured at read time by
    the caller's ``_read_source_file`` fstat); otherwise falls back to
    ``os.path.getmtime`` for callers that don't have a read-time mtime.
    """
    if source_mtime is None:
        try:
            source_mtime = os.path.getmtime(source_file)
        except OSError:
            pass
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
        "wing_resolution_version": WING_RESOLUTION_VERSION,
    }
    if source_mtime is not None:
        meta["source_mtime"] = source_mtime
    collection.upsert(
        documents=[f"[registry] {source_file}"],
        ids=[sentinel_id],
        metadatas=[meta],
    )


def _source_file_delete_ids(
    collection, source_file: str, extract_mode: str, min_chunk_index: Optional[int] = None
) -> list[str]:
    """Collect drawer IDs for one source file and extraction mode.

    Legacy conversation drawers did not carry extract_mode; treat those as
    exchange-mode rows so schema rebuilds can still clean them up without
    deleting newer general-mode drawers for the same transcript.

    ``min_chunk_index``, when given, additionally restricts this to
    drawers whose ``chunk_index`` is at or above it -- the incremental-
    mining path's partial purge (replace only the trailing exchange
    onward, keep the rest). A drawer with no usable ``chunk_index``
    (legacy rows, or the 0-chunk registry sentinel) is excluded rather
    than assumed to qualify, since "no chunk_index" isn't ">= N" for any
    N -- it's simply not a candidate for this comparison, and this
    filter is never used for the registry sentinel's own source_file
    scan anyway (that always goes through the ``None`` / delete-all
    path). Matched in Python rather than pushed into the ``where``
    query because ``extract_mode`` legacy-matching already requires
    per-row Python logic here (see above) that ChromaDB's ``where``
    can't express.
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
            meta = meta or {}
            if not _metadata_matches_extract_mode(meta, extract_mode):
                continue
            if min_chunk_index is not None:
                chunk_index = meta.get("chunk_index")
                if not isinstance(chunk_index, (int, float)) or isinstance(chunk_index, bool):
                    continue
                if chunk_index < min_chunk_index:
                    continue
            ids.append(drawer_id)
        if not batch_ids:
            break
        offset += len(batch_ids)
    return ids


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


def _normalize_or_register(filepath: Path, raw_content: str) -> Optional[str]:
    """normalize() ``raw_content``, swallowing the same ``(OSError,
    ValueError)`` the caller's own read step already tolerates, and
    returning None on failure -- the caller's existing "empty/too-short
    content" check already registers a sentinel and continues for a
    falsy ``content``, so that single check handles this failure case
    too rather than duplicating the registration here (which would add
    its own try/except to ``_mine_convos_impl``'s own body).
    """
    try:
        return normalize(str(filepath), content=raw_content)
    except (OSError, ValueError):
        return None


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


def _resolve_chunk_params(chunk_size: Optional[int], min_chunk_size: Optional[int]) -> tuple:
    """Resolve chunk_size/min_chunk_size against this module's defaults and
    validate them with the same rules ``chunk_exchanges`` enforces.

    ``_compute_convo_cursor`` and ``_incremental_reparse`` both call
    ``_chunk_by_exchange`` directly, bypassing ``chunk_exchanges``' own
    dispatcher (see their docstrings for why) -- which means they also
    bypass its upfront validation. Without this, a non-positive
    chunk_size reaches ``_emit_bounded``'s ``range(0, len(content),
    chunk_size)`` and fails with a confusing ``ValueError`` deep inside
    unrelated code instead of a clear one at the actual bad input.
    """
    resolved_chunk_size = chunk_size if chunk_size is not None else CHUNK_SIZE
    resolved_min_chunk_size = min_chunk_size if min_chunk_size is not None else MIN_CHUNK_SIZE
    if resolved_chunk_size <= 0:
        raise ValueError(f"chunk_size must be > 0, got {resolved_chunk_size}")
    if resolved_min_chunk_size < 0:
        raise ValueError(f"min_chunk_size must be >= 0, got {resolved_min_chunk_size}")
    return resolved_chunk_size, resolved_min_chunk_size


def _compute_convo_cursor(
    raw_content: str,
    num_chunks: int,
    chunk_size: int = None,
    min_chunk_size: int = None,
) -> Optional[dict]:
    """Compute the incremental-mining cursor for a Claude Code JSONL
    transcript: the raw line where the trailing exchange began, and which
    physical chunk it first occupies, so a future re-mine of a
    grown/extended session can resume from there instead of reprocessing
    the whole file. This only COMPUTES and STORES the cursor -- nothing
    yet reads or acts on it, so this changes no existing mining behavior.

    ``raw_content`` must be the SAME content the caller already read and
    passed to ``normalize()`` to produce the chunks being filed this
    pass -- not a fresh re-read of the file. A second, independent read
    here could observe a different state of a file that's actively being
    appended to (the exact scenario this feature exists for), producing
    a cursor that describes content different from what was actually
    mined in this pass.

    ``chunk_size``/``min_chunk_size`` must match whatever the caller
    passed to the real ``chunk_exchanges(content, ...)`` call that
    produced ``num_chunks`` -- they're needed here to correctly count how
    many physical chunks the trailing exchange itself occupies (see
    below); a mismatch would silently point the cursor at the wrong
    chunk. Defaults to this module's own ``CHUNK_SIZE``/``MIN_CHUNK_SIZE``
    when omitted, matching ``chunk_exchanges``' own defaults.

    Returns None (no cursor recorded, meaning "no incremental path
    available, full reprocess next time" -- always a safe default) when:
    - there are no chunks to anchor on;
    - the file doesn't parse as Claude Code JSONL specifically (the only
      format currently verified append-only across /compact and /clear;
      every other format falls back to full reprocess, unconditionally,
      by simply never getting a cursor);
    - ``chunk_exchanges`` would fall back to paragraph/character-offset
      chunking for this content (fewer than 3 quoted lines) rather than
      real exchange-pair chunking -- "the last user-role message" has no
      correspondence to "the last chunk" in that mode, so a cursor
      computed here would silently attach wrong position data to an
      unrelated chunk. Mirrors ``chunk_exchanges``' own ``quote_lines >=
      3`` condition exactly, checked against the same parsed transcript
      text, so the two can't silently drift out of sync with each other.

    A long trailing exchange can itself span more than one physical
    chunk (its AI response alone exceeds ``chunk_size``) -- the cursor
    must point at the FIRST of those chunks, not the last, or a future
    incremental re-mine would treat the earlier ones as part of the
    stable, unaffected prefix when they actually belong to the same
    (about-to-be-regenerated) trailing exchange. Found by re-chunking
    just the trailing exchange's own text in isolation (via
    ``_chunk_by_exchange`` directly, not the public dispatcher --  same
    reasoning as above: its own quote-line count is not the right gate
    here) and counting how many chunks that alone produces.

    Scoped to extract_mode="exchange" only for now (the caller does not
    invoke this for "general" mode): chunk_exchanges' one-chunk-per-user-
    turn shape maps directly onto "the last user-role message", but
    general mode's chunking doesn't share that direct correspondence and
    needs its own analysis before it can get the same treatment.
    """
    if num_chunks <= 0:
        return None

    resolved_chunk_size, resolved_min_chunk_size = _resolve_chunk_params(chunk_size, min_chunk_size)

    from .normalize import _messages_to_transcript, _try_claude_code_jsonl

    parsed = _try_claude_code_jsonl(raw_content, track_positions=True)
    if parsed is None:
        return None

    transcript_lines = parsed.text.split("\n")
    quote_lines = sum(1 for line in transcript_lines if line.strip().startswith(">"))
    if quote_lines < 3:
        return None

    last_user_idx = None
    for i in range(len(parsed.messages) - 1, -1, -1):
        if parsed.messages[i][0] == "user":
            last_user_idx = i
            break
    if last_user_idx is None:
        return None

    anchor_line_index = parsed.start_lines[last_user_idx]
    raw_lines = raw_content.strip().split("\n")
    if anchor_line_index >= len(raw_lines):
        return None
    anchor_line = raw_lines[anchor_line_index]

    trailing_text = _messages_to_transcript(parsed.messages[last_user_idx:])
    trailing_chunk_count = len(
        _chunk_by_exchange(trailing_text.split("\n"), resolved_chunk_size, resolved_min_chunk_size)
    )
    if trailing_chunk_count <= 0 or trailing_chunk_count > num_chunks:
        return None

    return {
        "cursor_line": anchor_line_index,
        "cursor_chunk_index": num_chunks - trailing_chunk_count,
        "cursor_anchor_hash": hashlib.sha256(anchor_line.encode("utf-8")).hexdigest(),
        "cursor_format": "claude_code_jsonl",
    }


def _incremental_reparse(
    cursor: dict,
    raw_content: str,
    chunk_size: int = None,
    min_chunk_size: int = None,
) -> Optional[list]:
    """Re-chunk only the trailing exchange onward, using a cursor stored
    by a prior mine, instead of re-chunking a whole (possibly huge)
    transcript for content that hasn't changed since.

    Nothing calls this yet -- it's a standalone unit, verified by
    equivalence tests to produce output identical to what a full re-mine
    of the same grown transcript would, without doing the full-transcript
    work.

    Returns None -- meaning "the append-only assumption this rests on
    doesn't hold (or can't be confirmed) for this file right now; the
    caller must fall back to a full re-mine" -- when:
    - the cursor's format isn't "claude_code_jsonl" (the only format this
      supports, matching ``_compute_convo_cursor``);
    - ``raw_content`` is now SHORTER than the anchor line (the file was
      truncated or rewritten, not grown);
    - the anchor line's content no longer matches the stored hash (the
      prefix up to and including the cursor was edited, not purely
      appended to -- the core precondition this whole feature depends on
      was violated for this file);
    - the trailing content, parsed on its own starting exactly at the
      anchor line, doesn't parse as a real Claude Code JSONL exchange
      (e.g. the file's tail is mid-write and momentarily has only a lone
      trailing user turn with no response yet -- rare, and always safe
      to fall back on rather than guess at incomplete data).

    When it returns a chunk list, ``chunks[i]["chunk_index"]`` continues
    the SAME global numbering the cursor's originating mine used --
    values start at ``cursor["cursor_chunk_index"]``, not 0 -- so a
    caller can replace exactly the drawers at or after that index
    (delete where chunk_index >= cursor_chunk_index, upsert this list)
    instead of the whole file's drawers.

    Re-chunks via ``_chunk_by_exchange`` directly rather than the public
    ``chunk_exchanges`` dispatcher: the tail slice's OWN quote-line count
    can fall under the exchange-vs-paragraph threshold even when the
    full transcript is well above it (the common case -- growing a
    session by one more exchange adds one '>' line to a slice that has
    at most a couple), which would wrongly switch just the tail to
    paragraph chunking. The append-only-verified precondition already
    establishes that the exchange-pair path applies to this transcript;
    the tail is a suffix of it, not a fresh decision point.

    A malformed ``cursor`` (missing/wrong-typed keys, or not a dict at
    all -- e.g. stored drawer metadata that predates this field or was
    hand-edited) is just another way the preconditions above can fail,
    so it also gets a safe ``None`` rather than raising ``KeyError`` /
    ``AttributeError`` / ``TypeError`` on the caller.
    """
    resolved_chunk_size, resolved_min_chunk_size = _resolve_chunk_params(chunk_size, min_chunk_size)

    if not isinstance(cursor, dict) or cursor.get("cursor_format") != "claude_code_jsonl":
        return None

    cursor_line = cursor.get("cursor_line")
    cursor_chunk_index = cursor.get("cursor_chunk_index")
    cursor_anchor_hash = cursor.get("cursor_anchor_hash")
    if (
        not isinstance(cursor_line, int)
        or isinstance(cursor_line, bool)
        or not isinstance(cursor_chunk_index, int)
        or isinstance(cursor_chunk_index, bool)
        or not isinstance(cursor_anchor_hash, str)
        or not isinstance(raw_content, str)
    ):
        return None

    raw_lines = raw_content.strip().split("\n")
    if cursor_line < 0 or cursor_line >= len(raw_lines):
        return None

    anchor_line = raw_lines[cursor_line]
    if hashlib.sha256(anchor_line.encode("utf-8")).hexdigest() != cursor_anchor_hash:
        return None

    from .normalize import _try_claude_code_jsonl

    tail_raw = "\n".join(raw_lines[cursor_line:])
    tail_text = _try_claude_code_jsonl(tail_raw)
    if tail_text is None:
        return None

    tail_chunks = _chunk_by_exchange(
        tail_text.split("\n"), resolved_chunk_size, resolved_min_chunk_size
    )
    if not tail_chunks:
        return None

    for chunk in tail_chunks:
        chunk["chunk_index"] += cursor_chunk_index
    return tail_chunks


def _fetch_stored_cursor(collection, source_file: str, extract_mode: str) -> Optional[dict]:
    """Look up the cursor stored by the last full mine of ``source_file``
    (see ``_compute_convo_cursor``), if any -- the anchor an incremental
    re-mine can attempt to resume from.

    Only scans this one source file's own drawers (bounded by however
    many chunks that file produced, not the whole palace), and is only
    called for files already confirmed changed-and-previously-mined --
    the same small subset of files that are about to pay the (larger)
    cost of re-chunking anyway, not every file in the sweep.

    Returns None when no drawer for this source_file/extract_mode
    carries cursor metadata (never computed -- wrong format, general
    mode, below the exchange-chunking threshold, etc.), when the only
    cursor-bearing drawer found is stamped with an older
    ``normalize_version`` (a schema bump means the stored chunk/cursor
    shape may not match what the current normalize/chunk pipeline would
    produce -- a full rebuild is required, same as it already is for
    any other stale-schema drawer), or when more than one candidate
    somehow does (a data inconsistency; safer to fall back to a full
    re-mine than guess which one is current). This function does not
    rely on its caller having already excluded stale-schema files (even
    though today's only caller does, via ``mined_mtimes``) -- the
    version check is repeated here so this function's own correctness
    doesn't depend on staying in sync with a gate several frames away.

    The returned dict also carries a ``"room"`` key -- the topic
    classification stored on that same drawer -- so a caller doing an
    incremental update can reuse the file's existing room rather than
    reclassify from a partial/tail-only sample. This is NOT one of the
    four cursor fields (``cursor_line``/``cursor_chunk_index``/
    ``cursor_anchor_hash``/``cursor_format``) that ``_incremental_reparse``
    validates -- it's along for the ride for this function's own callers.
    """
    found: Optional[dict] = None
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
        for meta in metadatas:
            meta = meta or {}
            if not _metadata_matches_extract_mode(meta, extract_mode):
                continue
            if not meta.get("cursor_format"):
                continue
            if meta.get("normalize_version", 1) < NORMALIZE_VERSION:
                continue
            if found is not None:
                return None
            found = {
                "cursor_line": meta.get("cursor_line"),
                "cursor_chunk_index": meta.get("cursor_chunk_index"),
                "cursor_anchor_hash": meta.get("cursor_anchor_hash"),
                "cursor_format": meta.get("cursor_format"),
                "room": meta.get("room"),
            }
        if not batch_ids:
            break
        offset += len(batch_ids)
    return found


def _flag_or_drop_duplicates(
    collection,
    source_file: str,
    batch_docs: list,
    batch_ids: list,
    batch_metas: list,
    batch_rooms: list,
) -> int:
    """Flag chunks whose closest existing match is a probable duplicate,
    and -- only when separately opted in -- drop the near-certain ones
    entirely instead of inserting them.

    Opt-in (MempalaceConfig.duplicate_detection_enabled, off by default):
    adds a batched cosine-similarity query per upsert batch on top of the
    embedding mining already computes -- a real per-batch cost. Off by
    default so this changes nothing unless explicitly enabled.

    Flagging never alters an insert -- it only attaches
    possible_duplicate_of / duplicate_similarity metadata to a chunk
    whose closest match is >= duplicate_detection_threshold (0.9 by
    default). Dropping is a second, separate opt-in
    (MempalaceConfig.duplicate_drop_enabled, also off by default): a
    chunk whose closest match clears the stricter duplicate_drop_threshold
    (0.97 by default) is removed from batch_docs/batch_ids/batch_metas/
    batch_rooms IN PLACE before this returns, so it's never upserted at
    all -- unlike flagging, this really does lose the content
    permanently on a false positive, which is why it needs its own
    higher bar and its own opt-in rather than reusing the flag
    threshold. drop_threshold is clamped to never go below
    duplicate_detection_threshold here, so a misconfigured lower value
    can't make dropping easier to trigger than flagging (find_near_duplicates
    already only returns matches at or above duplicate_detection_threshold,
    so an unclamped lower drop_threshold would drop everything that gets
    flagged).

    Returns the number of chunks dropped (0 when dropping is off, or
    when nothing clears the drop threshold) -- the caller's own
    mine-summary reporting.
    """
    from .config import MempalaceConfig

    cfg = MempalaceConfig()
    if not cfg.duplicate_detection_enabled:
        return 0

    from .searcher import find_near_duplicates

    matches = find_near_duplicates(
        collection, batch_docs, threshold=cfg.duplicate_detection_threshold
    )

    drop_enabled = cfg.duplicate_drop_enabled
    drop_threshold = max(cfg.duplicate_detection_threshold, cfg.duplicate_drop_threshold)

    drop_indices = []
    for i, match in enumerate(matches):
        if match is None:
            continue
        drawer_id, similarity = match
        if drop_enabled and similarity >= drop_threshold:
            drop_indices.append(i)
            logger.debug(
                "Dropped near-duplicate chunk (similarity=%.3f, matches %s) for %s",
                similarity,
                drawer_id,
                source_file,
            )
            continue
        batch_metas[i]["possible_duplicate_of"] = drawer_id
        batch_metas[i]["duplicate_similarity"] = similarity

    if not drop_indices:
        return 0

    drop_set = set(drop_indices)
    kept = [i for i in range(len(batch_docs)) if i not in drop_set]
    batch_docs[:] = [batch_docs[i] for i in kept]
    batch_ids[:] = [batch_ids[i] for i in kept]
    batch_metas[:] = [batch_metas[i] for i in kept]
    batch_rooms[:] = [batch_rooms[i] for i in kept]
    return len(drop_indices)


def _upsert_chunk_batch(
    collection,
    source_file,
    chunks,
    wing,
    room,
    agent,
    extract_mode,
    filed_at,
    source_mtime,
    authored_at,
    cursor,
) -> tuple:
    """Batch-upsert ``chunks`` for one source file: build each chunk's
    metadata (room/hall/entities, cursor stamping) and flag possible
    duplicates before insert. Shared by the full-rebuild path
    (``_file_chunks_locked``) and the incremental-update path
    (``_file_chunks_locked_incremental``) so the metadata construction
    can't drift between the two.

    ``cursor``, when given (see ``_compute_convo_cursor``), is stamped
    onto the metadata of whichever chunk's ``chunk_index`` matches
    ``cursor["cursor_chunk_index"]`` -- the caller decides what that
    cursor describes (a full mine's trailing exchange, or an
    incremental mine's new trailing exchange); this function only
    matches indices.

    Returns (drawers_added, room_counts_delta, dropped_count).
    ``dropped_count`` is always 0 unless both
    ``duplicate_detection_enabled`` and ``duplicate_drop_enabled`` are
    on (see ``_flag_or_drop_duplicates``).
    """
    room_counts_delta: dict = defaultdict(int)
    drawers_added = 0
    dropped_count = 0
    for batch_start in range(0, len(chunks), DRAWER_UPSERT_BATCH_SIZE):
        batch_docs: list = []
        batch_ids: list = []
        batch_metas: list = []
        # Parallel to the three lists above, tracked separately from
        # room_counts_delta so a chunk dropped as a near-certain
        # duplicate (see below) doesn't get counted toward a room it
        # never actually ends up filed under.
        batch_rooms: list = []
        for chunk in chunks[batch_start : batch_start + DRAWER_UPSERT_BATCH_SIZE]:
            chunk_room = chunk.get("memory_type", room) if extract_mode == "general" else room
            drawer_id = make_convo_drawer_id(
                wing, chunk_room, source_file, extract_mode, chunk["chunk_index"]
            )
            batch_docs.append(chunk["content"])
            batch_ids.append(drawer_id)
            batch_rooms.append(chunk_room)
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
                "wing_resolution_version": WING_RESOLUTION_VERSION,
            }
            if source_mtime is not None:
                meta["source_mtime"] = source_mtime
            if cursor is not None and chunk["chunk_index"] == cursor["cursor_chunk_index"]:
                meta["cursor_line"] = cursor["cursor_line"]
                meta["cursor_chunk_index"] = cursor["cursor_chunk_index"]
                meta["cursor_anchor_hash"] = cursor["cursor_anchor_hash"]
                meta["cursor_format"] = cursor["cursor_format"]
            batch_metas.append(meta)
        # May shrink batch_docs/batch_ids/batch_metas/batch_rooms in
        # place (dropping near-certain duplicates) -- must run before
        # assert_no_collisions and the room_counts_delta tally below, so
        # neither one considers a chunk that's about to be dropped.
        #
        # If the chunk carrying the cursor above is itself dropped here,
        # its cursor metadata is lost with it -- nothing rescues it onto
        # a different surviving chunk. Accepted, not fixed:
        # _fetch_stored_cursor finds no cursor on this file's next mine
        # and falls back to the full re-mine path (already the safe
        # default for "no cursor available"), which recomputes and
        # reattaches a fresh cursor to whatever ends up as the new last
        # chunk -- a one-time loss of the incremental-mining shortcut for
        # this file, not a permanent one.
        dropped_count += _flag_or_drop_duplicates(
            collection, source_file, batch_docs, batch_ids, batch_metas, batch_rooms
        )
        if extract_mode == "general":
            for r in batch_rooms:
                room_counts_delta[r] += 1
        if not batch_docs:
            continue
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
    return drawers_added, room_counts_delta, dropped_count


def _file_chunks_locked(
    collection,
    source_file,
    chunks,
    wing,
    room,
    agent,
    extract_mode,
    authored_at=None,
    cursor=None,
    source_mtime=None,
):
    """Lock the source file, purge stale drawers, and upsert fresh chunks.

    Combines the per-file serialization that prevents concurrent agents from
    duplicating work (via mine_lock) with the rebuild contract
    (purge-before-insert so stale drawers never survive) that fires on
    either a normalize-version bump OR a changed/grown source file (mtime
    differs from what's stored) -- transcripts are not assumed immutable,
    since a Claude Code session keeps appending to its own file while
    active and /compact or /clear can rewrite one in place.

    ``cursor``, when given (see ``_compute_convo_cursor``), is stamped onto
    the LAST chunk's metadata only -- the trailing exchange is the anchor
    a future incremental re-mine would resume from.

    Returns (drawers_added, room_counts_delta, skipped, dropped_count).
    """
    with mine_lock(source_file):
        # Re-check after lock — another agent may have just finished this file
        # at the current schema/mtime. A stale hit here returns False, so we
        # still fall through to the purge+rebuild path below.
        if file_already_mined(
            collection,
            source_file,
            check_mtime=True,
            extract_mode=extract_mode,
            min_wing_resolution_version=WING_RESOLUTION_VERSION,
        ):
            return 0, defaultdict(int), True, 0

        # Purge stale drawers first. Fires both on a normalize-schema bump
        # (file_already_mined() returned False for pre-v2 drawers) and on a
        # changed/grown transcript (mtime differs) — clean them out so the
        # source doesn't end up with mixed old/new drawers.
        try:
            delete_ids = _source_file_delete_ids(collection, source_file, extract_mode)
            if delete_ids:
                collection.delete(ids=delete_ids)
        except Exception:
            logger.debug("Stale-drawer purge failed for %s", source_file, exc_info=True)

        # Batch chunks into bounded upserts so large transcripts keep most of
        # the embedding speedup without one huge Chroma/SQLite request. Keep
        # one filed_at per source file so all transcript drawers share an
        # ingest timestamp.
        filed_at = datetime.now().isoformat()
        # source_mtime was captured at read time by the caller; fall back
        # to getmtime only for legacy callers that don't pass it.
        if source_mtime is None:
            try:
                source_mtime = os.path.getmtime(source_file)
            except OSError:
                pass
        drawers_added, room_counts_delta, dropped_count = _upsert_chunk_batch(
            collection,
            source_file,
            chunks,
            wing,
            room,
            agent,
            extract_mode,
            filed_at,
            source_mtime,
            authored_at,
            cursor,
        )
    return drawers_added, room_counts_delta, False, dropped_count


def _file_chunks_locked_incremental(
    collection,
    source_file,
    tail_chunks,
    replace_from_index,
    wing,
    room,
    agent,
    extract_mode,
    authored_at=None,
    cursor=None,
    source_mtime=None,
):
    """Incremental-mining counterpart to ``_file_chunks_locked``: replaces
    only the drawers at or after ``replace_from_index`` instead of
    purging the whole file, when ``_incremental_reparse`` produced a
    usable ``tail_chunks`` list for a grown transcript.

    Same locking/recheck contract as ``_file_chunks_locked`` -- another
    agent may have already handled this exact on-disk state under the
    lock, in which case this returns ``skipped=True`` and makes no
    changes, same as the full-rebuild path.

    Returns (drawers_added, room_counts_delta, skipped, dropped_count).
    """
    with mine_lock(source_file):
        if file_already_mined(
            collection,
            source_file,
            check_mtime=True,
            extract_mode=extract_mode,
            min_wing_resolution_version=WING_RESOLUTION_VERSION,
        ):
            return 0, defaultdict(int), True, 0

        try:
            delete_ids = _source_file_delete_ids(
                collection, source_file, extract_mode, min_chunk_index=replace_from_index
            )
            if delete_ids:
                collection.delete(ids=delete_ids)
        except Exception:
            logger.debug("Incremental purge failed for %s", source_file, exc_info=True)

        filed_at = datetime.now().isoformat()
        if source_mtime is None:
            try:
                source_mtime = os.path.getmtime(source_file)
            except OSError:
                pass
        drawers_added, room_counts_delta, dropped_count = _upsert_chunk_batch(
            collection,
            source_file,
            tail_chunks,
            wing,
            room,
            agent,
            extract_mode,
            filed_at,
            source_mtime,
            authored_at,
            cursor,
        )
    return drawers_added, room_counts_delta, False, dropped_count


def _path_parts_or_empty(path: Path) -> tuple:
    """Resolve ``path.parts``, treating any resolution failure (broken
    symlink, permission error) as "no parts" rather than raising -- shared
    by the AI-tool-path detection helpers below, which must never crash
    mid-scan on an unusual path.
    """
    try:
        return path.resolve().parts
    except (OSError, RuntimeError):
        return ()


def _is_claude_code_projects_path(path: Path) -> bool:
    """True when `path` lives under a ``.claude/projects`` tree
    specifically (Claude Code sessions) -- narrower than
    ``_is_ai_tool_path``, which also matches ``.codex``/``.gemini``.

    Per-file cwd-based wing resolution (``_resolve_wing_for_file``) is
    scoped per format: Claude Code JSONL is confirmed to carry a
    top-level ``cwd`` field on message records (see
    ``convo_scanner._extract_cwd_from_session``).
    """
    parts = _path_parts_or_empty(path)
    for i in range(len(parts) - 1):
        if parts[i] == ".claude" and parts[i + 1] == "projects":
            return True
    return False


def _is_codex_path(path: Path) -> bool:
    """True when `path` lives under a ``.codex`` tree (Codex CLI sessions).

    Codex nests its own ``cwd`` differently than Claude Code -- under
    ``payload`` on ``session_meta``/``turn_context`` records rather than
    top-level on every message -- so it gets its own extractor
    (``_extract_cwd_from_codex_session``) rather than reusing
    ``convo_scanner``'s Claude-Code-specific one.
    """
    return ".codex" in _path_parts_or_empty(path)


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
    ``wing_api`` when the user hasn't passed an explicit ``--wing``, and
    (in its narrower ``_is_claude_code_projects_path``/``_is_codex_path``
    forms) by ``_resolve_wing_for_file`` to decide which per-file
    cwd-extraction strategy applies, if any.
    """
    parts = _path_parts_or_empty(path)
    if ".codex" in parts:
        return True
    if ".gemini" in parts:
        return True
    return _is_claude_code_projects_path(path)


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


_CODEX_CWD_RECORD_TYPES = ("session_meta", "turn_context")
_CODEX_CWD_MAX_LINES = 20  # matches convo_scanner.MAX_HEADER_LINES


def _extract_cwd_from_codex_session(session_file: Path) -> Optional[str]:
    """Return the ``cwd`` from the first Codex CLI record that carries one.

    Codex CLI sessions (``~/.codex/sessions/**/*.jsonl``) nest ``cwd``
    under ``payload``, on ``session_meta`` or ``turn_context`` records --
    confirmed against real session files, e.g.
    ``{"type": "session_meta", "payload": {"cwd": "/Users/x/Code/y", ...}}``.
    This is a structurally different location than Claude Code's, where
    ``cwd`` is a top-level field on every message record (see
    ``convo_scanner._extract_cwd_from_session``), so it gets its own
    extractor rather than reusing that one.

    Returns None if the file can't be read, has no JSON, or no record in
    the first ``_CODEX_CWD_MAX_LINES`` lines carries a ``payload.cwd``.
    """
    try:
        with open(session_file, encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i >= _CODEX_CWD_MAX_LINES:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict) or obj.get("type") not in _CODEX_CWD_RECORD_TYPES:
                    continue
                payload = obj.get("payload")
                if not isinstance(payload, dict):
                    continue
                cwd = payload.get("cwd")
                if isinstance(cwd, str) and cwd:
                    return cwd
    except OSError:
        return None
    return None


def _resolve_wing_for_file(filepath: Path, explicit_wing: Optional[str], default_wing: str) -> str:
    """Per-file wing resolution for one conversation file within a
    ``mine_convos()`` sweep -- segregates a multi-project directory (most
    commonly the whole ``~/.claude/projects``) into one wing per project
    instead of collapsing every session into a single ``wing_api`` bucket.

    Precedence:

      1. ``explicit_wing`` (the caller's ``--wing`` argument) always wins
         -- checked against the ORIGINAL argument value, never the
         resolved sentinel. ``mine --wing wing_api`` must be
         indistinguishable from any other explicit choice, not silently
         overridden by per-file detection just because it happens to
         match the AI-tool-path default -- a real bug in an earlier,
         since-abandoned upstream attempt at this exact feature
         (MemPalace/mempalace#1757: ``if wing == "wing_api"`` couldn't
         tell an explicit choice from the auto-routed sentinel).
      2. A file under ``.claude/projects`` gets its own project identity
         from that session's own ``cwd``
         (``convo_scanner._extract_cwd_from_session``).
      3. A file under ``.codex`` gets the same treatment via its own
         nested-field extractor (``_extract_cwd_from_codex_session``).
      4. Anything else (``.gemini`` -- no confirmed cwd-equivalent field,
         no real session data or upstream reference to verify one against
         -- or a plain non-AI-tool directory) falls back to
         ``default_wing``, unchanged from today's behavior.

    Falls back to ``default_wing`` whenever the relevant extractor
    returns None (unreadable/malformed session, or no cwd found in the
    first ~20 lines) -- always safe.
    """
    if explicit_wing:
        return explicit_wing

    from .config import normalize_wing_name
    from .convo_scanner import _extract_cwd_from_session, _project_name_from_cwd

    cwd = None
    if _is_claude_code_projects_path(filepath):
        cwd = _extract_cwd_from_session(filepath)
    elif _is_codex_path(filepath):
        cwd = _extract_cwd_from_codex_session(filepath)

    if not cwd:
        return default_wing
    project_name = _project_name_from_cwd(cwd)
    if not project_name:
        return default_wing
    return normalize_wing_name(project_name)


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


def _compute_hallways_for_all_wings(wing_drawer_counts: dict, collection) -> None:
    """One `_compute_hallways_for_wing_safe` call per distinct wing
    touched this mine -- a multi-project sweep no longer files everything
    under a single wing, so a single hallway computation (the pre-
    per-file-wing-resolution behavior) would miss every wing but one.
    """
    for w, w_drawers in wing_drawer_counts.items():
        _compute_hallways_for_wing_safe(w, collection, w_drawers)


def _wing_header_label(wing_arg: Optional[str], default_wing: str) -> str:
    """Text for the mine summary's "Wing:" header line -- notes that
    per-file resolution may override `default_wing` for individual
    Claude Code/Codex sessions, unless the caller passed an explicit
    ``--wing`` (which always wins outright and needs no such caveat).
    """
    if wing_arg:
        return default_wing
    return f"{default_wing} (per-file for Claude Code/Codex sessions)"


def _print_wing_breakdown(wing_counts: dict) -> None:
    """ "By wing:" section of the mine summary -- only when a sweep
    actually touched more than one wing (the common single-wing case,
    e.g. an explicit --wing or a non-AI-tool directory, prints nothing
    extra here, unchanged from before per-file wing resolution existed).
    """
    if len(wing_counts) <= 1:
        return
    print("\n  By wing:")
    for w, count in sorted(wing_counts.items(), key=lambda x: x[1], reverse=True):
        print(f"    {w:20} {count} files")


def _attempt_incremental_mine(
    collection,
    source_file: str,
    raw_content: str,
    wing: str,
    agent: str,
    extract_mode: str,
    filepath: Path,
    palace_config,
    cfg_chunk_size: int,
    cfg_min_chunk_size: int,
    mined_mtimes: dict,
    dry_run: bool,
    source_mtime: Optional[float] = None,
) -> Optional[tuple]:
    """Try the incremental-mining path for one changed file: re-chunk only
    the trailing exchange onward instead of purging and rebuilding the
    whole file. Extracted out of ``_mine_convos_impl``'s loop body to keep
    it readable (and under this repo's function-complexity gate).

    Returns None whenever the attempt doesn't apply or can't proceed --
    extract_mode is "general" (no cursor to resume from, see
    ``_compute_convo_cursor``), the feature isn't opted in
    (``incremental_mining_enabled``, off by default), this file has no
    prior mine to be incremental relative to (``mined_mtimes`` holds no
    stored mtime for it), no stored cursor is found on its existing
    drawers, or ``_incremental_reparse`` itself declines (any of its own
    preconditions failing). The caller falls back to the existing full
    path in every ``None`` case -- always safe.

    Otherwise returns ``(drawers_added, room_counts_delta, skipped,
    room, dropped_count)`` -- the same four values
    ``_file_chunks_locked`` / ``_file_chunks_locked_incremental``
    return, plus the file's room (reused from its existing
    classification, not recomputed) so the caller can bump
    ``room_counts`` the same way the full path does.
    """
    if (
        dry_run
        or extract_mode == "general"
        or not palace_config.incremental_mining_enabled
        or mined_mtimes.get(source_file) is None
    ):
        return None

    stored_cursor = _fetch_stored_cursor(collection, source_file, extract_mode)
    if stored_cursor is None:
        return None

    tail_chunks = _incremental_reparse(
        stored_cursor, raw_content, chunk_size=cfg_chunk_size, min_chunk_size=cfg_min_chunk_size
    )
    if not tail_chunks:
        return None

    # Reuse the file's existing room classification rather than
    # reclassify from a partial/tail-only sample -- incremental mining
    # deliberately leaves everything about the stable prefix untouched,
    # including its topic classification. This matches what a full
    # re-mine would produce whenever the file was already past
    # detect_convo_room's ~3000-character sample size at its first mine
    # (the common case for a real transcript) -- it can only diverge
    # for a file still under that size when first mined, which then
    # keeps its original room indefinitely rather than picking up
    # whatever a later full re-mine's fresh sample would classify (see
    # MempalaceConfig.incremental_mining_enabled's docstring).
    room = stored_cursor.get("room")

    # The grown file's new total chunk count, without re-chunking it:
    # chunks before the OLD cursor are untouched (this is the whole
    # point), and tail_chunks' own chunk_index values already continue
    # that same global numbering (see _incremental_reparse), so the last
    # one IS the new total minus one.
    num_chunks_new_total = stored_cursor["cursor_chunk_index"] + len(tail_chunks)

    # Recomputing the cursor still parses the whole (grown) file once --
    # a real but bounded cost (JSONL parse + per-message noise-strip),
    # well short of the chunking, entity/hall detection, duplicate-
    # checking, and embedding work this path skips for the untouched
    # stable prefix, which is what actually dominates for large
    # transcripts. Making the cursor recomputation itself incremental
    # too is a natural follow-up, not required for this rollout.
    new_cursor = _compute_convo_cursor(
        raw_content,
        num_chunks_new_total,
        chunk_size=cfg_chunk_size,
        min_chunk_size=cfg_min_chunk_size,
    )

    drawers_added, room_delta, skipped, dropped_count = _file_chunks_locked_incremental(
        collection,
        source_file,
        tail_chunks,
        stored_cursor["cursor_chunk_index"],
        wing,
        room,
        agent,
        extract_mode,
        authored_at=_extract_authored_at(filepath),
        cursor=new_cursor,
        source_mtime=source_mtime,
    )
    return drawers_added, room_delta, skipped, room, dropped_count


def _record_mine_outcome(
    room_counts: dict,
    room_delta: dict,
    drawers_added: int,
    skipped: bool,
    i: int,
    total_files: int,
    filepath: Path,
    limit: int,
    files_mined_so_far: int,
    dropped_count: int = 0,
    label: str = "",
) -> tuple:
    """Apply one file's mining outcome -- full or incremental -- to
    ``room_counts``, print the standard progress line, and report the
    counter deltas the caller should apply. Shared by both mining paths
    in ``_mine_convos_impl`` (each just calls this once) so this
    bookkeeping isn't duplicated inline for each, which would otherwise
    count twice against that function's own complexity budget.

    Returns ``(drawers_delta, files_mined_delta, files_skipped_delta,
    dropped_delta, should_break)`` -- plain ints/bool rather than
    mutating the caller's counters directly, since those are simple
    local variables in ``_mine_convos_impl``, not a shared mutable
    object. ``dropped_delta`` is always 0 when ``skipped`` -- a skipped
    file made no changes at all, so it can't have dropped anything
    either.
    """
    if skipped:
        return 0, 0, 1, 0, False
    for r, n in room_delta.items():
        room_counts[r] += n
    new_files_mined = files_mined_so_far + 1
    print(f"  + [{i:4}/{total_files}] {filepath.name[:50]:50} +{drawers_added}{label}")
    should_break = limit > 0 and new_files_mined >= limit
    return drawers_added, 1, 0, dropped_count, should_break


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
    # Preserved separately from the resolved default so per-file wing
    # resolution below can tell "the caller explicitly passed --wing"
    # apart from "the sentinel default happened to resolve to wing_api" --
    # conflating the two was a real bug in an abandoned upstream attempt
    # at this exact feature (MemPalace/mempalace#1757).
    wing_arg = wing
    default_wing = _resolve_wing(convo_path, wing_arg)

    files = scan_convos(convo_dir)

    print(f"\n{'=' * 55}")
    print("  MemPalace Mine — Conversations")
    print(f"{'=' * 55}")
    print(f"  Wing:    {_wing_header_label(wing_arg, default_wing)}")
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
    # min_wing_resolution_version forces the one-time per-project-wing
    # backfill: a file whose stored drawers predate WING_RESOLUTION_VERSION
    # is excluded here, so it reads as "changed" below and falls through
    # to a full re-mine that resolves its correct per-file wing, exactly
    # once -- after that its drawers carry the current version and go
    # back to being skipped normally.
    mined_mtimes: dict = (
        prefetch_mined_set(
            collection,
            extract_mode=extract_mode,
            min_wing_resolution_version=WING_RESOLUTION_VERSION,
        )
        if not dry_run
        else {}
    )

    total_drawers = 0
    files_mined = 0
    files_skipped = 0
    files_processed = 0
    total_dropped = 0
    room_counts = defaultdict(int)
    wing_counts = defaultdict(int)
    wing_drawer_counts = defaultdict(int)

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
        if not dry_run and _is_unchanged_since_last_mine(source_file, mined_mtimes):
            files_skipped += 1
            continue

        if not _is_regular_source_file(filepath, Path(convo_dir).expanduser().resolve()):
            files_skipped += 1
            continue

        # Per-file wing resolution -- computed once past the cheap skip
        # checks above (so a file that's going to be skipped never pays
        # for it), reused for every downstream call in this iteration.
        # Counted toward wing_counts regardless of what happens next
        # (dry-run preview, in-lock skip, or a real mine), matching
        # room_counts' own "counts toward reporting even if skipped"
        # convention below.
        file_wing = _resolve_wing_for_file(filepath, wing_arg, default_wing)
        wing_counts[file_wing] += 1

        # Read once -- a second, independent read (e.g. for cursor
        # computation further below, or the incremental-mining attempt
        # right after) could otherwise observe a different state of a
        # file that's actively being appended to than the one actually
        # mined here.  The mtime captured at read time is threaded to
        # every downstream write so it always describes the bytes that
        # were actually parsed, not a later on-disk state.
        try:
            raw_content, read_mtime = _read_source_file(str(filepath))
        except (OSError, ValueError):
            if not dry_run:
                _register_file(collection, source_file, file_wing, agent, extract_mode)
            continue

        # Try the incremental path FIRST, before paying for a full
        # normalize()+chunk_exchanges() pass over content that (for the
        # stable prefix) hasn't changed. Returns None whenever it doesn't
        # apply or can't proceed (dry run, general mode, not opted in, no
        # prior mine, no usable stored cursor, or _incremental_reparse
        # itself declining) -- always safe, exactly the designed
        # fallback to the existing full path below.
        incremental_result = _attempt_incremental_mine(
            collection,
            source_file,
            raw_content,
            file_wing,
            agent,
            extract_mode,
            filepath,
            palace_config,
            cfg_chunk_size,
            cfg_min_chunk_size,
            mined_mtimes,
            dry_run,
            source_mtime=read_mtime,
        )
        if incremental_result is not None:
            drawers_added, room_delta, skipped, room, dropped_count = incremental_result
            # Bumped regardless of skipped, matching the full path's own
            # ordering below (another agent may have already handled
            # this file under the lock -- it still counts toward this
            # room for reporting purposes).
            room_counts[room] += 1
            drawers_delta, mined_delta, skipped_delta, dropped_delta, should_break = (
                _record_mine_outcome(
                    room_counts,
                    room_delta,
                    drawers_added,
                    skipped,
                    i,
                    len(files),
                    filepath,
                    limit,
                    files_mined,
                    dropped_count=dropped_count,
                    label=" (incremental)",
                )
            )
            total_drawers += drawers_delta
            wing_drawer_counts[file_wing] += drawers_delta
            files_mined += mined_delta
            files_skipped += skipped_delta
            total_dropped += dropped_delta
            if should_break:
                break
            continue

        content = _normalize_or_register(filepath, raw_content)

        if not content or len(content.strip()) < cfg_min_chunk_size:
            if not dry_run:
                _register_file(
                    collection, source_file, file_wing, agent, extract_mode,
                    source_mtime=read_mtime,
                )
            continue

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
                    collection, source_file, file_wing, agent, extract_mode,
                    source_mtime=read_mtime,
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
                print(
                    f"    [DRY RUN] {filepath.name} → wing:{file_wing} → {len(chunks)} memories ({types_str})"
                )
            else:
                print(
                    f"    [DRY RUN] {filepath.name} → wing:{file_wing} room:{room} ({len(chunks)} drawers)"
                )
            total_drawers += len(chunks)
            wing_drawer_counts[file_wing] += len(chunks)
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

        # Compute (but do not yet act on) the incremental-mining cursor.
        # Scoped to exchange mode, where chunk_exchanges' one-chunk-per-
        # user-turn shape maps directly onto "the last user-role message".
        # Returns None (no cursor stored) for any non-Claude-Code-JSONL
        # format, which is exactly the desired "no incremental path for
        # this format" default.
        cursor = (
            _compute_convo_cursor(
                raw_content,
                len(chunks),
                chunk_size=cfg_chunk_size,
                min_chunk_size=cfg_min_chunk_size,
            )
            if extract_mode != "general"
            else None
        )

        # Lock + purge stale + file fresh chunks. Lock serializes concurrent
        # agents; purge removes pre-v2 drawers so the schema bump applies.
        drawers_added, room_delta, skipped, dropped_count = _file_chunks_locked(
            collection,
            source_file,
            chunks,
            file_wing,
            room,
            agent,
            extract_mode,
            authored_at=_extract_authored_at(filepath),
            cursor=cursor,
            source_mtime=read_mtime,
        )
        drawers_delta, mined_delta, skipped_delta, dropped_delta, should_break = (
            _record_mine_outcome(
                room_counts,
                room_delta,
                drawers_added,
                skipped,
                i,
                len(files),
                filepath,
                limit,
                files_mined,
                dropped_count=dropped_count,
            )
        )
        total_drawers += drawers_delta
        wing_drawer_counts[file_wing] += drawers_delta
        files_mined += mined_delta
        files_skipped += skipped_delta
        total_dropped += dropped_delta
        if should_break:
            break

    if not dry_run:
        # Compute hallways before the FTS5 validation: the latter opens a direct sqlite
        # connection to the Chroma DB, which can invalidate the live collection handle on
        # some Chroma builds and make the hallway fetch fail.
        _compute_hallways_for_all_wings(wing_drawer_counts, collection)
        _validate_palace_fts5_after_mine(palace_path)

    print(f"\n{'=' * 55}")
    print("  Done.")
    print(f"  Files processed: {files_processed - files_skipped}")
    print(f"  Files skipped (already filed): {files_skipped}")
    print(f"  Drawers filed: {total_drawers}")
    print(f"  Possible duplicates skipped: {total_dropped}")
    if room_counts:
        print("\n  By room:")
        for room, count in sorted(room_counts.items(), key=lambda x: x[1], reverse=True):
            print(f"    {room:20} {count} files")
    _print_wing_breakdown(wing_counts)
    print('\n  Next: mempalace search "what you\'re looking for"')
    print(f"{'=' * 55}\n")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python convo_miner.py <convo_dir> [--palace PATH] [--limit N] [--dry-run]")
        sys.exit(1)
    from .config import MempalaceConfig

    mine_convos(sys.argv[1], palace_path=MempalaceConfig().palace_path)
