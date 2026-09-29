"""Pre-mining defense against drawer_id collisions.

Runs immediately before a batched chromadb upsert. Computes the union of
incoming drawer_ids and existing drawer_ids that share a key with the
batch; raises ``CollisionError`` if any drawer_id appears more than once
in that union with conflicting ``(source_file, chunk_index)`` metadata.

Under the v2 hash recipe (see :mod:`mempalace.ids`) accidental collisions
are vanishingly rare — SHA-256 truncated to 24 hex chars makes random
collision ~2^-96. The scan exists for two reasons:

1. Catch upstream bugs that emit duplicate ``(source_file, chunk_index)``
   pairs in the same batch with conflicting content. ChromaDB would
   silently let the last-write win; the scan surfaces it as an
   actionable error naming both call sites.
2. Catch the astronomical-but-possible SHA-256 hash collision with a
   clear message instead of a silent overwrite at upsert time.

The scan does NOT fire on idempotent re-mines — when an incoming drawer
matches an existing one with the SAME ``(source_file, chunk_index)``
metadata, that is normal re-write behavior, not collision.
"""

from __future__ import annotations

from collections import defaultdict


class CollisionError(Exception):
    """Raised by :func:`assert_no_collisions` when the pre-mining scan
    detects a drawer_id that would silently overwrite existing content
    or duplicate within a batch with conflicting metadata.

    The exception message names every colliding ``drawer_id`` and the
    full set of ``(source_file, chunk_index)`` pairs producing each one,
    so a user fixing one collision does not have to rediscover the next
    by re-running the mine.
    """


def _metadata_key(meta: dict) -> tuple:
    """Reduce a drawer metadata dict to the tuple used for collision
    discrimination. Two metadata dicts are 'the same chunk' iff their
    key tuples match. Falls back to ``(source_file,)`` when
    ``chunk_index`` is absent (diary entries, sentinels)."""
    source_file = meta.get("source_file")
    chunk_index = meta.get("chunk_index")
    if chunk_index is None:
        return (source_file,)
    return (source_file, chunk_index)


def assert_no_collisions(
    proposed: list[tuple[str, dict]],
    collection,
) -> None:
    """Abort the mine via ``CollisionError`` if any proposed drawer_id
    collides with itself or with an existing drawer in ``collection``.

    Args:
        proposed: list of ``(drawer_id, metadata)`` tuples for the
            chunks about to be upserted. ``metadata`` must carry at
            least ``source_file``; ``chunk_index`` is used when
            present.
        collection: a ChromaDB-shaped collection with ``get(ids=...)``
            returning a dict with ``ids`` and ``metadatas`` keys.

    Raises:
        CollisionError: when a drawer_id maps to two or more distinct
            ``(source_file, chunk_index)`` tuples in the union of
            incoming and existing rows.
    """
    if not proposed:
        return

    # Build incoming map: drawer_id -> set of metadata key tuples.
    # Using a set collapses duplicate-metadata cases (same chunk twice
    # in the batch) without flagging them as collisions.
    incoming: dict[str, set[tuple]] = defaultdict(set)
    for drawer_id, meta in proposed:
        incoming[drawer_id].add(_metadata_key(meta))

    # Query existing rows for any incoming id. ChromaDB's get(ids=...)
    # returns only the rows whose ids are present; missing ids are
    # silently absent from the result, which is what we want.
    incoming_ids = list(incoming.keys())
    result = collection.get(ids=incoming_ids, include=["metadatas"])
    existing_ids: list = result["ids"] if hasattr(result, "__getitem__") else []
    existing_metas: list = result["metadatas"] if existing_ids else []

    # Merge existing metadata into the incoming map. A real collision is
    # a drawer_id whose incoming + existing metadata key tuples are not
    # all the same.
    for drawer_id, meta in zip(existing_ids, existing_metas):
        incoming[drawer_id].add(_metadata_key(meta or {}))

    collisions = {did: keys for did, keys in incoming.items() if len(keys) > 1}
    if collisions:
        raise CollisionError(_format_collisions(collisions))


def _format_collisions(collisions: dict[str, set[tuple]]) -> str:
    """Render a CollisionError message that enumerates every colliding
    drawer_id and the metadata tuples producing it."""
    lines = [
        f"Pre-mining collision scan detected {len(collisions)} "
        f"colliding drawer_id{'s' if len(collisions) != 1 else ''}:",
    ]
    for drawer_id, keys in sorted(collisions.items()):
        lines.append(f"  {drawer_id}:")
        for key in sorted(keys, key=lambda k: tuple(str(part) for part in k)):
            if len(key) == 1:
                lines.append(f"    source_file={key[0]!r}")
            else:
                lines.append(f"    source_file={key[0]!r}, chunk_index={key[1]!r}")
    lines.append(
        "Each colliding drawer_id would cause the second ChromaDB upsert "
        "to silently overwrite the first. Fix the upstream chunker / "
        "miner to emit distinct keys, or investigate the SHA-256 hash "
        "collision."
    )
    return "\n".join(lines)
