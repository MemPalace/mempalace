"""
exporter.py — Export the palace as browsable markdown.

Supports two output shapes:

- Plain export: root index plus wing/room markdown files
- Snapshot export: timestamped directory with overview, manifest,
  root index, wing indexes, and room markdown files

Room-level drawer content remains verbatim in both modes.
"""

import errno
import json
import os
import re
from collections import defaultdict
from datetime import datetime

from .palace import get_collection
from .version import __version__


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
def _quote_content(text: str) -> str:
    """Format content for a markdown blockquote, handling multiline."""
    lines = text.rstrip("\n").split("\n")
    return "\n> ".join(lines)
    return open(path, mode, encoding=encoding)


def _iter_batches(col, where=None, batch_size: int = 1000):
    """Yield paginated drawer batches from the collection."""
    offset = 0
    while True:
        kwargs = {"limit": batch_size, "offset": offset, "include": ["documents", "metadatas"]}
        if where:
            kwargs["where"] = where
        batch = col.get(**kwargs)
        if not batch["ids"]:
            break
        yield batch
        offset += len(batch["ids"])


def _write_room_markdown(room_path: str, wing: str, room: str, drawers: list, is_new: bool):
    """Write or append drawers to a room markdown file."""
    with _safe_open_for_write(room_path, "a" if not is_new else "w") as f:
        if is_new:
            f.write(f"# {wing} / {room}\n\n")

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


def _stream_export_tree(
    palace_path: str,
    output_dir: str,
    wing: str = None,
    write_wing_indexes: bool = False,
) -> dict:
    """Write room markdown files and summary indexes into output_dir."""
    col = get_collection(palace_path)
    total = col.count()
    if total == 0:
        print("  Palace is empty — nothing to export.")
        return {"wings": 0, "rooms": 0, "drawers": 0, "wing_rows": []}

    _reject_symlink(output_dir, "output_dir")
    os.makedirs(output_dir, exist_ok=True)
    try:
        os.chmod(output_dir, 0o700)
    except (OSError, NotImplementedError):
        pass

    opened_rooms = set()
    created_wing_dirs = set()
    wing_stats = defaultdict(lambda: defaultdict(int))
    total_drawers = 0
    where = {"wing": wing} if wing else None

    print(f"  Streaming {total} drawers...")
    for batch in _iter_batches(col, where=where):
        batch_grouped = defaultdict(lambda: defaultdict(list))
        for doc_id, doc, meta in zip(batch["ids"], batch["documents"], batch["metadatas"]):
            wing_name = meta.get("wing", "unknown")
            room_name = meta.get("room", "general")
            batch_grouped[wing_name][room_name].append(
                {
                    "id": doc_id,
                    "content": doc,
                    "source": meta.get("source_file", ""),
                    "filed_at": meta.get("filed_at", ""),
                    "added_by": meta.get("added_by", ""),
                }
            )

        for wing_name, rooms in batch_grouped.items():
            safe_wing = _safe_path_component(wing_name)
            wing_dir = os.path.join(output_dir, safe_wing)
            if wing_dir not in created_wing_dirs:
                _reject_symlink(wing_dir, f"wing directory {safe_wing!r}")
                os.makedirs(wing_dir, exist_ok=True)
                try:
                    os.chmod(wing_dir, 0o700)
                except (OSError, NotImplementedError):
                    pass
                created_wing_dirs.add(wing_dir)

            for room_name, drawers in rooms.items():
                safe_room = _safe_path_component(room_name)
                room_path = os.path.join(wing_dir, f"{safe_room}.md")
                key = (wing_name, room_name)
                is_new = key not in opened_rooms

                _write_room_markdown(room_path, wing_name, room_name, drawers, is_new=is_new)

                if is_new:
                    opened_rooms.add(key)
                wing_stats[wing_name][room_name] += len(drawers)
                total_drawers += len(drawers)

    wing_rows = []
    for wing_name in sorted(wing_stats):
        rooms = wing_stats[wing_name]
        drawer_count = sum(rooms.values())
        wing_rows.append(
            {
                "name": wing_name,
                "safe_name": _safe_path_component(wing_name),
                "rooms": len(rooms),
                "drawers": drawer_count,
                "room_rows": [
                    {
                        "name": room_name,
                        "safe_name": _safe_path_component(room_name),
                        "drawers": count,
                    }
                    for room_name, count in sorted(rooms.items())
                ],
            }
        )
        print(f"  {wing_name}: {len(rooms)} rooms, {drawer_count} drawers")

    if write_wing_indexes:
        _write_wing_indexes(output_dir, wing_rows)

    return {
        "wings": len(wing_rows),
        "rooms": sum(row["rooms"] for row in wing_rows),
        "drawers": total_drawers,
        "wing_rows": wing_rows,
    }


def _write_root_index(output_dir: str, wing_rows: list, title: str):
    """Write the root index file for an export tree."""
    index_lines = [
        title,
        "",
        "| Wing | Rooms | Drawers |",
        "|------|-------|---------|",
    ]
    for row in wing_rows:
        index_lines.append(f"| [{row['name']}]({row['safe_name']}/) | {row['rooms']} | {row['drawers']} |")
    index_lines.append("")

    index_path = os.path.join(output_dir, "index.md")
    with _safe_open_for_write(index_path, "w") as f:
        f.write("\n".join(index_lines))


def _write_wing_indexes(output_dir: str, wing_rows: list):
    """Write a navigation index inside each wing directory."""
    for row in wing_rows:
        wing_dir = os.path.join(output_dir, row["safe_name"])
        index_lines = [
            f"# Wing Export — {row['name']}",
            "",
            "| Room | Drawers |",
            "|------|---------|",
        ]
        for room_row in row["room_rows"]:
            index_lines.append(
                f"| [{room_row['name']}]({room_row['safe_name']}.md) | {room_row['drawers']} |"
            )
        index_lines.append("")

        with open(os.path.join(wing_dir, "index.md"), "w", encoding="utf-8") as f:
            f.write("\n".join(index_lines))


def _make_snapshot_path(output_dir: str, snapshot_name: str = None) -> tuple[str, str]:
    """Build the final snapshot directory path."""
    base_dir = os.path.abspath(output_dir)
    os.makedirs(base_dir, exist_ok=True)

    if snapshot_name:
        final_path = os.path.join(base_dir, snapshot_name)
        if os.path.exists(final_path):
            raise FileExistsError(f"Snapshot already exists: {final_path}")
        return final_path, snapshot_name

    generated = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    final_path = os.path.join(base_dir, generated)
    suffix = 2
    while os.path.exists(final_path):
        final_path = os.path.join(base_dir, f"{generated}-{suffix}")
        suffix += 1
    return final_path, os.path.basename(final_path)


def _build_manifest(
    palace_path: str,
    snapshot_name: str,
    wing: str,
    stats: dict,
) -> dict:
    """Build machine-readable snapshot metadata."""
    return {
        "snapshot_name": snapshot_name,
        "exported_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "mempalace_version": __version__,
        "palace_path": os.path.abspath(palace_path),
        "format": "markdown_snapshot",
        "filters": {"wing": wing},
        "stats": {
            "wings": stats["wings"],
            "rooms": stats["rooms"],
            "drawers": stats["drawers"],
        },
        "wings": [
            {"name": row["name"], "rooms": row["rooms"], "drawers": row["drawers"]}
            for row in stats["wing_rows"]
        ],
    }


def _write_overview(snapshot_path: str, manifest: dict):
    """Write the snapshot's human-readable overview page."""
    lines = [
        "# Palace Snapshot",
        "",
        f"- Snapshot: `{manifest['snapshot_name']}`",
        f"- Exported at: `{manifest['exported_at']}`",
        f"- Palace path: `{manifest['palace_path']}`",
        f"- Wing filter: `{manifest['filters']['wing'] or 'all'}`",
        "",
        "## Totals",
        "",
        f"- Wings: {manifest['stats']['wings']}",
        f"- Rooms: {manifest['stats']['rooms']}",
        f"- Drawers: {manifest['stats']['drawers']}",
        "",
        "## Wings",
        "",
        "| Wing | Rooms | Drawers |",
        "|------|-------|---------|",
    ]
    for row in manifest["wings"]:
        safe_wing = _safe_path_component(row["name"])
        lines.append(f"| [{row['name']}]({safe_wing}/index.md) | {row['rooms']} | {row['drawers']} |")
    lines.append("")

    with open(os.path.join(snapshot_path, "overview.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def _write_manifest(snapshot_path: str, manifest: dict):
    """Write the snapshot manifest."""
    with open(os.path.join(snapshot_path, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")


def export_palace(palace_path: str, output_dir: str, format: str = "markdown") -> dict:
    """Export all palace drawers as markdown files organized by wing/room."""
    stats = _stream_export_tree(palace_path=palace_path, output_dir=output_dir)
    if stats["drawers"] == 0:
        return {"wings": 0, "rooms": 0, "drawers": 0}

    today = datetime.now().strftime("%Y-%m-%d")
    _write_root_index(output_dir, stats["wing_rows"], f"# Palace Export — {today}\n")

    result = {
        "wings": stats["wings"],
        "rooms": stats["rooms"],
        "drawers": stats["drawers"],
    }
    print(
        f"\n  Exported {result['drawers']} drawers across {result['wings']} wings, {result['rooms']} rooms"
    )
    print(f"  Output: {output_dir}")
    return result


def export_snapshot(
    palace_path: str,
    output_dir: str,
    snapshot_name: str = None,
    wing: str = None,
) -> dict:
    """Export a timestamped snapshot with overview and manifest files."""
    snapshot_path, final_name = _make_snapshot_path(output_dir, snapshot_name=snapshot_name)
    stats = _stream_export_tree(
        palace_path=palace_path,
        output_dir=snapshot_path,
        wing=wing,
        write_wing_indexes=True,
    )
    result = {
        "wings": stats["wings"],
        "rooms": stats["rooms"],
        "drawers": stats["drawers"],
        "snapshot_path": snapshot_path,
    }
    if result["drawers"] == 0:
        return result

    _write_root_index(snapshot_path, stats["wing_rows"], "# Snapshot Index\n")
    manifest = _build_manifest(palace_path, final_name, wing, stats)
    _write_overview(snapshot_path, manifest)
    _write_manifest(snapshot_path, manifest)

    print(
        f"\n  Exported {result['drawers']} drawers across {result['wings']} wings, {result['rooms']} rooms"
    )
    print(f"  Snapshot: {snapshot_path}")
    return result
