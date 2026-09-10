# MemPalace native exact-vector engine

The optional Rust accelerator shares `sqlite_exact.sqlite3` with the Python
`sqlite_exact` backend. No database migration is needed. Python still handles
writes, document hydration, and complex filters; Rust loads an owned contiguous
float buffer and performs cosine scans. Wing names are interned; rooms remain
strings. The implementation does not guarantee 64-byte alignment or particular
SIMD instructions.

- `mempalace-core`: safe little-endian SQLite decoding, collection-scoped loading,
  deterministic top-k ranking, and Rayon parallel scans.
- `mempalace-py`: PyO3 bindings that release the GIL while loading and scanning.
- `mempalace-cli`: standalone executable for vector search, stats, and benchmarks.
  It needs no Python, but platform runtime libraries may be required; the Linux
  GNU build is not a static executable suitable for a scratch container.

## Install without a Rust compiler

Install MemPalace normally. From the matching GitHub release, download the
`mempalace_native_core` wheel for your OS and CPU, then install the downloaded
wheel with `python -m pip install <wheel-file>`. Release builds attach wheels and
executables directly to the release; manual workflow runs retain Actions
artifacts. Wheels are distributed separately from the ordinary Python package.

Verify `python -c "import mempalace_core_rs"`, then select `--backend rust_exact`
or set `MEMPALACE_BACKEND=rust_exact`. The disk format continues to autodetect as
`sqlite_exact`; native acceleration is an explicit selection. If the extension
is unavailable, the adapter uses the Python backend. Complex filters and requests
for returned embeddings also use Python and may consume its larger vector cache.

## Build and test from source

```sh
python -m pip install ./crates/mempalace-py
cargo test -p mempalace-core -p mempalace-cli --locked
cargo build --release --locked --bin mempalace-native
```

Run the Python backend suites with `MEMPALACE_REQUIRE_NATIVE=1` to require the
installed extension instead of accepting fallback-only coverage. CI does this
on Linux, Windows, and macOS.

## Use the executable

```sh
mempalace-native stats --db /path/to/sqlite_exact.sqlite3
mempalace-native bench --db /path/to/sqlite_exact.sqlite3
mempalace-native search --db /path/to/sqlite_exact.sqlite3 --vector '[1,0]' -k 5
mempalace-native search --db /path/to/sqlite_exact.sqlite3 --vector - < query.json
```

Supply a JSON float array with the collection's embedding dimension, produced by
the same embedding model used for ingestion. The example `[1,0]` is for a
2-dimensional fixture. This executable does not embed text. The default collection
is `mempalace_drawers`; use `--collection` to select another explicitly.

## Performance evidence

The initial Windows benchmark reported 557 MB RSS for the Python/Rust engine
versus 2,430 MB for the Python baseline on a database with 334,224 rows. Reported
warm native latencies were 7.2-11.8 ms across 168k and 334k-row workloads. These
are historical measurements, not guarantees for this revision, other machines,
or queries that fall back to Python. Correctness tests use synthetic data; the
private corpus benchmark has not been rerun for the hardening changes.
