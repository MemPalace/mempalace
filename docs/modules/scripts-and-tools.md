## scripts-and-tools

- **Paths:** `scripts/`, `tools/`, `benchmarks/`
- **Owns:**

  - Maintenance and release scripts, review tooling, evaluation scripts and benchmarks.

- **Promises:**

  - Release email login waits for the accessible signed-in Board region after identity checks; step failure details retain exception messages through the shared credential redactor.

  - Import release checks retain owner listing and exact read-back and require the write-only bearer to receive the not-found response without metadata.

  - Scripts are run by hand or by the release process; none runs on import or ships in the package.

  - Benchmarks are informational and never gate a change.

  - `benchmarks/thread_search.py` uses deterministic synthetic chunks, exports complete search lists for revision comparisons, and reports p50/p95 latency.


- **How it fails:**

  - A script can rot unnoticed between releases; benchmark numbers vary with the machine.

- **Tests:**

  - `tests/test_commonplace_release_gate.py`, `tests/test_backfill_authored_at.py`; benchmarks run under the `benchmark` marker only.
