# Spec: Cursor Plugin Manifest Contract Tests

This module is a contract test suite that inspects the on-disk Cursor plugin
artifacts shipped with the repository. All checks are pure file inspection — no
subprocesses, no network — and validate the structural contracts a Cursor user
relies on once they install the plugin (tests/test_cursor_plugin_manifest.py:L1-L27).

## Path Constants and Layout Contract

The repository root is the directory two levels above this test file; the plugin
directory is `<repo>/.cursor-plugin/` (tests/test_cursor_plugin_manifest.py:L38-L39).
The following files live under the plugin directory: `plugin.json` (manifest),
`marketplace.json`, `mcp.json`, and `README.md`
(tests/test_cursor_plugin_manifest.py:L40-L43).

Plugin component directories are located at the **repo root**, not inside
`.cursor-plugin/`: `skills/`, `commands/`, and `rules/`. The canonical
discovery location is the plugin root (= repo root); `.cursor-plugin/` is not used
for component directories (tests/test_cursor_plugin_manifest.py:L45-L50).

## Shared Validation Rules

A plugin identifier (name) must match lowercase kebab-case: it begins and ends with
an alphanumeric character and contains only lowercase alphanumerics, hyphens, and
periods in between. Regex: `^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$`
(tests/test_cursor_plugin_manifest.py:L63-L66).

Manifest-declared paths are "safe relative" if and only if: the value is a non-empty
string, the path is not absolute, and no path component equals the forbidden fragment
`..` (tests/test_cursor_plugin_manifest.py:L72-L72, L99-L106).

### Frontmatter Parsing Contract

Markdown files with YAML frontmatter are split into a `(meta, body)` pair. Frontmatter
is recognized only if the file begins (at byte 0) with the literal line `---\n`. The
frontmatter block ends at the next occurrence of `\n---\n` (searched starting after the
opening delimiter). The text between the delimiters is parsed as YAML; the body is
everything after the closing delimiter (tests/test_cursor_plugin_manifest.py:L78-L96).

If the file does not start with `---\n`, or no closing `---\n` is found, the result is
an empty `meta` dict and the full text as body (tests/test_cursor_plugin_manifest.py:L86-L90).
If the YAML parses to something other than a dict (and is non-null), this is an error
(tests/test_cursor_plugin_manifest.py:L93-L96). A null/empty YAML block yields an empty
meta dict (tests/test_cursor_plugin_manifest.py:L93-L93).

## plugin.json Contract

- `plugin.json` must exist and be a regular file (tests/test_cursor_plugin_manifest.py:L113-L114).
- It must be valid JSON (tests/test_cursor_plugin_manifest.py:L116-L117).
- It must have a non-empty string `name` field — the only field required by Cursor's
  schema (tests/test_cursor_plugin_manifest.py:L119-L123).
- The `name` must satisfy the kebab-case rule above
  (tests/test_cursor_plugin_manifest.py:L125-L129).
- It must have a non-empty string `description`, and an `author` object whose `name`
  is a non-empty string (tests/test_cursor_plugin_manifest.py:L131-L139).
- It must NOT contain a `version` field; version is resolved from the package's single
  source of truth at publish time, so a hardcoded value here is forbidden to prevent
  drift (tests/test_cursor_plugin_manifest.py:L141-L154).

### Component Path Fields

For each of the fields `skills`, `commands`, and `mcpServers`: if the field is absent
(null) no validation occurs (default discovery applies). If it is a string, it is the
single path to validate; if it is a list, only its string elements are validated; any
other type (e.g. an inline object) is not path-validated. Every validated path must be
safe-relative (relative, no `..`) (tests/test_cursor_plugin_manifest.py:L166-L184).

For the same fields, when the value is a string it must resolve to a real on-disk
target relative to the **repo root** (not the plugin dir): `skills` and `commands` must
resolve to directories; `mcpServers` must resolve to a file. Non-string values are not
resolution-checked (tests/test_cursor_plugin_manifest.py:L186-L210).

## marketplace.json Contract

- Must exist as a regular file and be valid JSON
  (tests/test_cursor_plugin_manifest.py:L217-L221).
- Must have a non-empty string `name`, an `owner` object with a string `name`, and a
  `plugins` list whose length is between 1 and 500 inclusive
  (tests/test_cursor_plugin_manifest.py:L223-L229).
- The set of plugin `name` values listed under `plugins` (from dict entries) must
  include the `name` declared in `plugin.json` (tests/test_cursor_plugin_manifest.py:L231-L242).
- No dict entry under `plugins` may contain a `version` field (same drift guard as
  plugin.json) (tests/test_cursor_plugin_manifest.py:L156-L164).

## mcp.json Contract

- Must exist as a regular file and be valid JSON
  (tests/test_cursor_plugin_manifest.py:L249-L253).
- The top-level JSON must contain a key `mcpServers` whose value is an object (the
  "wrapped" shape; the flat shape used by Claude's `.mcp.json` would not register with
  Cursor) (tests/test_cursor_plugin_manifest.py:L255-L264).
- `mcpServers` must contain an entry keyed `mempalace`. That entry must be an object
  whose `command` is the exact string `mempalace-mcp` (the binary shipped by the
  package) (tests/test_cursor_plugin_manifest.py:L266-L275).

## skills/ Contract

- `skills/` must exist as a directory at the repo root
  (tests/test_cursor_plugin_manifest.py:L282-L283).
- There must be at least one file matching `skills/<name>/SKILL.md`
  (tests/test_cursor_plugin_manifest.py:L285-L290).
- `skills/mempalace/SKILL.md` and `skills/mempalace-recall/SKILL.md` must both exist as
  files (tests/test_cursor_plugin_manifest.py:L292-L300).
- Every `skills/*/SKILL.md` must have YAML frontmatter containing: a non-empty string
  `name` that is kebab-case, and a non-empty string `description`; and its body must be
  non-empty after trimming whitespace (tests/test_cursor_plugin_manifest.py:L302-L321).
- The frontmatter `name` of each `SKILL.md` must equal its containing directory's name
  (tests/test_cursor_plugin_manifest.py:L323-L335).

## rules/ Contract

- `rules/` must exist as a directory at the repo root and must be a real directory, not
  a symlink (Cursor does not follow symlinks for local-plugin discovery)
  (tests/test_cursor_plugin_manifest.py:L348-L355).
- `rules/mempalace-recall.mdc` must exist as a file
  (tests/test_cursor_plugin_manifest.py:L357-L358).
- There must be at least one `*.mdc` rule file. Every `*.mdc` rule must have YAML
  frontmatter containing a non-empty string `description` and a boolean `alwaysApply`,
  with a non-empty body after trimming (tests/test_cursor_plugin_manifest.py:L360-L378).
- The shipped rule `rules/mempalace-recall.mdc` must have `alwaysApply` exactly equal to
  `false` (the always-on variant belongs only under examples/, never the default bundle)
  (tests/test_cursor_plugin_manifest.py:L380-L395).

## Default Discovery Layout Contract (no symlinks)

- `<repo>/commands` must be a real directory, not a symlink
  (tests/test_cursor_plugin_manifest.py:L411-L417).
- `<repo>/skills` must be a real directory, not a symlink
  (tests/test_cursor_plugin_manifest.py:L419-L425).
- `<repo>/mcp.json` must be a real file, not a symlink
  (tests/test_cursor_plugin_manifest.py:L427-L433).
- No path anywhere under `.cursor-plugin/` (recursively) may be a symlink; committed
  symlinks break Windows clones with `core.symlinks=false`
  (tests/test_cursor_plugin_manifest.py:L435-L447).

## commands/ Contract

- `commands/` must exist as a directory (tests/test_cursor_plugin_manifest.py:L451-L452).
- The set of `*.md` filename stems under `commands/` must equal exactly the promised set:
  `mempalace-help`, `mempalace-init`, `mempalace-mine`, `mempalace-search`,
  `mempalace-status` — no more, no fewer. Cursor derives the slash-command slug from the
  filename stem, so stems are compared (not frontmatter names)
  (tests/test_cursor_plugin_manifest.py:L55-L61, L454-L467).
- Every `commands/*.md` file must have YAML frontmatter with a non-empty string
  `description` and a non-empty body after trimming. A `name` field is intentionally not
  required for command files (tests/test_cursor_plugin_manifest.py:L469-L485).
- Every `commands/*.md` filename stem must begin with the prefix `mempalace-` to avoid
  global-namespace collisions (tests/test_cursor_plugin_manifest.py:L487-L500).

## README.md Contract

- `<plugin>/README.md` must exist as a file (tests/test_cursor_plugin_manifest.py:L507-L508).
- For each promised command name, the README text must contain the slash-prefixed token
  (`/mempalace-help`, etc.) (tests/test_cursor_plugin_manifest.py:L510-L516).
- The README text must contain the literal string `hooks/cursor/install.sh`, telling
  users how to enable auto-save hooks (which are not part of the plugin)
  (tests/test_cursor_plugin_manifest.py:L518-L527).

## Side Effects and Observable Contract

This module performs only read-only filesystem inspection of the listed paths; it spawns
no processes and makes no network calls, and is platform-independent
(tests/test_cursor_plugin_manifest.py:L24-L26). All files are read as UTF-8
(e.g. tests/test_cursor_plugin_manifest.py:L117, L308). A failing assertion in any check
constitutes a contract violation for the corresponding on-disk artifact.
