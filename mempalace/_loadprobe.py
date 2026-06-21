"""
_loadprobe.py — Subprocess-isolated HNSW loadability probe.

Run as ``python -m mempalace._loadprobe <palace_path>`` to test whether
ChromaDB can open the palace and call ``collection.count()`` without
segfaulting or raising an "Error loading hnsw index" error.

Exits:
  0  — all collections loaded successfully; prints "OK" to stdout.
  2  — chromadb raised a known error (e.g. "Error loading hnsw index");
       prints the error to stderr.
  Other non-zero — unexpected exception; message on stderr.

A native SIGSEGV (torn binary segment) kills the process with signal 11,
yielding returncode -11 / 139 — the caller detects this without any
handling needed here.
"""

import faulthandler
import sys


def _probe(palace_path: str) -> None:
    """Open every known mempalace collection and call count().

    Imports are kept lazy so module-load cost is minimal (this code runs as
    a fresh subprocess; importing chromadb at the top level would add ~200 ms
    of cold-load overhead even when the probe succeeds quickly).
    """
    import chromadb
    from mempalace.embedding import get_embedding_function

    client = chromadb.PersistentClient(path=palace_path)
    collection_names = ("mempalace_drawers", "mempalace_closets")

    # list_collections() returns Collection objects in chromadb ≥ 1.x.
    try:
        existing = {col.name for col in client.list_collections()}
    except Exception as exc:
        print(f"Error listing collections: {exc}", file=sys.stderr)
        sys.exit(2)

    ef = get_embedding_function()
    for name in collection_names:
        if name not in existing:
            continue
        try:
            col = client.get_collection(name, embedding_function=ef)
            col.count()
        except Exception as exc:
            print(str(exc), file=sys.stderr)
            sys.exit(2)

    print("OK")
    sys.exit(0)


if __name__ == "__main__":
    faulthandler.enable()
    if len(sys.argv) != 2:
        print("Usage: python -m mempalace._loadprobe <palace_path>", file=sys.stderr)
        sys.exit(1)
    _probe(sys.argv[1])
