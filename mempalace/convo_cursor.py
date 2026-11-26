"""Resumable scan of a Claude Code JSONL transcript for the convo miner.

A full convo mine of a transcript reads, normalizes and chunks the whole
file, so a growing session costs more on every pass. This module reads only
from a verified resume point. The miner keeps a
:class:`~mempalace.transcript_cursor.Cursor` in the source's record
(``source_head.cursor``):

* ``offset`` is the end of the last complete line read, and the anchor hashes
  the bytes ending there. The anchor is checked every time, even when the
  inode is unchanged, because writers rewrite a tail in place and resume or
  compact can shrink or replace the file.
* ``state["resume_offset"]`` is the start of a user message from which the
  miner can rebuild every drawer after ``next_chunk_index``. The scan reads
  from there to the end of the file. A chained digest (``chain_at_resume``,
  ``chain_at_offset``) must match the bytes it reads again up to ``offset``.
* The rest of the state is what the parse had folded in before
  ``resume_offset``: the pending tool names, the transformations applied,
  the newest timestamp and the count of raw ``>`` lines.

Any mismatch reads the file from the start. The miner then refiles every
drawer, and an unchanged one costs no write. Lines are parsed with orjson,
falling back to stdlib ``json`` per line. Blank, NUL and malformed lines are
skipped like the full normalizer skips them.

Only plain UTF-8 files take this path. A byte-order mark or a byte that is
not UTF-8 makes the file's decoding a whole-file decision, so the miner
leaves such files to the full normalizer.
"""

from __future__ import annotations

import codecs
import hashlib
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Optional

from ._source_state import SourceChangedDuringReadError, source_fingerprint
from .fsio import open_source_file
from .normalize import ClaudeCodeParser
from .timeutil import parse_timestamp
from .transcript_cursor import ANCHOR_BYTES, MAX_ANCHOR_BYTES, Cursor, loads_line, verify

logger = logging.getLogger(__name__)

MAX_TRANSCRIPT_BYTES = 500 * 1024 * 1024

_BAD = object()


def chain(prev: str, data: bytes) -> str:
    """The chained digest after ``data``, given the digest before it."""
    return hashlib.sha256(bytes.fromhex(prev) + data).hexdigest()


@dataclass(frozen=True)
class Mark:
    """A user message the next scan could resume at, and the parse state before it."""

    offset: int
    message: int
    transforms: frozenset
    tool_names: dict
    latest_timestamp: Optional[str]
    quote_lines: int


@dataclass
class TranscriptScan:
    """What :func:`scan_transcript` read.

    ``messages`` start at ``start`` (0 for a full scan). ``committed`` is the
    end of the last complete line and ``anchor`` is ``(sha256, length)`` of
    the bytes ending there. ``tail_blank`` is false when the bytes after
    ``committed`` hold more than whitespace; ``complete_messages`` is then
    the messages of the complete lines alone.
    """

    start: int
    resumed: bool
    reason: str
    state: dict
    data: bytes
    messages: list
    marks: list
    committed: int
    anchor: tuple
    tail_blank: bool
    latest_timestamp: Optional[str]
    quote_lines: int
    tool_missed: bool
    complete_messages: Optional[list] = None
    stat: Any = field(repr=False, default=None)


class NotPlainUtf8(ValueError):
    """The bytes read are not plain UTF-8; the full normalizer must decode them."""


def _open_source(path):
    """``(file, stat)`` for ``path``, refusing symlinks and non-regular files."""
    fd, st = open_source_file(path)
    return os.fdopen(fd, "rb"), st


_STATE_TYPES = {
    "resume_offset": int,
    "chain_at_resume": str,
    "chain_at_offset": str,
    "transforms": list,
    "tool_names": dict,
    "quote_lines": int,
}


def _state_ok(state: dict, offset: int) -> bool:
    """True when ``state`` has every field a resumed scan reads."""
    if any(type(state.get(key)) is not kind for key, kind in _STATE_TYPES.items()):
        return False
    latest = state.get("latest_timestamp")
    if latest is not None and not isinstance(latest, str):
        return False
    try:
        bytes.fromhex(state["chain_at_resume"])
    except ValueError:
        return False
    return 0 < state["resume_offset"] < offset


def _read_rest(f, start: int, limit: int) -> bytes:
    f.seek(start)
    data = f.read(limit + 1)
    if len(data) > limit:
        raise OSError(f"file too large (exceeds {limit} bytes)")
    return data


def _anchor(f, data: bytes, start: int, committed: int) -> tuple:
    """``(sha256, length)`` of the last complete line, widened to ANCHOR_BYTES."""
    end = committed - start
    line_start = start + data.rfind(b"\n", 0, end - 1) + 1
    anchor_start = min(line_start, max(0, committed - ANCHOR_BYTES))
    anchor_start = max(anchor_start, committed - MAX_ANCHOR_BYTES)
    if anchor_start >= start:
        window = data[anchor_start - start : end]
    else:
        f.seek(anchor_start)
        window = f.read(start - anchor_start) + data[:end]
    return hashlib.sha256(window).hexdigest(), len(window)


def scan_transcript(
    path,
    cursor: Optional[Cursor],
    *,
    run,
    source_metadata: dict,
) -> Optional[TranscriptScan]:
    """Read and parse a Claude Code transcript, from the cursor when it verifies.

    ``run`` is the open ``recording_transforms`` run; a resumed scan seeds it
    with the transformations recorded before the resume point. Returns None
    when the full normalizer must read the file instead (not plain UTF-8, too
    large). Raises ``SourceChangedDuringReadError`` when the file changed
    while it was read, and ``OSError`` when it cannot be read.
    """
    f, st = _open_source(path)
    with f:
        reason = "no-cursor"
        state: dict = {}
        start = 0
        if cursor is not None:
            reason = verify(f, cursor, st.st_size)
            state = cursor.state
            if reason == "match":
                if _state_ok(state, cursor.offset):
                    start = state["resume_offset"]
                else:
                    reason = "state"
        data = _read_rest(f, start, MAX_TRANSCRIPT_BYTES)
        if start and chain(state["chain_at_resume"], data[: cursor.offset - start]) != state.get(
            "chain_at_offset"
        ):
            reason, start = "digest", 0
            data = _read_rest(f, 0, MAX_TRANSCRIPT_BYTES)
        if source_fingerprint(os.fstat(f.fileno())) != source_fingerprint(st):
            raise SourceChangedDuringReadError(f"Source changed while reading {path}")
        if not start:
            state = {}
            if data.startswith(codecs.BOM_UTF8):
                return None
        try:
            scan = _parse(data, start, state, run)
        except NotPlainUtf8:
            if not start:
                return None
            reason, start, state = "decode", 0, {}
            data = _read_rest(f, 0, MAX_TRANSCRIPT_BYTES)
            try:
                scan = _parse(data, 0, state, run)
            except NotPlainUtf8:
                return None
        scan.reason = "match" if start else reason
        scan.stat = st
        if scan.committed:
            scan.anchor = _anchor(f, data, start, scan.committed)
    source_metadata.update(source_mtime=st.st_mtime, source_fingerprint=source_fingerprint(st))
    logger.debug("transcript cursor %s for %s from byte %d", scan.reason, path, start)
    return scan


def _parse(data: bytes, start: int, state: dict, run) -> TranscriptScan:
    """Fold the records of ``data`` (file bytes from ``start``) into a scan."""
    resumed = bool(start)
    parser = ClaudeCodeParser(state["tool_names"] if resumed else None)
    run.applied = set(state["transforms"]) if resumed else set()
    latest = state.get("latest_timestamp") if resumed else None
    latest_instant = parse_timestamp(latest) if latest is not None else None
    quotes = state["quote_lines"] if resumed else 0
    marks: list = []
    complete_messages = None
    pos = 0
    size = len(data)
    while pos < size:
        end = data.find(b"\n", pos)
        complete = end >= 0
        if not complete:
            # The half-written last line may only extend the transcript.
            complete_messages = list(parser.messages)
        line_end = end + 1 if complete else size
        first = True
        for record in data[pos:line_end].splitlines():
            try:
                text = record.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise NotPlainUtf8(str(exc)) from exc
            text = text.strip()
            if not text:
                continue
            snapshot = None
            # Only a user entry can start a message; the substring test
            # keeps the copy off every other line.
            if first and complete and ('"user"' in text or '"human"' in text):
                snapshot = (frozenset(run.applied), dict(parser.tool_use_map.pending))
                before = (latest, quotes)
            first = False
            if text.startswith(">"):
                quotes += 1
            try:
                entry = loads_line(text)
            except ValueError:
                continue
            if parser.feed(entry) and snapshot is not None:
                marks.append(
                    Mark(
                        offset=start + pos,
                        message=len(parser.messages) - 1,
                        transforms=snapshot[0],
                        tool_names=snapshot[1],
                        latest_timestamp=before[0],
                        quote_lines=before[1],
                    )
                )
            if not isinstance(entry, dict):
                continue
            ts = entry.get("timestamp")
            instant = parse_timestamp(ts)
            if instant is not None and (latest_instant is None or instant > latest_instant):
                latest = ts
                latest_instant = instant
        pos = line_end
    last_newline = data.rfind(b"\n")
    committed = start + last_newline + 1 if last_newline >= 0 else start
    return TranscriptScan(
        start=start,
        resumed=resumed,
        reason="",
        state=state,
        data=data,
        messages=parser.messages,
        marks=marks,
        committed=committed,
        anchor=("", 0),
        tail_blank=not data[committed - start :].strip(),
        latest_timestamp=latest,
        quote_lines=quotes,
        tool_missed=parser.tool_use_map.missed,
        complete_messages=complete_messages,
    )
