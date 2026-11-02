# Pinned regression adapter protocol, version 1

This optional developer tool proves a selected regression against immutable Git
objects. It does not load API keys or contact a review service.

```python
from tools.review_support.regression import RegressionBlocked, verify_regression

evidence = verify_regression(
    repo_path, base_sha, head_sha, selected_tests, config_path, evidence_path,
    allow_execution=True,
)
```

`base_sha` and `head_sha` must be full 40-hex **commit** object IDs in a normal
local checkout. Uppercase hex is normalized to lowercase. The caller supplies
the pinned merge-base as `base_sha`; this module does not resolve a branch, fetch
objects, choose a merge-base, or obtain the reviewed diff. The caller must bind
its reviewed diff and pinned target-base to these same commits and run this
preflight before credentials or budget reservation.

`selected_tests` is a nonempty list (or tuple) of at most 256 explicit repository
paths, including any needed fixtures/helpers. Every selected path must be added
or changed between these commits, tracked as a head regular file, and below an
allowed test root. Directories, deleted/unchanged files, duplicates, absolute
paths, traversal, `.git` components, backslashes and colon paths are refused.
Only selected head files are overlaid on base; production files remain from
base. New tests and fixtures are included. An administrator must configure only
test/fixture roots, never production roots: file purpose cannot be inferred
from a filename. Unselected changed helpers are not copied automatically.

The caller's checkout, index, branch and uncommitted files are untouched.
Snapshots use `ls-tree` and unfiltered `cat-file` blob reads with replacement
objects disabled, without checkout hooks, archive attributes or worktree code.
Version 1 conservatively refuses **any** symlink, submodule, non-UTF-8 or unsafe
path in either snapshot, not just selected paths. Each snapshot is limited to
50,000 regular files, 32 MiB per file and 256 MiB total. Tracked snapshot file
content and executable modes are checked again after each run. Temporary
snapshots and reports are removed on success, refusal and timeout.

## Trusted configuration

The caller must explicitly opt in with `allow_execution=True` on **each call**.
The module executes PR-controlled tests on the host through an external adapter.
The adapter, not this module, owns dependency provisioning, actual process,
filesystem and network isolation, and faithful test-result classification.
This module is not a sandbox. A config declaration is an administrator's trust
statement, not verification that isolation exists. Do not opt in without a
trusted adapter implementing that policy. Test code must not be allowed to forge
the adapter's report or access credentials through the filesystem; an empty
environment alone cannot prevent those actions.

The JSON config is trusted operator input **outside every Git checkout or bare
repository**, never a file supplied by the PR. It must be a regular nonsymlink
file with one hard link, owned by the current user or root, without group/world
write permission. The same requirements apply to each argv file. Paths must be
canonical, with no symlink components. Use resolved interpreter paths where an
installation normally provides a symlink. Exactly these keys are required:

```json
{
  "schema_version": 1,
  "runner_argv": ["/opt/regression/bin/python3", "/opt/regression/adapter.py"],
  "allowed_test_roots": ["tests"],
  "suite": {"profile": "unit", "version": "1"},
  "isolation": {
    "filesystem": "Describe the adapter's filesystem and credential isolation.",
    "network": "Describe the adapter's network restrictions.",
    "processes": "Describe process isolation and descendant cleanup."
  },
  "timeout_seconds": 60
}
```

`runner_argv` contains 1–32 absolute external regular-file paths: an executable
and optional file operands. Version 1 accepts no flags, inline commands, module
names, relative operands or shell expressions. The first file must be
executable; a single trusted executable adapter is also supported. All argv
strings are at most 4096 printable characters. Roots contain 1–32 distinct
normalized relative directory paths; `.` is not allowed. Suite profile/version
are nonempty printable strings up to 512 characters, and each isolation policy
is a nonempty printable string up to 2048 characters. Timeout is an integer
1–300 seconds, separately applied to each invocation. Config is capped at
64 KiB. Unknown keys, duplicate JSON keys and unsupported versions are refused.

## Request on stdin

The utility invokes the configured argv directly twice, without a shell, once
per snapshot. `cwd` equals the absolute `snapshot_cwd`. JSON stdin has exactly:

```json
{
  "schema_version": 1,
  "snapshot_cwd": "/private/work/base",
  "commit_sha": "1111111111111111111111111111111111111111",
  "selected_tests": ["tests/test_regression.py", "tests/fixture.txt"],
  "report_path": "/private/work/base.json",
  "suite": {"profile": "unit", "version": "1"}
}
```

The BASE `commit_sha` names base production code even though selected test files
come from head. HEAD receives its own exact SHA. The selected path list and suite
identity are identical for both requests. The adapter must collect and actually
execute tests from those paths, treating fixture paths as supporting inputs.
It must normalize runnable IDs as `selected/path::framework-specific-id` and
report actual assertion failures separately from collection, import, setup,
teardown, timeout, skip, signal and other infrastructure problems. Setup and
teardown assertion errors are infrastructure failures too. Failure to collect
an intended runnable test is an adapter error, never a pass. Fixture paths need
not produce their own runnable IDs.

The report file is precreated as an empty regular file with mode `0600` outside
the snapshot. Adapter stdout/stderr are discarded; diagnostics belong in the
bounded report. The adapter inherits **no caller environment**, only fixed
`PATH`, locale, Git safety flags and Python no-user-site/no-bytecode flags, plus
disposable `HOME` and `TMPDIR`/`TMP`/`TEMP`. No credential file is read. The adapter
must clean up descendants before returning; on a POSIX timeout the module kills
the adapter process group. Home/temp directories are private to each run and are
not shared between BASE and HEAD.

## Report and exit status

The adapter writes UTF-8 JSON with exactly these keys, at most 1 MiB, to the
provided regular nonsymlink, single-link report path:

```json
{
  "schema_version": 1,
  "commit_sha": "1111111111111111111111111111111111111111",
  "suite": {"profile": "unit", "version": "1"},
  "selected_tests": ["tests/test_regression.py", "tests/fixture.txt"],
  "collected_ids": ["tests/test_regression.py::Regression.test_answer"],
  "counts": {"collected": 1, "passed": 0, "failed": 1, "skipped": 0, "errors": 0},
  "failed_ids": ["tests/test_regression.py::Regression.test_answer"],
  "failures": [{
    "id": "tests/test_regression.py::Regression.test_answer",
    "kind": "assertion",
    "message": "AssertionError: expected corrected behavior"
  }]
}
```

Commit, suite and selected paths must match the request exactly. Collection
contains 1–10,000 distinct printable IDs of at most 2048 characters, each prefixed
by a selected path and `::`, with a nonempty suffix. Counts are integers
(booleans are invalid), each 0–10,000. Collected count equals ID count and the sum
of pass/fail/skip/error counts. Failed IDs are distinct, belong to collection,
and match the failed count. There must be exactly one diagnostic per failed ID,
with `kind` equal to `assertion` and a nonempty message of at most 16,384
characters. Other failure kinds, skips, errors or inconsistent reports block.
An adapter may report infrastructure via nonzero `errors` or exit >=2; it cannot
encode that as an assertion failure. Untrusted reports alone are never proof.

BASE must exit **1**, have at least one actual assertion failure and have zero
skips/errors. HEAD must exit **0**, pass every collected test and have zero
failures/skips/errors. Both runs must collect the same ID set and suite version.
Both-pass blocks. Wrong exits, signals, timeouts, stale revisions, invalid JSON,
empty collection and schema violations block. An invalid BASE may stop execution
before HEAD; successful proof always requires both invocations.

## Private evidence and refusal

`evidence_path` is a fresh, canonical path outside **every** Git checkout or bare
repository, with an existing current-user/root-owned parent lacking group/world
write permission. Existing destinations (including caller-written PASS JSON),
symlinks and paths inside checkouts are refused. Workspaces are fresh private
directories below this parent; evidence is outside their snapshots. Evidence
is published atomically without replacing an existing file, with mode `0600`.
The `.regression-` path-component prefix is reserved for private workspaces;
config, adapter and evidence paths containing it are refused, including paths
inside a concurrent or previous snapshot.

On success the returned JSON-serializable dict equals the published evidence.
It contains `schema_version: 1`, `status: "PASS"`, `repo_path`, normalized
`base_sha`/`head_sha`, `config_sha256`, `suite`, `isolation`, `selected_tests`,
`trees` (original base/head Git tree IDs), `snapshots` (base/head manifest SHA256
and selected-file SHA256 maps), `runs` (base/head `exit_code` and validated
`report`) and `evidence_path`. A manifest digest hashes the UTF-8 encoding of
Python `json.dumps(mapping, sort_keys=True)`, mapping each path to its
`[Git-mode-string, content-sha256]`; BASE's manifest describes the test overlay.

Every ordinary refusal raises `RegressionBlocked`. Its `.evidence` dict has
`status: "BLOCKED"`, a `reason` and whatever binding/results were available.
It is also written privately if the output path was validated and can still be
published safely. Unsafe/unavailable output destinations cannot receive an
artifact. A BLOCKED exception, missing evidence, or failed evidence write must
never be treated as proof. Evidence may contain private paths and assertion
diagnostics: attach it to private verdict metadata, never to a public PR.

Focused verification uses only disposable Git repositories and a fake trusted
adapter that actually executes Python unittest. That controlled fixture does
not implement a production sandbox. Run the one file with:

```sh
python3 -m unittest discover -s tests -p test_review_regression.py -v
```
