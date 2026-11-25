"""Sub-window vectors for drawers longer than the embedder's token window.

The default MiniLM embedder reads at most 256 tokens. A drawer longer than
that is embedded from its head only, so a query that matches its tail cannot
find it through the vector path (BM25 still can). With
``embedding_subwindows`` on, MemPalace also embeds overlapping
``<= max-token`` windows of the drawer's verbatim text and stores those
vectors as **derived rows** in a sibling collection,
:data:`mempalace.palace.WINDOWS_COLLECTION_NAME`.

The drawer itself never changes. A window row holds a vector and a pointer
(``window_of`` = parent drawer id, ``char_start``/``char_end`` into the
parent's text) and an empty document: it never carries a second copy of the
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

import hashlib
import logging
from dataclasses import dataclass
from typing import Callable, Iterable, Optional

logger = logging.getLogger(__name__)

# ``kind`` stamped on every window row (rows of the windows collection only).
WINDOW_KIND = "drawer_window"
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
# Drawers read / windows written per batch.
BATCH_SIZE = 500


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
    def offsets(text: str) -> list:
        return list(tokenizer.encode(text, add_special_tokens=False).offsets)

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
    data = (text or "").encode("utf-8", "surrogatepass")
    return hashlib.sha256(data).hexdigest()[:32]


def window_id(parent_id: str, index: int) -> str:
    return f"{parent_id}::w{index:03d}"


# ``source_file`` lets a whole-source delete purge windows without the
# parent ids. Wing and room are not copied: search scopes windows through the
# parent row, so a drawer that moves keeps working windows.
_COPIED_KEYS = ("source_file",)


def window_rows(parent_id: str, document: str, metadata: dict, spans: list, profile) -> list:
    """``(id, metadata)`` for each window of one parent.

    Window rows copy the parent's ``source_file`` (see ``_COPIED_KEYS``); the
    parent row stays the authority and is re-read at query time.
    """
    sha = parent_sha(document)
    base = {key: metadata[key] for key in _COPIED_KEYS if metadata.get(key) is not None}
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


def _fresh(existing: list, parent_id: str, document: str, metadata: dict, profile, spans) -> bool:
    """Whether stored windows ``existing`` already match this parent exactly."""
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
        to_write, to_delete = [], []
        for drawer_id, document, metadata, spans in planned:
            stored = existing.get(drawer_id, [])
            if spans:
                stats["parents"] += 1
            if spans and _fresh(
                [m for _, m in stored], drawer_id, document, metadata, self.profile, spans
            ):
                stats["skipped"] += 1
                continue
            keep = {window_id(drawer_id, i) for i in range(1, len(spans) + 1)}
            to_delete.extend(row_id for row_id, _ in stored if row_id not in keep)
            for (row_id, meta), (start, end) in zip(
                window_rows(drawer_id, document, metadata, spans, self.profile), spans
            ):
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
        stats["removed"] = len(to_delete)
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
    from .palace import MINE_ABORTS

    try:
        windows.index(rows)
    except MINE_ABORTS:
        raise
    except Exception:
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
    if not palace_path or not ids:
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


def fold_windows(window_results: dict, profile_key: str) -> dict:
    """Best window per parent: ``{parent_id: (distance, parent_sha)}``.

    Rows of another profile are skipped. ``window_results`` is a one-query
    Chroma-shaped result (``ids``/``metadatas``/``distances`` nested lists).
    """
    metas = (window_results.get("metadatas") or [[]])[0] or []
    dists = (window_results.get("distances") or [[]])[0] or []
    best: dict = {}
    for meta, dist in zip(metas, dists):
        meta = meta or {}
        parent = meta.get("window_of")
        if not parent or dist is None or meta.get("window_profile") != profile_key:
            continue
        if parent not in best or dist < best[parent][0]:
            best[parent] = (dist, meta.get("parent_sha"))
    return best


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
    by distance and cut to ``limit`` so pool accounting is unchanged.
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
    for parent_id, (distance, sha) in best.items():
        parent = parents.get(parent_id)
        if parent is None or parent_sha(parent[0]) != sha:
            continue
        valid[parent_id] = to_drawer_scale(distance)
    if not valid:
        return drawer_results, 0

    promoted = 0
    rows = []
    seen = set()
    for row in zip(ids, docs, metas, dists):
        row_id, doc, meta, dist = row
        window = valid.get(row_id)
        if window is not None and (dist is None or window < dist):
            dist = window
            promoted += 1
        rows.append((row_id, doc, meta, dist))
        seen.add(row_id)
    for parent_id, window in valid.items():
        if parent_id in seen:
            continue
        doc, meta = parents[parent_id]
        rows.append((parent_id, doc, meta, window))
        promoted += 1
    rows.sort(key=lambda r: float("inf") if r[3] is None else r[3])
    rows = rows[:limit]
    merged = {
        "ids": [[r[0] for r in rows]],
        "documents": [[r[1] for r in rows]],
        "metadatas": [[r[2] for r in rows]],
        "distances": [[r[3] for r in rows]],
    }
    return merged, promoted


# ── backfill ─────────────────────────────────────────────────────────────


class SubwindowsUnsupported(RuntimeError):
    """The active embedder exposes no tokenizer MemPalace can window with."""


def _page_rows(collection, *, where=None, include=("documents", "metadatas")):
    offset = 0
    while True:
        kwargs = {"include": list(include), "limit": BATCH_SIZE, "offset": offset}
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
        if len(ids) < BATCH_SIZE:
            return


def build_windows(
    palace_path: str,
    *,
    wing: Optional[str] = None,
    dry_run: bool = False,
    progress: Optional[Callable[[dict], None]] = None,
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
    for page in _page_rows(drawers_col, where=where):
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
            done = index.index(rows)
            stats["embedded"] += done["embedded"]
            stats["skipped"] += done["skipped"]
            stats["removed"] += done["removed"]
        if progress is not None:
            progress(dict(stats))

    if wing is None and windows_col is not None:
        orphans = [
            row_id
            for page in _page_rows(windows_col, include=("metadatas",))
            for row_id, _, meta in page
            if (meta or {}).get("window_of") not in seen
        ]
        stats["orphans_pruned"] = len(orphans)
        if not dry_run:
            for start in range(0, len(orphans), BATCH_SIZE):
                windows_col.delete(ids=orphans[start : start + BATCH_SIZE])
    stats["window_rows"] = windows_col.count() if windows_col is not None else 0
    return stats
