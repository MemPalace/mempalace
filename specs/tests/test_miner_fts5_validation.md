# Spec: End-of-Mine FTS5 Validation Test Suite

This is a behavior-verification test module. It does not define product code; it
asserts the externally observable contract of the end-of-mine FTS5 validation
feature across the validator helper, the three mine entry points, the CLI
dispatcher, and the `MineValidationError` type (tests/test_miner_fts5_validation.py:L1-L8).
The specification below describes the contracts that the system-under-test MUST
satisfy for these tests to pass; any equivalent implementation must honor them.

## Overview of the validated feature

A mine operation MUST NOT report success on a palace whose on-disk SQLite store
(`chroma.sqlite3`) is left in a malformed state. A validation hook,
`_validate_palace_fts5_after_mine`, runs an integrity check at the end of every
non-dry-run mine and raises `MineValidationError` so the mine command can surface
the same recovery banner that the repair command prints
(tests/test_miner_fts5_validation.py:L1-L8).

## On-disk palace contract

A palace is a directory. After a real mine that creates at least one drawer, the
palace directory contains a file named `chroma.sqlite3`
(tests/test_miner_fts5_validation.py:L27-L41, L141). A built palace exposes a
collection named `mempalace_drawers` into which drawer records (id, document text,
and metadata containing `wing` and `room` keys) are upserted
(tests/test_miner_fts5_validation.py:L34-L39). The SQLite store contains an FTS5
shadow data table named `embedding_fulltext_search_data` with rows keyed by an
integer `id` and holding a `block` blob; this name is treated as a private,
version-dependent detail and may be absent (tests/test_miner_fts5_validation.py:L72-L102).

## `_validate_palace_fts5_after_mine(palace_path: str) -> None`

The validator takes a palace path string and returns nothing (a null/None value)
when validation passes (tests/test_miner_fts5_validation.py:L127-L132).

Clean-palace behavior: given a freshly built palace with one drawer whose
integrity check reports "ok", the validator returns silently without raising
(tests/test_miner_fts5_validation.py:L127-L132).

Missing-store behavior: given a palace path where `chroma.sqlite3` does not exist
(directory missing or never written), the validator returns silently (null)
without raising. The underlying integrity primitive short-circuits on a missing
path (tests/test_miner_fts5_validation.py:L354-L360).

Corruption behavior (whole-file / page-level): when mid-file SQLite pages are
corrupted such that the integrity quick-check fails, the validator raises
`MineValidationError`. The raised error's `palace_path` equals the supplied path,
its `errors` list is non-empty, and the joined lowercased error text contains
either `"malformed"` or `"quick_check failed"`
(tests/test_miner_fts5_validation.py:L138-L150).

Corruption behavior (FTS5-only): when only an FTS5 segment blob is replaced with
garbage while the main database pages remain valid, the validator raises
`MineValidationError` whose joined lowercased `errors` text contains `"fts5"`
(tests/test_miner_fts5_validation.py:L153-L163).

Handle-release ordering invariant: before re-opening the store read-only to run
the integrity check, the validator MUST first release/close any open ChromaDB
file handles. Observably, the close operation (`_close_chroma_handles`) is invoked
strictly before the integrity-error scan (`sqlite_integrity_errors`); the test
asserts the call order is exactly `["close", "quick_check"]`
(tests/test_miner_fts5_validation.py:L396-L423). The integrity scan is performed
via a primitive named `sqlite_integrity_errors` residing in the repair module
(tests/test_miner_fts5_validation.py:L405-L417).

## `MineValidationError` type

Construction takes a palace path string and an errors collection
(tests/test_miner_fts5_validation.py:L176-L178, L342-L344).

Constructor validation: constructing with an empty errors collection MUST raise a
value error whose message matches `"at least one error"`; constructing with an
empty (blank) palace path MUST raise a value error whose message matches
`"non-empty palace_path"` (tests/test_miner_fts5_validation.py:L337-L344).

Attributes: a constructed error exposes `palace_path` (the supplied path string)
and `errors`. The `errors` attribute is an immutable sequence (a tuple); attempts
to mutate it (e.g. append) raise an attribute error
(tests/test_miner_fts5_validation.py:L347-L351, L147, L297).

## Mine entry points and dry-run guarantee

Three mine entry points exist, each accepting a palace path, a source directory, a
wing, an agent identifier, an integer `limit`, and a boolean `dry_run`:
`miner.mine` (project files, parameter `project_dir`)
(tests/test_miner_fts5_validation.py:L233-L240); `convo_miner.mine_convos`
(conversations, parameter `convo_dir`, requires a `wing`)
(tests/test_miner_fts5_validation.py:L381-L388); and `format_miner.mine_formats`
(extract mode, parameter `format_dir`, requires a `wing`)
(tests/test_miner_fts5_validation.py:L475-L482).

Dry-run skips validation: when any of the three entry points is called with
`dry_run=True`, it MUST NOT invoke `_validate_palace_fts5_after_mine`. Each test
replaces the validator with a spy and asserts it was never called because no
writes occurred (tests/test_miner_fts5_validation.py:L217-L242, L363-L390,
L457-L484).

Non-dry-run invokes validation: when a non-dry-run mine completes, the validator
is invoked with the palace path. In the full-chain tests the validator spy records
exactly `[str(palace)]`, proving the validator is the explicit source of any
`MineValidationError` raised (tests/test_miner_fts5_validation.py:L248-L297,
L527-L565).

KeyboardInterrupt does not trigger validation: if a `KeyboardInterrupt` (a
non-`Exception` interrupt) is raised mid-mine, it routes through the interrupt
handler and bypasses the end-of-mine validation branch. The per-file exception
handler does not catch the interrupt; it propagates to the outer handler. The
validator MUST NOT be called in this case (asserted call list is empty)
(tests/test_miner_fts5_validation.py:L487-L524).

## Partial-progress banner suppression invariant

When `_validate_palace_fts5_after_mine` raises inside the project miner's internal
mine implementation, that implementation MUST re-raise the `MineValidationError`
directly and MUST NOT print the partial-progress banner containing the text
`"Mine aborted by exception"`. That banner is reserved for true mid-loop failures
and would otherwise duplicate the command-level recovery banner
(tests/test_miner_fts5_validation.py:L300-L334).

## CLI command `cmd_mine` — error surfacing contract

`cmd_mine` reads an arguments object with fields: `palace`, `dir`, `mode`, `wing`,
`agent`, `limit`, `dry_run`, `no_gitignore`, `include_ignored`, `extract`, and
`redetect_origin` (tests/test_miner_fts5_validation.py:L105-L121). The `mode`
field selects the entry point: `"project"` dispatches to the project miner,
`"convos"` to the conversation miner, and `"extract"` to the format miner
(tests/test_miner_fts5_validation.py:L180-L183, L202-L205, L445-L448).

When the selected entry point raises `MineValidationError`, `cmd_mine` MUST exit
the process with status code 1 (tests/test_miner_fts5_validation.py:L182-L185,
L204-L207, L447-L450).

On that exit, `cmd_mine` MUST emit a recovery banner (to standard out and/or
standard error) that includes the literal strings `"SQLite-layer corruption
detected"` and `"mempalace repair --yes"`
(tests/test_miner_fts5_validation.py:L186-L191, L208-L211, L451-L454). For the
project-mode path the banner additionally includes `"PRAGMA quick_check"` and
echoes the underlying error text `"malformed inverted index"`
(tests/test_miner_fts5_validation.py:L186-L191). All three modes (project,
convos, extract) surface the same recovery banner
(tests/test_miner_fts5_validation.py:L194-L211, L429-L454).

## Test corruption fixtures (observable preconditions, not product contract)

Page-mangle fixture: corrupts 4 contiguous SQLite pages (page size 4096 bytes,
16384 bytes total) starting at a 4096-aligned offset that is at least two pages
past the file start and within file bounds, writing the repeated byte pattern
`DE AD BE EF`; it requires the database be large enough to mangle
(tests/test_miner_fts5_validation.py:L44-L59).

FTS5 segment-corruption fixture: targets the `embedding_fulltext_search_data`
table, selects a row with `id > 10` (falling back to the first row), and overwrites
its `block` blob with `DE AD BE EF` garbage of equal length. The test is skipped
(not failed) when the shadow table is absent, when no segment rows exist, or when
the SQLite build refuses direct writes to the FTS5 shadow table (error text "may
not be modified") (tests/test_miner_fts5_validation.py:L62-L102).

<promise>SPEC_WRITTEN path=specs/tests/test_miner_fts5_validation.md citations=33</promise>
