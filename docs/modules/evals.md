## evals

- **Paths:** `commonplace/evals/`, `docs/evals.md`, `tests/eval_support.py`, `tests/test_eval_guards.py`, `tests/test_eval_lifecycle_guards.py`, `tests/test_eval_archive.py`, `tests/test_eval_access.py`, `tests/test_eval_storage.py`
- **Owns:**

  - Owner-only local eval archives (slice 1): `commonplace eval archive|list|show|read|purge|preflight` and the per-workspace `evals/runs.sqlite3`.

- **Promises:**

  - The continuity probe covers `sync_streams`, `sync_groups`, `sync_backfill.after_id` and `capture_ops.response`, each absent table proven by `sqlite_master`; the commit re-hashes the stored `rows` bytes against `rows_sha256`; `preflight` takes no lock and creates nothing.

  - A run is the exact rows of one eligible stored thread (`threads` wing, no capture, fork or grant-import prefix, no continuity marker) at one read snapshot, proven dense, digest-equal and re-read field by field before commit; appends after the snapshot are excluded.

  - The control database is read `?mode=ro` with a write-denying authorizer and the thread store through `read_store`; no `ControlDB`, wiring, continuity writer, network, model or subprocess is used. Owner role, the eligibility rule, the read policy and the continuity probe are rechecked inside every archive commit and every list/show/read response.

  - Runs and chunks are immutable (triggers); only purge, by exact run ID and live ownership, deletes one, writing a payload-free tombstone. Keys are stored only as a domain-separated, length-prefixed digest and stay retired for 30 days after purge.

  - Caps: 16 MiB per run, 256 KiB chunks and pages, 1 GiB per workspace; with plans enabled the archive holds the thread-store writer lock while it measures plan storage. Refusals carry fixed codes without totals, payload or keys.

  - Privacy flags come from `commonplace.privacy.scan` per string (plus decoded JSON leaves of `text` and `raw`), never gate or redact, and are versioned by the scanner file's digest; rescans write nothing.

- **How it fails:**

  - A failed source or continuity lookup refuses the whole response (`eval_lookup_failed` or `eval_busy`); `list` never omits a run because of one. The permission gate runs before stdin artifacts or source rows are read.

  - Any scanner failure is `eval_scan_failed` for archive/list/show/read before the eval DB is opened; purge and preflight never load the scanner.

  - Loose modes, foreign owners or symlinks refuse with `eval_permissions` and the exact fix; eval never chmods. Busy locks and a held reader during the WAL switch refuse with `eval_busy` after a bounded 5 s wait; a crash, FULL or refusal leaves no run, chunk or key.

  - A run whose source is missing or now continuity-related is hidden from list/show/read and keeps its quota until purged by ID; purge does not erase retired generations, backups, WAL or free pages (residual in `SYSTEM.md`).

- **Tests:**

  - The A4 scan is a best-effort lint: the union of a per-piece rule (each literal and f-string, upper-case dynamic match) and a joined, anchored, case-insensitive rule over f-strings and `+` chains. Allowlist keys are scoped to file, statement kind and text, and a path-aware test checks the scan never catches less than the per-piece rule with its own allowlist. A committed positive corpus must stay flagged and a negative corpus must stay clean; three prose lines are accepted false positives. Known limits include the forms in `KNOWN_LIMITS` and `docs/evals.md`.

  - `tests/test_eval_guards.py` and `tests/test_eval_lifecycle_guards.py` hold the base guards eval relies on (read-only opener, continuity invariants, identity-rewrite scan, import-view scope and auth, backup, encryption and switch carriage) without importing eval.

  - `tests/test_eval_archive.py`, `tests/test_eval_access.py` and `tests/test_eval_storage.py` cover content fidelity, caps, keys, retention, quota, flags, eligibility fixtures, races, authorization, purge, permissions, refusals, plan storage, atomicity, WAL mode and encryption.
