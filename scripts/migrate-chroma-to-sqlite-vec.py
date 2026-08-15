#!/usr/bin/env python3
"""Migrate drawers from ChromaDB palace to sqlite_vec palace via APIs.

Reads all drawers from an existing ChromaDB palace using the ChromaDB client,
then writes them into a new sqlite_vec palace with vec0 ANN indexes and FTS5.
"""

import sys, os, sqlite3, struct, json
from pathlib import Path

sys.path.insert(0, str(Path.home() / "mempalace"))

CHROMA_PATH = str(Path.home() / ".mempalace" / "palace")
VEC_PATH = str(Path.home() / ".mempalace" / "palace_vec")

# Load sqlite-vec
try:
    import sqlite_vec
except ImportError:
    sp = str(Path.home() / ".local/share/uv/tools/mempalace/lib/python3.11/site-packages")
    if sp not in sys.path:
        sys.path.insert(0, sp)
    import sqlite_vec

COLLECTION_NAME = "mempalace_drawers"
TABLE_NAME = f"doc_vec_{COLLECTION_NAME}"

print("=" * 60)
print("ChromaDB -> sqlite_vec Migration")
print("=" * 60)

# 1. Read from ChromaDB
print("1. Reading from ChromaDB...")
import chromadb
client = chromadb.PersistentClient(path=CHROMA_PATH)
col = client.get_collection(COLLECTION_NAME)
total = col.count()
print(f"   Total: {total} drawers")

all_ids, all_docs, all_metas, all_embs = [], [], [], []
page, page_size = 0, 500
while page * page_size < total:
    offset = page * page_size
    result = col.get(limit=page_size, offset=offset,
                     include=["documents", "metadatas", "embeddings"])
    all_ids.extend(result["ids"])
    all_docs.extend(result.get("documents", [""] * len(result["ids"])))
    all_metas.extend(result.get("metadatas", [{}] * len(result["ids"])))
    all_embs.extend(result.get("embeddings", []))
    page += 1
print(f"   Read: {len(all_ids)} documents with embeddings")

# 2. Create sqlite_vec destination
print(f"2. Creating sqlite_vec palace at {VEC_PATH}...")
Path(VEC_PATH).mkdir(parents=True, exist_ok=True)
vec_db_path = os.path.join(VEC_PATH, "sqlite_vec.sqlite3")
if os.path.exists(vec_db_path):
    os.unlink(vec_db_path)

dst = sqlite3.connect(vec_db_path)
dst.enable_load_extension(True)
try:
    sqlite_vec.load(dst)
finally:
    dst.enable_load_extension(False)

dst.executescript("""
    PRAGMA journal_mode=WAL;
    PRAGMA mmap_size=0;
    
    CREATE TABLE meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    CREATE TABLE collections (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        dimension INTEGER,
        created_at TEXT NOT NULL
    );
    CREATE TABLE documents (
        collection_id INTEGER NOT NULL,
        id TEXT NOT NULL,
        document TEXT NOT NULL,
        metadata_json TEXT NOT NULL,
        embedding BLOB NOT NULL,
        dim INTEGER NOT NULL,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now')),
        PRIMARY KEY (collection_id, id)
    );
""")

# Create collection + metadata
dst.execute(
    "INSERT INTO collections(name, dimension, created_at) VALUES (?, 384, datetime('now'))",
    (COLLECTION_NAME,)
)
cid = dst.execute("SELECT id FROM collections WHERE name=?", (COLLECTION_NAME,)).fetchone()[0]
dst.execute("INSERT INTO meta(key, value) VALUES ('sqlite_vec_available', '1')")
dst.execute("INSERT INTO meta(key, value) VALUES (?, '1')", (f"vec0_table_created:{COLLECTION_NAME}",))
dst.execute("INSERT INTO meta(key, value) VALUES (?, 'embeddinggemma')",
            (f"embedder_model:{COLLECTION_NAME}",))

# Create vec0 table
dst.execute(f"CREATE VIRTUAL TABLE {TABLE_NAME} USING vec0("
            f"  embedding float[384] distance_metric=cosine)")

# Create FTS5 for lexical search
dst.execute(
    "CREATE VIRTUAL TABLE docs_fts USING fts5(collection_id UNINDEXED, doc_id UNINDEXED, document)"
)
dst.execute("INSERT INTO meta(key, value) VALUES ('fts5_available', '1')")

# 3. Insert all documents
print(f"3. Importing {len(all_ids)} drawers...")
cur = dst.cursor()
imported = 0
for i, (doc_id, doc, meta, emb) in enumerate(zip(all_ids, all_docs, all_metas, all_embs)):
    if emb is None or (hasattr(emb, '__len__') and len(emb) == 0):
        continue
    try:
        if hasattr(emb, 'tolist'):
            emb = emb.tolist()
        emb_blob = struct.pack(f"{len(emb)}f", *emb)
        cur.execute(
            "INSERT INTO documents(collection_id, id, document, metadata_json, embedding, dim) "
            "VALUES (?, ?, ?, ?, ?, 384)",
            (cid, str(doc_id), str(doc or ""), json.dumps(meta or {}), emb_blob)
        )
        rowid = cur.lastrowid
        cur.execute(f"INSERT INTO {TABLE_NAME} (rowid, embedding) VALUES (?, ?)", (rowid, emb_blob))
        # Populate FTS5
        cur.execute(
            "INSERT INTO docs_fts(collection_id, doc_id, document) VALUES (?, ?, ?)",
            (cid, str(doc_id), str(doc or ""))
        )
        imported += 1
        if (i + 1) % 500 == 0:
            dst.commit()
            print(f"   {i + 1}/{len(all_ids)}...")
    except Exception as e:
        print(f"   WARNING {doc_id}: {e}")

dst.commit()
count = dst.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
fts_count = dst.execute("SELECT COUNT(*) FROM docs_fts").fetchone()[0]
size_mb = os.path.getsize(vec_db_path) / 1024 / 1024
print(f"   Imported: {imported} drawers")

print(f"\nDone! {count} drawers, {fts_count} FTS entries, {size_mb:.1f} MB")
print(f"   Palace: {VEC_PATH}")
