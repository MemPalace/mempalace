# MemPalace Rust Native Engine (`crates/`)

This directory contains the high-performance native Rust implementation of the MemPalace vector search engine. It operates alongside the Python core in a **Dual-Track** architecture, providing a 77% memory reduction and sub-10ms query latencies on databases with hundreds of thousands of vectors.

---

## Workspace Structure

```
crates/
├── mempalace-core/   # Pure domain library: zero-copy SQLite reading, SIMD cosine math, Rayon parallelism
├── mempalace-py/     # PyO3 C-extension bindings (exposing NativeVectorIndex to Python)
└── mempalace-cli/    # Standalone, zero-dependency static CLI binary (mempalace-native)
```

### 1. `mempalace-core`
- **Zero-Copy SQLite Blob Reading**: Uses `rusqlite` to read raw `float32` byte blobs directly via `row.get_ref(idx)?.as_blob()?`, preventing intermediate Python object allocations.
- **Memory Alignment**: Stores vectors in a contiguous, 64-byte aligned buffer (`Vec<f32>`) maximizing L1/L2 cache line utilization.
- **SIMD Cosine Distance**: Unrolls 384-dimensional vector dot-products into 8-wide float accumulators.
- **Zero-Allocation Category Interning**: Maps string taxonomy values (`wing`, `room`) to compact `u16` integers so filtered queries perform integer comparisons with zero heap allocations.
- **Bounded Min-Heap**: Maintains a top-$k$ min-heap using `peek_mut()`, performing $O(N \log k)$ candidate selection without full array sorting.
- **Multi-Core Parallel Scanning**: Chunks large vector matrices across CPU cores using `rayon`.

### 2. `mempalace-py`
- Exposes `NativeVectorIndex` as a native Python extension class (`mempalace_core_rs`).
- Releases the Python Global Interpreter Lock (GIL) via `py.allow_threads` during parallel scans so background MCP requests or worker threads never stall.
- Integrated into `mempalace.backends.rust_exact.RustExactBackend`.

### 3. `mempalace-cli`
- Standalone static binary (`mempalace-native.exe`, 2.2 MB).
- Runs with zero Python, CPython, or pip dependencies.
- Ideal for resource-constrained edge deployments, Docker scratch containers, or fast health probes.

---

## Performance Benchmarks

Benchmarked against a **live 1.75 GB database** containing 334,224 rows (384-dimensional embeddings):

| Implementation | RSS Memory (334k items) | Query Latency (Warm p50) | QPS (100 sequential) |
| -------------- | ----------------------- | ------------------------ | -------------------- |
| **Python Baseline** | 2,430 MB | 191.6 ms | ~5 QPS |
| **Python `sqlite_exact` (Optimized)** | 2,430 MB | 14.7 ms | ~68 QPS |
| **Python `rust_exact` (PyO3)** | **557 MB (-77%)** | **7.2 ms - 11.8 ms** | **140 QPS** |
| **`mempalace-native` (Rust CLI)** | **526 MB (-78%)** | **6.1 ms - 11.6 ms** | **160 QPS** |

---

## Building and Testing

### Build Everything
```bash
cargo build --release
```

### Run Rust Unit Tests
```bash
cargo test --workspace
```

### Run Python Extension Build
Using Maturin:
```bash
uv run maturin develop -m crates/mempalace-py/Cargo.toml
```

### Run Standalone Native CLI
```bash
# Show database stats
./target/release/mempalace-native stats

# Run benchmark
./target/release/mempalace-native bench

# Search
./target/release/mempalace-native search "my search term"
```
