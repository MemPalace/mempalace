"""
exporter.py — Export the palace as a browsable folder of markdown files.

Produces:
  output_dir/
    index.md              — table of contents
    wing_name/
      room_name.md        — one file per room, drawers as sections

The markdown export streams drawers in paginated batches so memory usage
stays bounded regardless of palace size; the JSONL export buffers the
grouped drawers so files can be written fully sorted, so its memory use is
proportional to the total exported text.
"""

import errno
import json
import os
import re
from collections import defaultdict
from datetime import datetime

from .palace import get_collection


def _safe_path_component(name: str) -> str:
    """Sanitize a string for use as a directory/file name component."""
    name = re.sub(r'[/\\:*?"<>|]', "_", name)
    name = name.strip(". ")
    return name or "unknown"


def _reject_symlink(path: str, label: str) -> None:
    """Refuse to write into a path that is itself a symlink.

    Defense-in-depth: a pre-placed symlink at the export target would
    redirect writes to wherever it points (e.g., system directories).
    Mirrors the miner's input-side caution.
    """
    if os.path.islink(path):
        raise ValueError(
            f"refusing to export: {label} is a symbolic link ({path!r}). "
            f"Remove the symlink or choose a different output path."
        )


def _safe_open_for_write(path: str, mode: str, encoding: str = "utf-8"):
    """Open a file for writing, refusing to follow a symlink at the target path.

    On POSIX (O_NOFOLLOW available) the open itself fails with ELOOP if path is
    a symlink — closing the TOCTOU window between an islink check and the open.
    On platforms without O_NOFOLLOW (Windows), pre-checks ``os.path.islink``,
    which is narrower than no check at all.
    """
    o_nofollow = getattr(os, "O_NOFOLLOW", 0)
    if o_nofollow:
        flags = os.O_WRONLY | os.O_CREAT | o_nofollow
        flags |= os.O_APPEND if "a" in mode else os.O_TRUNC
        try:
            fd = os.open(path, flags, 0o600)
        except OSError as e:
            if e.errno == errno.ELOOP:
                raise ValueError(f"refusing to write: {path!r} is a symbolic link.") from None
            raise
        return os.fdopen(fd, mode, encoding=encoding)
    if os.path.islink(path):
        raise ValueError(f"refusing to write: {path!r} is a symbolic link.")
    return open(path, mode, encoding=encoding)


def export_palace(palace_path: str, output_dir: str, format: str = "markdown") -> dict:
    """Export all palace drawers as markdown files organized by wing/room.

    Streams drawers in batches of 1000 and writes each wing/room file
    incrementally, keeping memory usage proportional to batch size rather
    than total palace size.

    Args:
        palace_path: Path to the ChromaDB palace directory.
        output_dir: Where to write the exported markdown tree.
        format: Output format (currently only "markdown").

    Returns:
        Stats dict: {"wings": N, "rooms": N, "drawers": N}
    """
    col = get_collection(palace_path)
    total = col.count()

    if total == 0:
        print("  Palace is empty -- nothing to export.")
        return {"wings": 0, "rooms": 0, "drawers": 0}

    _reject_symlink(output_dir, "output_dir")
    os.makedirs(output_dir, exist_ok=True)
    try:
        os.chmod(output_dir, 0o700)
    except (OSError, NotImplementedError):
        pass

    # Track which room files have been opened (so we can append vs overwrite)
    opened_rooms: set[tuple[str, str]] = set()
    # Track which wing directories have been created and chmoded
    created_wing_dirs: set[str] = set()
    # Track stats per wing: {wing: {room: count}}
    wing_stats: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    total_drawers = 0

    print(f"  Streaming {total} drawers...")
    offset = 0
    while offset < total:
        batch = col.get(limit=1000, offset=offset, include=["documents", "metadatas"])
        if not batch["ids"]:
            break

        # Group this batch by wing/room so we do one file write per room per batch
        batch_grouped: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
        for doc_id, doc, meta in zip(batch["ids"], batch["documents"], batch["metadatas"]):
            wing = meta.get("wing", "unknown")
            room = meta.get("room", "general")
            batch_grouped[wing][room].append(
                {
                    "id": doc_id,
                    "content": doc,
                    "source": meta.get("source_file", ""),
                    "filed_at": meta.get("filed_at", ""),
                    "added_by": meta.get("added_by", ""),
                }
            )

        # Write/append each room file
        for wing, rooms in batch_grouped.items():
            safe_wing = _safe_path_component(wing)
            wing_dir = os.path.join(output_dir, safe_wing)
            if wing_dir not in created_wing_dirs:
                _reject_symlink(wing_dir, f"wing directory {safe_wing!r}")
                os.makedirs(wing_dir, exist_ok=True)
                try:
                    os.chmod(wing_dir, 0o700)
                except (OSError, NotImplementedError):
                    pass
                created_wing_dirs.add(wing_dir)

            for room, drawers in rooms.items():
                safe_room = _safe_path_component(room)
                room_path = os.path.join(wing_dir, f"{safe_room}.md")
                key = (wing, room)
                is_new = key not in opened_rooms

                with _safe_open_for_write(room_path, "a" if not is_new else "w") as f:
                    if is_new:
                        f.write(f"# {wing} / {room}\n\n")
                        opened_rooms.add(key)

                    for drawer in drawers:
                        source = drawer["source"] or "unknown"
                        filed = drawer["filed_at"] or "unknown"
                        added_by = drawer["added_by"] or "unknown"

                        f.write(
                            f"## {drawer['id']}\n"
                            f"\n"
                            f"> {_quote_content(drawer['content'])}\n"
                            f"\n"
                            f"| Field | Value |\n"
                            f"|-------|-------|\n"
                            f"| Source | {source} |\n"
                            f"| Filed | {filed} |\n"
                            f"| Added by | {added_by} |\n"
                            f"\n"
                            f"---\n\n"
                        )

                    wing_stats[wing][room] += len(drawers)
                    total_drawers += len(drawers)

        offset += len(batch["ids"])

    # Build and print stats
    index_rows = []
    for wing in sorted(wing_stats):
        rooms = wing_stats[wing]
        wing_drawer_count = sum(rooms.values())
        index_rows.append((wing, len(rooms), wing_drawer_count))
        print(f"  {wing}: {len(rooms)} rooms, {wing_drawer_count} drawers")

    # Write index.md
    today = datetime.now().strftime("%Y-%m-%d")
    index_lines = [
        f"# Palace Export — {today}\n",
        "",
        "| Wing | Rooms | Drawers |",
        "|------|-------|---------|",
    ]
    for wing, room_count, drawer_count in index_rows:
        index_lines.append(f"| [{wing}]({wing}/) | {room_count} | {drawer_count} |")
    index_lines.append("")

    index_path = os.path.join(output_dir, "index.md")
    with _safe_open_for_write(index_path, "w") as f:
        f.write("\n".join(index_lines))

    stats = {
        "wings": len(wing_stats),
        "rooms": sum(r for _, r, _ in index_rows),
        "drawers": total_drawers,
    }
    print(
        f"\n  Exported {stats['drawers']} drawers across {stats['wings']} wings, {stats['rooms']} rooms"
    )
    print(f"  Output: {output_dir}")
    return stats


def _quote_content(text: str) -> str:
    """Format content for a markdown blockquote, handling multiline."""
    lines = text.rstrip("\n").split("\n")
    return "\n> ".join(lines)


def _prune_stale_exports(output_dir: str, written: set) -> int:
    """Remove ``*.jsonl`` files a PREVIOUS export left behind. Returns the count.

    Re-exporting rewrites the rooms that still exist and used to leave the rest
    in place. That is not only a cosmetic git-diff wrinkle: import walks every
    ``*.jsonl`` under the tree without consulting the manifest, so a room whose
    last drawer was deleted would be re-imported on the next device and the
    drawer would come back.

    Two safety properties, both deliberate:

    * The caller passes ``had_manifest`` from BEFORE the manifest is rewritten,
      so this only ever deletes inside a directory we can prove was already one
      of our own exports. Pointing ``export`` at an arbitrary directory removes
      nothing on the first run.
    * Only regular files are unlinked, and symlinks are skipped rather than
      followed — the same posture ``_reject_symlink`` applies on the write side.
    """
    removed = 0
    for root, _dirs, files in os.walk(output_dir):
        for name in files:
            if not name.endswith(".jsonl"):
                continue
            path = os.path.join(root, name)
            if os.path.abspath(path) in written:
                continue
            if os.path.islink(path) or not os.path.isfile(path):
                continue
            try:
                os.unlink(path)
                removed += 1
            except OSError:
                pass
    # Drop wing directories the prune emptied; never the output root itself.
    for root, _dirs, _files in os.walk(output_dir, topdown=False):
        if os.path.abspath(root) == os.path.abspath(output_dir):
            continue
        try:
            if not os.listdir(root):
                os.rmdir(root)
        except OSError:
            pass
    return removed


def export_palace_jsonl(palace_path: str, output_dir: str) -> dict:
    """Export all palace drawers as JSONL files organized by wing/room.

    Produces a git-friendly tree suitable for cross-device sync (#452)::

        output_dir/
          export-manifest.json      — format version + counts
          wing_name/
            room_name.jsonl         — one drawer per line

    Each line is a JSON object with the drawer's ``id``, ``document``, and
    ``metadata`` — the exact triple needed to re-file it on another machine.
    Embeddings are deliberately not included: they are large, binary, and tied
    to the embedding model; import re-embeds instead.

    Output is deterministic (keys sorted, drawers sorted by id within each
    room, no timestamps), so re-exporting an unchanged palace produces a
    byte-identical tree and therefore an empty git diff.

    Re-exporting into an existing export also PRUNES room files that no longer
    correspond to a room in the palace, so a deleted drawer does not survive in
    a stale file and get re-imported on another device. Pruning is gated on a
    previous ``export-manifest.json`` being present, so exporting into an
    unrelated directory never deletes anything, and it is skipped when the
    palace reports zero drawers — an empty count is also what a palace that
    failed to open looks like, and that path warns instead.

    Streams drawers in paginated batches like :func:`export_palace`, but
    buffers the grouped drawers in memory so each file can be written fully
    sorted; memory is proportional to the total exported text. Wing/room
    names that sanitize to the same path component are merged into one file
    (drawer ids stay unique, so nothing is lost).

    Returns:
        Stats dict: {"wings": N, "rooms": N, "drawers": N}
    """
    # A pure read: ask the backend not to run schema init, migrations or
    # metadata writes. (`create` is left at its default — flipping it changes
    # what exporting a not-yet-existing palace does, which is a separate call.)
    col = get_collection(palace_path, read_only=True)
    total = col.count()

    manifest_path = os.path.join(output_dir, "export-manifest.json")
    had_manifest = os.path.isfile(manifest_path)

    if total == 0:
        print("  Palace is empty — nothing to export.")
        if had_manifest:
            # Deliberately NOT pruned. An empty count is also what a palace that
            # failed to open looks like, and silently deleting a good export on
            # that reading is far worse than leaving a stale one in place.
            print(
                f"  WARNING: {output_dir} still holds a previous export. It was left "
                f"untouched because this palace reports zero drawers — delete it by "
                f"hand if the palace really is empty, or it will re-import elsewhere."
            )
        return {"wings": 0, "rooms": 0, "drawers": 0}

    _reject_symlink(output_dir, "output_dir")
    os.makedirs(output_dir, exist_ok=True)
    try:
        os.chmod(output_dir, 0o700)
    except (OSError, NotImplementedError):
        pass

    # {wing: {room: {id: line_dict}}} — buffered so each file writes sorted.
    grouped: dict[str, dict[str, dict[str, dict]]] = defaultdict(lambda: defaultdict(dict))

    print(f"  Streaming {total} drawers...")
    offset = 0
    while offset < total:
        batch = col.get(limit=1000, offset=offset, include=["documents", "metadatas"])
        if not batch["ids"]:
            break
        for doc_id, doc, meta in zip(batch["ids"], batch["documents"], batch["metadatas"]):
            meta = meta or {}
            wing = _safe_path_component(meta.get("wing", "unknown"))
            room = _safe_path_component(meta.get("room", "general"))
            grouped[wing][room][doc_id] = {
                "id": doc_id,
                "document": doc,
                "metadata": meta,
            }
        offset += len(batch["ids"])

    total_drawers = 0
    room_count = 0
    written: set = set()
    for wing in sorted(grouped):
        wing_dir = os.path.join(output_dir, wing)
        _reject_symlink(wing_dir, f"wing directory {wing!r}")
        os.makedirs(wing_dir, exist_ok=True)
        try:
            os.chmod(wing_dir, 0o700)
        except (OSError, NotImplementedError):
            pass

        for room in sorted(grouped[wing]):
            drawers = grouped[wing][room]
            room_path = os.path.join(wing_dir, f"{room}.jsonl")
            with _safe_open_for_write(room_path, "w") as f:
                for doc_id in sorted(drawers):
                    f.write(json.dumps(drawers[doc_id], ensure_ascii=False, sort_keys=True))
                    f.write("\n")
            written.add(os.path.abspath(room_path))
            room_count += 1
            total_drawers += len(drawers)
        print(
            f"  {wing}: {len(grouped[wing])} rooms, {sum(len(r) for r in grouped[wing].values())} drawers"
        )

    if had_manifest:
        pruned = _prune_stale_exports(output_dir, written)
        if pruned:
            print(f"  Pruned {pruned} stale room file(s) from the previous export")

    manifest = {
        "format_version": 1,
        "wings": len(grouped),
        "rooms": room_count,
        "drawers": total_drawers,
    }
    with _safe_open_for_write(manifest_path, "w") as f:
        f.write(json.dumps(manifest, indent=2, sort_keys=True))
        f.write("\n")

    stats = {"wings": len(grouped), "rooms": room_count, "drawers": total_drawers}
    print(
        f"\n  Exported {stats['drawers']} drawers across {stats['wings']} wings, {stats['rooms']} rooms"
    )
    print(f"  Output: {output_dir}")
    return stats
