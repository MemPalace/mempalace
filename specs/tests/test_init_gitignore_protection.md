# Spec: gitignore protection on `init`

This file is a regression test suite verifying the behavior of the function under test
`_ensure_mempalace_files_gitignored(dir)`. It addresses the requirement that `init <dir>`
must not leave `mempalace.yaml` and `entities.json` exposed to accidental commit
(tests/test_init_gitignore_protection.py:L1-L11). The behavioral contract described below is
that of the function under test, as observed through these tests.

## Function under test

`_ensure_mempalace_files_gitignored(dir)` takes a single directory path and returns a boolean
(tests/test_init_gitignore_protection.py:L11,L20,L26). It ensures the two protected filenames
`mempalace.yaml` and `entities.json` are present in `<dir>/.gitignore` when `<dir>` is a git
repository (tests/test_init_gitignore_protection.py:L4-L7,L28-L29).

## Git-repository detection

A directory is treated as a git repository if and only if it contains a `.git` subdirectory; the
test marks a directory as a repo solely by creating `.git` as a directory, without invoking git
(tests/test_init_gitignore_protection.py:L14-L16).

## Behavior contracts

### Non-git directory: no-op
When `<dir>` is not a git repository (no `.git` present), the function returns `False` and does
not create a `.gitignore` file (tests/test_init_gitignore_protection.py:L19-L21).

### Git repo, no existing `.gitignore`: create with both entries
When `<dir>` is a git repository and no `.gitignore` exists, the function returns `True` and
creates `<dir>/.gitignore` whose contents include the substrings `mempalace.yaml`,
`entities.json`, and `issue #185` (an explanatory marker comment)
(tests/test_init_gitignore_protection.py:L24-L30).

### Git repo, partial `.gitignore`: append only missing entries
When `.gitignore` already exists and contains some but not all protected entries, the function
returns `True` and appends only the missing entries. An entry already present (e.g.
`mempalace.yaml`) must not be duplicated — it appears exactly once. A missing entry (e.g.
`entities.json`) is added. All pre-existing unrelated entries (e.g. `node_modules/`) are preserved
(tests/test_init_gitignore_protection.py:L33-L43).

### Git repo, both entries already present: idempotent no-op
When `.gitignore` already contains both protected entries, the function returns `False` and leaves
the file contents byte-for-byte unchanged (tests/test_init_gitignore_protection.py:L46-L51).

### Git repo, `.gitignore` without trailing newline: clean append
When the existing `.gitignore` ends without a trailing newline (e.g. content `dist`), the function
returns `True` and the original entry is preserved on its own line (the resulting content contains
`dist\n`), i.e. the newly appended block is not glued onto the existing final line. Both
`mempalace.yaml` and `entities.json` are present afterward
(tests/test_init_gitignore_protection.py:L54-L62).

## Invariants summary

- Return value is `True` exactly when the function modifies or creates `.gitignore`; `False` when
  no change is made (non-git directory, or both entries already present)
  (tests/test_init_gitignore_protection.py:L20,L26,L36,L50,L57).
- Protected filenames are `mempalace.yaml` and `entities.json`
  (tests/test_init_gitignore_protection.py:L28-L29).
- Pre-existing `.gitignore` content is always preserved; entries are never removed or duplicated
  (tests/test_init_gitignore_protection.py:L39,L43,L51,L60).

## Side effects

The function reads and may write `<dir>/.gitignore` on the filesystem; no other paths are touched
(tests/test_init_gitignore_protection.py:L21,L27,L35-L37,L49-L51).
