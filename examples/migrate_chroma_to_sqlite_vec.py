"""One-shot migration: chromadb palace -> sqlite-vec backend.

Reads ``chroma.sqlite3`` directly via stdlib ``sqlite3`` (no chromadb
code involved, so platforms affected by chromadb-rust-bindings UAFs —
e.g. macOS 26 / ARM64, see chroma-core/chroma#6852 — can run this without
crashing). Re-embeds every document with the local ONNX MiniLM since
chromadb wraps hnswlib's index with its own segment envelope and stock
hnswlib can't load it. Writes a fresh ``sqlite_vec.db`` next to the
chroma files; nothing in the original palace dir is touched, so a swap
back is one config change away.

Usage:
    pip install mempalace[sqlite-vec]
    python -m mempalace.examples.migrate_chroma_to_sqlite_vec [palace_path]

Defaults palace_path to ``~/.mempalace/palace``.

The script is **resumable** — re-running picks up where it left off via
``drawer_id`` uniqueness on the destination side.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import time
from pathlib import Path

from mempalace.backends.base import PalaceRef
from mempalace.backends.sqlite_vec import SqliteVecBackend
from mempalace.embedding import get_embedding_function

BATCH = 256  # docs per re-embed batch — onnx model overhead amortizes here


def _fetch_collection_meta(src):
    """Yield ``(chroma_collection_id, name, dimension, metadata_segment_id)``."""
    cur = src.execute(
        """
        SELECT c.id, c.name, c.dimension, s.id
        FROM collections c
        JOIN segments s ON s.collection = c.id
        WHERE s.scope = 'METADATA'
        """
    )
    yield from cur.fetchall()


def _materialize_metadata(rows):
    """Group flat ``embedding_metadata`` rows into ``{id: (doc, meta_dict)}``."""
    out: dict[int, tuple[str, dict]] = {}
    for eid, key, sv, iv, fv, bv in rows:
        doc, meta = out.setdefault(eid, ("", {}))
        if key == "chroma:document":
            doc = sv or ""
        else:
            if sv is not None:
                meta[key] = sv
            elif iv is not None:
                meta[key] = iv
            elif fv is not None:
                meta[key] = fv
            elif bv is not None:
                meta[key] = bool(bv)
        out[eid] = (doc, meta)
    return out


def migrate(palace_path: Path) -> int:
    """Migrate chroma.sqlite3 -> sqlite_vec.db. Return total drawers migrated."""
    src_db = palace_path / "chroma.sqlite3"
    if not src_db.is_file():
        sys.exit(f"chroma.sqlite3 not found at {src_db}")

    src = sqlite3.connect(f"file:{src_db}?mode=ro", uri=True)
    src.execute("PRAGMA temp_store=MEMORY")

    print("loading onnx embedder...", flush=True)
    t0 = time.time()
    ef = get_embedding_function()
    print(f"  embedder ready ({time.time() - t0:.1f}s)", flush=True)

    backend = SqliteVecBackend()
    palace_ref = PalaceRef(id="default", local_path=str(palace_path))

    grand_total = 0
    grand_start = time.time()
    for _chroma_col_id, col_name, dim, meta_seg_id in _fetch_collection_meta(src):
        print(f"\n=== collection {col_name!r} (dim={dim}) ===", flush=True)
        n_total = src.execute(
            "SELECT COUNT(*) FROM embeddings WHERE segment_id=?", (meta_seg_id,)
        ).fetchone()[0]
        print(f"  source rows: {n_total}", flush=True)
        if n_total == 0:
            continue

        col = backend.get_collection(
            palace=palace_ref,
            collection_name=col_name,
            create=True,
            options={"dimension": dim},
        )

        # Resume: skip drawers we already have on the destination side.
        cur = backend._conn(str(palace_path)).cursor()
        cur.execute("SELECT drawer_id FROM drawers WHERE collection=?", (col_name,))
        seen = {r[0] for r in cur.fetchall()}
        if seen:
            print(f"  destination already has {len(seen)} rows — resuming", flush=True)

        offset = 0
        page = 1024
        col_done = 0
        col_skipped = 0
        col_start = time.time()
        while True:
            rows = src.execute(
                "SELECT id, embedding_id FROM embeddings WHERE segment_id=? "
                "ORDER BY id LIMIT ? OFFSET ?",
                (meta_seg_id, page, offset),
            ).fetchall()
            if not rows:
                break
            ids_in_page = [r[0] for r in rows]
            drawer_by_id = {r[0]: r[1] for r in rows}
            placeholders = ",".join(["?"] * len(ids_in_page))
            md_rows = src.execute(
                f"SELECT id, key, string_value, int_value, float_value, bool_value "
                f"FROM embedding_metadata WHERE id IN ({placeholders})",
                ids_in_page,
            ).fetchall()
            meta_map = _materialize_metadata(md_rows)

            ids_buf, docs_buf, metas_buf = [], [], []
            for eid in ids_in_page:
                drawer_id = drawer_by_id[eid]
                if drawer_id in seen:
                    col_skipped += 1
                    continue
                doc, meta = meta_map.get(eid, ("", {}))
                ids_buf.append(drawer_id)
                docs_buf.append(doc)
                metas_buf.append(meta)
                seen.add(drawer_id)

            for i in range(0, len(ids_buf), BATCH):
                chunk_ids = ids_buf[i : i + BATCH]
                chunk_docs = docs_buf[i : i + BATCH]
                chunk_metas = metas_buf[i : i + BATCH]
                # Empty docs would make the embedder unhappy; substitute a
                # single space so the vector exists but carries no signal.
                chunk_docs_safe = [d if d else " " for d in chunk_docs]
                vecs = [list(v) for v in ef(chunk_docs_safe)]
                col.add(
                    documents=chunk_docs,
                    ids=chunk_ids,
                    metadatas=chunk_metas,
                    embeddings=vecs,
                )
                col_done += len(chunk_ids)
                rate = col_done / max(time.time() - col_start, 1e-3)
                pct = (col_done + col_skipped) * 100.0 / n_total
                print(
                    f"  [{col_name}] {col_done:>7}/{n_total} migrated "
                    f"({col_skipped} skipped) — {rate:.0f}/s — {pct:.1f}%",
                    flush=True,
                )
            offset += len(rows)
        elapsed = time.time() - col_start
        print(
            f"  done collection {col_name!r}: migrated {col_done}, "
            f"skipped {col_skipped}, {elapsed:.0f}s",
            flush=True,
        )
        grand_total += col_done

    backend.close()
    src.close()
    total_time = time.time() - grand_start
    print(f"\nALL DONE: migrated {grand_total} drawers in {total_time:.0f}s")
    return grand_total


def main():
    palace = Path(sys.argv[1] if len(sys.argv) > 1 else "~/.mempalace/palace").expanduser()
    migrate(palace)


if __name__ == "__main__":
    main()
