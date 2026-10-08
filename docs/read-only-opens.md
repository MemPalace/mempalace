# What a read-only open writes

Search, `status`, and the MCP read tools (`mempalace_status`, `mempalace_search`,
`mempalace_list_drawers`, `mempalace_list_wings`, `mempalace_get_drawer`) never
add, change or delete drawers, closets or the embedder identity record. Some
opens still touch files in the palace folder. This page lists every such write
and why it can't be avoided. Measured on 3.11.x with chromadb 1.5.7 by hashing every file in the palace
folder before and after each command.

## Chroma

| Write | When | Why it stays |
|---|---|---|
| `chroma.sqlite3` and the HNSW segment files (`length.bin`, sometimes `data_level0.bin`) | Every open that loads a segment (search, MCP search and list) | chromadb itself writes them. Constructing `PersistentClient` updates its system database. Loading a segment applies the queued embeddings to the HNSW index and persists it. MemPalace can't open a Chroma palace without a client. |
| `collection.modify(hnsw num_threads=1)` | Every collection open | This pins single-threaded HNSW inserts (#974/#965). chromadb 1.5.x doesn't persist the setting across clients, so it has to be re-applied on every open. A read-only open can later serve a write in the same process. |
| `_type` added to `collections.config_json_str`, plus `.collection_type_fixed` | Once per legacy palace, and only when the installed chromadb is 1.5.9 or later | chromadb 1.5.9+ raises `KeyError: '_type'` when it opens a collection that 1.5.8 or older created, so the palace can't be read without it (#1611). Older chromadb reads both forms, so it never migrates. |
| `.blob_seq_ids_migrated` (and an `embeddings.seq_id` rewrite on 0.6-era palaces) | Once per palace that MemPalace did not create in this chromadb | Without it the chromadb 1.5 compactor crashes on chromadb 0.6 BLOB seq_ids. A database created by chromadb 1.x is marked at creation, so it never needs the scan. |
| HNSW segment quarantine (renames a segment aside) | Only when a segment is unloadable or stale | This prevents a SIGSEGV on open (#1121, #1266). |

Since 3.11.x:

- A read-only open of a folder without `chroma.sqlite3` raises `CollectionNotInitializedError` and creates nothing. Before, it opened a client, and that created the database.
- A fresh palace no longer gets the `_type` migration on its first read. That migration was the "Fixed 2 collection(s) missing _type in config_json_str" log after a failed first mine.

## sqlite_exact

The database runs in WAL mode. The first connection creates `sqlite_exact.sqlite3-wal` and `-shm` (the WAL index) if they are missing, even for a reader. SQLite needs both files for any WAL connection. The database file itself does not change.

## Qdrant, pgvector

Nothing in the palace folder changes. Server-side state is not touched by reads.
