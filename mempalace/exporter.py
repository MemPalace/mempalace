"""
exporter.py — Export the palace as a browsable folder of markdown files.

Produces:
  output_dir/
    index.md              — table of contents
    wing_name/
      room_name.md        — one file per room, drawers as sections

Streams drawers in paginated batches so memory usage stays bounded
regardless of palace size.
"""

import errno
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
        print("  Palace is empty — nothing to export.")
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
