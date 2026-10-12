# Historical engine comparison harness

These exploratory tools produced the initial Windows benchmark snapshot. The Go
RSS probes use Windows APIs; they are not cross-platform production executables.
Set `MEMPALACE_DB_PATH` to an existing SQLite exact database before running them.
The comparison assumes 384-dimensional embeddings. Build the Rust and Go probes
first (`cargo build --release` in `rust`, `go build -o lean_mempalace_go.exe main.go`
in `go`), install numpy/psutil and Bun, then run `python runner.py`.

The runner resolves tools relative to this directory and writes
`benchmark_results.json` here. `all` intentionally measures every collection for
historical comparison; production native searches always select one collection.
The saved results predate the correctness fixes and are not a fresh benchmark.
