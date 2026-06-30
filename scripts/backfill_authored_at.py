#!/usr/bin/env python3
"""Backfill ``authored_at`` onto existing conversation drawers.

New mines stamp ``authored_at`` automatically (see ``convo_miner._extract_authored_at``),
but drawers mined before that change only have ``filed_at`` (ingest time). Re-mining does
NOT fix them: the scanner skips files already mined at the current ``NORMALIZE_VERSION``.

This migration updates the affected drawers IN PLACE — metadata only, embeddings are left
untouched, so there is no re-embedding cost. It is idempotent (drawers already correct are
skipped) and safe to re-run. It only touches ``ingest_mode == "convos"`` drawers; markdown
drawers have no per-line timestamps and keep their ``filed_at`` fallback.

Drawers whose source transcript is no longer on disk are left as-is (they keep falling back
to ``filed_at``), so point ``--sessions`` at the directories that still hold your ``.jsonl``
transcripts (e.g. ``~/.claude`` and ``~/.codex``).

Usage (dry-run prints what would change; pass --apply to write):

    python scripts/backfill_authored_at.py \
        --palace ~/.mempalace/palace \
        --sessions ~/.claude --sessions ~/.codex [--apply]

In Docker (the MCP image), mount the volume and your session dirs read-only:

    docker run --rm \
      -v mempalace-data:/data \
      -v ~/.claude:/sessions/claude:ro -v ~/.codex:/sessions/codex:ro \
      -v "$PWD/scripts/backfill_authored_at.py:/tmp/backfill.py:ro" \
      --entrypoint /app/.venv/bin/python mempalace:local \
      /tmp/backfill.py --palace /data/.mempalace/palace \
        --sessions /sessions/claude --sessions /sessions/codex --apply
"""

import argparse
import glob
import os

import chromadb

from mempalace.convo_miner import _extract_authored_at

COLLECTION = "mempalace_drawers"
PAGE = 2000
BATCH = 1000


def _index_sessions(session_dirs):
    """Map ``basename.jsonl -> realpath`` for every transcript under the given dirs."""
    index = {}
    for root in session_dirs:
        for f in glob.glob(os.path.join(os.path.expanduser(root), "**", "*.jsonl"), recursive=True):
            index.setdefault(os.path.basename(f), f)
    return index


def backfill_authored_at(collection, session_dirs, apply=False):
    """Stamp ``authored_at`` on convos drawers from their source transcript timestamps.

    Returns a stats dict: ``scanned``, ``updated``, ``resolved_files``, ``unresolved_files``.
    """
    index = _index_sessions(session_dirs)
    cache = {}
    unresolved = set()
    pending_ids, pending_metas = [], []
    scanned = updated = 0

    def flush():
        nonlocal pending_ids, pending_metas, updated
        if pending_ids and apply:
            collection.update(ids=pending_ids, metadatas=pending_metas)
        updated += len(pending_ids)
        pending_ids, pending_metas = [], []

    offset = 0
    while True:
        res = collection.get(
            where={"ingest_mode": "convos"}, include=["metadatas"], limit=PAGE, offset=offset
        )
        ids = res["ids"]
        if not ids:
            break
        for drawer_id, meta in zip(ids, res["metadatas"]):
            scanned += 1
            basename = os.path.basename(meta.get("source_file") or "")
            if basename in cache:
                authored = cache[basename]
            else:
                path = index.get(basename)
                authored = _extract_authored_at(path) if path else None
                cache[basename] = authored
                if path is None and basename:
                    unresolved.add(basename)
            if authored and meta.get("authored_at") != authored:
                new_meta = dict(meta)
                new_meta["authored_at"] = authored
                pending_ids.append(drawer_id)
                pending_metas.append(new_meta)
                if len(pending_ids) >= BATCH:
                    flush()
        offset += len(ids)
    flush()
    return {
        "scanned": scanned,
        "updated": updated,
        "resolved_files": sum(1 for v in cache.values() if v),
        "unresolved_files": len(unresolved),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--palace", required=True, help="Path to the ChromaDB palace dir")
    parser.add_argument(
        "--sessions",
        action="append",
        default=[],
        required=True,
        help="Directory holding .jsonl transcripts (repeatable)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write changes (default is a dry run that only reports counts)",
    )
    args = parser.parse_args()

    client = chromadb.PersistentClient(path=os.path.expanduser(args.palace))
    collection = client.get_collection(COLLECTION)
    stats = backfill_authored_at(collection, args.sessions, apply=args.apply)
    mode = "APPLIED" if args.apply else "DRY-RUN (use --apply to write)"
    print(
        f"{mode}: scanned={stats['scanned']} updated={stats['updated']} "
        f"resolved_files={stats['resolved_files']} unresolved_files={stats['unresolved_files']}"
    )


if __name__ == "__main__":
    main()
