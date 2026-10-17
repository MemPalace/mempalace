#!/usr/bin/env python3
"""Migrate drawers from a ChromaDB palace to a sqlite_vec palace.

Reads every drawer from an existing ChromaDB palace, then writes them through
the SQLiteVecBackend API. The script never touches the sqlite_vec schema
directly — the backend owns table layout, vec0 indexes, FTS and embedder
identity — so a migrated palace is byte-for-byte what the backend itself
would have written.

Usage:
    python scripts/migrate-chroma-to-sqlite-vec.py
    python scripts/migrate-chroma-to-sqlite-vec.py \
        --chroma-path ~/.mempalace/palace \
        --vec-path ~/.mempalace/palace_vec \
        --collection mempalace_drawers

Requires the sqlite_vec extra: pip install mempalace[sqlite_vec]
"""

import argparse
import os
from pathlib import Path

import chromadb


def read_chroma(chroma_path: str, collection_name: str):
    """Page all documents + embeddings out of the ChromaDB palace."""
    client = chromadb.PersistentClient(path=chroma_path)
    col = client.get_collection(collection_name)
    total = col.count()
    print(f"   Total: {total} drawers")

    all_ids, all_docs, all_metas, all_embs = [], [], [], []
    page, page_size = 0, 500
    while page * page_size < total:
        offset = page * page_size
        result = col.get(
            limit=page_size,
            offset=offset,
            include=["documents", "metadatas", "embeddings"],
        )
        all_ids.extend(result["ids"])
        all_docs.extend(result.get("documents", [""] * len(result["ids"])))
        all_metas.extend(result.get("metadatas", [{}] * len(result["ids"])))
        embs = result.get("embeddings", [])
        all_embs.extend(list(e) if hasattr(e, "tolist") else e for e in embs)
        page += 1
    return all_ids, all_docs, all_metas, all_embs


def main() -> None:
    parser = argparse.ArgumentParser(description="ChromaDB -> sqlite_vec migration")
    parser.add_argument("--chroma-path", default=str(Path.home() / ".mempalace" / "palace"))
    parser.add_argument("--vec-path", default=str(Path.home() / ".mempalace" / "palace_vec"))
    parser.add_argument("--collection", default="mempalace_drawers")
    args = parser.parse_args()

    from mempalace.backends.base import EmbedderIdentity
    from mempalace.backends.sqlite_vec import SQLiteVecBackend

    print("=" * 60)
    print("ChromaDB -> sqlite_vec Migration")
    print("=" * 60)

    # 1. Read from ChromaDB
    print(f"1. Reading from ChromaDB at {args.chroma_path}...")
    all_ids, all_docs, all_metas, all_embs = read_chroma(args.chroma_path, args.collection)
    print(f"   Read: {len(all_ids)} documents with embeddings")

    # 2. Fresh destination palace
    print(f"2. Creating sqlite_vec palace at {args.vec_path}...")
    Path(args.vec_path).mkdir(parents=True, exist_ok=True)
    vec_db_path = os.path.join(args.vec_path, "sqlite_vec.sqlite3")
    if os.path.exists(vec_db_path):
        print(f"   Removing existing {vec_db_path} (migration is not incremental)")
        os.unlink(vec_db_path)

    backend = SQLiteVecBackend()
    dst = backend.create_collection(args.vec_path, args.collection)
    dimension = len(all_embs[0]) if all_embs else 384
    try:
        dst.set_embedder_identity(
            EmbedderIdentity(model_name="chromadb-migration", dimension=dimension)
        )
    except Exception as exc:  # identity is advisory for a migrated palace
        print(f"   NOTE: could not set embedder identity: {exc}")

    # 3. Import in batches through the backend API
    print(f"3. Importing {len(all_ids)} drawers...")
    page_size = 500
    imported = 0
    for start in range(0, len(all_ids), page_size):
        batch_ids = [str(i) for i in all_ids[start : start + page_size]]
        batch_docs = [str(d or "") for d in all_docs[start : start + page_size]]
        batch_metas = [m or {} for m in all_metas[start : start + page_size]]
        batch_embs = list(all_embs[start : start + page_size])
        kept = [
            (
                doc_id,
                doc,
                meta,
                emb,
            )
            for doc_id, doc, meta, emb in zip(batch_ids, batch_docs, batch_metas, batch_embs)
            if emb is not None and len(emb) > 0
        ]
        if not kept:
            continue
        try:
            dst.add(
                documents=[k[1] for k in kept],
                ids=[k[0] for k in kept],
                metadatas=[k[2] for k in kept],
                embeddings=[k[3] for k in kept],
            )
        except Exception as exc:
            print(f"   WARNING batch at offset {start}: {exc}")
            continue
        imported += len(kept)
        print(f"   {imported}/{len(all_ids)}...")

    count = dst.count()
    size_mb = os.path.getsize(vec_db_path) / 1024 / 1024
    backend.close()

    print(f"\nDone! {count} drawers in sqlite_vec palace ({imported} imported, {size_mb:.1f} MB)")
    print(f"   Palace: {args.vec_path}")
    if count != imported:
        print("   WARNING: backend count differs from imported rows — rerun the migration")


if __name__ == "__main__":
    main()
