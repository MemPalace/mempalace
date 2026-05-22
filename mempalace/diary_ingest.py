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
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from .config import MempalaceConfig
from .miner import _extract_entities_for_metadata
from .palace import (
    build_closet_lines,
    get_closets_collection,
    get_collection,
    mine_lock,
    purge_file_closets,
    upsert_closet_lines,
)

logger = logging.getLogger(__name__)

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
    """Stable, wing-scoped legacy drawer ID (file-level).

    Retained for backwards-compatible cleanup of palaces that ingested
    diaries before #1539 — those palaces hold one ``drawer_diary_{...}``
    per file. New drawers use ``_diary_drawer_id_entry`` so each ``##``
    entry becomes its own drawer (with per-entry character chunking
    when an entry exceeds ``chunk_size``).
    """
    suffix = hashlib.sha256(f"{wing}|{date_str}".encode()).hexdigest()[:24]
    return f"drawer_diary_{suffix}"


def _diary_drawer_id_entry(wing: str, date_str: str, entry_idx: int, entry_chunk_idx: int) -> str:
    """Per-entry, per-chunk drawer ID introduced in #1539.

    The ``v2_`` prefix distinguishes new IDs from the legacy file-level
    scheme (one drawer per file). Legacy drawers from pre-#1539 palaces
    are auto-purged on any ``ingest_diaries`` call that triggers a full
    rebuild (``force=True`` or detected content change). The per-source
    ``delete(where=...)`` step on full rebuild collects both legacy and
    stale v2 drawers via the ``source_file`` metadata key, so the
    schema migration runs as a side effect of normal use.
    """
    suffix = hashlib.sha256(
        f"{wing}|{date_str}|{entry_idx}|{entry_chunk_idx}".encode()
    ).hexdigest()[:24]
    return f"drawer_diary_v2_{suffix}"


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
    chunk_size = MempalaceConfig().chunk_size

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
        entities = _extract_entities_for_metadata(text)
        source_file = str(diary_path)

        # Serialize per source — two terminals running ingest at once must
        # not interleave the upsert + closet-rebuild.
        with mine_lock(source_file):
            entries = _split_entries(text)
            prev_entry_count = state.get(state_key, {}).get("entry_count", 0)
            full_rebuild = force or content_changed

            base_meta = {
                "date": date_str,
                "wing": wing,
                "room": "daily",
                "source_file": source_file,
                "source_session": "daily_diary",
                "filed_at": now_iso,
            }
            if entities:
                base_meta["entities"] = entities

            # On full rebuild, purge ALL prior drawers for this source_file
            # before writing fresh entries. This covers three cases in one
            # step: (a) legacy pre-#1539 file-level drawers under the old
            # ``drawer_diary_`` prefix, (b) v2 drawers from a prior pass
            # where the file had MORE entries than the current run (entry
            # deletion would otherwise leave trailing orphan drawers), and
            # (c) in-place edits that shift content across entry boundaries.
            if full_rebuild:
                try:
                    drawers_col.delete(where={"source_file": source_file})
                except Exception as exc:
                    # ChromaDB ``delete(where=...)`` against an empty collection
                    # returns silently rather than raising, so this catch only
                    # fires on real backend errors (locked DB, schema mismatch,
                    # transient I/O). Log at debug to preserve diagnostics
                    # without aborting the rebuild: the upsert below is the
                    # load-bearing write, and if it also fails the state file
                    # is left unchanged so the next pass retries cleanly.
                    logger.debug(
                        "legacy purge skipped for %s: %s",
                        source_file,
                        exc,
                        exc_info=True,
                    )

            # Per-entry drawers, with character chunking inside any entry
            # whose serialized text exceeds chunk_size. ``chunk_index`` is
            # a global counter across the file so the searcher's
            # ``_expand_with_neighbors`` (which queries by source_file +
            # chunk_index) can stitch sibling chunks back together
            # regardless of entry boundary. ``entry_index`` and
            # ``entry_chunk_index`` are preserved for entry-grouping
            # consumers.
            #
            # Accumulate all drawers for the file into one batched upsert
            # so the embedding pass either commits every chunk or none.
            # A mid-loop embedding failure would otherwise leave a
            # half-written day's drawers behind, which the next
            # incremental pass would skip (state file already updated)
            # and the searcher would silently return partial results.
            global_chunk_index = 0
            batch_ids: list[str] = []
            batch_docs: list[str] = []
            batch_metas: list[dict] = []
            for entry_idx, (header, body) in enumerate(entries):
                entry_text = f"{header}\n{body}" if body else header
                if len(entry_text) <= chunk_size:
                    batch_ids.append(_diary_drawer_id_entry(wing, date_str, entry_idx, 0))
                    batch_docs.append(entry_text)
                    batch_metas.append(
                        {
                            **base_meta,
                            "chunk_index": global_chunk_index,
                            "entry_index": entry_idx,
                            "entry_chunk_index": 0,
                            "entry_header_preview": header[:120],
                        }
                    )
                    global_chunk_index += 1
                else:
                    for entry_chunk_idx, start in enumerate(range(0, len(entry_text), chunk_size)):
                        batch_ids.append(
                            _diary_drawer_id_entry(wing, date_str, entry_idx, entry_chunk_idx)
                        )
                        batch_docs.append(entry_text[start : start + chunk_size])
                        batch_metas.append(
                            {
                                **base_meta,
                                "chunk_index": global_chunk_index,
                                "entry_index": entry_idx,
                                "entry_chunk_index": entry_chunk_idx,
                                "entry_header_preview": header[:120],
                            }
                        )
                        global_chunk_index += 1

            if batch_ids:
                drawers_col.upsert(
                    ids=batch_ids,
                    documents=batch_docs,
                    metadatas=batch_metas,
                )

            new_entries = entries if full_rebuild else entries[prev_entry_count:]
            if new_entries:
                all_lines = []
                for offset, (header, body) in enumerate(new_entries):
                    entry_idx = offset if full_rebuild else prev_entry_count + offset
                    entry_text = f"{header}\n{body}" if body else header
                    # Closet references the canonical (entry_chunk_idx=0)
                    # drawer for the entry. Searcher._expand_with_neighbors
                    # stitches sibling chunks back via the
                    # (source_file, chunk_index) pair.
                    entry_drawer_id = _diary_drawer_id_entry(wing, date_str, entry_idx, 0)
                    entry_lines = build_closet_lines(
                        source_file, [entry_drawer_id], entry_text, wing, "daily"
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
