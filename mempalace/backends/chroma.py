"""ChromaDB-backed MemPalace storage backend (RFC 001 reference implementation)."""

import contextlib
import datetime as _dt
import json
import logging
import math
import os
import pickle
import re
import sqlite3
from collections import defaultdict
from numbers import Integral
from pathlib import Path
from typing import Any, Optional

import chromadb
from chromadb.errors import NotFoundError as _ChromaNotFoundError

from ._sidecar import EMBEDDER_SIDECAR_FILENAME, read_embedder_sidecar, write_embedder_sidecar
from .base import (
    BaseBackend,
    BaseCollection,
    CollectionNotInitializedError,
    GetResult,
    HealthStatus,
    LexicalHit,
    LexicalResult,
    PalaceNotFoundError,
    PalaceRef,
    QueryResult,
    UnsupportedFilterError,
    _IncludeSpec,
)

logger = logging.getLogger(__name__)


_REQUIRED_OPERATORS = frozenset({"$eq", "$ne", "$in", "$nin", "$and", "$or", "$contains"})
_OPTIONAL_OPERATORS = frozenset({"$gt", "$gte", "$lt", "$lte"})
_SUPPORTED_OPERATORS = _REQUIRED_OPERATORS | _OPTIONAL_OPERATORS
_TOKEN_RE = re.compile(r"\w{2,}", re.UNICODE)

# A healthy HNSW payload should keep link_lists.bin proportional to
# data_level0.bin. When link_lists.bin grows orders of magnitude larger than
# data_level0.bin, Chroma/HNSW can segfault while opening the segment even if
# index_metadata.pickle is structurally valid.
#
# The report in #1218 showed ratios above 300x, while healthy snapshots were far below 1x.
# Treat only >10x as corruption so normal flush lag or small segments do not get
# quarantined.
_HNSW_LINK_TO_DATA_MAX_RATIO = 10.0


def _hnsw_link_to_data_ratio(seg_dir: str) -> Optional[float]:
    """Return link_lists.bin / data_level0.bin size ratio for a segment.

    ``None`` means the ratio is not meaningful, usually because one file is
    missing or data_level0.bin is empty. ``float("inf")`` means the files were
    present but could not be statted safely, which should be treated as
    suspicious by callers.
    """

    link_path = os.path.join(seg_dir, "link_lists.bin")
    data_path = os.path.join(seg_dir, "data_level0.bin")

    if not (os.path.isfile(link_path) and os.path.isfile(data_path)):
        return None

    try:
        data_size = os.path.getsize(data_path)
        link_size = os.path.getsize(link_path)
    except OSError:
        return float("inf")

    if data_size <= 0:
        return None

    return link_size / data_size


def _hnsw_link_lists_is_usable_for_payload(seg_dir: str) -> bool:
    """Return False when a non-trivial HNSW payload lacks usable link lists.

    A missing or empty link_lists.bin is acceptable only for a fresh/empty
    segment. Once data_level0.bin has real payload, a zero-byte link_lists.bin
    is not a harmless async-flush shape: ChromaDB can later hand the broken
    graph to hnswlib and crash in native code.
    """
    data_path = os.path.join(seg_dir, "data_level0.bin")
    link_path = os.path.join(seg_dir, "link_lists.bin")

    try:
        if not os.path.isfile(data_path):
            return True

        data_size = os.path.getsize(data_path)
        if data_size <= _HNSW_MISSING_METADATA_DATA_FLOOR:
            return True

        return os.path.isfile(link_path) and os.path.getsize(link_path) > 0
    except OSError:
        return False


def _hnsw_payload_appears_sane(seg_dir: str) -> bool:
    """Return False when HNSW payload files are structurally implausible."""
    if not _hnsw_link_lists_is_usable_for_payload(seg_dir):
        return False

    ratio = _hnsw_link_to_data_ratio(seg_dir)
    return ratio is None or ratio <= _HNSW_LINK_TO_DATA_MAX_RATIO


# HNSW batch/sync thresholds applied at collection creation.
#
# chromadb's Rust HNSW segment writes index_metadata.pickle and
# link_lists.bin only when internal counters cross both thresholds
# (batch_size gates _apply_batch; sync_threshold gates _persist).
# Records below both thresholds stay in memory and are lost on exit.
#
# Previously 50k/50k to work around link_lists.bin sparse-file bloat
# in pre-1.5.x Python chromadb (#344).  chromadb >=1.5.4 Rust bindings
# (the minimum mempalace supports) do not exhibit that bloat; verified
# at batch_size=2 with 20k records: link_lists.bin = 171 KB, no
# sparse-file inflation.
#
# The 50k guard caused #1579: mines under 50k drawers never triggered
# _persist(), leaving index_metadata.pickle absent and link_lists.bin
# empty.  quarantine_stale_hnsw then renamed the segment on every cold
# open after a 300s mtime gap, accumulating .drift-* directories.
#
# Lowered to 2 (empirical Rust-side minimum for chromadb >=1.5.4; the
# Rust bindings reject 1 with InvalidArgumentError) so any mine of 2+
# drawers triggers a natural persist.  Existing palaces created under
# the old 50k guard keep those thresholds in their collection metadata
# until the user runs repair --mode from-sqlite --archive-existing.
_HNSW_BLOAT_GUARD = {
    "hnsw:batch_size": 2,
    "hnsw:sync_threshold": 2,
}

# Below this size, data_level0.bin is too small for a meaningful HNSW graph.
# Used by _hnsw_link_lists_is_usable_for_payload (empty link_lists is fine
# when data is trivially small) and _missing_dimensionality_appears_recoverable
# (don't attempt recovery on segments with negligible data).
_HNSW_MISSING_METADATA_DATA_FLOOR = 1024


def _validate_where(where: Optional[dict]) -> None:
    """Scan a where-clause for unknown operators and raise ``UnsupportedFilterError``.

    Spec (RFC 001 §1.4): silent dropping of unknown operators is forbidden.
    """
    if not where:
        return
    stack = [where]
    while stack:
        node = stack.pop()
        if not isinstance(node, dict):
            continue
        for k, v in node.items():
            if k.startswith("$") and k not in _SUPPORTED_OPERATORS:
                raise UnsupportedFilterError(f"operator {k!r} not supported by chroma backend")
            if isinstance(v, dict):
                stack.append(v)
            elif isinstance(v, list):
                stack.extend(x for x in v if isinstance(x, dict))


def _tokenize(text: str) -> list[str]:
    if not text:
        return []
    return _TOKEN_RE.findall(text.lower())


def _bm25_scores(
    query: str,
    documents: list[str],
    k1: float = 1.5,
    b: float = 0.75,
) -> list[float]:
    query_terms = set(_tokenize(query))
    n_docs = len(documents)
    if not query_terms or n_docs == 0:
        return [0.0] * n_docs

    tokenized = [_tokenize(doc) for doc in documents]
    doc_lens = [len(toks) for toks in tokenized]
    if not any(doc_lens):
        return [0.0] * n_docs
    avgdl = sum(doc_lens) / n_docs or 1.0

    df = {term: 0 for term in query_terms}
    for toks in tokenized:
        for term in set(toks) & query_terms:
            df[term] += 1

    idf = {
        term: math.log((n_docs - df[term] + 0.5) / (df[term] + 0.5) + 1.0) for term in query_terms
    }

    scores = []
    for toks, dl in zip(tokenized, doc_lens):
        if dl == 0:
            scores.append(0.0)
            continue
        tf: dict[str, int] = {}
        for token in toks:
            if token in query_terms:
                tf[token] = tf.get(token, 0) + 1
        score = 0.0
        for term, freq in tf.items():
            num = freq * (k1 + 1)
            den = freq + k1 * (1 - b + b * dl / avgdl)
            score += idf[term] * num / den
        scores.append(score)
    return scores


def _coerce_metadata_value(value: Any) -> Any:
    if isinstance(value, bool):
        return int(value)
    return value


def _compare_metadata(actual: Any, op: str, expected: Any) -> bool:
    actual = _coerce_metadata_value(actual)
    expected = _coerce_metadata_value(expected)
    if op == "$eq":
        return actual == expected
    if op == "$ne":
        return actual != expected
    if op == "$in":
        return actual in (expected or [])
    if op == "$nin":
        return actual not in (expected or [])
    if op == "$contains":
        return str(expected) in str(actual or "")
    try:
        if op == "$gt":
            return actual > expected
        if op == "$gte":
            return actual >= expected
        if op == "$lt":
            return actual < expected
        if op == "$lte":
            return actual <= expected
    except TypeError:
        return False
    raise UnsupportedFilterError(f"operator {op!r} not supported by chroma backend")


def _matches_where(meta: dict, where: Optional[dict]) -> bool:
    if not where:
        return True
    if not isinstance(where, dict):
        return False
    for key, expected in where.items():
        if key == "$and":
            if not all(_matches_where(meta, clause) for clause in expected or []):
                return False
            continue
        if key == "$or":
            if not any(_matches_where(meta, clause) for clause in expected or []):
                return False
            continue
        if key.startswith("$"):
            raise UnsupportedFilterError(f"operator {key!r} not supported by chroma backend")
        actual = meta.get(key)
        if isinstance(expected, dict):
            for op, operand in expected.items():
                if not _compare_metadata(actual, op, operand):
                    return False
        elif actual != expected:
            return False
    return True


def _metadata_cell_value(sval, ival, fval, bval):
    if sval is not None:
        return sval
    if ival is not None:
        return ival
    if fval is not None:
        return fval
    if bval is not None:
        return bool(bval)
    return None


def _segment_appears_healthy(seg_dir: str) -> bool:
    """Return True if a chromadb HNSW segment dir looks intact.

    Sniff-tests the chromadb-written segment metadata file
    (``index_metadata.pickle``) for its expected format bytes without
    parsing it. ChromaDB writes that file after a successful HNSW flush;
    a complete write starts with byte ``0x80`` and ends with byte
    ``0x2e`` (the protocol/terminator byte sequence chromadb serializes
    with).

    When metadata is missing, the segment is either *never-persisted*
    (sub-threshold: fewer records than ``batch_size``, so chromadb never
    triggered ``_persist()``) or *partially flushed* (persist started but
    crashed).  The two are distinguished by ``link_lists.bin``: chromadb
    writes link data during persist, so an empty/absent ``link_lists.bin``
    together with absent metadata means no persist was ever attempted.
    Note: ``data_level0.bin`` is pre-allocated at index creation and its
    size does not indicate actual record count.

    Deliberately format-sniffs only; never deserializes. Deserialization
    can execute arbitrary code, and the byte-sniff is sufficient to
    distinguish a complete write from truncation, zero-fill, or
    partial-flush corruption.

    Assumes pickle protocol >= 2 (``0x80`` PROTO marker). Matches what
    chromadb writes today; if a future chromadb version emits protocol
    0/1 segments, this check would start returning False on healthy
    files and quarantine_stale_hnsw would conservatively rename them
    out of the way.
    """
    meta_path = os.path.join(seg_dir, "index_metadata.pickle")

    if not os.path.isfile(meta_path):
        link_path = os.path.join(seg_dir, "link_lists.bin")
        try:
            link_has_data = os.path.isfile(link_path) and os.path.getsize(link_path) > 0
        except OSError:
            return False
        # Both absent → sub-threshold, never persisted.
        # link_lists written but metadata not → interrupted persist.
        return not link_has_data

    if not _hnsw_payload_appears_sane(seg_dir):
        return False

    try:
        size = os.path.getsize(meta_path)
        if size < 16:
            return False
        with open(meta_path, "rb") as f:
            head = f.read(2)
            f.seek(-1, 2)  # last byte
            tail = f.read(1)
    except OSError:
        return False
    return len(head) == 2 and head[0] == 0x80 and tail == b"\x2e"


def quarantine_stale_hnsw(palace_path: str, stale_seconds: float = 300.0) -> list[str]:
    """Rename HNSW segment dirs that look unsafe to open.

    This catches two classes of HNSW corruption before ChromaDB opens the
    native segment reader:

    1. stale-by-mtime segments whose ``index_metadata.pickle`` fails the
       existing format sniff-test;
    2. structurally impossible HNSW payloads where ``link_lists.bin`` is much
       larger than ``data_level0.bin``.

    The second check is intentionally not gated by mtime. A segment with a
    300x link/data ratio is unsafe regardless of whether its mtime is recent;
    letting Chroma open it can SIGSEGV before Python fallback code runs.

    The original directory is renamed, not deleted, so recovery remains
    possible if the heuristic ever misfires.
    """

    db_path = os.path.join(palace_path, "chroma.sqlite3")
    if not os.path.isfile(db_path):
        return []

    try:
        sqlite_mtime = os.path.getmtime(db_path)
    except OSError:
        return []

    moved: list[str] = []

    try:
        entries = os.listdir(palace_path)
    except OSError:
        return []

    for name in entries:
        if "-" not in name or name.startswith(".") or ".drift-" in name:
            continue

        seg_dir = os.path.join(palace_path, name)
        if not os.path.isdir(seg_dir):
            continue

        hnsw_bin = os.path.join(seg_dir, "data_level0.bin")
        if not os.path.isfile(hnsw_bin):
            continue

        try:
            hnsw_mtime = os.path.getmtime(hnsw_bin)
        except OSError:
            continue

        payload_ratio = _hnsw_link_to_data_ratio(seg_dir)
        payload_corrupt = payload_ratio is not None and payload_ratio > _HNSW_LINK_TO_DATA_MAX_RATIO

        if not payload_corrupt and sqlite_mtime - hnsw_mtime < stale_seconds:
            continue

        # Stage 2: integrity gate. Mtime drift alone is not corruption because
        # Chroma flushes HNSW asynchronously. A healthy metadata file proves the
        # ordinary stale-by-mtime case is just flush lag.
        if not payload_corrupt and _segment_appears_healthy(seg_dir):
            logger.info(
                "HNSW mtime gap %.0fs on %s exceeds threshold but segment "
                "metadata and payload size are intact — flush-lag, not "
                "corruption. Leaving in place.",
                sqlite_mtime - hnsw_mtime,
                seg_dir,
            )
            continue

        stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        target = f"{seg_dir}.drift-{stamp}"

        if payload_corrupt:
            reason = (
                f"link_lists.bin/data_level0.bin ratio {payload_ratio:.1f}x "
                f"exceeds {_HNSW_LINK_TO_DATA_MAX_RATIO:.1f}x"
            )
        else:
            reason = (
                f"sqlite {sqlite_mtime - hnsw_mtime:.0f}s newer than HNSW "
                "and integrity check failed"
            )

        try:
            os.rename(seg_dir, target)
            moved.append(target)
            logger.warning(
                "Quarantined corrupt HNSW segment %s (%s); renamed to %s",
                seg_dir,
                reason,
                target,
            )
        except OSError:
            logger.exception("Failed to quarantine corrupt HNSW segment %s", seg_dir)

    return moved


def _vector_segment_id(palace_path: str, collection_name: str) -> Optional[str]:
    """Return the VECTOR segment UUID for ``collection_name`` or ``None``.

    Reads ``chroma.sqlite3`` directly so we never have to load a segment
    that may segfault on open (#1222 is exactly this case).
    """
    db_path = os.path.join(palace_path, "chroma.sqlite3")
    if not os.path.isfile(db_path):
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            row = conn.execute(
                """
                SELECT s.id
                FROM segments s
                JOIN collections c ON s.collection = c.id
                WHERE c.name = ? AND s.scope = 'VECTOR'
                LIMIT 1
                """,
                (collection_name,),
            ).fetchone()
            return row[0] if row else None
        finally:
            conn.close()
    except sqlite3.Error:
        return None


class _PersistentDataStub:
    """Minimal stand-in for chromadb's ``PersistentData`` during safe unpickling.

    Accepts any constructor args so pickle's REDUCE opcode succeeds,
    captures ``__setstate__`` into ``__dict__``. Only used by
    :func:`_hnsw_element_count` — never persisted, never re-pickled.
    """

    def __init__(self, *args, **kwargs):
        # Some chromadb versions pickle PersistentData by passing init args
        # positionally via REDUCE. We don't care about reconstructing the
        # object faithfully — we only need the id_to_label dict — so swallow
        # all positional args and re-expose the relevant attributes via
        # __setstate__ or __dict__ population further down.
        pass

    def __setstate__(self, state):
        if isinstance(state, dict):
            self.__dict__.update(state)
        elif isinstance(state, tuple) and len(state) == 2 and isinstance(state[1], dict):
            # (slot_state, dict_state) two-tuple form — only the dict part has
            # the named attributes we care about.
            self.__dict__.update(state[1])


class _SafePersistentDataUnpickler:
    """Whitelist-only unpickler for ``index_metadata.pickle``.

    Allows only ``PersistentData`` from chromadb's HNSW module; everything
    else raises ``UnpicklingError``. Standard container types (dict, list,
    tuple, str, int, float) are handled by the pickle machinery itself
    and don't need allowlisting via ``find_class`` — only constructed
    classes do.

    This is the same trust model chromadb uses (it pickles its own files),
    but with a tight class allowlist so a tampered file can't instantiate
    arbitrary classes during deserialization.
    """

    _ALLOWED = frozenset(
        {
            (
                "chromadb.segment.impl.vector.local_persistent_hnsw",
                "PersistentData",
            ),
        }
    )

    @classmethod
    def load(cls, path: str):
        import pickle

        class _Restricted(pickle.Unpickler):
            def find_class(self, module: str, name: str):
                if (module, name) in cls._ALLOWED:
                    return _PersistentDataStub
                raise pickle.UnpicklingError(f"disallowed class: {module}.{name}")

        with open(path, "rb") as f:
            return _Restricted(f).load()


def _hnsw_element_count(palace_path: str, segment_id: str) -> Optional[int]:
    """Return the element count chromadb thinks the HNSW segment holds.

    Reads ``index_metadata.pickle`` via a tight-allowlist unpickler and
    counts ``id_to_label`` entries. This is the count chromadb consults
    when sizing/loading the HNSW index on next open — distinct from
    hnswlib's internal ``cur_element_count`` in the binary files. For
    #1222's divergence check this is the number that matters, because it
    is what gets compared against ``count() * resize_factor`` when
    chromadb decides whether to resize HNSW on load.

    Uses :class:`_SafePersistentDataUnpickler` rather than chromadb's own
    ``PersistentData.load_from_file`` so the probe works even when
    ``hnswlib`` is not installed (chromadb's persistent_hnsw module
    imports hnswlib at module load — a probe that requires hnswlib would
    refuse to run in environments where the segfault risk is moot
    anyway). The allowlisted unpickler is also the safer default: the
    pickle file is owned by the same user, but a tighter trust boundary
    costs us nothing.

    Returns ``None`` when the file is absent (fresh / never-flushed
    segment) or the unpickle fails. Callers treat ``None`` as "unknown".
    """
    pickle_path = os.path.join(palace_path, segment_id, "index_metadata.pickle")
    if not os.path.isfile(pickle_path):
        return None
    try:
        pd = _SafePersistentDataUnpickler.load(pickle_path)
        # ChromaDB serializes PersistentData differently across versions:
        # 1.5.x writes a plain dict via ``__reduce_ex__``; older versions
        # pickled the class instance and rely on ``__setstate__`` to
        # populate ``__dict__``. Handle both shapes.
        if isinstance(pd, dict):
            id_to_label = pd.get("id_to_label")
        else:
            id_to_label = getattr(pd, "id_to_label", None)
        if isinstance(id_to_label, dict):
            return len(id_to_label)
        return None
    except Exception:
        logger.debug("_hnsw_element_count failed for %s", pickle_path, exc_info=True)
        return None


# Divergence threshold: chromadb's HNSW flushes asynchronously, so HNSW
# typically lags sqlite by up to ``sync_threshold`` records under active
# write load — that's the *brute-force batch* that hasn't been compacted
# into HNSW yet, plus the un-persisted tail beyond the last sync. Two
# synchronization windows worth (2 × sync_threshold) is a safe steady-
# state ceiling; anything past that is real divergence, not flush-lag.
#
# The threshold floor scales with whatever ``hnsw:sync_threshold`` the
# collection was created with (read via :func:`_read_sync_threshold`).
# ``_HNSW_DIVERGENCE_FALLBACK_FLOOR`` is the floor used when we can't
# read the collection metadata (older palaces missing the row, sqlite
# unreadable). 2000 = 2 × chromadb's default sync_threshold of 1000.
#
# Why dynamic: legacy palaces may still carry ``sync_threshold = 50_000``
# (the pre-#1579 guard), so flush-lag can grow up to 50K on those palaces.
# New palaces use sync_threshold=2 (#1579) and flush almost immediately.
# A fixed 2000 floor would flag actively-written legacy palaces as
# DIVERGED the moment their queue exceeded 10% of sqlite_count, even
# though chromadb is behaving correctly. The floor must scale with the
# per-collection sync_threshold to distinguish real corruption (#1222 was
# 176 613 missing of 192 997, orders of magnitude past any reasonable
# sync_threshold) from expected steady-state lag.
_HNSW_DIVERGENCE_FALLBACK_FLOOR = 2000
_HNSW_DIVERGENCE_FRACTION = 0.10


def _read_sync_threshold(palace_path: str, collection_name: str) -> int:
    """Return the ``hnsw:sync_threshold`` for a collection, or 1000 default.

    The configured sync_threshold drives chromadb's HNSW flush cadence —
    larger values mean fewer, bigger flushes (less index-bloat risk per
    PR #1191) but also larger steady-state lag between
    ``index_metadata.pickle`` and the live sqlite count. The divergence
    probe scales its tolerance to ``2 × sync_threshold`` so that lag is
    not mistaken for corruption.

    Falls back to 1000 (chromadb's own default) if the collection has no
    explicit setting — matches what older mempalace palaces were created
    with before PR #1191.
    """
    db_path = os.path.join(palace_path, "chroma.sqlite3")
    if not os.path.isfile(db_path):
        return 1000
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT cm.int_value
                FROM collection_metadata cm
                JOIN collections c ON cm.collection_id = c.id
                WHERE c.name = ? AND cm.key = 'hnsw:sync_threshold'
                """,
                (collection_name,),
            )
            row = cur.fetchone()
            if row and row[0] is not None:
                return int(row[0])
            return 1000
        finally:
            conn.close()
    except Exception:
        logger.debug("_read_sync_threshold failed", exc_info=True)
        return 1000


def hnsw_capacity_status(palace_path: str, collection_name: str = "mempalace_drawers") -> dict:
    """Compare sqlite embedding count against HNSW element count.

    The #1222 failure mode: ``max_elements`` froze at 16 384 while sqlite
    accumulated 192 997 embeddings. Every subsequent tool call segfaulted
    when chromadb tried to load the undersized HNSW. This probe runs
    *before* anything touches the segment so we can warn (or fall back to
    BM25) instead of crashing.

    Returns a dict with:

    * ``segment_id``       — VECTOR segment UUID, or ``None`` if no palace
    * ``sqlite_count``     — embeddings present in chroma.sqlite3
    * ``hnsw_count``       — elements chromadb's pickle knows about
    * ``divergence``       — ``sqlite_count - hnsw_count`` when both known
    * ``diverged``         — True when divergence exceeds the threshold
    * ``status``           — ``"ok"`` | ``"diverged"`` | ``"unknown"``
    * ``message``          — human-readable summary

    Never raises — a probe that throws would defeat the point.
    """
    out: dict[str, Any] = {
        "segment_id": None,
        "sqlite_count": None,
        "hnsw_count": None,
        "divergence": None,
        "diverged": False,
        "status": "unknown",
        "message": "",
    }

    try:
        seg_id = _vector_segment_id(palace_path, collection_name)
        out["segment_id"] = seg_id

        sqlite_count = _sqlite_embedding_count(palace_path, collection_name)
        out["sqlite_count"] = sqlite_count

        if seg_id is None or sqlite_count is None:
            out["message"] = "palace state unreadable; skipping HNSW capacity check"
            return out

        hnsw_count = _hnsw_element_count(palace_path, seg_id)
        out["hnsw_count"] = hnsw_count

        sync_threshold = _read_sync_threshold(palace_path, collection_name)
        # Two synchronization windows worth — see comment above
        # _HNSW_DIVERGENCE_FALLBACK_FLOOR for the rationale.
        divergence_floor = max(_HNSW_DIVERGENCE_FALLBACK_FLOOR, 2 * sync_threshold)

        if hnsw_count is None:
            # No pickle yet, so this probe cannot measure HNSW capacity.
            # Chroma 1.5.x can have binary HNSW files without a flushed
            # metadata pickle; absence of the pickle alone is not proof that
            # vector search is unusable or dangerous. Keep the status unknown
            # so MCP does not globally disable vectors on an inconclusive
            # signal. Corrupt/invalid metadata, when present, is handled by
            # quarantine_invalid_hnsw_metadata before Chroma opens.
            out["message"] = (
                "HNSW capacity unavailable: metadata has not been flushed; "
                "leaving vector search enabled"
            )
            return out

        divergence = sqlite_count - hnsw_count
        out["divergence"] = divergence
        threshold = max(divergence_floor, int(sqlite_count * _HNSW_DIVERGENCE_FRACTION))
        if divergence > threshold:
            out["status"] = "diverged"
            out["diverged"] = True
            pct = 100.0 * divergence / max(sqlite_count, 1)
            out["message"] = (
                f"HNSW index holds {hnsw_count:,} elements but sqlite has "
                f"{sqlite_count:,} embeddings — {divergence:,} drawers ({pct:.0f}%) "
                "are invisible to vector search. Run `mempalace repair` to rebuild."
            )
        else:
            out["status"] = "ok"
            out["message"] = (
                f"HNSW {hnsw_count:,} / sqlite {sqlite_count:,} (within flush-lag tolerance)"
            )
    except Exception:
        logger.debug("hnsw_capacity_status failed", exc_info=True)
        out["message"] = "HNSW capacity probe raised; skipping"
    return out


def _sqlite_embedding_count(palace_path: str, collection_name: str) -> Optional[int]:
    """Count rows in chroma.sqlite3.embeddings for ``collection_name``.

    Mirrors :func:`mempalace.repair.sqlite_drawer_count` but kept in this
    module so the backend probe doesn't pull in the repair CLI module.
    """
    db_path = os.path.join(palace_path, "chroma.sqlite3")
    if not os.path.isfile(db_path):
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
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
            return int(row[0]) if row and row[0] is not None else None
        finally:
            conn.close()
    except sqlite3.Error:
        return None


def _sqlite_wing_room_counts(
    palace_path: str, collection_name: str
) -> Optional[tuple[int, dict[str, dict[str, int]]]]:
    """Tally drawers by wing/room straight from ``chroma.sqlite3``.

    Returns ``(total, {wing: {room: count}})`` or ``None`` when the read
    cannot be trusted — missing DB file, the collection has not been
    bootstrapped, or any sqlite error (including a sustained writer lock).
    ``None`` signals the caller to fall back to the ChromaDB client path
    (which also emits the right state-specific guidance for absent/empty
    palaces).

    The point of reading sqlite directly is to count drawers **without opening
    the collection**, because opening it cold-loads the HNSW vector index. On
    large palaces that load costs tens of seconds of CPU per call — a steep,
    pointless tax for an inspection command that only needs metadata the
    relational tables already hold (#1681). Wings/rooms live in plain
    ``embedding_metadata`` rows, joined to ``embeddings`` on the
    ``(id, key)`` primary key, so the tally is a bounded scan of the metadata
    segment: sub-second warm, a few seconds cold on a multi-GB DB — versus the
    ~60s the vector-index load costs.

    Sibling readers that count the same way: :func:`_sqlite_embedding_count`
    (total only) and ``mcp_server._tool_status_via_sqlite`` (independent
    wing/room histograms for the #1222 fallback). This one cross-tabulates
    wing→room to match the ChromaDB ``status()`` output shape.

    Notes:
    - ``busy_timeout`` lets a transient checkpoint lock resolve instead of
      instantly demoting to the slow HNSW path; a *sustained* lock still
      raises and falls back (slow but correct).
    - ``s.scope = 'METADATA'`` makes the single-segment join explicit so a
      future ChromaDB that also stored per-vector-segment rows could not
      silently double every count.
    - ``COALESCE`` over ``string_value``/``int_value``/``float_value`` matches
      the ChromaDB path, which surfaces a numeric wing/room natively rather
      than dropping it to ``"?"``.
    """
    db_path = os.path.join(palace_path, "chroma.sqlite3")
    if not os.path.isfile(db_path):
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            # Wait out a transient writer/checkpoint lock rather than falling
            # straight back to the expensive vector-index path (#1681).
            conn.execute("PRAGMA busy_timeout = 3000")
            # Distinguish "collection never bootstrapped" (-> None, so the
            # caller can show the 'initialized but empty' message) from
            # "collection exists with zero drawers" (-> a real 0 tally).
            if (
                conn.execute(
                    "SELECT 1 FROM collections WHERE name = ?", (collection_name,)
                ).fetchone()
                is None
            ):
                return None
            rows = conn.execute(
                """
                SELECT COALESCE(wm.string_value, CAST(wm.int_value AS TEXT),
                                CAST(wm.float_value AS TEXT), '?') AS wing,
                       COALESCE(rm.string_value, CAST(rm.int_value AS TEXT),
                                CAST(rm.float_value AS TEXT), '?') AS room,
                       COUNT(*) AS n
                FROM embeddings e
                JOIN segments s ON e.segment_id = s.id AND s.scope = 'METADATA'
                JOIN collections c ON s.collection = c.id
                LEFT JOIN embedding_metadata wm ON wm.id = e.id AND wm.key = 'wing'
                LEFT JOIN embedding_metadata rm ON rm.id = e.id AND rm.key = 'room'
                WHERE c.name = ?
                GROUP BY wing, room
                """,
                (collection_name,),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return None

    total = 0
    wing_rooms: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for wing, room, n in rows:
        wing_rooms[wing][room] += int(n)
        total += int(n)
    return total, wing_rooms


def _pin_hnsw_threads(collection) -> None:
    """Best-effort retrofit: pin ``hnsw:num_threads=1`` on an existing collection.

    Fresh collections set this via ``metadata=`` at creation. Legacy palaces
    built before that change keep the default (parallel insert) and can hit
    the HNSW race described in #974/#965. ChromaDB's
    ``collection.modify(configuration=...)`` lets us re-apply ``num_threads=1``
    in memory at load time so every new process is protected.

    Note: in chromadb 1.5.x the modified ``configuration_json["hnsw"]`` does
    not persist to disk across ``PersistentClient`` reopens, so this must
    run on every ``get_collection`` call, not just once.
    """
    try:
        from chromadb.api.collection_configuration import (
            UpdateCollectionConfiguration,
            UpdateHNSWConfiguration,
        )
    except ImportError:
        logger.debug("_pin_hnsw_threads skipped: chromadb too old", exc_info=True)
        return
    try:
        collection.modify(
            configuration=UpdateCollectionConfiguration(hnsw=UpdateHNSWConfiguration(num_threads=1))
        )
    except Exception:
        logger.debug("_pin_hnsw_threads modify failed", exc_info=True)


_BLOB_FIX_MARKER = ".blob_seq_ids_migrated"
_COLLECTION_TYPE_MARKER = ".collection_type_fixed"


def _valid_dimensionality(value: object) -> bool:
    return isinstance(value, Integral) and not isinstance(value, bool) and int(value) > 0


def _persisted_metadata_value(obj: object, name: str) -> object:
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _persisted_metadata_fields(obj: object) -> tuple[object, object]:
    return _persisted_metadata_value(obj, "dimensionality"), _persisted_metadata_value(
        obj, "id_to_label"
    )


def _missing_dimensionality_appears_recoverable(
    persisted: object, id_to_label: dict, seg_dir: str
) -> bool:
    total = _persisted_metadata_value(persisted, "total_elements_added")
    label_to_id = _persisted_metadata_value(persisted, "label_to_id")
    data_path = os.path.join(seg_dir, "data_level0.bin")
    link_path = os.path.join(seg_dir, "link_lists.bin")

    if not isinstance(total, Integral) or isinstance(total, bool):
        return False
    if not isinstance(label_to_id, dict):
        return False
    try:
        if not (
            os.path.isfile(data_path)
            and os.path.isfile(link_path)
            and os.path.getsize(data_path) > _HNSW_MISSING_METADATA_DATA_FLOOR
        ):
            return False
    except OSError:
        return False
    if not _hnsw_payload_appears_sane(seg_dir):
        return False

    label_count = len(id_to_label)
    # total_elements_added is monotonic across every add, while id_to_label and
    # label_to_id hold only live elements, so a segment that has had deletions
    # carries total_elements_added > label_count. Require >= (not ==), otherwise
    # every post-deletion dim-None segment is wrongly quarantined (#1710); the
    # label-map size and bijection checks still reject inconsistent label maps.
    if int(total) < label_count or len(label_to_id) != label_count:
        return False
    try:
        return all(label_to_id.get(label) == item_id for item_id, label in id_to_label.items())
    except TypeError:
        return False


def quarantine_invalid_hnsw_metadata(palace_path: str) -> list[str]:
    """Quarantine segment dirs whose ``index_metadata.pickle`` is unreadable or invalid.

    Chroma's persisted HNSW metadata is untrusted disk state. If a segment has
    labels but invalid or partial metadata, current Chroma versions can accept
    the pickle and crash later in the Rust loader. We rename the entire segment
    out of the way before ``PersistentClient`` opens so Chroma can rebuild
    cleanly instead of touching known-bad metadata.
    """
    try:
        entries = os.listdir(palace_path)
    except OSError:
        return []

    moved: list[str] = []
    for name in entries:
        if "-" not in name or name.startswith(".") or ".drift-" in name or ".corrupt-" in name:
            continue
        seg_dir = os.path.join(palace_path, name)
        if not os.path.isdir(seg_dir):
            continue

        meta_path = os.path.join(seg_dir, "index_metadata.pickle")
        if not os.path.isfile(meta_path):
            continue

        reason = None
        try:
            persisted = _SafePersistentDataUnpickler.load(meta_path)
        except (EOFError, OSError):
            logger.debug(
                "Skipping invalid-HNSW quarantine for transient metadata read in %s",
                meta_path,
                exc_info=True,
            )
            continue
        except pickle.UnpicklingError as exc:
            if "truncated" in str(exc).lower() or "ran out of input" in str(exc).lower():
                logger.debug(
                    "Skipping invalid-HNSW quarantine for transient metadata read in %s",
                    meta_path,
                    exc_info=True,
                )
                continue
            reason = f"invalid index_metadata.pickle: {exc}"
        except Exception as exc:
            reason = f"invalid index_metadata.pickle: {exc}"
        else:
            if not isinstance(persisted, dict) and not (
                hasattr(persisted, "dimensionality") or hasattr(persisted, "id_to_label")
            ):
                reason = f"unrecognized index_metadata.pickle payload: {type(persisted).__name__}"
            else:
                dimensionality, id_to_label = _persisted_metadata_fields(persisted)
                if id_to_label is not None and not isinstance(id_to_label, dict):
                    reason = f"invalid id_to_label type {type(id_to_label).__name__}"
                else:
                    has_labels = bool(id_to_label)
                    if (
                        has_labels
                        and dimensionality is None
                        and not _missing_dimensionality_appears_recoverable(
                            persisted, id_to_label, seg_dir
                        )
                    ):
                        reason = (
                            "labels present but dimensionality is missing or invalid "
                            f"({dimensionality!r})"
                        )
                    elif (
                        has_labels
                        and dimensionality is not None
                        and not _valid_dimensionality(dimensionality)
                    ):
                        reason = (
                            "labels present but dimensionality is missing or invalid "
                            f"({dimensionality!r})"
                        )
                    elif dimensionality is not None and not _valid_dimensionality(dimensionality):
                        reason = f"invalid dimensionality {dimensionality!r}"

        if reason is None:
            continue

        stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        target = f"{seg_dir}.corrupt-{stamp}"
        try:
            os.rename(seg_dir, target)
            moved.append(target)
            logger.warning("Quarantined invalid HNSW metadata in %s: %s", seg_dir, reason)
        except OSError:
            logger.exception("Failed to quarantine invalid HNSW metadata in %s", seg_dir)

    return moved


def _fix_blob_seq_ids(palace_path: str) -> None:
    """Fix ChromaDB 0.6.x -> 1.5.x migration bug: BLOB seq_ids -> INTEGER.

    ChromaDB 0.6.x stored seq_id as big-endian 8-byte BLOBs. ChromaDB 1.5.x
    expects INTEGER. The auto-migration doesn't convert existing rows, causing
    the Rust compactor to crash with "mismatched types; Rust type u64 (as SQL
    type INTEGER) is not compatible with SQL type BLOB".

    Scoped to the ``embeddings`` table only. The ``max_seq_id`` table used
    to be included in this loop, but chromadb 1.5.x writes its own BLOB
    format there (``b'\\x11\\x11'`` + 6 ASCII digits). Misinterpreting that
    format via ``int.from_bytes(..., 'big')`` yields a ~1.23e18 integer
    that silently suppresses every subsequent write for the affected
    segment (``embeddings_queue`` filters on ``seq_id > start``). chromadb
    owns the ``max_seq_id`` column — we leave it alone. Palaces already
    poisoned by the old behaviour can be repaired via
    ``mempalace repair --mode max-seq-id``.

    Defense-in-depth: rows with the sysdb-10 ``b'\\x11\\x11'`` prefix in
    ``embeddings`` are skipped rather than converted. Real 0.6.x BLOBs are
    pure big-endian u64 with no text prefix, so the prefix check is a
    no-op for genuine legacy data.

    Must run BEFORE PersistentClient is created (the compactor fires on init).

    Opening a Python sqlite3 connection against a ChromaDB 1.5.x WAL-mode
    database leaves state that segfaults the next PersistentClient call. After
    the migration has run once successfully, a marker file is written so
    subsequent opens skip the sqlite connection entirely. Already-migrated
    palaces can touch the marker manually to opt into the fast path.
    """
    db_path = os.path.join(palace_path, "chroma.sqlite3")
    if not os.path.isfile(db_path):
        return
    marker = os.path.join(palace_path, _BLOB_FIX_MARKER)
    if os.path.isfile(marker):
        return
    try:
        with contextlib.closing(sqlite3.connect(db_path)) as conn:
            try:
                rows = conn.execute(
                    "SELECT rowid, seq_id FROM embeddings WHERE typeof(seq_id) = 'blob'"
                ).fetchall()
            except sqlite3.OperationalError:
                return
            safe_rows = [(rowid, blob) for rowid, blob in rows if not blob.startswith(b"\x11\x11")]
            skipped = len(rows) - len(safe_rows)
            if skipped:
                logger.warning(
                    "Skipped %d sysdb-10-format BLOB seq_id(s) in embeddings (not converting)",
                    skipped,
                )
            if safe_rows:
                updates = [
                    (int.from_bytes(blob, byteorder="big"), rowid) for rowid, blob in safe_rows
                ]
                conn.executemany("UPDATE embeddings SET seq_id = ? WHERE rowid = ?", updates)
                logger.info("Fixed %d BLOB seq_ids in embeddings", len(updates))
                conn.commit()
    except Exception:
        logger.exception("Could not fix BLOB seq_ids in %s", db_path)
        return
    # Write marker whether or not rows needed migration — the palace is now
    # confirmed to be in the INTEGER-seq_id state and future opens can skip the
    # sqlite3.connect() entirely.
    try:
        Path(marker).touch()
    except OSError:
        logger.exception("Could not write migration marker %s", marker)


def _fix_missing_collection_type(palace_path: str) -> None:
    """Add ``_type`` to ``collections.config_json_str`` where absent.

    chromadb <= 1.5.8 writes ``config_json_str = '{}'`` (empty JSON) when
    creating collections.  chromadb 1.5.9 switched from the permissive
    ``load_collection_configuration_from_json_str`` to
    ``CollectionConfigurationInternal.from_json`` which requires a ``_type``
    key — its absence raises ``KeyError: '_type'`` on palace open.

    This migration adds the missing marker so both old and new chromadb
    versions can load the collection.  The value
    ``"CollectionConfigurationInternal"`` matches what ``to_json()`` writes
    for freshly-created collections.

    Same lifecycle constraints as :func:`_fix_blob_seq_ids`: must run
    BEFORE ``PersistentClient`` is created.
    """
    db_path = os.path.join(palace_path, "chroma.sqlite3")
    if not os.path.isfile(db_path):
        return
    marker = os.path.join(palace_path, _COLLECTION_TYPE_MARKER)
    if os.path.isfile(marker):
        return
    conn = sqlite3.connect(db_path)
    try:
        try:
            rows = conn.execute("SELECT id, config_json_str FROM collections").fetchall()
        except sqlite3.OperationalError:
            return
        updates = []
        for coll_id, config_str in rows:
            if not config_str:
                config_str = "{}"
            try:
                config = json.loads(config_str)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(config, dict):
                continue
            if "_type" not in config:
                config["_type"] = "CollectionConfigurationInternal"
                updates.append((json.dumps(config), coll_id))
        if updates:
            conn.executemany(
                "UPDATE collections SET config_json_str = ? WHERE id = ?",
                updates,
            )
            conn.commit()
            logger.info(
                "Fixed %d collection(s) missing _type in config_json_str",
                len(updates),
            )
    except Exception:
        logger.exception("Could not fix collection config_json_str in %s", db_path)
        return
    finally:
        conn.close()
    try:
        Path(marker).touch()
    except OSError:
        logger.exception("Could not write migration marker %s", marker)


# ---------------------------------------------------------------------------
# Collection adapter
# ---------------------------------------------------------------------------


def _as_list(v: Any) -> list:
    """Coerce possibly-None scalar-or-list into a list (defensive for chroma nulls)."""
    if v is None:
        return []
    if isinstance(v, list):
        return v
    return [v]


def _close_client(client) -> None:
    """Call ``PersistentClient.close()`` if available, swallow otherwise.

    chromadb 1.5.x exposes ``Client.close()`` to release rust-side SQLite
    file locks; older versions relied on GC. Try/except keeps forward-compat.
    """
    if client is None:
        return
    try:
        client.close()
    except Exception:
        logger.debug("client.close() unavailable or failed", exc_info=True)


class ChromaCollection(BaseCollection):
    """Thin adapter translating ChromaDB dict returns into typed results.

    When ``palace_path`` is set, all write methods (``add``, ``upsert``,
    ``update``, ``delete``) acquire ``mine_palace_lock(palace_path)`` for the
    duration of the underlying chromadb call. This serializes MCP and other
    direct-backend writers against ``mempalace mine`` and against each other,
    closing the race between concurrent writers that triggers ChromaDB's
    multi-threaded HNSW corruption (#974/#965).

    The lock is the same primitive used by ``miner.mine()`` so re-entrant
    acquisition from inside the mine pipeline (mine -> _mine_body ->
    collection.upsert) is short-circuited by the per-thread guard inside
    ``mine_palace_lock`` — no self-deadlock.

    ``palace_path=None`` disables the wrapping, preserving the legacy
    no-lock behaviour for callers that construct a ``ChromaCollection``
    directly without going through ``ChromaBackend``.
    """

    def __init__(self, collection, palace_path: Optional[str] = None):
        self._collection = collection
        self._palace_path = palace_path

    @contextlib.contextmanager
    def _write_lock(self):
        """Acquire ``mine_palace_lock`` for the configured palace, if any.

        No-op (yields immediately) when ``self._palace_path`` is None.
        """
        if self._palace_path is None:
            yield
            return
        # Late import — palace.py imports ChromaBackend from this module.
        from ..palace import mine_palace_lock

        with mine_palace_lock(self._palace_path):
            yield

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    @staticmethod
    def _sanitize_metadatas_for_chromadb(metadatas):
        """chromadb 1.5.x rejects None and empty-dict entries in the metadatas
        list (ValueError: Expected metadata to be a non-empty dict, got 0
        metadata attributes in add). Coerce any such entry to a sentinel so
        the write succeeds. Operators can later locate coerced drawers via
        ``where={"_repaired_empty_meta": True}``.

        This is the chokepoint catch-all: even if a caller's own sanitizer
        misses a case (or skips for performance), reaching the chromadb
        client always goes through here first.
        """
        if metadatas is None:
            return None
        return [
            m if (isinstance(m, dict) and len(m) > 0) else {"_repaired_empty_meta": True}
            for m in metadatas
        ]

    @staticmethod
    def _sanitize_documents_for_chromadb(documents):
        """Strip lone UTF-16 surrogates from every document before it reaches
        the chromadb client.

        A single lone surrogate (U+D800–U+DFFF) raises ``UnicodeEncodeError``
        inside chromadb's encode path and aborts the *entire* add/upsert batch
        with a ``-32000`` Internal Error, silently dropping every other row in
        the same batch (#1235).

        #1235 fixed this for the MCP write tools via ``sanitize_content``, but
        the bulk ingest paths (miner, convo_miner, sweeper, diary_ingest) build
        documents without routing through that helper and reach this backend
        directly. Sanitising here makes the chokepoint catch-all complete: the
        sibling :meth:`_sanitize_metadatas_for_chromadb` already guarantees this
        for metadata one method over; documents get the same guarantee.
        """
        if documents is None:
            return None
        from ..config import strip_lone_surrogates

        # chromadb accepts OneOrMany[Document]: a bare str is a single document,
        # not an iterable of characters. Handle it explicitly so we don't split
        # it into per-character documents — that would be exactly the kind of
        # silent corruption this method exists to prevent.
        if isinstance(documents, str):
            return strip_lone_surrogates(documents)
        return [strip_lone_surrogates(d) if isinstance(d, str) else d for d in documents]

    def add(self, *, documents, ids, metadatas=None, embeddings=None):
        kwargs: dict[str, Any] = {
            "documents": self._sanitize_documents_for_chromadb(documents),
            "ids": ids,
        }
        sanitized = self._sanitize_metadatas_for_chromadb(metadatas)
        if sanitized is not None:
            kwargs["metadatas"] = sanitized
        if embeddings is not None:
            kwargs["embeddings"] = embeddings
        with self._write_lock():
            self._collection.add(**kwargs)

    def upsert(self, *, documents, ids, metadatas=None, embeddings=None):
        kwargs: dict[str, Any] = {
            "documents": self._sanitize_documents_for_chromadb(documents),
            "ids": ids,
        }
        sanitized = self._sanitize_metadatas_for_chromadb(metadatas)
        if sanitized is not None:
            kwargs["metadatas"] = sanitized
        if embeddings is not None:
            kwargs["embeddings"] = embeddings
        with self._write_lock():
            self._collection.upsert(**kwargs)

    def update(
        self,
        *,
        ids,
        documents=None,
        metadatas=None,
        embeddings=None,
    ):
        if documents is None and metadatas is None and embeddings is None:
            raise ValueError("update requires at least one of documents, metadatas, embeddings")
        kwargs: dict[str, Any] = {"ids": ids}
        if documents is not None:
            kwargs["documents"] = self._sanitize_documents_for_chromadb(documents)
        if metadatas is not None:
            kwargs["metadatas"] = metadatas
        if embeddings is not None:
            kwargs["embeddings"] = embeddings
        with self._write_lock():
            self._collection.update(**kwargs)

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def query(
        self,
        *,
        query_texts=None,
        query_embeddings=None,
        n_results=10,
        where=None,
        where_document=None,
        include=None,
    ) -> QueryResult:
        _validate_where(where)
        _validate_where(where_document)

        if (query_texts is None) == (query_embeddings is None):
            raise ValueError("query requires exactly one of query_texts or query_embeddings")
        chosen = query_texts if query_texts is not None else query_embeddings
        if not chosen:
            raise ValueError("query input must be a non-empty list")

        spec = _IncludeSpec.resolve(include, default_distances=True)
        chroma_include: list[str] = []
        if spec.documents:
            chroma_include.append("documents")
        if spec.metadatas:
            chroma_include.append("metadatas")
        if spec.distances:
            chroma_include.append("distances")
        if spec.embeddings:
            chroma_include.append("embeddings")

        kwargs: dict[str, Any] = {
            "n_results": n_results,
            "include": chroma_include,
        }
        if query_texts is not None:
            kwargs["query_texts"] = query_texts
        if query_embeddings is not None:
            kwargs["query_embeddings"] = query_embeddings
        if where is not None:
            kwargs["where"] = where
        if where_document is not None:
            kwargs["where_document"] = where_document

        raw = self._collection.query(**kwargs)

        num_queries = (
            len(query_texts)
            if query_texts is not None
            else (len(query_embeddings) if query_embeddings is not None else 1)
        )

        ids = raw.get("ids") or []
        if not ids:
            return QueryResult.empty(
                num_queries=num_queries,
                embeddings_requested=spec.embeddings,
            )

        documents = raw.get("documents") or [[] for _ in ids]
        metadatas = raw.get("metadatas") or [[] for _ in ids]
        distances = raw.get("distances") or [[] for _ in ids]
        embeddings_raw = raw.get("embeddings") if spec.embeddings else None

        def _none_list_to_empty(outer):
            return [(inner or []) for inner in outer]

        return QueryResult(
            ids=_none_list_to_empty(ids),
            documents=_none_list_to_empty(documents),
            metadatas=_none_list_to_empty(metadatas),
            distances=_none_list_to_empty(distances),
            embeddings=(
                [list(inner) for inner in embeddings_raw]
                if spec.embeddings and embeddings_raw is not None
                else None
            ),
        )

    def get(
        self,
        *,
        ids=None,
        where=None,
        where_document=None,
        limit=None,
        offset=None,
        include=None,
    ) -> GetResult:
        _validate_where(where)
        _validate_where(where_document)

        spec = _IncludeSpec.resolve(include, default_distances=False)
        chroma_include: list[str] = []
        if spec.documents:
            chroma_include.append("documents")
        if spec.metadatas:
            chroma_include.append("metadatas")
        if spec.embeddings:
            chroma_include.append("embeddings")

        kwargs: dict[str, Any] = {"include": chroma_include}
        if ids is not None:
            kwargs["ids"] = ids
        if where is not None:
            kwargs["where"] = where
        if where_document is not None:
            kwargs["where_document"] = where_document
        if limit is not None:
            kwargs["limit"] = limit
        if offset is not None:
            kwargs["offset"] = offset

        raw = self._collection.get(**kwargs)
        out_ids = list(raw.get("ids") or [])
        out_docs = list(raw.get("documents") or []) if spec.documents else []
        out_metas = list(raw.get("metadatas") or []) if spec.metadatas else []
        out_embeds = raw.get("embeddings") if spec.embeddings else None

        # Pad doc/meta lists to match ids so downstream zipping is safe.
        if spec.documents and len(out_docs) < len(out_ids):
            out_docs = out_docs + [""] * (len(out_ids) - len(out_docs))
        if spec.metadatas and len(out_metas) < len(out_ids):
            out_metas = out_metas + [{}] * (len(out_ids) - len(out_metas))

        return GetResult(
            ids=out_ids,
            documents=out_docs,
            metadatas=out_metas,
            embeddings=[list(v) for v in out_embeds] if out_embeds is not None else None,
        )

    def delete(self, *, ids=None, where=None):
        _validate_where(where)
        kwargs: dict[str, Any] = {}
        if ids is not None:
            kwargs["ids"] = ids
        if where is not None:
            kwargs["where"] = where
        with self._write_lock():
            self._collection.delete(**kwargs)

    def count(self):
        return self._collection.count()

    def lexical_search(
        self,
        *,
        query: str,
        n_results: int = 10,
        where: Optional[dict] = None,
    ) -> LexicalResult:
        """Return lexical BM25 candidates for this collection.

        This is the normal healthy-Chroma implementation behind the optional
        backend capability. The HNSW-disabled fallback in ``searcher.py`` still
        reads ``chroma.sqlite3`` directly and remains Chroma-only.
        """
        _validate_where(where)
        sqlite_hits = self._lexical_search_via_sqlite(query=query, n_results=n_results, where=where)
        if sqlite_hits is not None:
            return LexicalResult(hits=sqlite_hits)

        # Directly-constructed ChromaCollection test doubles may not carry a
        # palace path. Keep lexical_search usable in that shape, but normal
        # MemPalace paths above use Chroma's FTS table instead of scanning every
        # drawer through the Python client.
        total = self.count()
        docs: list[str] = []
        metas: list[dict] = []
        ids: list[str] = []
        offset = 0
        batch_size = 1000
        while offset < total:
            kwargs: dict[str, Any] = {
                "include": ["documents", "metadatas"],
                "limit": batch_size,
                "offset": offset,
            }
            if where:
                kwargs["where"] = where
            batch = self.get(**kwargs)
            if not batch.ids:
                break
            ids.extend(batch.ids)
            docs.extend(doc or "" for doc in batch.documents)
            metas.extend(meta or {} for meta in batch.metadatas)
            offset += len(batch.ids)

        scores = _bm25_scores(query, docs)
        hits = [
            LexicalHit(id=doc_id, document=doc, metadata=meta, score=float(score))
            for doc_id, doc, meta, score in zip(ids, docs, metas, scores)
            if score > 0
        ]
        hits.sort(key=lambda hit: hit.score, reverse=True)
        return LexicalResult(hits=hits[:n_results])

    def _collection_name(self) -> Optional[str]:
        name = getattr(self._collection, "name", None)
        if callable(name):
            try:
                name = name()
            except TypeError:
                name = None
        return str(name) if name else None

    def _lexical_search_via_sqlite(
        self,
        *,
        query: str,
        n_results: int,
        where: Optional[dict],
        max_candidates: int = 500,
    ) -> Optional[list[LexicalHit]]:
        if not self._palace_path:
            return None
        db_path = os.path.join(self._palace_path, "chroma.sqlite3")
        if not os.path.isfile(db_path):
            return []
        collection_name = self._collection_name()
        if not collection_name:
            return []

        tokens = [t for t in _tokenize(query) if len(t) >= 3]
        use_recency_fallback = not tokens
        candidate_ids: list[int] = []
        # Map internal embeddings.id (rowid, used to join embedding_metadata)
        # to the public embeddings.embedding_id so returned LexicalHit.id values
        # round-trip through get(ids=...). The two differ: id is the integer
        # rowid, embedding_id is the user-facing drawer id.
        public_ids: dict[int, str] = {}
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
        except sqlite3.Error:
            logger.debug("Chroma lexical sqlite open failed", exc_info=True)
            return []

        try:
            if tokens:
                fts_query = " OR ".join(tokens)
                # If a metadata filter is present, do not cap before filtering:
                # otherwise a common term can fill the window with wrong-scope
                # rows and hide valid scoped hits later in the FTS result set.
                limit_sql = "" if where else "LIMIT ?"
                params = [fts_query, collection_name]
                if not where:
                    params.append(max(max_candidates, n_results))
                try:
                    rows = conn.execute(
                        f"""
                        SELECT e.id, e.embedding_id
                        FROM embedding_fulltext_search
                        JOIN embeddings e ON e.id = embedding_fulltext_search.rowid
                        JOIN segments s ON e.segment_id = s.id
                        JOIN collections c ON s.collection = c.id
                        WHERE embedding_fulltext_search MATCH ?
                          AND c.name = ?
                        {limit_sql}
                        """,
                        params,
                    ).fetchall()
                    candidate_ids = [int(row[0]) for row in rows]
                    public_ids.update({int(row[0]): str(row[1]) for row in rows})
                except sqlite3.Error:
                    logger.debug(
                        "Chroma lexical FTS query failed; using recency fallback", exc_info=True
                    )
                    use_recency_fallback = True

            if not candidate_ids and use_recency_fallback:
                order_expr = "e.created_at DESC"
                try:
                    rows = conn.execute(
                        f"""
                        SELECT e.id, e.embedding_id
                        FROM embeddings e
                        JOIN segments s ON e.segment_id = s.id
                        JOIN collections c ON s.collection = c.id
                        WHERE c.name = ?
                        ORDER BY {order_expr}
                        LIMIT ?
                        """,
                        (collection_name, max(max_candidates, n_results)),
                    ).fetchall()
                except sqlite3.Error:
                    logger.debug(
                        "Chroma lexical recency fallback failed; ordering by id", exc_info=True
                    )
                    rows = conn.execute(
                        """
                        SELECT e.id, e.embedding_id
                        FROM embeddings e
                        JOIN segments s ON e.segment_id = s.id
                        JOIN collections c ON s.collection = c.id
                        WHERE c.name = ?
                        ORDER BY e.id DESC
                        LIMIT ?
                        """,
                        (collection_name, max(max_candidates, n_results)),
                    ).fetchall()
                candidate_ids = [int(row[0]) for row in rows]
                public_ids.update({int(row[0]): str(row[1]) for row in rows})

            if not candidate_ids:
                return []

            meta_columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(embedding_metadata)").fetchall()
            }
            value_columns = [
                col
                for col in ("string_value", "int_value", "float_value", "bool_value")
                if col in meta_columns
            ]
            if not value_columns:
                return []
            meta_rows = []
            for start in range(0, len(candidate_ids), 900):
                chunk_ids = candidate_ids[start : start + 900]
                placeholders = ",".join("?" for _ in chunk_ids)
                meta_rows.extend(
                    conn.execute(
                        f"""
                        SELECT id, key, {", ".join(value_columns)}
                        FROM embedding_metadata
                        WHERE id IN ({placeholders})
                        """,
                        chunk_ids,
                    ).fetchall()
                )
        except sqlite3.Error:
            logger.debug("Chroma lexical sqlite read failed", exc_info=True)
            return []
        finally:
            conn.close()

        drawers: dict[int, dict] = {}
        for row in meta_rows:
            emb_id = int(row["id"])
            key = row["key"]
            values = {col: row[col] if col in row.keys() else None for col in value_columns}
            value = _metadata_cell_value(
                values.get("string_value"),
                values.get("int_value"),
                values.get("float_value"),
                values.get("bool_value"),
            )
            drawer = drawers.setdefault(emb_id, {"metadata": {}, "document": ""})
            if key == "chroma:document":
                drawer["document"] = str(value or "")
            else:
                drawer["metadata"][key] = value

        ordered = []
        for emb_id in candidate_ids:
            drawer = drawers.get(emb_id)
            if drawer is None:
                continue
            meta = drawer["metadata"]
            if not _matches_where(meta, where):
                continue
            ordered.append((emb_id, drawer["document"], meta))

        docs = [doc for _, doc, _ in ordered]
        scores = _bm25_scores(query, docs)
        hits = [
            LexicalHit(
                id=public_ids.get(emb_id, str(emb_id)),
                document=doc,
                metadata=meta,
                score=float(score),
            )
            for (emb_id, doc, meta), score in zip(ordered, scores)
            if score > 0
        ]
        hits.sort(key=lambda hit: hit.score, reverse=True)
        return hits[:n_results]

    @property
    def metadata(self) -> dict:
        """Pass-through to the underlying ChromaDB collection's metadata.

        Used by the searcher to detect legacy palaces that were created
        without ``hnsw:space=cosine`` and therefore silently use L2
        distance, which breaks cosine-based similarity interpretation.
        Returns ``{}`` when metadata is absent so callers can do a plain
        ``.get("hnsw:space")`` without None-checks.
        """
        return self._collection.metadata or {}

    @property
    def distance_metric(self) -> str:
        """Report this collection's actual space from ``hnsw:space``.

        MemPalace sets ``hnsw:space=cosine`` on every creation path, so a
        healthy palace reports ``"cosine"``. When the key is absent, empty, or
        an unrecognized value, the collection is genuinely using Chroma's HNSW
        default — **L2** (Euclidean) — because cosine was never set on it. We
        report ``"l2"`` in that case so core ranking maps the distances
        correctly; reporting ``"cosine"`` here would reintroduce the
        floor-every-result-to-zero misranking this property exists to fix.
        """
        space = str(self.metadata.get("hnsw:space", "") or "").lower()
        if space in ("cosine", "l2", "ip"):
            return space
        return "l2"

    # ------------------------------------------------------------------
    # Embedder identity (RFC 001)
    #
    # Stored in a small sidecar JSON in the palace dir rather than the Chroma
    # collection metadata: ``collection.modify(metadata=...)`` replaces the
    # whole dict and some Chroma versions reject re-passing the immutable
    # ``hnsw:*`` construction keys, so mutating it on every open is fragile.
    # The sidecar is keyed by collection name (a palace may hold several).
    # This is complementary to Chroma's own embedding-function-name check —
    # the core check runs at open time and yields the clean cross-backend
    # error before Chroma's read-time rejection fires.
    # ------------------------------------------------------------------
    def _embedder_sidecar_path(self) -> Optional[str]:
        if not self._palace_path:
            return None
        return os.path.join(self._palace_path, EMBEDDER_SIDECAR_FILENAME)

    def get_stored_embedder_identity(self):
        return read_embedder_sidecar(self._embedder_sidecar_path(), self._collection_name())

    def set_embedder_identity(self, identity) -> None:
        write_embedder_sidecar(self._embedder_sidecar_path(), self._collection_name(), identity)


# ---------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------


class ChromaBackend(BaseBackend):
    """MemPalace's default ChromaDB backend.

    Maintains two caches:

    * ``self._clients`` — ``palace_path -> PersistentClient`` for callers
      using the ``PalaceRef`` / :meth:`get_collection` path.
    * An inode+mtime freshness check absorbed from ``mcp_server._get_client``
      (merged via #757) ensuring a palace rebuild on disk is detected on the
      next :meth:`get_collection` call.
    """

    name = "chroma"
    capabilities = frozenset(
        {
            "supports_embeddings_in",
            "supports_embeddings_passthrough",
            "supports_embeddings_out",
            "supports_metadata_filters",
            "supports_contains_fast",
            "supports_lexical_search",
            "local_mode",
        }
    )

    def __init__(self):
        # palace_path -> PersistentClient
        self._clients: dict[str, Any] = {}
        # palace_path -> (inode, mtime) of chroma.sqlite3 at cache time.
        self._freshness: dict[str, tuple[int, float]] = {}
        self._closed = False

    @staticmethod
    def _resolve_embedding_function():
        """Return the EF for the user's ``embedding_device`` setting.

        Both ``get_collection`` and ``get_or_create_collection`` must receive
        the EF explicitly — ChromaDB 1.x does not persist it with the
        collection, so a reader that omits the argument silently gets the
        library default and its queries won't match the writer's vectors.
        """
        try:
            from ..embedding import get_embedding_function

            return get_embedding_function()
        except Exception:
            logger.exception("Failed to build embedding function; using chromadb default")
            return None

    @staticmethod
    def _explain_ef_mismatch(error: Exception, palace_path: str) -> Optional[str]:
        """If ``error`` looks like a ChromaDB EF-name mismatch, return a
        user-friendly explanation. Otherwise return None so the caller can
        re-raise unchanged.

        Triggered when ``MEMPALACE_EMBEDDING_MODEL`` is switched on an
        existing palace — ChromaDB persists the EF name on the collection
        and refuses reads with a different one. The bare ValueError
        ChromaDB raises doesn't mention rebuild-index or the env var, so
        users hit it and don't know how to recover.
        """
        msg = str(error)
        if "Embedding function conflict" not in msg and "embedding function" not in msg.lower():
            return None
        try:
            from ..config import MempalaceConfig

            current_model = MempalaceConfig().embedding_model
        except Exception:
            current_model = "unknown"
        return (
            f"Embedding model mismatch reading palace at {palace_path!r}.\n"
            f"  Underlying ChromaDB error: {msg}\n"
            f"  Current MEMPALACE_EMBEDDING_MODEL={current_model!r}.\n"
            f"  The palace was built with a different embedding model. Either:\n"
            f"    (a) revert the model: unset MEMPALACE_EMBEDDING_MODEL (or set "
            f"the previous value), or\n"
            f"    (b) re-embed in place: `mempalace repair rebuild-index "
            f"--palace {palace_path}` (writes new vectors with the current model)."
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _db_stat(palace_path: str) -> tuple[int, float]:
        """Return ``(inode, mtime)`` of ``chroma.sqlite3`` or ``(0, 0.0)`` if absent."""
        db_path = os.path.join(palace_path, "chroma.sqlite3")
        try:
            st = os.stat(db_path)
            return (st.st_ino, st.st_mtime)
        except OSError:
            return (0, 0.0)

    def _client(self, palace_path: str):
        """Return a cached ``PersistentClient``, rebuilding on inode/mtime change.

        Handles the palace-rebuild case (repair/nuke/purge) by invalidating the
        cache when ``chroma.sqlite3`` changes on disk. Mirrors the semantics of
        ``mcp_server._get_client`` (merged via #757):

        * DB file missing while we hold a cached client → drop the cache so we
          do not serve stale data after a rebuild that has not yet re-created
          the DB.
        * Transition 0 → nonzero stat (DB created after cache) counts as a
          change, so the cached client is replaced with one that sees the DB.
        * FAT/exFAT filesystems return inode 0; we never fire inode comparisons
          when either side is 0 (safe fallback) but still honor mtime.
        * Mtime change uses an epsilon (0.01 s) to tolerate FS timestamp
          granularity without thrashing.
        """
        if self._closed:
            from .base import BackendClosedError  # late import avoids cycles at module load

            raise BackendClosedError("ChromaBackend has been closed")

        cached = self._clients.get(palace_path)
        cached_inode, cached_mtime = self._freshness.get(palace_path, (0, 0.0))
        current_inode, current_mtime = self._db_stat(palace_path)

        db_path = os.path.join(palace_path, "chroma.sqlite3")
        # DB was present when cache was built but is now missing → invalidate.
        if cached is not None and not os.path.isfile(db_path):
            _close_client(self._clients.pop(palace_path, None))
            self._freshness.pop(palace_path, None)
            cached = None
            cached_inode, cached_mtime = 0, 0.0

        inode_changed = current_inode != 0 and cached_inode != 0 and current_inode != cached_inode
        # Transition from no-stat (0.0) to a real stat counts as a change so we
        # pick up a DB that was created after the cache was built.
        mtime_appeared = cached_mtime == 0.0 and current_mtime != 0.0
        mtime_changed = (
            current_mtime != 0.0
            and cached_mtime != 0.0
            and abs(current_mtime - cached_mtime) > 0.01
        )

        if cached is None or inode_changed or mtime_changed or mtime_appeared:
            # Drop the per-process quarantine gate so the HNSW pre-checks
            # run again against the new disk state.  An inode swap means a
            # different physical DB (post-restore, fresh palace at the same
            # path); an mtime/appearance change means an external in-place
            # write (closet_llm, mine, compress) that may have drifted the
            # HNSW index while this process was running.
            if (
                inode_changed
                or mtime_changed
                or (mtime_appeared and palace_path in self._freshness)
            ):
                ChromaBackend._quarantined_paths.discard(palace_path)
            ChromaBackend._prepare_palace_for_open(palace_path)
            cached = chromadb.PersistentClient(path=palace_path)
            self._clients[palace_path] = cached
            # Re-stat after the client constructor runs: chromadb creates
            # chroma.sqlite3 lazily, so the stat captured before the call
            # may still be (0, 0.0) on first open.
            self._freshness[palace_path] = self._db_stat(palace_path)
        return cached

    # ------------------------------------------------------------------
    # Public static helpers (legacy; prefer :meth:`get_collection`)
    # ------------------------------------------------------------------

    # Per-process record of palaces that have already had the cold-start
    # quarantine invoked at least once. The proactive HNSW checks are a
    # *cold-start* protection -- they catch segments that arrive stale relative
    # to ``chroma.sqlite3`` or invalid on disk (e.g. cross-machine replication,
    # partial restore, crashed-mid-write). The gate is cleared whenever the
    # palace changes on disk (inode swap, mtime bump, or file appearance), so
    # external writes that drift HNSW segments are caught on the next open
    # without requiring a full process restart.
    #
    # Thread-safety: this set is mutated without a lock. Two concurrent
    # ``make_client()`` calls for the same palace can both pass the
    # membership check and both invoke the cold-start quarantine. That's
    # safe because the functions are idempotent (mtime checks + timestamped
    # rename of distinct directories), so the worst-case race produces one
    # redundant rename attempt that no-ops. Idempotency is the safety
    # property; locking would add cost without correctness gain.
    _quarantined_paths: set[str] = set()

    @staticmethod
    def _prepare_palace_for_open(palace_path: str) -> None:
        """Run the pre-open safety pass shared by :meth:`make_client` and
        :meth:`_client`.

        Four steps, all required before constructing a ``PersistentClient``:

        1. ``_fix_missing_collection_type`` — adds the ``_type`` marker to
           ``collections.config_json_str`` that chromadb 1.5.9+ requires
           but <= 1.5.8 never wrote (#1611).
        2. ``_fix_blob_seq_ids`` — repairs the BLOB seq_id quirk that bites
           certain chromadb migrations.
        3. ``quarantine_invalid_hnsw_metadata`` — renames aside any HNSW
           ``index_metadata.pickle`` that fails to load, so chromadb opens
           against an empty index instead of crashing on the unloadable
           pickle (#1266 / PR #1285).
        4. ``quarantine_stale_hnsw`` -- gated by :attr:`_quarantined_paths`
           so it fires once per palace until the gate is re-armed by a
           disk change. This is the SIGSEGV prevention path for stale
           HNSW segments (see #1121, #1132, #1263); wiring it through
           this helper means CLI mining, search, repair, and status all
           benefit, not just the legacy ``make_client`` callers.

        Idempotent: safe to call from any code path that is about to open or
        re-open a palace. The ``_quarantined_paths`` gate prevents thrash on
        hot paths (e.g. ``_client()`` is called on every backend operation).
        """
        _fix_missing_collection_type(palace_path)
        _fix_blob_seq_ids(palace_path)
        if palace_path not in ChromaBackend._quarantined_paths:
            quarantine_invalid_hnsw_metadata(palace_path)
            quarantine_stale_hnsw(palace_path)
            ChromaBackend._quarantined_paths.add(palace_path)

    @staticmethod
    def make_client(palace_path: str):
        """Create a fresh ``PersistentClient`` (runs pre-open safety pass first).

        Deprecated-ish: exposed for legacy long-lived callers that manage their
        own client cache. New code should obtain a collection through
        :meth:`get_collection` which manages caching internally.

        Quarantines HNSW segments on first open and after any detected
        disk change. See :attr:`_quarantined_paths` for the gate logic.
        """
        ChromaBackend._prepare_palace_for_open(palace_path)
        return chromadb.PersistentClient(path=palace_path)

    @staticmethod
    def backend_version() -> str:
        """Return the installed chromadb package version string."""
        return chromadb.__version__

    # ------------------------------------------------------------------
    # BaseBackend surface
    # ------------------------------------------------------------------

    def get_collection(
        self,
        *args,
        **kwargs,
    ) -> ChromaCollection:
        """Obtain a collection for a palace.

        Supports two calling conventions during the RFC 001 transition:

        * New (preferred): ``get_collection(palace=PalaceRef, collection_name=...,
          create=False, options=None)``.
        * Legacy: ``get_collection(palace_path, collection_name, create=False)``
          — still used by callers not yet migrated.
        """
        palace_ref, collection_name, create, options = _normalize_get_collection_args(args, kwargs)

        palace_path = palace_ref.local_path
        if palace_path is None:
            raise PalaceNotFoundError("ChromaBackend requires PalaceRef.local_path")

        if not create and not os.path.isdir(palace_path):
            raise PalaceNotFoundError(palace_path)

        if create:
            os.makedirs(palace_path, exist_ok=True)
            try:
                os.chmod(palace_path, 0o700)
            except (OSError, NotImplementedError):
                pass

        client = self._client(palace_path)
        hnsw_space = "cosine"
        if options and isinstance(options, dict):
            hnsw_space = options.get("hnsw_space", hnsw_space)

        ef = self._resolve_embedding_function()
        ef_kwargs = {"embedding_function": ef} if ef is not None else {}

        if create:
            try:
                collection = client.get_collection(collection_name, **ef_kwargs)
            except _ChromaNotFoundError:
                collection = client.create_collection(
                    collection_name,
                    metadata={
                        "hnsw:space": hnsw_space,
                        "hnsw:num_threads": 1,
                        **_HNSW_BLOAT_GUARD,
                    },
                    **ef_kwargs,
                )
            except ValueError as e:
                explanation = self._explain_ef_mismatch(e, palace_path)
                if explanation:
                    raise ValueError(explanation) from e
                raise
        else:
            try:
                collection = client.get_collection(collection_name, **ef_kwargs)
            except _ChromaNotFoundError as e:
                raise CollectionNotInitializedError(palace_path) from e
            except ValueError as e:
                explanation = self._explain_ef_mismatch(e, palace_path)
                if explanation:
                    raise ValueError(explanation) from e
                raise
        _pin_hnsw_threads(collection)
        return ChromaCollection(collection, palace_path=palace_path)

    def close_palace(self, palace) -> None:
        """Drop cached handles for ``palace`` and release its SQLite file lock.

        Accepts ``PalaceRef`` or legacy path str. chromadb's rust-side file
        lock is held until ``PersistentClient.close()`` is called, so plain
        dict eviction would leave the palace path unreopenable and
        unremovable in the same process.
        """
        path = palace.local_path if isinstance(palace, PalaceRef) else palace
        if path is None:
            return
        _close_client(self._clients.pop(path, None))
        self._freshness.pop(path, None)

    def close(self) -> None:
        for client in self._clients.values():
            _close_client(client)
        self._clients.clear()
        self._freshness.clear()
        self._closed = True

    def health(self, palace: Optional[PalaceRef] = None) -> HealthStatus:
        if self._closed:
            return HealthStatus.unhealthy("backend closed")
        return HealthStatus.healthy()

    @classmethod
    def detect(cls, path: str) -> bool:
        return os.path.isfile(os.path.join(path, "chroma.sqlite3"))

    # ------------------------------------------------------------------
    # Legacy (pre-RFC 001) surface — retained while callers migrate.
    # ------------------------------------------------------------------

    def get_or_create_collection(self, palace_path: str, collection_name: str) -> ChromaCollection:
        """Legacy shim for ``get_collection(..., create=True)`` by path string."""
        return self.get_collection(palace_path, collection_name, create=True)

    def delete_collection(self, palace_path: str, collection_name: str) -> None:
        """Delete ``collection_name`` from the palace at ``palace_path``."""
        self._client(palace_path).delete_collection(collection_name)

    def create_collection(
        self, palace_path: str, collection_name: str, hnsw_space: str = "cosine"
    ) -> ChromaCollection:
        """Create (not get-or-create) ``collection_name`` with the given HNSW space."""
        ef = self._resolve_embedding_function()
        ef_kwargs = {"embedding_function": ef} if ef is not None else {}
        collection = self._client(palace_path).create_collection(
            collection_name,
            metadata={
                "hnsw:space": hnsw_space,
                "hnsw:num_threads": 1,
                **_HNSW_BLOAT_GUARD,
            },
            **ef_kwargs,
        )
        return ChromaCollection(collection, palace_path=palace_path)


def _normalize_get_collection_args(args, kwargs):
    """Unify legacy positional ``(palace_path, collection_name, create)`` calls
    with the new kwargs-only ``(palace=PalaceRef, collection_name=..., create=...)``.

    Returns ``(PalaceRef, collection_name, create, options)``.
    """
    # New-style: palace= kwarg with a PalaceRef (spec path).
    if "palace" in kwargs:
        palace_ref = kwargs.pop("palace")
        if not isinstance(palace_ref, PalaceRef):
            raise TypeError("palace= must be a PalaceRef instance")
        collection_name = kwargs.pop("collection_name")
        create = kwargs.pop("create", False)
        options = kwargs.pop("options", None)
        if kwargs:
            raise TypeError(f"unexpected kwargs: {sorted(kwargs)}")
        if args:
            raise TypeError("positional args not allowed with palace= kwarg")
        return palace_ref, collection_name, create, options

    # Legacy: first positional is a path string.
    if args:
        palace_path = args[0]
        rest = list(args[1:])
        collection_name = kwargs.pop("collection_name", None) or (rest.pop(0) if rest else None)
        if collection_name is None:
            raise TypeError("collection_name is required")
        create = kwargs.pop("create", False)
        if rest:
            create = rest.pop(0)
        if rest:
            raise TypeError(f"unexpected positional args: {rest!r}")
        if kwargs:
            raise TypeError(f"unexpected kwargs: {sorted(kwargs)}")
        return (
            PalaceRef(id=palace_path, local_path=palace_path),
            collection_name,
            bool(create),
            None,
        )

    # Legacy kwargs-only (palace_path=..., collection_name=..., create=...)
    if "palace_path" in kwargs:
        palace_path = kwargs.pop("palace_path")
        collection_name = kwargs.pop("collection_name")
        create = kwargs.pop("create", False)
        if kwargs:
            raise TypeError(f"unexpected kwargs: {sorted(kwargs)}")
        return (
            PalaceRef(id=palace_path, local_path=palace_path),
            collection_name,
            bool(create),
            None,
        )

    raise TypeError("get_collection requires palace= or a positional palace_path")
