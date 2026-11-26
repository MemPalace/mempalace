"""Sub-window vectors for drawers longer than the embedder's token window.

The default MiniLM embedder reads at most 256 tokens. A drawer longer than
that is embedded from its head only, so a query that matches its tail cannot
find it through the vector path (BM25 still can). With
``embedding_subwindows`` on, MemPalace also embeds overlapping
``<= max-token`` windows of the drawer's verbatim text and stores those
vectors as **derived rows** in a sibling collection,
:data:`mempalace.palace.WINDOWS_COLLECTION_NAME`.

The drawer itself never changes. A window row holds a vector, a pointer
(``window_of`` = parent drawer id, ``char_start``/``char_end`` into the
parent's text), copies of the parent's ``source_file`` and ``wing`` (purge
key and search prefilter hint; the parent row stays the authority) and an
empty document: it never carries a second copy of the
text, and no listing, count, export or delete path reads the windows
collection, so a window can never surface as a drawer.

At query time a drawer is scored by its best vector (max-sim over its own
vector and its windows); windows are folded into their parent before top-k.
Window 0 is not stored: the drawer's own vector already is the embedding of
its first ``max-token`` tokens.

Staleness is handled twice. Deleting or rewriting a drawer purges its
windows through :func:`mempalace.palace.drawers_removed`; and search only
trusts a window whose ``parent_sha`` still matches the parent text and whose
``window_profile`` matches the active embedder, so a window a path forgot
to purge, or one built by another model, is ignored rather than misread.
``mempalace windows build`` backfills, refreshes and prunes incrementally.
"""

from __future__ import annotations

import contextlib
import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional

from .search_util import query_post_filtered

logger = logging.getLogger(__name__)

# ``kind`` stamped on every window row (rows of the windows collection only).
WINDOW_KIND = "drawer_window"
# A file in the palace directory, written when the windows collection is first
# created or opened for writing. With the flag off, a palace without it skips
# every windows lookup, so default-off writes and deletes cost nothing extra.
WINDOWS_MARKER = ".mempalace-drawer-windows"
# Tokens shared by neighbouring windows, so a fact cut by a window edge is
# still whole in one window.
WINDOW_OVERLAP_TOKENS = 64
# Window hits fetched per drawer slot in the search pool. A long drawer can
# own several windows near the query, and folding them leaves fewer parents.
WINDOW_OVERFETCH = 3
# A wing/room/source-filtered search reads windows unfiltered (their copy of
# the parent's metadata can be stale) and keeps only parents that pass the
# filter, so it fetches this many times wider, up to WINDOW_MAX_FETCH.
WINDOW_FILTERED_WIDEN = 4
WINDOW_MAX_FETCH = 1000
# When a filtered search's global window pass finds too few in-scope parents,
# a scoped pass asks the in-scope windows directly. A wing scope filters the
# window rows by their ``wing`` hint, with no cap. A scope the hint cannot
# express (room, source), or a wing whose nearest windows still leave too few
# in-scope parents, asks the windows of the in-scope parent ids, this many ids
# per query, over at most WINDOW_SCOPED_MAX_PARENTS parents; hitting that cap
# is logged at info.
WINDOW_SCOPED_PAGE = 2000
WINDOW_SCOPED_MAX_PARENTS = 20_000
WINDOW_SCOPED_FETCH = 500
# Drawers read / windows written per batch.
BATCH_SIZE = 500
# ``windows build`` takes the palace lock per batch. While another writer
# holds it, the build retries this often, for at most LOCK_WAIT_SECONDS.
LOCK_RETRY_SECONDS = 0.5
LOCK_WAIT_SECONDS = 600.0


@dataclass(frozen=True)
class WindowTokenizer:
    """The active embedder's real tokenizer, without truncation.

    ``offsets(text)`` returns one ``(char_start, char_end)`` per content token
    (no special tokens); ``budget`` is how many content tokens one embedding
    call reads before the embedder truncates.
    """

    offsets: Callable[[str], list]
    budget: int


@dataclass(frozen=True)
class WindowProfile:
    """What a window vector depends on: the embedder and the window plan."""

    model: str
    dimension: int
    budget: int
    overlap: int

    @property
    def key(self) -> str:
        return f"{self.model}|d{self.dimension}|t{self.budget}|o{self.overlap}"


def _untruncated(tokenizer):
    from tokenizers import Tokenizer

    copy = Tokenizer.from_str(tokenizer.to_str())
    copy.no_truncation()
    copy.no_padding()
    return copy


def _offsets_fn(tokenizer) -> Callable[[str], list]:
    from .config import strip_lone_surrogates

    def offsets(text: str) -> list:
        # One replacement character per undecodable byte preserves parent offsets.
        return list(tokenizer.encode(strip_lone_surrogates(text), add_special_tokens=False).offsets)

    return offsets


def window_tokenizer(ef, model_name: str) -> Optional[WindowTokenizer]:
    """The window tokenizer for embedding function ``ef``, or None if unknown.

    Supported: ``minilm`` (ChromaDB's ONNX MiniLM, 256 tokens) and
    ``embeddinggemma`` (ONNX, 2048 tokens including its task prefix). An
    embedder whose tokenizer MemPalace cannot see (an ``openai-compat``
    endpoint, EmbeddingGemma 2 on PyTorch) returns None: windows are only
    built from a real tokenizer, never a character estimate.
    """
    if model_name == "minilm":
        base = getattr(ef, "tokenizer", None)
        max_tokens = getattr(ef, "max_tokens", None)
        if base is None or not callable(max_tokens):
            return None
        tok = _untruncated(base)
        specials = len(tok.encode("").ids)
        return WindowTokenizer(_offsets_fn(tok), int(max_tokens()) - specials)
    if model_name == "embeddinggemma":
        from .embedding import _EMBEDDINGGEMMA_MAX_LEN, _EMBEDDINGGEMMA_PREFIX

        load = getattr(ef, "_lazy_load", None)
        if getattr(ef, "_tokenizer", None) is None and callable(load):
            load()
        base = getattr(ef, "_tokenizer", None)
        if base is None:
            return None
        tok = _untruncated(base)
        reserved = len(tok.encode(_EMBEDDINGGEMMA_PREFIX).ids)
        # Prefix and text tokenized together can merge one token at the seam.
        return WindowTokenizer(_offsets_fn(tok), _EMBEDDINGGEMMA_MAX_LEN - reserved - 1)
    return None


def plan_windows(text: str, tokenizer: WindowTokenizer, overlap: int) -> list:
    """Char spans of the windows ``text`` needs beyond its first one.

    Empty when ``text`` fits the embedder's window (short drawers get no
    window rows). Otherwise each span covers at most ``budget`` tokens, starts
    ``budget - overlap`` tokens after the previous one, and the last one ends
    at the last token. The first window ``[0, budget)`` is omitted: it is the
    drawer's own vector.
    """
    offsets = tokenizer.offsets(text)
    budget = tokenizer.budget
    count = len(offsets)
    if budget <= 0 or count <= budget:
        return []
    stride = max(1, budget - max(0, min(overlap, budget - 1)))
    spans = []
    start = stride
    while start < count:
        end = min(start + budget, count)
        spans.append((offsets[start][0], offsets[end - 1][1]))
        if end == count:
            break
        start += stride
    return spans


def parent_sha(text: str) -> str:
    """Fingerprint of the parent text a window was cut from."""
    from .ids import content_fingerprint

    return content_fingerprint(text)


def window_id(parent_id: str, index: int) -> str:
    return f"{parent_id}::w{index:03d}"


# ``source_file`` lets a whole-source delete purge windows without the
# parent ids.
_COPIED_KEYS = ("source_file",)
# ``wing`` is a prefilter hint for the wing-scoped window pass. Search still
# checks every hit against the parent row, so a stale hint can only hide a
# window, never admit an out-of-scope one; a hint-only change is a metadata
# update (:func:`retag_windows`), not a re-embed. Room is not copied.
_HINT_KEYS = ("wing",)
_STORED_KEYS = _COPIED_KEYS + _HINT_KEYS


def window_rows(parent_id: str, document: str, metadata: dict, spans: list, profile) -> list:
    """``(id, metadata)`` for each window of one parent.

    Window rows copy the parent's ``source_file`` and ``wing`` (see
    ``_STORED_KEYS``); the parent row stays the authority and is re-read at
    query time.
    """
    sha = parent_sha(document)
    base = {key: metadata[key] for key in _STORED_KEYS if metadata.get(key) is not None}
    rows = []
    for index, (start, end) in enumerate(spans, start=1):
        rows.append(
            (
                window_id(parent_id, index),
                {
                    **base,
                    "kind": WINDOW_KIND,
                    "window_of": parent_id,
                    "window_index": index,
                    "window_total": len(spans),
                    "char_start": int(start),
                    "char_end": int(end),
                    "parent_sha": sha,
                    "window_profile": profile.key,
                },
            )
        )
    return rows


def _hint(metadata: dict) -> dict:
    """The hint keys a parent's windows should carry (absent keys are not hinted)."""
    return {key: metadata[key] for key in _HINT_KEYS if metadata.get(key) is not None}


def _fresh(existing: list, parent_id: str, document: str, metadata: dict, profile, spans) -> bool:
    """Whether stored windows ``existing`` already match this parent's vectors.

    The wing hint is not compared here; :meth:`WindowIndex.index` re-tags it.
    """
    if len(existing) != len(spans):
        return False
    sha = parent_sha(document)
    want = {key: metadata.get(key) for key in _COPIED_KEYS}
    for meta in existing:
        if (
            meta.get("parent_sha") != sha
            or meta.get("window_profile") != profile.key
            or meta.get("window_total") != len(spans)
            or any(meta.get(key) != value for key, value in want.items())
        ):
            return False
    return True


def _retag(collection, updates: list) -> None:
    """Write ``(window id, hint metadata)`` pairs as metadata-only updates."""
    for start in range(0, len(updates), BATCH_SIZE):
        batch = updates[start : start + BATCH_SIZE]
        collection.update(ids=[i for i, _ in batch], metadatas=[m for _, m in batch])


# ── the index ────────────────────────────────────────────────────────────


class WindowIndex:
    """Writes and purges window rows for one palace's windows collection."""

    def __init__(self, collection, embedder, tokenizer: WindowTokenizer, profile: WindowProfile):
        self.collection = collection
        self.embedder = embedder
        self.tokenizer = tokenizer
        self.profile = profile

    def _existing(self, parent_ids: list) -> dict:
        by_parent: dict = {}
        for start in range(0, len(parent_ids), BATCH_SIZE):
            chunk = parent_ids[start : start + BATCH_SIZE]
            got = self.collection.get(where={"window_of": {"$in": chunk}}, include=["metadatas"])
            for row_id, meta in zip(got.get("ids") or [], got.get("metadatas") or []):
                meta = meta or {}
                by_parent.setdefault(meta.get("window_of"), []).append((row_id, meta))
        return by_parent

    def index(self, rows: Iterable) -> dict:
        """Bring the windows of ``rows`` (``(drawer_id, document, metadata)``) up to date.

        Short drawers get none and lose any they had; a drawer whose stored
        windows already match its text, metadata and the profile is skipped,
        so a re-run after an interruption only embeds what is missing.
        Returns counts: ``parents`` (long drawers seen), ``embedded``
        (windows written), ``skipped`` (parents already fresh), ``removed``.
        """
        from .metadata_types import is_bookkeeping

        planned = []
        for drawer_id, document, metadata in rows:
            metadata = metadata or {}
            if not isinstance(document, str) or is_bookkeeping(metadata, drawer_id):
                continue
            planned.append(
                (
                    drawer_id,
                    document,
                    metadata,
                    plan_windows(document, self.tokenizer, self.profile.overlap),
                )
            )
        stats = {"parents": 0, "embedded": 0, "skipped": 0, "removed": 0}
        if not planned:
            return stats
        existing = self._existing([drawer_id for drawer_id, _, _, _ in planned])
        to_write, to_delete, to_retag = [], [], []
        for drawer_id, document, metadata, spans in planned:
            stored = existing.get(drawer_id, [])
            if spans:
                stats["parents"] += 1
            if spans and _fresh(
                [m for _, m in stored], drawer_id, document, metadata, self.profile, spans
            ):
                stats["skipped"] += 1
                hint = _hint(metadata)
                to_retag.extend(
                    (row_id, hint)
                    for row_id, meta in stored
                    if any(meta.get(key) != value for key, value in hint.items())
                )
                continue
            keep = {window_id(drawer_id, i) for i in range(1, len(spans) + 1)}
            to_delete.extend(row_id for row_id, _ in stored if row_id not in keep)
            previous = dict(stored)
            for (row_id, meta), (start, end) in zip(
                window_rows(drawer_id, document, metadata, spans, self.profile), spans
            ):
                if getattr(self.collection, "upsert_merges_metadata", False):
                    for key in _STORED_KEYS:
                        if key in previous.get(row_id, {}) and key not in meta:
                            meta[key] = None
                to_write.append((row_id, document[start:end], meta))
        for start in range(0, len(to_write), BATCH_SIZE):
            batch = to_write[start : start + BATCH_SIZE]
            from .backends.embedding_wrapper import _embed_texts

            vectors = _embed_texts([text for _, text, _ in batch], embedder=self.embedder)
            self.collection.upsert(
                ids=[row_id for row_id, _, _ in batch],
                # A window row never stores text: the parent drawer is the
                # one verbatim copy. The span points into it.
                documents=["" for _ in batch],
                metadatas=[meta for _, _, meta in batch],
                embeddings=vectors,
            )
            stats["embedded"] += len(batch)
        for start in range(0, len(to_delete), BATCH_SIZE):
            self.collection.delete(ids=to_delete[start : start + BATCH_SIZE])
        _retag(self.collection, to_retag)
        stats["removed"] = len(to_delete)
        stats["retagged"] = len(to_retag)
        return stats


def subwindows_enabled() -> bool:
    from .config import MempalaceConfig

    try:
        return bool(MempalaceConfig().embedding_subwindows)
    except Exception:
        return False


# model -> (embedder, WindowTokenizer | None). Copying a
# tokenizer is milliseconds; search calls this once per query.
_TOKENIZER_CACHE: dict = {}


def active_profile(embedder=None):
    """``(embedder, tokenizer, profile)`` for the configured model, or None.

    None when the embedder's tokenizer is not reachable (see
    :func:`window_tokenizer`).
    """
    from .embedding import configured_embedding_function, current_model_name, probe_dimension

    model = current_model_name()
    ef = embedder if embedder is not None else configured_embedding_function()
    cached = _TOKENIZER_CACHE.get(model)
    if cached is None or cached[0] is not ef:
        # One entry per model: a reloaded embedder replaces the old one, so
        # an idle unload does not leave embedders pinned here.
        cached = (ef, window_tokenizer(ef, model))
        _TOKENIZER_CACHE[model] = cached
    tokenizer = cached[1]
    if tokenizer is None:
        return None
    dimension = probe_dimension()
    if dimension <= 0:
        # A failed probe is not cached; a profile stamped d0 would never match.
        return None
    profile = WindowProfile(model, dimension, tokenizer.budget, WINDOW_OVERLAP_TOKENS)
    return ef, tokenizer, profile


class WindowPurger:
    """Stand-in for :class:`WindowIndex` with sub-windows off.

    A palace that has windows keeps purging them with their drawers after the
    flag is turned off, but writes none.
    """

    def __init__(self, collection):
        self.collection = collection

    def index(self, rows) -> dict:
        return {"parents": 0, "embedded": 0, "skipped": 0, "removed": 0}


def retag_windows(palace_or_collection, moves: dict) -> int:
    """Point the wing hint of moved drawers' windows at their new wing.

    ``moves`` maps a drawer id to its new wing. For writers that change a
    drawer's ``wing`` metadata without re-filing it (wing split, wing-name
    migration); a re-filed drawer gets its hint through
    :meth:`WindowIndex.index`. Never raises: the move has committed, the
    parent stays the authority at query time, and ``windows build`` re-tags
    whatever this missed. Returns the number of window rows updated.
    """
    if not moves:
        return 0
    palace_path = palace_or_collection
    if not isinstance(palace_path, str):
        palace_path = _collection_palace_path(palace_or_collection)
    if not palace_path or not windows_may_exist(palace_path):
        return 0
    try:
        from .palace import open_windows_collection

        col = open_windows_collection(palace_path)
        if col is None:
            return 0
        ids = list(moves)
        updates = []
        for start in range(0, len(ids), BATCH_SIZE):
            chunk = ids[start : start + BATCH_SIZE]
            got = col.get(where={"window_of": {"$in": chunk}}, include=["metadatas"])
            for row_id, meta in zip(got.get("ids") or [], got.get("metadatas") or []):
                wing = moves.get((meta or {}).get("window_of"))
                if wing is not None and (meta or {}).get("wing") != wing:
                    updates.append((row_id, {"wing": wing}))
        _retag(col, updates)
        return len(updates)
    except Exception:
        logger.warning(
            "Window wing hints not updated for %d moved drawers; `mempalace windows build` "
            "re-tags them",
            len(moves),
            exc_info=True,
        )
        return 0


def windows_may_exist(palace_path) -> bool:
    """Whether the palace may hold window rows worth opening or purging.

    True when ``embedding_subwindows`` is on (the windows collection is then
    opened and, if it exists, its marker is restored) or when the palace's
    :data:`WINDOWS_MARKER` says a windows collection was created. False keeps
    a default-off palace that never had windows off the windows storage.
    """
    if not palace_path:
        return False
    if subwindows_enabled():
        return True
    try:
        return (Path(palace_path) / WINDOWS_MARKER).is_file()
    except OSError:
        return False


def open_window_index(palace_path: str, *, create: bool = True):
    """The palace's :class:`WindowIndex` when sub-windows are on.

    Off, a :class:`WindowPurger` when the palace already has a windows
    collection, else None. Never raises: a mine keeps going without windows
    (``mempalace windows build`` fills them in later) when the collection or
    tokenizer is missing.
    """
    if not subwindows_enabled():
        try:
            from .palace import open_windows_collection

            if not windows_may_exist(palace_path):
                return None
            col = open_windows_collection(palace_path)
        except Exception:
            return None
        return WindowPurger(col) if col is not None else None
    try:
        resolved = active_profile()
        if resolved is None:
            logger.warning(
                "embedding_subwindows is on but the active embedder's tokenizer or "
                "dimension is not available; drawers are embedded as before"
            )
            return None
        from .palace import get_windows_collection

        return WindowIndex(get_windows_collection(palace_path, create=create), *resolved)
    except Exception:
        logger.warning("Sub-window index unavailable; mining without it", exc_info=True)
        return None


def index_quietly(windows: Optional[WindowIndex], rows: list) -> None:
    """:meth:`WindowIndex.index` for a miner: a failure is logged, never raised."""
    if windows is None or not rows:
        return
    from .palace import MineAlreadyRunning

    try:
        windows.index(rows)
    except MineAlreadyRunning:
        raise
    except Exception:
        # A derived index never aborts a mine, even when its handle went bad
        # (a backend reset closes it): the drawers are already filed.
        logger.warning(
            "Sub-window indexing failed for %d drawers; `mempalace windows build` retries",
            len(rows),
            exc_info=True,
        )


def index_after_write(palace_path: str, ids, documents, metadatas) -> None:
    """Index the windows of drawers just written outside a miner (MCP tools).

    The drawer write already succeeded, so nothing here may fail it: every
    error is logged and ``mempalace windows build`` picks the drawers up later.
    """
    # Flag off, nothing is written: a re-filed drawer's old windows were
    # already purged by the caller's delete cascade.
    if not palace_path or not ids or not subwindows_enabled():
        return
    try:
        windows = open_window_index(palace_path)
        if windows is not None:
            windows.index(list(zip(ids, documents, metadatas)))
    except Exception:
        logger.warning(
            "Sub-window indexing failed for %d drawers; `mempalace windows build` retries",
            len(ids),
            exc_info=True,
        )


def _collection_palace_path(collection) -> Optional[str]:
    """The palace directory a (possibly wrapped) drawer collection lives in."""
    seen = 0
    while collection is not None and seen < 4:
        # ``_handle``: a sqlite_exact collection keeps its path on the handle.
        for owner in (collection, getattr(collection, "_handle", None)):
            for attr in ("palace_path", "_palace_path"):
                value = getattr(owner, attr, None)
                if isinstance(value, str) and value:
                    return value
        collection = getattr(collection, "_inner", None)
        seen += 1
    return None


def index_collection_write(collection, ids, documents, metadatas) -> None:
    """:func:`index_after_write` for a writer that holds only the drawer collection.

    Used by the single-drawer helpers external integrations call
    (``file_conversation_exchange``, ``miner.add_drawer``). A collection
    whose palace directory is unknown is skipped; ``windows build`` covers it.
    """
    index_after_write(_collection_palace_path(collection), ids, documents, metadatas)


def delete_windows(windows_col, parent_ids=(), source_file: Optional[str] = None) -> int:
    """Delete the windows of ``parent_ids`` and/or every window of ``source_file``."""
    ids: set = set()
    parent_ids = list(dict.fromkeys(parent_ids))
    for start in range(0, len(parent_ids), BATCH_SIZE):
        chunk = parent_ids[start : start + BATCH_SIZE]
        got = windows_col.get(where={"window_of": {"$in": chunk}}, include=[])
        ids.update(got.get("ids") or [])
    if source_file:
        ids.update(windows_col.get(where={"source_file": source_file}, include=[]).get("ids") or [])
    ordered = sorted(ids)
    for start in range(0, len(ordered), BATCH_SIZE):
        windows_col.delete(ids=ordered[start : start + BATCH_SIZE])
    return len(ordered)


# ── query time ───────────────────────────────────────────────────────────


def fold_windows(window_results: dict, profile_key: str, parents: dict) -> dict:
    """Best window per parent: ``{parent_id: (distance, parent_sha, (char_start, char_end))}``.

    Rows of another profile are skipped. ``window_results`` is a one-query
    Chroma-shaped result (``ids``/``metadatas``/``distances`` nested lists).
    """
    metas = (window_results.get("metadatas") or [[]])[0] or []
    dists = (window_results.get("distances") or [[]])[0] or []
    current = {i: parent_sha(doc) for i, (doc, _) in parents.items()}
    best: dict = {}
    for meta, dist in zip(metas, dists):
        meta = meta or {}
        parent = meta.get("window_of")
        if not parent or dist is None or meta.get("window_profile") != profile_key:
            continue
        if parent not in current or meta.get("parent_sha") != current[parent]:
            continue
        if parent not in best or dist < best[parent][0]:
            best[parent] = (
                dist,
                meta.get("parent_sha"),
                (meta.get("char_start"), meta.get("char_end")),
            )
    return best


_UNSUPPORTED = object()


def _where_matches(meta: dict, where: dict):
    """Evaluate a Chroma metadata ``where`` on ``meta``; ``_UNSUPPORTED`` if unknown.

    Covers what search scopes build (bare equality, ``$eq``/``$ne``/``$in``/
    ``$nin``, ``$and``/``$or``). Chroma applies a ``where`` next to ``ids`` by
    scanning the filter first, which is seconds on a large wing, so the
    window path fetches by id and checks the scope here.
    """
    results = []
    for key, cond in where.items():
        if key in ("$and", "$or"):
            parts = [_where_matches(meta, clause) for clause in cond]
            if _UNSUPPORTED in parts:
                return _UNSUPPORTED
            results.append(all(parts) if key == "$and" else any(parts))
            continue
        if key.startswith("$"):
            return _UNSUPPORTED
        value = meta.get(key)
        if not isinstance(cond, dict):
            results.append(key in meta and value == cond)
            continue
        if len(cond) != 1:
            return _UNSUPPORTED
        op, arg = next(iter(cond.items()))
        if op == "$eq":
            results.append(key in meta and value == arg)
        elif op == "$ne":
            results.append(value != arg)
        elif op == "$in":
            results.append(key in meta and value in arg)
        elif op == "$nin":
            results.append(value not in arg)
        else:
            return _UNSUPPORTED
    return all(results)


def _parents_under(drawers, ids, where) -> dict:
    """``{id: (document, metadata)}`` of the drawers ``ids`` that pass ``where``."""
    parents: dict = {}
    ids = list(dict.fromkeys(ids))
    for start in range(0, len(ids), BATCH_SIZE):
        batch = ids[start : start + BATCH_SIZE]
        got = drawers.get(ids=batch, include=["documents", "metadatas"])
        rows = list(
            zip(got.get("ids") or [], got.get("documents") or [], got.get("metadatas") or [])
        )
        if where:
            checks = [_where_matches(meta or {}, where) for _, _, meta in rows]
            if _UNSUPPORTED in checks:
                kept = set(drawers.get(ids=batch, where=where, include=[]).get("ids") or [])
                rows = [row for row in rows if row[0] in kept]
            else:
                rows = [row for row, ok in zip(rows, checks) if ok]
        for row_id, doc, meta in rows:
            if isinstance(doc, str):
                parents[row_id] = (doc, meta or {})
    return parents


def _window_parent_ids(window_results: dict) -> list:
    metas = (window_results.get("metadatas") or [[]])[0] or []
    return list(dict.fromkeys(m["window_of"] for m in metas if m and m.get("window_of")))


def _scoped_ids(collection, where) -> list:
    """Ids of up to :data:`WINDOW_SCOPED_MAX_PARENTS` drawers under ``where``.

    One limit-bounded read without text: offset paging re-scans the filter
    for every page, which is minutes on a 100k-drawer wing.
    """
    got = collection.get(where=where, include=[], limit=WINDOW_SCOPED_MAX_PARENTS)
    return list(got.get("ids") or [])


def _is_wing_clause(clause) -> bool:
    if not isinstance(clause, dict) or set(clause) != {"wing"}:
        return False
    cond = clause["wing"]
    if not isinstance(cond, dict):
        return isinstance(cond, str)
    return len(cond) == 1 and next(iter(cond)) in ("$eq", "$in")


def _wing_clause(where):
    """The wing clause of a search ``where``, or None.

    That is what :func:`mempalace.search_util.build_where_filter` builds
    (``{"wing": {"$in": aliases}}``), alone or as one part of a top-level
    ``$and``. It applies to window rows as is, through their ``wing`` hint.
    """
    if _is_wing_clause(where):
        return where
    if isinstance(where, dict) and set(where) == {"$and"}:
        for clause in where["$and"] or []:
            if _is_wing_clause(clause):
                return clause
    return None


def window_candidates(drawers, windows, vectors, profile_key, where, pool_size):
    """Best current window per parent, and the parents it was checked against.

    Returns ``(best, parents)``: ``best`` as :func:`fold_windows` builds it,
    ``parents`` the ``(document, metadata)`` of each parent, read from the
    drawers collection under ``where``. Only windows whose ``parent_sha``
    matches that current text count, so a stale or orphaned window is ignored.

    One windows query of the active profile is enough unless ``where`` scopes
    the search and the nearest windows mostly belong to out-of-scope parents.
    Then a scoped pass asks the in-scope windows directly: by their ``wing``
    hint when ``where`` names a wing (every hit is still checked against its
    parent, which is the authority), and, when there is no wing or the wing's
    nearest windows still leave too few in-scope parents, by the ids of the
    in-scope parents (one bounded read, up to
    :data:`WINDOW_SCOPED_MAX_PARENTS`, logged at info when the cap is hit).
    """
    fetch = min(
        WINDOW_MAX_FETCH, pool_size * WINDOW_OVERFETCH * (WINDOW_FILTERED_WIDEN if where else 1)
    )
    profile_filter = {"window_profile": profile_key}

    def query(window_filter, n_results):
        kwargs = {
            "query_embeddings": vectors,
            "where": window_filter,
            "n_results": n_results,
            "include": ["metadatas", "distances"],
        }
        try:
            return windows.query(**kwargs)
        except Exception as exc:
            # Chroma's HNSW reader can fail a filtered query only. Same rows,
            # read unfiltered and widened until the filter's own count is met.
            logger.warning(
                "Filtered window search failed (%s); falling back to unfiltered + post-filter",
                exc,
            )
            return query_post_filtered(
                windows,
                kwargs,
                lambda meta: _where_matches(meta, window_filter) is True,
                n_results,
                n_results,
            )

    def saturated(result, n_results):
        return len((result.get("ids") or [[]])[0] or []) >= n_results

    raw = query(profile_filter, fetch)
    hit_ids = _window_parent_ids(raw)
    if not hit_ids:
        return {}, {}
    parents = _parents_under(drawers, hit_ids, where)
    best = fold_windows(raw, profile_key, parents)
    if not where or len(best) >= pool_size or not saturated(raw, fetch):
        return best, parents

    # The globally nearest windows can all belong to out-of-scope parents.
    # Ask the in-scope windows directly and keep each parent's nearest few
    # (several, so a stale nearest window cannot hide a current one).
    per_page = min(WINDOW_SCOPED_FETCH, pool_size * WINDOW_OVERFETCH)

    def add_scoped(rows):
        if not rows:
            return
        ranked = sorted(rows, key=lambda parent: min(d for d, _ in rows[parent]))
        ranked = ranked[: pool_size * WINDOW_OVERFETCH]
        scoped_parents = _parents_under(drawers, ranked, where)
        flat = [hit for parent in ranked for hit in rows[parent]]
        scoped_best = fold_windows(
            {"metadatas": [[m for _, m in flat]], "distances": [[d for d, _ in flat]]},
            profile_key,
            scoped_parents,
        )
        for parent, hit in scoped_best.items():
            if parent not in best or hit[0] < best[parent][0]:
                best[parent] = hit
        parents.update(scoped_parents)

    def collect(result, rows):
        metas = (result.get("metadatas") or [[]])[0] or []
        dists = (result.get("distances") or [[]])[0] or []
        for meta, dist in zip(metas, dists):
            if meta and meta.get("window_of") and dist is not None:
                rows.setdefault(meta["window_of"], []).append((dist, meta))

    wing_clause = _wing_clause(where)
    by_ids = True
    if wing_clause is not None:
        hinted: dict = {}
        result = query({"$and": [profile_filter, wing_clause]}, per_page)
        collect(result, hinted)
        add_scoped(hinted)
        # Unsaturated, the hinted query saw every window of the wing. Else
        # its nearest windows can still miss a room or source scope, or be
        # many windows of few parents: the id pass below can only add more.
        by_ids = len(best) < pool_size and saturated(result, per_page)
    if by_ids:
        rows: dict = {}
        in_scope = _scoped_ids(drawers, where)
        if len(in_scope) >= WINDOW_SCOPED_MAX_PARENTS:
            logger.info(
                "Scoped window pass limited to %d parents; windows of further in-scope "
                "drawers are not searched (their head vector and BM25 still are)",
                len(in_scope),
            )
        for start in range(0, len(in_scope), WINDOW_SCOPED_PAGE):
            page = in_scope[start : start + WINDOW_SCOPED_PAGE]
            collect(query({"$and": [profile_filter, {"window_of": {"$in": page}}]}, per_page), rows)
        add_scoped(rows)
    best = dict(sorted(best.items(), key=lambda item: item[1][0])[:pool_size])
    return best, {i: parents[i] for i in best}


def merge_window_hits(
    drawer_results: dict,
    best: dict,
    parents: dict,
    *,
    to_drawer_scale: Callable[[float], float],
    limit: int,
) -> tuple:
    """Fold window distances into a one-query drawer result.

    ``best`` comes from :func:`fold_windows`, with window distances already on
    the drawer collection's scale via ``to_drawer_scale``. ``parents`` maps a
    parent id to its current ``(document, metadata)``, read from the drawers
    collection under the search's own filter. A window counts only when its
    parent is there and its ``parent_sha`` matches the parent's text.

    A drawer's distance becomes the smaller of its own and its best window's.
    A parent the drawer query missed joins the pool. The result is re-sorted
    by distance, retaining the top ``limit`` plus any displaced drawer candidates.
    Returns ``(results, promoted)`` where ``promoted`` counts drawers whose
    distance a window improved or that a window brought in.
    """
    ids = list((drawer_results.get("ids") or [[]])[0] or [])
    docs = list((drawer_results.get("documents") or [[]])[0] or [])
    metas = list((drawer_results.get("metadatas") or [[]])[0] or [])
    dists = list((drawer_results.get("distances") or [[]])[0] or [])
    n = len(docs)
    ids = (ids + [None] * n)[:n]
    metas = (metas + [None] * n)[:n]
    dists = (dists + [None] * n)[:n]

    valid: dict = {}
    spans: dict = {}
    for parent_id, hit in best.items():
        distance, sha = hit[0], hit[1]
        parent = parents.get(parent_id)
        if parent is None or parent_sha(parent[0]) != sha:
            continue
        valid[parent_id] = to_drawer_scale(distance)
        if len(hit) > 2 and hit[2] is not None:
            spans[parent_id] = tuple(hit[2])
    if not valid:
        return drawer_results, 0

    promoted_ids = set()
    rows = []
    seen = set()
    for row in zip(ids, docs, metas, dists):
        row_id, doc, meta, dist = row
        window = valid.get(row_id)
        if window is not None and doc != parents[row_id][0]:
            # A concurrent rewrite changed the text after the drawer query.
            doc, meta = parents[row_id]
            dist = None
        if window is not None and (dist is None or window < dist):
            dist = window
            promoted_ids.add(row_id)
        rows.append((row_id, doc, meta, dist))
        seen.add(row_id)
    for parent_id, window in valid.items():
        if parent_id in seen:
            continue
        doc, meta = parents[parent_id]
        rows.append((parent_id, doc, meta, window))
        promoted_ids.add(parent_id)
    rows.sort(key=lambda r: float("inf") if r[3] is None else r[3])
    # Hybrid BM25 must still see the original drawer candidates. A strong
    # tail can otherwise evict an exact lexical match before reranking. So
    # ``rows`` can be longer than ``limit``: the search's ``candidates_fetched``
    # counts these displaced drawers too and can exceed its ``pool_size``.
    rows = rows[:limit] + [row for row in rows[limit:] if row[0] in seen]
    kept = {r[0] for r in rows}
    merged = {
        # Drawer id -> (char_start, char_end) of the window that set its
        # distance, for ``matched_via: "window"`` on the search hit.
        "window_spans": {i: spans.get(i) for i in promoted_ids if i in kept},
        "displaced_drawers": max(0, len(rows) - limit),
        "ids": [[r[0] for r in rows]],
        "documents": [[r[1] for r in rows]],
        "metadatas": [[r[2] for r in rows]],
        "distances": [[r[3] for r in rows]],
    }
    return merged, len(promoted_ids)


# ── backfill ─────────────────────────────────────────────────────────────


class SubwindowsUnsupported(RuntimeError):
    """Sub-windows cannot be built here: no usable tokenizer, backend or collection."""


class SubwindowsBackendUnsupported(SubwindowsUnsupported):
    """The palace's backend cannot store window vectors (see ``WINDOW_BACKENDS``)."""


def _page_rows(collection, *, where=None, include=("documents", "metadatas"), page_size=BATCH_SIZE):
    offset = 0
    while True:
        kwargs = {"include": list(include), "limit": page_size, "offset": offset}
        if where:
            kwargs["where"] = where
        got = collection.get(**kwargs)
        ids = got.get("ids") or []
        if not ids:
            return
        docs = got.get("documents") or [None] * len(ids)
        metas = got.get("metadatas") or [None] * len(ids)
        yield list(zip(ids, docs, metas))
        offset += len(ids)
        if len(ids) < page_size:
            return


@contextlib.contextmanager
def _held(lock: Optional[Callable]):
    """Hold ``lock()`` (a writer lock factory), waiting while another writer has it.

    Retries every :data:`LOCK_RETRY_SECONDS` for up to :data:`LOCK_WAIT_SECONDS`,
    then lets ``MineAlreadyRunning`` through.
    """
    if lock is None:
        yield
        return
    from .palace import MineAlreadyRunning

    deadline = time.monotonic() + LOCK_WAIT_SECONDS
    while True:
        held = lock()
        try:
            held.__enter__()
        except MineAlreadyRunning:
            if time.monotonic() >= deadline:
                raise
            time.sleep(LOCK_RETRY_SECONDS)
            continue
        break
    try:
        yield
    except BaseException:
        if not held.__exit__(*sys.exc_info()):
            raise
    else:
        held.__exit__(None, None, None)


def build_windows(
    palace_path: str,
    *,
    wing: Optional[str] = None,
    dry_run: bool = False,
    progress: Optional[Callable[[dict], None]] = None,
    lock: Optional[Callable] = None,
) -> dict:
    """Backfill, refresh and prune the sub-window rows of a palace.

    Incremental and resumable: every drawer is read once (read-only), and a
    long drawer whose windows already match its text, metadata and the active
    embedder profile is skipped, so a re-run after an interruption only embeds
    what is still missing. Windows of drawers that changed, or of another
    embedder profile, are rewritten under the same ids; windows past a
    parent's new window count are deleted. Without ``wing``, windows whose
    parent no longer exists are pruned too. Drawers are never written.

    ``dry_run`` only counts what a build would embed. Works whether or not
    ``embedding_subwindows`` is on, so a palace can be prepared before the flag
    is flipped. Raises :class:`SubwindowsUnsupported` when the active embedder
    has no reachable tokenizer.

    ``lock`` is a writer-lock factory (``mempalace windows build`` passes
    ``lambda: mine_palace_lock(palace)``). It is taken per batch, never across
    the whole build: each page of drawers is read without it, then indexed
    under it, so mines, ``sync`` and the daemon write between batches. A
    batch is idempotent, so a drawer that changed between the read and the
    write is caught by its own writer's window update or by the next build.
    """
    from .palace import get_collection, get_windows_collection, open_windows_collection
    from .search_util import build_where_filter

    drawers_col = get_collection(palace_path, create=False, read_only=True)
    resolved = active_profile()
    if resolved is None:
        raise SubwindowsUnsupported(
            "the active embedder's tokenizer or dimension is not available; "
            "sub-windows need minilm or embeddinggemma"
        )
    embedder, tokenizer, profile = resolved
    where = build_where_filter(wing, col=drawers_col) or None
    if dry_run:
        windows_col = open_windows_collection(palace_path, read_only=True)
    else:
        with _held(lock):
            windows_col = get_windows_collection(palace_path, create=True)
    index = WindowIndex(windows_col, embedder, tokenizer, profile) if windows_col else None

    from .metadata_types import is_bookkeeping

    stats = {
        "profile": profile.key,
        "drawers": 0,
        "long_drawers": 0,
        "windows_needed": 0,
        "embedded": 0,
        "skipped": 0,
        "removed": 0,
        "orphans_pruned": 0,
    }
    seen: set = set()
    for page in _page_rows(drawers_col, where=where, page_size=BATCH_SIZE):
        rows = [
            (row_id, doc, meta or {})
            for row_id, doc, meta in page
            if isinstance(doc, str) and not is_bookkeeping(meta, row_id)
        ]
        seen.update(row_id for row_id, _, _ in page)
        stats["drawers"] += len(rows)
        for _, doc, _ in rows:
            spans = plan_windows(doc, tokenizer, profile.overlap)
            if spans:
                stats["long_drawers"] += 1
                stats["windows_needed"] += len(spans)
        if not dry_run and index is not None:
            with _held(lock):
                done = index.index(rows)
            stats["embedded"] += done["embedded"]
            stats["skipped"] += done["skipped"]
            stats["removed"] += done["removed"]
        if progress is not None:
            progress(dict(stats))

    if wing is None and windows_col is not None:
        candidates = [
            (row_id, (meta or {}).get("window_of"))
            for page in _page_rows(windows_col, include=("metadatas",), page_size=BATCH_SIZE)
            for row_id, _, meta in page
            if (meta or {}).get("window_of") not in seen
        ]
        by_parent: dict = {}
        for row_id, parent in candidates:
            by_parent.setdefault(parent, []).append(row_id)
        parents = list(by_parent)
        for start in range(0, len(parents), BATCH_SIZE):
            chunk = parents[start : start + BATCH_SIZE]
            # Checked and deleted under one hold: a drawer filed (and windowed)
            # after the scan above is not an orphan.
            with contextlib.nullcontext() if dry_run else _held(lock):
                named = [parent for parent in chunk if parent]
                got = drawers_col.get(ids=named, include=[]) if named else {}
                alive = set(got.get("ids") or [])
                orphans = [
                    row_id
                    for parent in chunk
                    if parent not in alive
                    for row_id in by_parent[parent]
                ]
                stats["orphans_pruned"] += len(orphans)
                if not dry_run and orphans:
                    windows_col.delete(ids=orphans)
    stats["window_rows"] = windows_col.count() if windows_col is not None else 0
    return stats
