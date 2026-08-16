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
* **Single-writer** — import is an ordinary palace write and follows the same
  single-writer expectations as ``mine``; it is not safe to race another
  writer, though a concurrent insert of the same id degrades to a skip
  rather than an overwrite.

Malformed input is counted and reported, never fatal: a partially corrupted
export should import everything that survives, loudly. Metadata values that
the backend cannot store (non-scalars) are dropped, not treated as malformed.

Non-regular entries carrying a ``.jsonl`` name — a FIFO, socket, device node
or directory — are refused by type rather than opened, so an import never
blocks in the kernel waiting for a writer (#2221, #2244).
"""

import errno
import glob
import json
import os
import stat

from .palace import get_collection

# Chroma metadata values must be scalars; anything else on a line is dropped
# rather than failing the whole import.
_SCALAR_TYPES = (str, int, float, bool)

_ADD_BATCH_SIZE = 500


def _open_regular_text(path):
    """Open ``path`` for UTF-8 text reading, refusing anything but a regular file.

    Mirrors ``miner._read_text_no_follow``'s guard (#2221, #2244). ``O_NONBLOCK``
    is what makes the ``S_ISREG`` test below reachable: opening a FIFO for reading
    parks in the kernel until a writer shows up, so a plain ``open()`` on a named
    pipe named ``*.jsonl`` never raises — it hangs, and the caller's ``except
    OSError`` cannot see it. ``O_NOFOLLOW`` keeps a symlinked entry from reading
    through to a target outside the import tree, matching the ``_reject_symlink``
    posture the exporter already applies on the write side.

    Returns an open text-mode file object, or ``None`` when the entry is not a
    regular file — the caller books that exactly like an unreadable one.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = -1
    try:
        try:
            fd = os.open(path, flags)
        except OSError as exc:
            # A reader that breaks a write lease gets EAGAIN when it passes
            # O_NONBLOCK, where a blocking open waits out lease-break-time and
            # succeeds. The kernel grants leases on regular files only, so
            # re-check the type and then read it the way a plain open would.
            if exc.errno != errno.EAGAIN or not stat.S_ISREG(os.lstat(path).st_mode):
                raise
            fd = os.open(path, flags & ~getattr(os, "O_NONBLOCK", 0))
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        f = os.fdopen(fd, "r", encoding="utf-8")
        fd = -1
        return f
    finally:
        if fd != -1:
            try:
                os.close(fd)
            except OSError:
                pass


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
    """Parse one JSONL line into an (id, document, metadata) triple, or None.

    ``RecursionError`` guards against pathological deeply-nested-but-valid
    JSON, which would otherwise abort the whole import.
    """
    try:
        obj = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    if not isinstance(obj, dict):
        return None
    doc_id = obj.get("id")
    document = obj.get("document")
    if not isinstance(doc_id, str) or not doc_id or not isinstance(document, str):
        return None
    return doc_id, document, _sanitize_metadata(obj.get("metadata"))


def _add_batch(col, batch, stats):
    """Add (id, document, metadata) triples, skipping ids that already exist.

    The existence check and the add are not one atomic operation, so a
    concurrent writer can file an id between them. On a failed batch add,
    degrade to per-item adds with a fresh existence check each, so a
    just-appeared duplicate becomes a skip instead of an abort — and a
    genuine backend error still surfaces.
    """
    got = col.get(ids=[i for i, _, _ in batch], include=[])
    existing = set(got.get("ids") or [])
    new = [(i, d, m) for i, d, m in batch if i not in existing]
    stats["skipped_existing"] += len(batch) - len(new)
    if not new:
        return
    try:
        col.add(
            ids=[i for i, _, _ in new],
            documents=[d for _, d, _ in new],
            metadatas=[m for _, _, m in new],
        )
        stats["imported"] += len(new)
    except Exception:
        # Retry per item, and classify each failure AFTER it happens: an id
        # that exists post-failure (a concurrent writer, or a partial commit
        # of the batch above) is a skip; anything else re-raises so a real
        # backend error is never silently converted into a merge.
        for doc_id, document, meta in new:
            try:
                col.add(ids=[doc_id], documents=[document], metadatas=[meta])
                stats["imported"] += 1
            except Exception:
                got = col.get(ids=[doc_id], include=[])
                if got.get("ids"):
                    stats["skipped_existing"] += 1
                else:
                    raise


def import_palace(palace_path: str, input_dir: str, dry_run: bool = False) -> dict:
    """Merge JSONL drawers from ``input_dir`` into the palace at ``palace_path``.

    Args:
        palace_path: Palace directory. Created (with its collection) on a
            first import to a fresh machine — unless ``dry_run`` is set.
        input_dir: Directory tree containing ``*.jsonl`` export files.
        dry_run: Parse-only preview. Reports what the export contains without
            opening (or creating) the palace at all — opening a local backend
            is itself a write, and a true preview must not touch it. The
            existing-id dedup therefore runs only at real import time; a dry
            run counts every parsed drawer as importable.

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

    col = None if dry_run else get_collection(palace_path)

    stats = {"files": 0, "imported": 0, "skipped_existing": 0, "malformed": 0}
    seen_ids: set = set()  # dedup across files within this run
    pending: list = []
    malformed_examples: list = []

    def _flush_batch(batch):
        if not batch:
            return
        if dry_run:
            stats["imported"] += len(batch)
        else:
            _add_batch(col, batch, stats)

    for path in jsonl_files:
        stats["files"] += 1
        # Read under a guard, flush outside it: the guard must catch only
        # filesystem failures on THIS file, never a backend error from a
        # flush (which would be miscounted as malformed input and leave the
        # batch pending). Lines parsed before a mid-file read error still
        # import; idempotent re-import picks up a repaired file cleanly.
        triples: list = []
        try:
            handle = _open_regular_text(path)
            if handle is None:
                # A FIFO, socket, device node or directory carrying a .jsonl
                # name. Booked like any other unreadable input — the point of
                # the guard is that we get here at all instead of blocking.
                stats["malformed"] += 1
                if len(malformed_examples) < 5:
                    malformed_examples.append(f"{path} (not a regular file)")
            else:
                with handle as f:
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
                        triples.append((doc_id, document, meta))
        except (OSError, UnicodeDecodeError) as exc:
            # Unreadable input: permissions, a symlink refused by O_NOFOLLOW,
            # bad UTF-8, I/O errors. Count it and continue.
            stats["malformed"] += 1
            if len(malformed_examples) < 5:
                malformed_examples.append(f"{path} ({type(exc).__name__})")
        pending.extend(triples)
        while len(pending) >= _ADD_BATCH_SIZE:
            _flush_batch(pending[:_ADD_BATCH_SIZE])
            del pending[:_ADD_BATCH_SIZE]
    if pending:
        _flush_batch(pending)
        pending.clear()

    if stats["imported"] and not dry_run:
        print(
            f"  Note: exports do not include embeddings — {stats['imported']} imported "
            f"drawers were re-embedded by this machine's embedder."
        )
    if dry_run:
        print(
            f"\n  Would import up to {stats['imported']} drawers "
            f"({stats['malformed']} malformed) from {stats['files']} files"
        )
        print(
            "  (dedup against existing drawers happens at import time; a dry run never opens the palace)"
        )
    else:
        print(
            f"\n  Imported {stats['imported']} drawers "
            f"({stats['skipped_existing']} already present, "
            f"{stats['malformed']} malformed) from {stats['files']} files"
        )
    if malformed_examples:
        print("  Malformed input at: " + ", ".join(malformed_examples))
    return stats
