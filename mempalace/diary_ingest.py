"""
diary_ingest.py — Ingest daily summary files into the palace.

Architecture:
- ONE drawer per (wing, day) — full verbatim content, upserted as the day grows.
- Closets pack topics up to CLOSET_CHAR_LIMIT, never split mid-topic.
- A re-ingest fully purges the prior day's closets before rebuilding so a
  shorter day never leaves orphans behind.
- Only new entries are processed by default (tracks entry count in a state
  file under ``~/.mempalace/state/`` — never inside the user's diary dir).
- Per-file ``mine_lock`` so concurrent ingest from two terminals can't race.
- Entities extracted and stamped on metadata for filterable search.

Usage:
    python -m mempalace.diary_ingest --dir ~/daily_summaries --palace ~/.mempalace/palace
    python -m mempalace.diary_ingest --dir ~/daily_summaries --palace ~/.mempalace/palace --force
"""

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from .miner import _extract_entities_for_metadata
from .palace import (
    build_closet_lines,
    get_closets_collection,
    get_collection,
    mine_lock,
    purge_file_closets,
    upsert_closet_lines,
)

DIARY_ENTRY_RE = re.compile(r"^## .+", re.MULTILINE)


def _state_file_for(palace_path: str, diary_dir: Path) -> Path:
    """Return the per-(palace, diary-dir) state-file path under ~/.mempalace/state.

    Keyed by sha256 of (palace_path, diary_dir) so multiple diary folders
    pointing at the same palace each get an independent state file. The
    state file is *never* written inside the user's diary directory.
    """
    state_root = Path(os.path.expanduser("~")) / ".mempalace" / "state"
    state_root.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(f"{palace_path}|{diary_dir}".encode()).hexdigest()[:24]
    return state_root / f"diary_ingest_{key}.json"


def _split_entries(text):
    """Split diary text into (header, body) pairs per ## entry."""
    parts = DIARY_ENTRY_RE.split(text)
    headers = DIARY_ENTRY_RE.findall(text)
    entries = []
    for i, header in enumerate(headers):
        body = parts[i + 1] if i + 1 < len(parts) else ""
        entries.append((header.strip(), body.strip()))
    return entries


def _diary_drawer_id(wing: str, date_str: str) -> str:
    """Stable, wing-scoped drawer ID. Two diaries (e.g. 'work' vs 'personal')
    sharing the same date never collide."""
    suffix = hashlib.sha256(f"{wing}|{date_str}".encode()).hexdigest()[:24]
    return f"drawer_diary_{suffix}"


def _diary_closet_id_base(wing: str, date_str: str) -> str:
    suffix = hashlib.sha256(f"{wing}|{date_str}".encode()).hexdigest()[:24]
    return f"closet_diary_{suffix}"


def ingest_diaries(
    diary_dir,
    palace_path,
    wing="diary",
    force=False,
):
    """Ingest daily summary files into the palace.

    Each date file gets ONE drawer keyed by ``(wing, date)`` and closets that
    pack topics atomically up to ``CLOSET_CHAR_LIMIT``. ``force=True`` rebuilds
    every entry's closets from scratch (purging stale ones); the default
    incremental mode only processes entries appended since the last run.
    """
    diary_dir = Path(diary_dir).expanduser().resolve()
    if not diary_dir.exists():
        print(f"Diary directory not found: {diary_dir}")
        return {"days_updated": 0, "closets_created": 0}

    diary_files = sorted(diary_dir.glob("*.md"))
    if not diary_files:
        print(f"No .md files in {diary_dir}")
        return {"days_updated": 0, "closets_created": 0}

    state_file = _state_file_for(str(palace_path), diary_dir)
    if force or not state_file.exists():
        state: dict = {}
    else:
        try:
            state = json.loads(state_file.read_text())
        except Exception:
            state = {}

    drawers_col = get_collection(palace_path)
    closets_col = get_closets_collection(palace_path)

    days_updated = 0
    closets_created = 0

    for diary_path in diary_files:
        text = diary_path.read_text(encoding="utf-8", errors="replace")
        if len(text.strip()) < 50:
            continue

        date_match = re.match(r"(\d{4}-\d{2}-\d{2})", diary_path.stem)
        if not date_match:
            continue
        date_str = date_match.group(1)

        # Skip if content hasn't changed. Hash-based — size alone false-negatives
        # on same-length edits (e.g. "teh" → "the"), silently dropping real edits.
        state_key = f"{wing}|{diary_path.name}"
        prev_entry = state.get(state_key, {})
        prev_hash = prev_entry.get("content_hash")
        prev_size = prev_entry.get("size", 0)
        curr_size = len(text)
        curr_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if not force:
            if prev_hash is not None:
                if curr_hash == prev_hash:
                    continue
            elif curr_size == prev_size and prev_size > 0:
                # Legacy state without content_hash: keep size-based skip but
                # backfill the hash so future runs use the strict check.
                state[state_key] = {**prev_entry, "content_hash": curr_hash}
                continue

        # An in-place edit (same entry count, different content) means existing
        # closets are stale. Force a full rebuild whenever the hash changes,
        # not only on entry-count growth.
        content_changed = prev_hash is not None and curr_hash != prev_hash

        now_iso = datetime.now(timezone.utc).isoformat()
        drawer_id = _diary_drawer_id(wing, date_str)
        entities = _extract_entities_for_metadata(text)
        source_file = str(diary_path)

        # Serialize per source — two terminals running ingest at once must
        # not interleave the upsert + closet-rebuild.
        with mine_lock(source_file):
            drawer_meta = {
                "date": date_str,
                "wing": wing,
                "room": "daily",
                "source_file": source_file,
                "source_session": "daily_diary",
                "filed_at": now_iso,
            }
            if entities:
                drawer_meta["entities"] = entities
            drawers_col.upsert(
                documents=[text],
                ids=[drawer_id],
                metadatas=[drawer_meta],
            )

            entries = _split_entries(text)
            prev_entry_count = state.get(state_key, {}).get("entry_count", 0)
            full_rebuild = force or content_changed
            new_entries = entries if full_rebuild else entries[prev_entry_count:]

            if new_entries:
                all_lines = []
                for header, body in new_entries:
                    entry_text = f"{header}\n{body}"
                    entry_lines = build_closet_lines(
                        source_file, [drawer_id], entry_text, wing, "daily"
                    )
                    all_lines.extend(entry_lines)

                if all_lines:
                    closet_id_base = _diary_closet_id_base(wing, date_str)
                    closet_meta = {
                        "date": date_str,
                        "wing": wing,
                        "room": "daily",
                        "source_file": source_file,
                        "filed_at": now_iso,
                    }
                    if entities:
                        closet_meta["entities"] = entities
                    # On any full rebuild (force or detected content edit),
                    # wipe leftover closets from a prior run before re-writing.
                    if full_rebuild:
                        purge_file_closets(closets_col, source_file)
                    n = upsert_closet_lines(closets_col, closet_id_base, all_lines, closet_meta)
                    closets_created += n

            state[state_key] = {
                "size": curr_size,
                "content_hash": curr_hash,
                "entry_count": len(entries),
                "ingested_at": now_iso,
            }
        days_updated += 1

    state_file.write_text(json.dumps(state, indent=2))
    if days_updated:
        print(f"Diary: {days_updated} days updated, {closets_created} new closets")

    return {"days_updated": days_updated, "closets_created": closets_created}


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Ingest daily summaries into the palace")
    parser.add_argument("--dir", required=True, help="Path to daily_summaries directory")
    parser.add_argument("--palace", default=os.path.expanduser("~/.mempalace/palace"))
    parser.add_argument("--wing", default="diary")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    ingest_diaries(args.dir, args.palace, wing=args.wing, force=args.force)
