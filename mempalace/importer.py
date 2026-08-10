"""
importer.py — Merge a JSONL palace export back into a palace.

The read-side counterpart to :func:`mempalace.exporter.export_palace_jsonl`
(#452, cross-device sync). Walks ``input_dir`` for ``*.jsonl`` files, parses
one drawer per line, and files every drawer whose id is not already present.

Import semantics:

* **Merge, not overwrite** — existing drawers (matched by id, which is a
  content-derived hash for mined drawers) are skipped, never replaced.
* **Idempotent** — importing the same export twice is a no-op the second time.
* **Re-embedding** — exports carry no embedding vectors, so every newly filed
  drawer is embedded on this machine by the backend's configured embedder.
  This keeps the format small, text-only, and independent of the embedding
  model, at the cost of import time on large first-time imports.

Malformed lines are counted and reported, never fatal: a partially corrupted
export should import everything that survives, loudly.
"""

import glob
import json
import os

from .palace import get_collection

# Chroma metadata values must be scalars; anything else on a line is dropped
# (with a count) rather than failing the whole import.
_SCALAR_TYPES = (str, int, float, bool)

_ADD_BATCH_SIZE = 500


def _sanitize_metadata(meta) -> dict:
    """Keep only scalar-valued metadata entries; guarantee a non-empty dict."""
    if not isinstance(meta, dict):
        meta = {}
    clean = {k: v for k, v in meta.items() if isinstance(k, str) and isinstance(v, _SCALAR_TYPES)}
    if not clean:
        # Chroma rejects empty metadata dicts; record provenance instead.
        clean = {"imported_without_metadata": True}
    return clean


def _parse_line(raw: str):
    """Parse one JSONL line into an (id, document, metadata) triple, or None."""
    try:
        obj = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None
    doc_id = obj.get("id")
    document = obj.get("document")
    if not isinstance(doc_id, str) or not doc_id or not isinstance(document, str):
        return None
    return doc_id, document, _sanitize_metadata(obj.get("metadata"))


def import_palace(palace_path: str, input_dir: str, dry_run: bool = False) -> dict:
    """Merge JSONL drawers from ``input_dir`` into the palace at ``palace_path``.

    Args:
        palace_path: Palace directory. Created (with its collection) on a
            first import to a fresh machine — unless ``dry_run`` is set.
        input_dir: Directory tree containing ``*.jsonl`` export files.
        dry_run: Report what would be imported without writing anything.
            A dry run never creates a palace; against a missing palace every
            parsed drawer counts as new.

    Returns:
        Stats dict: {"files": N, "imported": N, "skipped_existing": N,
        "malformed": N}
    """
    if not os.path.isdir(input_dir):
        raise ValueError(f"import source is not a directory: {input_dir!r}")

    jsonl_files = sorted(
        glob.glob(os.path.join(glob.escape(input_dir), "**", "*.jsonl"), recursive=True)
    )
    if not jsonl_files:
        print(f"  No .jsonl files found under {input_dir} — nothing to import.")
        return {"files": 0, "imported": 0, "skipped_existing": 0, "malformed": 0}

    col = None
    if dry_run:
        try:
            col = get_collection(palace_path, create=False, read_only=True)
        except Exception:
            col = None  # no palace yet — everything counts as new
    else:
        col = get_collection(palace_path)

    stats = {"files": 0, "imported": 0, "skipped_existing": 0, "malformed": 0}
    seen_ids: set = set()  # dedup across files within this run
    pending_ids: list = []
    pending_docs: list = []
    pending_metas: list = []
    malformed_examples: list = []

    def _existing_ids(ids):
        if col is None:
            return set()
        got = col.get(ids=list(ids), include=[])
        return set(got.get("ids") or [])

    def _flush():
        if not pending_ids:
            return
        existing = _existing_ids(pending_ids)
        new = [
            (i, d, m)
            for i, d, m in zip(pending_ids, pending_docs, pending_metas)
            if i not in existing
        ]
        stats["skipped_existing"] += len(pending_ids) - len(new)
        if new and not dry_run and col is not None:
            col.add(
                ids=[i for i, _, _ in new],
                documents=[d for _, d, _ in new],
                metadatas=[m for _, _, m in new],
            )
        stats["imported"] += len(new)
        pending_ids.clear()
        pending_docs.clear()
        pending_metas.clear()

    for path in jsonl_files:
        stats["files"] += 1
        with open(path, encoding="utf-8") as f:
            for lineno, raw in enumerate(f, 1):
                raw = raw.strip()
                if not raw:
                    continue
                parsed = _parse_line(raw)
                if parsed is None:
                    stats["malformed"] += 1
                    if len(malformed_examples) < 5:
                        malformed_examples.append(f"{path}:{lineno}")
                    continue
                doc_id, document, meta = parsed
                if doc_id in seen_ids:
                    continue
                seen_ids.add(doc_id)
                pending_ids.append(doc_id)
                pending_docs.append(document)
                pending_metas.append(meta)
                if len(pending_ids) >= _ADD_BATCH_SIZE:
                    _flush()
    _flush()

    if stats["imported"] and not dry_run:
        print(
            f"  Note: exports do not include embeddings — {stats['imported']} imported "
            f"drawers were re-embedded by this machine's embedder."
        )
    verb = "Would import" if dry_run else "Imported"
    print(
        f"\n  {verb} {stats['imported']} drawers "
        f"({stats['skipped_existing']} already present, "
        f"{stats['malformed']} malformed lines) from {stats['files']} files"
    )
    if malformed_examples:
        print("  Malformed lines at: " + ", ".join(malformed_examples))
    return stats
