# Behavior Spec: Antigravity `install.sh` end-to-end tests

This file is an end-to-end test suite that pins the externally observable contract
of the shell installer at `hooks/antigravity/install.sh` (relative to repo root)
(tests/test_antigravity_hooks_install.py:L1-L13). Each behavior below is a contract
the installer-under-test MUST satisfy; the test file is the executable specification.

## Test harness / system under test

- The system under test is the bash script located at `<repo_root>/hooks/antigravity/install.sh`,
  where `<repo_root>` is the parent of the directory containing the test file
  (tests/test_antigravity_hooks_install.py:L25-L26).
- The installer is invoked as a subprocess: `bash <install.sh> --install-dir <dir> [extra args...]`.
  Output (stdout/stderr) is captured as text, and a per-invocation timeout of 30 seconds
  applies by default (tests/test_antigravity_hooks_install.py:L46-L66).
- Invocations may be run with an arbitrary current working directory (`cwd`); when set,
  the installer resolves relative paths against that directory (tests/test_antigravity_hooks_install.py:L48-L66, L262-L275).
- The entire suite is skipped on Windows (`os.name == "nt"`) because the installer is a
  bash script relying on POSIX path semantics; Windows uses a separate code path
  (tests/test_antigravity_hooks_install.py:L28-L32).

## Installed file layout contract

A successful real install (no extra flags) MUST exit with return code 0
(tests/test_antigravity_hooks_install.py:L94-L98) and produce exactly this set of regular
files under the install directory, each of which MUST be a real file and MUST NOT be a
symlink (tests/test_antigravity_hooks_install.py:L34-L43, L69-L73):

- `plugin.json`
- `mcp_config.json`
- `README.md`
- `hooks.json`
- `skills/mempalace/SKILL.md`
- `hooks/lib/common.sh`
- `hooks/mempal_save_hook_antigravity.sh`
- `hooks/mempal_wake_hook_antigravity.sh`

(tests/test_antigravity_hooks_install.py:L34-L43)

The two hook scripts `hooks/mempal_save_hook_antigravity.sh` and
`hooks/mempal_wake_hook_antigravity.sh` MUST be executable (have the user-execute bit set)
after install (tests/test_antigravity_hooks_install.py:L126-L136).

## `hooks.json` rendering contract (on-disk format)

`hooks.json` is valid JSON (tests/test_antigravity_hooks_install.py:L106). Its structure is a
top-level object; each value that is itself an object ("namespace payload") maps keys to
lists of "entry" objects, and each entry object has a `command` string field
(tests/test_antigravity_hooks_install.py:L109-L116). At least one such `command` entry MUST exist
in a rendered `hooks.json` (tests/test_antigravity_hooks_install.py:L116). Top-level values that are
not objects are ignored by the contract (tests/test_antigravity_hooks_install.py:L110-L111).

Every rendered `command` string MUST:
- contain no `__PLUGIN_DIR__` placeholder (the template token must be fully substituted)
  (tests/test_antigravity_hooks_install.py:L101-L102, L119),
- be an absolute path (begin with `/`) (tests/test_antigravity_hooks_install.py:L120),
- live under the install directory, i.e. begin with `<install_dir>/`
  (tests/test_antigravity_hooks_install.py:L117, L121-L123).

## Relative install-dir absolutization

When `--install-dir` is given a relative path (e.g. `build/agy-out/mempalace`), the installer
resolves it against the current working directory at invocation time and creates the directory
at that absolute location (tests/test_antigravity_hooks_install.py:L262-L275). The rendered
`hooks.json` command paths MUST be absolute (begin with `/`) and MUST retain the relative
path segment context (the original relative subpath substring appears within the command)
(tests/test_antigravity_hooks_install.py:L276-L288).

## `--dry-run` contract

With `--dry-run`, the installer MUST exit 0 (tests/test_antigravity_hooks_install.py:L82-L83) and MUST
be fully side-effect free: it MUST NOT create the install directory or any of its files
(tests/test_antigravity_hooks_install.py:L79-L86). Its stdout MUST contain the literal substring
`DRY-RUN` at least once (tests/test_antigravity_hooks_install.py:L87-L88).

## Idempotency contract

Running the installer a second time against the same already-installed directory MUST exit 0
and leave every expected file byte-identical to the first run; each file's mode bits MUST also
be preserved across the re-run (tests/test_antigravity_hooks_install.py:L142-L173). The first run
emits `wrote:` in stdout for files it writes; the second (idempotent) run MUST NOT emit any
`wrote:` line, i.e. it is a no-op that re-writes nothing
(tests/test_antigravity_hooks_install.py:L176-L187). The idempotency derives from a content-compare
(`cmp`) gate that avoids rewriting unchanged files (tests/test_antigravity_hooks_install.py:L142-L147,
L167-L169).

## `--uninstall` contract

A successful uninstall of a directory that the installer itself produced MUST exit 0 and remove
the install directory entirely (tests/test_antigravity_hooks_install.py:L193-L199).

Uninstalling a non-existent target directory is a graceful no-op that exits 0
(tests/test_antigravity_hooks_install.py:L252-L256).

`--uninstall` enforces safety guards and MUST refuse (exit with non-zero return code) without
deleting anything in these cases:

- Basename mismatch: the target directory's basename is not `mempalace`. This refusal holds
  even if the directory contains a `plugin.json` naming `mempalace`. The directory and any
  unrelated files inside it MUST survive (tests/test_antigravity_hooks_install.py:L202-L222).
- Missing plugin marker: the target directory exists but has no `plugin.json`. The directory
  and unrelated files MUST survive (tests/test_antigravity_hooks_install.py:L225-L234).
- Wrong plugin name: the target directory has a `plugin.json` whose `name` field is some other
  plugin (not `mempalace`). The directory and unrelated files MUST survive
  (tests/test_antigravity_hooks_install.py:L237-L249).

The `plugin.json` consulted by these guards is a JSON object with a `name` field; `name` equal
to `mempalace` is the expected value (tests/test_antigravity_hooks_install.py:L213, L242, L237-L243).

## `--help` and argument-parsing contract

`bash <install.sh> --help` MUST exit 0, print a usage message containing the literal substring
`Usage:` on stdout or stderr, and MUST NOT create the install directory
(tests/test_antigravity_hooks_install.py:L294-L305).

An unknown/unrecognized flag (e.g. `--this-flag-does-not-exist`) MUST cause a non-zero exit
(fail loudly rather than silently ignore) and MUST NOT create the install directory
(tests/test_antigravity_hooks_install.py:L308-L324).
