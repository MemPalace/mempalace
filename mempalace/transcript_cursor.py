"""Verified append cursor for growing JSONL transcripts.

A transcript grows by appending lines, but not only that: Claude Code can
truncate and rewrite the tail on the same inode, and resume or compact can
shrink or replace the file. A plain ``(inode, size)`` check misses both. The
cursor here remembers where the last complete line ended (``offset``) and a
SHA-256 of the bytes just before that point (the anchor). Every call
re-verifies the anchor against the file, even when the inode is unchanged:

* size < offset, or the anchor bytes differ  -> mismatch, rescan from 0;
* otherwise                                  -> only ``[offset, size)`` is new.

Only complete ``\\n``-terminated lines are committed. Bytes after the last
newline are a partial record that a writer may still be appending to; they are
handed back as ``tail`` so a caller can fold them in provisionally without
committing them.

The module is deliberately generic. A caller supplies a ``recipe`` (an id plus
version for whatever it computes from the lines) and a JSON-serializable
``state`` dict it folds lines into. A different recipe never resumes from a
cursor written by another one. The lower-level steps (:func:`anchor_digest`,
:func:`verify`, :func:`iter_complete_lines`) are public so a reader that
commits at a record boundary other than the last newline can reuse them.

Imports stay stdlib-only so the Stop hook can use this without pulling in
chromadb or numpy; ``orjson`` is imported lazily and is optional.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

CURSOR_FORMAT = 1
ANCHOR_BYTES = 4096
READ_BLOCK = 8 * 1024 * 1024

_OPEN_FLAGS = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)


@dataclass(frozen=True)
class Cursor:
    """Where a scan of one file stopped, and how to check it still applies.

    ``offset`` is the end of the last committed line. ``anchor_sha256`` hashes
    the up to ``ANCHOR_BYTES`` bytes ending at ``offset``; it is the
    authority. ``dev``/``ino``/``size`` are only hints for logging.
    """

    recipe: str
    offset: int
    anchor_sha256: str
    dev: int = 0
    ino: int = 0
    size: int = 0
    state: dict = field(default_factory=dict)

    def to_json(self) -> dict:
        return {
            "format": CURSOR_FORMAT,
            "recipe": self.recipe,
            "offset": self.offset,
            "anchor_sha256": self.anchor_sha256,
            "dev": self.dev,
            "ino": self.ino,
            "size": self.size,
            "state": self.state,
        }

    @classmethod
    def from_json(cls, data: Any) -> Optional["Cursor"]:
        """Parse a stored cursor, or None when it is malformed."""
        if not isinstance(data, dict) or data.get("format") != CURSOR_FORMAT:
            return None
        try:
            recipe = data["recipe"]
            offset = data["offset"]
            anchor = data["anchor_sha256"]
            state = data.get("state", {})
            hints = [data.get(k, 0) for k in ("dev", "ino", "size")]
        except (KeyError, TypeError):
            return None
        if not isinstance(recipe, str) or not isinstance(anchor, str):
            return None
        if type(offset) is not int or offset < 0 or not isinstance(state, dict):
            return None
        if any(type(h) is not int for h in hints):
            return None
        return cls(recipe, offset, anchor, *hints, state=state)


@dataclass(frozen=True)
class Scan:
    """Result of :func:`advance`.

    ``cursor`` covers every complete line up to its ``offset``; ``tail`` is the
    partial line after it (empty when the file ends in a newline). ``reason``
    says why the scan resumed or restarted: ``match``, ``no-cursor``,
    ``recipe``, ``shrunk`` or ``anchor``.
    """

    cursor: Cursor
    tail: bytes
    resumed: bool
    reason: str


def _read_at(f, start: int, length: int) -> bytes:
    f.seek(start)
    return f.read(length)


def anchor_digest(f, offset: int, anchor_bytes: Optional[int] = None) -> Optional[str]:
    """SHA-256 of the up to ``anchor_bytes`` bytes ending at ``offset``.

    Returns None when the file no longer holds that many bytes.
    """
    anchor_bytes = anchor_bytes or ANCHOR_BYTES
    start = max(0, offset - anchor_bytes)
    data = _read_at(f, start, offset - start)
    if len(data) != offset - start:
        return None
    return hashlib.sha256(data).hexdigest()


def verify(f, cursor: Cursor, size: int, anchor_bytes: Optional[int] = None) -> str:
    """Check ``cursor`` against an open file of ``size`` bytes.

    Returns ``"match"``, or the reason it does not: ``"shrunk"`` or
    ``"anchor"``. The anchor is re-hashed every time; an unchanged inode
    proves nothing because writers rewrite tails in place.
    """
    if size < cursor.offset:
        return "shrunk"
    if anchor_digest(f, cursor.offset, anchor_bytes) != cursor.anchor_sha256:
        return "anchor"
    return "match"


def iter_complete_lines(f, start: int, end: int, block: Optional[int] = None) -> Iterator[bytes]:
    """Yield chunks of whole ``\\n``-terminated lines from ``[start, end)``.

    Each chunk ends with ``\\n``. After the generator is exhausted its return
    value (``StopIteration.value``) is the bytes after the last newline, which
    were not yielded. A short read (the file shrank underneath) ends the scan
    early.
    """
    block = block or READ_BLOCK
    f.seek(start)
    pos = start
    carry = b""
    while pos < end:
        data = f.read(min(block, end - pos))
        if not data:
            break
        pos += len(data)
        buf = carry + data if carry else data
        cut = buf.rfind(b"\n")
        if cut < 0:
            carry = buf
            continue
        carry = buf[cut + 1 :]
        yield buf[: cut + 1]
    return carry


def advance(
    path: os.PathLike | str,
    cursor: Optional[Cursor],
    *,
    recipe: str,
    initial_state: Callable[[], dict],
    feed: Callable[[dict, bytes], None],
    anchor_bytes: Optional[int] = None,
) -> Optional[Scan]:
    """Fold the complete lines of ``path`` into a state, resuming when it can.

    ``feed(state, chunk)`` mutates ``state`` with a chunk of whole lines. When
    ``cursor`` verifies, only bytes after ``cursor.offset`` are fed, starting
    from a copy of ``cursor.state``; otherwise everything is fed from
    ``initial_state()``. Returns None when ``path`` is not a regular file.
    OSError propagates to the caller.
    """
    anchor_bytes = anchor_bytes or ANCHOR_BYTES
    fd = os.open(os.fspath(path), _OPEN_FLAGS)
    with os.fdopen(fd, "rb") as f:
        st = os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode):
            return None
        size = st.st_size

        if cursor is None:
            reason = "no-cursor"
        elif cursor.recipe != recipe:
            reason = "recipe"
        else:
            reason = verify(f, cursor, size, anchor_bytes)

        if reason == "match":
            start = cursor.offset
            state = json.loads(json.dumps(cursor.state))
            anchor_window = _read_at(f, max(0, start - anchor_bytes), min(start, anchor_bytes))
        else:
            start = 0
            state = initial_state()
            anchor_window = b""

        offset = start
        lines = iter_complete_lines(f, start, size)
        while True:
            try:
                chunk = next(lines)
            except StopIteration as done:
                tail = done.value or b""
                break
            feed(state, chunk)
            offset += len(chunk)
            anchor_window = (anchor_window + chunk[-anchor_bytes:])[-anchor_bytes:]

    new_cursor = Cursor(
        recipe=recipe,
        offset=offset,
        anchor_sha256=hashlib.sha256(anchor_window).hexdigest(),
        dev=st.st_dev,
        ino=st.st_ino,
        size=size,
        state=state,
    )
    return Scan(new_cursor, tail, reason == "match", reason)


# --- JSONL record parsing ----------------------------------------------------

_orjson: Any = None
_orjson_loaded = False


def _get_orjson():
    global _orjson, _orjson_loaded
    if not _orjson_loaded:
        try:
            import orjson as module
        except ImportError:  # pragma: no cover - orjson ships with chromadb
            module = None
        _orjson = module
        _orjson_loaded = True
    return _orjson


def loads_line(raw: bytes) -> Any:
    """Parse one JSON line exactly as ``json.loads`` on its UTF-8 text would.

    Uses orjson when available. orjson rejects some input stdlib accepts (lone
    surrogate escapes, NaN, huge numbers, invalid UTF-8 after ``replace``
    decoding), so any orjson error retries that line with stdlib ``json`` on
    the ``errors="replace"`` decoded text. Raises ``ValueError`` (or
    ``RecursionError``) when the line is not JSON.
    """
    fast = _get_orjson()
    if fast is not None:
        try:
            return fast.loads(raw)
        except fast.JSONDecodeError:
            pass
    return json.loads(raw.decode("utf-8", "replace"))


def iter_records(chunk: bytes, keep: Optional[Callable[[bytes], bool]] = None) -> Iterator[Any]:
    """Yield the parsed JSON value of every line in ``chunk``, skipping bad lines.

    Lines split on ``\\n``, ``\\r\\n`` and lone ``\\r`` (``bytes.splitlines``),
    which is what iterating a text-mode file does. ``keep`` is an optional
    raw-byte prefilter; a line it rejects is never parsed.
    """
    for line in chunk.splitlines():
        if keep is not None and not keep(line):
            continue
        try:
            yield loads_line(line)
        except (ValueError, RecursionError):
            continue


# --- sidecar persistence -----------------------------------------------------


def sidecar_name(path: os.PathLike | str) -> str:
    """File name for the cursor of ``path``, keyed by its resolved path."""
    resolved = str(Path(path).expanduser().resolve())
    return hashlib.sha256(resolved.encode("utf-8", "surrogatepass")).hexdigest()[:32] + ".json"


def load_cursor(sidecar: Path) -> Optional[Cursor]:
    """Read a stored cursor; None when missing, unreadable or corrupt."""
    try:
        data = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    return Cursor.from_json(data)


def save_cursor(sidecar: Path, cursor: Cursor) -> None:
    """Atomically write ``cursor`` to ``sidecar`` (temp file, fsync, rename)."""
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(cursor.to_json(), separators=(",", ":")).encode("utf-8")
    fd, tmp = tempfile.mkstemp(dir=sidecar.parent, prefix=".cursor-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, sidecar)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
