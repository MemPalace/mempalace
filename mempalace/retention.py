"""Which drawers are preserved memory and which index a project file.

Conversations, diaries and manual entries are memory the palace is the only
copy of: a re-mine, a sync or another miner must never be the reason their
text is gone. A file in a mined project folder is the authority for its own
text; its drawers index the current version and may be replaced or removed.

No drawer records its class, so it is read from the metadata each producer
already writes. A row no rule recognises is ``None``: a miner's prune treats
it as someone else's and keeps it. ``sync`` still removes such a row when its
source is gone or ignored, as it always has; it keeps only rows recognised as
preserved.
"""

from __future__ import annotations

from typing import Optional

PRESERVE = "preserve"
INDEX_CURRENT = "index_current"

# Producers whose rows are memory the palace must keep.
PRESERVED_PRODUCERS = frozenset({"convo", "sweep", "diary", "diary_write", "manual"})
# Producers whose rows index a file that is its own authority.
INDEX_PRODUCERS = frozenset({"project", "format"})

# The producers whose rows each writer of ``write_file_drawers`` may replace
# or remove: its own text rows and its own bookkeeping rows.
_OWNED = {
    "project": frozenset({"project"}),
    "format": frozenset({"format"}),
    "convo": frozenset({"convo", "convo_registry"}),
    "diary": frozenset({"diary"}),
}
OWNERS = frozenset(_OWNED)
PRESERVING_OWNERS = frozenset({"convo", "diary"})


def drawer_producer(meta: Optional[dict]) -> Optional[str]:
    """Name the producer that filed a drawer, from its metadata.

    ``ingest_mode`` names the convo miner (``convos``, and ``registry`` for
    its bookkeeping rows), the sweeper (``sweep``) and the format miner
    (``extract``). Without one, ``source_session="daily_diary"`` is diary
    ingest, ``type="diary_entry"`` is ``diary_write``, and ``adapter_name`` is
    a source adapter. The project miner stamps ``normalize_version`` and its
    writer stamps ``chunk_total``; ``add_drawer`` stamps neither but records
    ``id_recipe``. A project drawer from before ``normalize_version`` still
    carries the ``source_mtime`` only the project miner wrote without an
    ``ingest_mode``. A row with none of these is ``None``: it cannot be told
    apart.
    """
    meta = meta or {}
    ingest_mode = meta.get("ingest_mode")
    if ingest_mode == "convos":
        return "convo"
    if ingest_mode == "registry":
        return "convo_registry"
    if ingest_mode == "sweep":
        return "sweep"
    if ingest_mode == "extract":
        return "format"
    if ingest_mode is not None:
        return "other"
    if meta.get("source_session") == "daily_diary":
        return "diary"
    if meta.get("type") == "diary_entry":
        return "diary_write"
    if meta.get("adapter_name"):
        return "adapter"
    # Chroma can leave a key present with value None after a merging upsert,
    # so presence alone proves nothing.
    if meta.get("normalize_version") is not None or meta.get("chunk_total") is not None:
        return "project"
    if meta.get("id_recipe") is not None:
        return "manual"
    if meta.get("source_mtime") is not None:
        return "project"
    return None


def retention_class(meta: Optional[dict]) -> Optional[str]:
    """``PRESERVE``, ``INDEX_CURRENT``, or ``None`` when the producer is unknown."""
    producer = drawer_producer(meta)
    if producer in PRESERVED_PRODUCERS:
        return PRESERVE
    if producer in INDEX_PRODUCERS:
        return INDEX_CURRENT
    return None


def is_preserved(meta: Optional[dict]) -> bool:
    """Whether a drawer is memory the palace must keep (a known preserving producer)."""
    return retention_class(meta) == PRESERVE


def owns(owner: str, meta: Optional[dict]) -> bool:
    """Whether ``owner``'s re-file may replace or remove this drawer."""
    return drawer_producer(meta) in _OWNED[owner]
