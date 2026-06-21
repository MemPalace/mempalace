# Spec: `tests/test_init.py` — Package-init `sys.path` leak guard

This module is a test suite verifying behavior that the `mempalace` package
performs **at import time** (the package's `__init__`-level guard), specifically
the filtering of leaked `PYTHONPATH` entries out of the runtime module search
path. The tests are the observable contract for that import-time behavior; an
implementation in any language must satisfy these guarantees when the package /
runtime is loaded (tests/test_init.py:L1-L1).

## Sentinel constant

A sentinel prefix string `"/__mempalace_leak_test_sentinel__"` is used to mark
fabricated search-path entries so the tests can detect leakage without coupling
to the production normalization logic (tests/test_init.py:L10-L10, L34-L35).

## Behavior under test: import-time `sys.path` filtering

The package, when imported, MUST remove any module-search-path entry whose value
contains the sentinel prefix, so that transitive imports do not pull compiled
extensions from a leaked `PYTHONPATH` (tests/test_init.py:L26-L32).

### Contract 1 — sentinel-prefixed entries are stripped from the search path

For each of the following `PYTHONPATH` environment-variable inputs, after the
package is imported in a fresh subprocess, no entry remaining in the runtime
search path may contain the sentinel prefix (tests/test_init.py:L13-L25, L48-L48, L71-L71):

- `"/__mempalace_leak_test_sentinel__/single"` — single entry (id `single`) (tests/test_init.py:L16-L16, L24-L24).
- `"/__mempalace_leak_test_sentinel__/a<SEP>/__mempalace_leak_test_sentinel__/b"` — two entries joined by the platform path separator (id `multi`) (tests/test_init.py:L17-L17, L24-L24).
- `"/__mempalace_leak_test_sentinel__/with-trailing<DIRSEP>"` — entry with a trailing directory separator (id `trailing-sep`) (tests/test_init.py:L18-L18, L24-L24).
- `"<SEP>/__mempalace_leak_test_sentinel__/leading-sep"` — a leading path separator producing an empty leading element (id `leading-pathsep`) (tests/test_init.py:L19-L19, L24-L24).
- `"."` — current directory marker (id `dot`) (tests/test_init.py:L20-L20, L24-L24).
- `""` — empty string (id `empty`) (tests/test_init.py:L21-L21, L24-L24).
- unset / absent `PYTHONPATH` (id `unset`) (tests/test_init.py:L22-L22, L24-L24).

`<SEP>` is the platform path-list separator and `<DIRSEP>` is the platform
directory separator (tests/test_init.py:L17-L18). The `dot`, `empty`, and `unset`
cases exist to exercise the early-return and collision paths without crashing
(tests/test_init.py:L36-L37).

### Contract 2 — the environment variable is preserved verbatim

Importing the package MUST NOT mutate the `PYTHONPATH` environment variable. After
import, the in-process value of `PYTHONPATH` must equal the input exactly: the
original string when set, or absent/null when unset. This lets host applications
that embed the package as a library retain their environment for their own
subprocesses; the environment strip lives instead in the CLI/MCP entry points
(tests/test_init.py:L27-L32, L66-L70).

### Contract 3 — the filter must not over-strip (package stays importable)

The filter MUST NOT remove the search-path entry that is the parent directory of
the package itself. After import, the package's parent directory (the directory
containing the package, compared after path normalization and case folding) must
still be present somewhere on the search path, so the package remains importable
(tests/test_init.py:L49-L51, L72-L76).

## Behavior under test: current-directory marker vs. `"."` collision

When `PYTHONPATH` is set to `"."`, the dot entry normalizes to the same value as
the implicit empty-string current-working-directory marker on the search path. The
filter MUST remove the literal `"."` entry while leaving the implicit
current-directory marker (the empty string `""`) intact (tests/test_init.py:L79-L82).

Concretely, after importing the package in a fresh subprocess with `PYTHONPATH="."`
(tests/test_init.py:L83-L84):

- The empty-string CWD marker `""` MUST still be present in the search path (tests/test_init.py:L87-L87, L99-L99).
- The literal `"."` entry MUST be absent from the search path (tests/test_init.py:L88-L88, L100-L100).

## Test harness / observable mechanics

Each test launches a fresh runtime subprocess with a copied-and-modified
environment, imports the package, and prints diagnostic lines that the test then
asserts on (tests/test_init.py:L38-L59, L83-L96). For the `unset` case the
`PYTHONPATH` key is removed from the child environment; otherwise it is set to the
parametrized value (tests/test_init.py:L39-L42).

The first test's probe program emits three labeled lines from the child process
(tests/test_init.py:L43-L52):

- `ENV: <repr of PYTHONPATH or None>` (tests/test_init.py:L47-L47).
- `SENTINEL_IN_PATH: <True|False>` — whether any search-path entry contains the sentinel prefix (tests/test_init.py:L48-L48).
- `MEMPALACE_PARENT_PRESENT: <True|False>` — whether the package's normalized parent directory is present among non-empty search-path entries (tests/test_init.py:L49-L51).

The second test's probe program emits two labeled lines (tests/test_init.py:L85-L89):

- `CWD_IN_PATH: <True|False>` — whether `""` is in the search path (tests/test_init.py:L87-L87).
- `DOT_IN_PATH: <True|False>` — whether `"."` is in the search path (tests/test_init.py:L88-L88).

### Exit code / pass conditions

Both tests require the child subprocess to exit with return code `0`
(tests/test_init.py:L64-L64, L98-L98). The first test passes only when the child
output contains `ENV: <expected>`, `SENTINEL_IN_PATH: False`, and
`MEMPALACE_PARENT_PRESENT: True`, where `<expected>` is the repr of the input value
(or of null when unset) (tests/test_init.py:L67-L76). The second test passes only
when the child output contains `CWD_IN_PATH: True` and `DOT_IN_PATH: False`
(tests/test_init.py:L99-L100). On any failure, a diagnostic string including the
input, return code, stdout, and stderr is included in the assertion message
(tests/test_init.py:L60-L63, L97-L97).
