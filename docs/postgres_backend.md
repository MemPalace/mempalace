# PostgreSQL Backend

MemPalace uses ChromaDB by default. The PostgreSQL backend is optional and is
intended for larger, long-lived, or team/server deployments where a local
Chroma directory is not the right storage boundary.

The backend supports two database extension paths:

- `pg_sorted_heap` (optional optimized self-managed path): uses `sorted_heap`,
  `svec`, and `sorted_hnsw`.
- `pgvector` (broadly available managed-service path): uses a regular heap
  table, `vector`, and `hnsw`.

When both extensions are available in the target database, MemPalace selects
`pg_sorted_heap` to exercise the optimized storage/index path. Operators who
prefer a simpler supply-chain profile or run on managed PostgreSQL services
should install only `vector` and use the `pgvector` path. Both paths require a
PostgreSQL extension to be available to the server and created in the target
database. MemPalace does not vendor or install database extensions at Python
package install time.

`pg_sorted_heap` is listed in the
[PostgreSQL Software Catalogue](https://www.postgresql.org/download/products/6/)
and on [PGXN](https://pgxn.org/dist/pg_sorted_heap/), but it is not bundled with
managed PostgreSQL providers; use this path only when you control extension
installation for the target PostgreSQL server.

## Install MemPalace Dependencies

```bash
pip install "mempalace[postgres]"
```

The PostgreSQL extra installs the Python driver. Text queries and writes that do
not pass embeddings directly reuse the same Chroma default local embedding
function that MemPalace already depends on for the default backend.

## Optional: Install `pg_sorted_heap`

Requirements:

- PostgreSQL 17 or 18.
- `pg_config` for the PostgreSQL version you want to use.
- Standard PGXS build tools (`make`, compiler toolchain, PostgreSQL server
  development files).
- Database privileges to run `CREATE EXTENSION`.

Automated helper (from a source checkout of the MemPalace repository):

```bash
scripts/install_pg_backend.sh --dsn "postgresql://mempalace_user@localhost:5432/mempalace"
```

The helper clones `https://github.com/skuznetsov/pg_sorted_heap.git`, runs
`make`, runs `make install`, verifies the installed control/library files, and
then creates the extension in the database if `--dsn` is supplied.

Use an explicit PostgreSQL installation when multiple versions are installed:

```bash
scripts/install_pg_backend.sh \
  --pg-config /usr/lib/postgresql/18/bin/pg_config \
  --dsn "postgresql://mempalace_user@localhost:5432/mempalace"
```

Build from an existing checkout instead of cloning:

```bash
scripts/install_pg_backend.sh \
  --source /path/to/pg_sorted_heap \
  --dsn "postgresql://mempalace_user@localhost:5432/mempalace"
```

Manual installation:

```bash
git clone https://github.com/skuznetsov/pg_sorted_heap.git
cd pg_sorted_heap
make
make install
psql "postgresql://mempalace_user@localhost:5432/mempalace" \
  -c "CREATE EXTENSION IF NOT EXISTS pg_sorted_heap;"
```

If `make install` needs elevated permissions for your PostgreSQL installation,
run the helper with `--sudo`, or run the manual `make install` step with the
appropriate privilege escalation for your environment.

## Fallback: Install `pgvector`

If `pg_sorted_heap` is not installed but the `vector` extension is available in
the target database, MemPalace will fall back to `pgvector` automatically.
`pgvector` is still a PostgreSQL extension, so it must be installed or exposed by
your PostgreSQL distribution/provider and created in the database.

Check whether the server exposes the extension:

```sql
SELECT name, default_version, installed_version
FROM pg_available_extensions
WHERE name = 'vector';
```

Create the extension in the target database:

```bash
psql "postgresql://mempalace_user@localhost:5432/mempalace" \
  -c "CREATE EXTENSION IF NOT EXISTS vector;"
```

The helper can also install `pgvector` from source for self-managed PostgreSQL:

```bash
scripts/install_pg_backend.sh \
  --extension vector \
  --dsn "postgresql://mempalace_user@localhost:5432/mempalace"
```

Manual source installation:

```bash
git clone https://github.com/pgvector/pgvector.git
cd pgvector
make
make install
psql "postgresql://mempalace_user@localhost:5432/mempalace" \
  -c "CREATE EXTENSION IF NOT EXISTS vector;"
```

For managed PostgreSQL services, including AWS RDS/Aurora PostgreSQL versions
that expose `vector` as a supported extension, do not run local `make install`
against the managed server. Use the provider-supported extension mechanism and
verify availability with `pg_available_extensions`.

Use `pg_sorted_heap` when you control the PostgreSQL installation and want the
optional sorted storage plus planner-integrated `sorted_hnsw` path. Use
`pgvector` when you need a managed-database setup, a more common extension, or a
simpler supply-chain profile.

## Configure MemPalace

Environment variables:

```bash
export MEMPALACE_BACKEND=postgres
export MEMPALACE_POSTGRES_DSN="postgresql://mempalace_user@localhost:5432/mempalace"

# optional, defaults to mempalace_drawers
export MEMPALACE_COLLECTION_NAME=mempalace_drawers
```

Equivalent `~/.mempalace/config.json`:

```json
{
  "backend": "postgres",
  "postgres_dsn": "postgresql://mempalace_user@localhost:5432/mempalace",
  "collection_name": "mempalace_drawers"
}
```

Then use MemPalace normally:

```bash
mempalace mine ~/projects/myapp
mempalace search "why did we change the auth flow"
```

## Verify The Backend

Check the selected PostgreSQL extension:

```sql
SELECT extname
FROM pg_extension
WHERE extname IN ('pg_sorted_heap', 'vector')
ORDER BY extname;
```

For a `pg_sorted_heap` collection, the MemPalace table should use
`sorted_heap`:

```sql
SELECT am.amname
FROM pg_class c
JOIN pg_am am ON am.oid = c.relam
WHERE c.relname = 'mempalace_drawers';
```

Expected:

```text
sorted_heap
```

For fallback `pgvector`, the table access method is the regular heap access
method and the `vector` extension should be present.

## Operational Notes

- The PostgreSQL backend creates the collection table on first write when
  `create=True`.
- For `pg_sorted_heap`, MemPalace stores drawers with primary key
  `(wing, room, id)` so wing/room locality is preserved in the sorted table.
- Vector indexes are created lazily after the collection reaches the backend's
  index threshold; small collections use exact vector ordering.
- `count()` is exact and may scan large tables; use `estimated_count()` for
  progress/status paths where PostgreSQL catalog statistics are acceptable.
- ChromaDB remains the zero-config default and is still the benchmarked raw-mode
  path in the public README.
