# Spec: tests/test_palace.py

Behavior specification for the test suite covering the shared palace helper
`_open_collection_or_explain` and its supporting collection-open path
(`get_collection`) (tests/test_palace.py:L1-L6).

## Subject Under Test

The tests pin the externally observable contract of a helper that attempts to
open a palace's storage collection at a given directory path and, on failure,
emits a human-readable explanation describing the palace's state. The helper
takes a palace directory path (string) and an optional output sink `out`
(a function that receives one message string at a time). When `out` is omitted,
messages are written to standard output (tests/test_palace.py:L20,L111-L118).

The helper returns the opened collection object on success, or `None` on any
failure-to-open condition (tests/test_palace.py:L22,L43,L59,L88,L106).

### Test scaffolding

A capture helper produces a pair `(emit, lines)`: `emit` is a function that
appends each received message to the list `lines`, allowing tests to inspect
all emitted output (tests/test_palace.py:L9-L12).

## State A — Palace Directory Missing

When the palace directory path does not exist, the helper returns `None`,
emits a message containing the text `No palace found`, and emits guidance
referencing `mempalace init` (tests/test_palace.py:L15-L24). The helper MUST
NOT create the directory as a side effect; the path remains nonexistent after
the call (tests/test_palace.py:L25-L26).

## State B — Directory Exists But No Database File

When the directory exists but contains no `chroma.sqlite3` database file, the
helper returns `None` and emits a message containing `has no chroma.sqlite3 yet`
(tests/test_palace.py:L29-L44). Critical invariant: the helper MUST NOT reach
the storage backend in a way that triggers lazy database creation. After the
call the directory MUST remain empty, i.e. no files are created — a read-only
inspection stays read-only (tests/test_palace.py:L30-L46).

## State C — Database Exists But Collection Never Created

When the database file `chroma.sqlite3` exists (the underlying store has been
initialized) but the collection has never been created, the helper returns
`None`, emits a message containing `initialized but empty`, and emits guidance
referencing `mempalace mine` (tests/test_palace.py:L49-L61).

## State D — Healthy Palace

When the palace is healthy (collection exists and opens successfully), the
helper returns a non-`None` collection object and emits NOTHING; the healthy
path is silent — `lines` is empty (tests/test_palace.py:L79-L89).

## State E — Unexpected Error Opening Backend

When opening the collection raises an unexpected error (e.g. a generic runtime
failure) after the database-file guard has passed, the helper returns `None`,
emits a message containing `Error opening palace`, and emits a hint referencing
`repair-status` (tests/test_palace.py:L92-L108). The database-file guard is
satisfied by the mere presence of an (empty) `chroma.sqlite3` file
(tests/test_palace.py:L97).

## Unknown Backend Name

When an unknown backend is selected (e.g. a typo in the `MEMPALACE_BACKEND`
environment variable or `--backend` flag), the helper returns `None` and
surfaces a CLI state message rather than an escaping error/stack trace. The
emitted output contains `Unknown backend selected` and includes the offending
backend name (here `does_not_exist`) (tests/test_palace.py:L64-L76). The
backend selection is read from the `MEMPALACE_BACKEND` environment variable
(tests/test_palace.py:L70).

## Default Output Sink

When the `out` argument is omitted (`None`), emitted messages are routed to
standard output. For a missing palace directory the helper returns `None` and
the standard output contains `No palace found` (tests/test_palace.py:L111-L118).

## Backend-Raised PalaceNotFoundError

If the backend raises a bare "palace not found" error after the filesystem
guards have passed (a rare race or backend-internal not-found condition), the
helper still emits the State A message containing `No palace found` and returns
`None` (tests/test_palace.py:L121-L138).

## Backend Closed Error Propagation

A "backend closed" error represents a programmer error (the caller violated the
backend lifecycle), not a palace-state UX condition. The helper MUST propagate
this error to the caller instead of swallowing it into the State E
`repair-status` hint. When the collection-open path raises a backend-closed
error, the helper re-raises that same error type rather than returning `None`
or emitting a state message (tests/test_palace.py:L141-L164).

## Collection-Not-Initialized Subclass Distinction

The "collection not initialized" error is a subclass of the broader
"palace not found" error. The helper MUST distinguish them: when the
collection-not-initialized error is raised, the helper emits the
`initialized but empty` message and returns `None`, and MUST NOT emit the
broader `No palace found` message (tests/test_palace.py:L167-L185).

## Error-Type Surface (Observable Contract)

The helper distinguishes at least four error/state categories from the backend,
each mapped to a distinct emitted message:
- "palace not found" → `No palace found` (tests/test_palace.py:L121-L138)
- "collection not initialized" (a subclass of the above) → `initialized but empty`
  (tests/test_palace.py:L167-L185)
- "backend closed" → propagated, not caught (tests/test_palace.py:L141-L164)
- any other unexpected error → `Error opening palace` + `repair-status`
  (tests/test_palace.py:L99-L108)
