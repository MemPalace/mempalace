# Spec: `.antigravity-plugin/` directory schema contract tests

This module is a suite of contract tests that assert the on-disk shape of the in-repo
`.antigravity-plugin/` directory and its supporting recall-layer files. The tests fail
the moment any in-repo artifact drifts from Antigravity's documented plugin schema
(tests/test_antigravity_plugin_manifest.py:L1-L20). An implementation in any language is a
checker that validates these same files and contracts.

## Resolved paths (inputs)

All paths are resolved relative to the repository root, defined as the parent directory of
the directory containing this test file (tests/test_antigravity_plugin_manifest.py:L28-L29).
The checked artifacts are:

- `PLUGIN_DIR` = `<repo>/.antigravity-plugin` (tests/test_antigravity_plugin_manifest.py:L29).
- `PLUGIN_JSON` = `PLUGIN_DIR/plugin.json` (tests/test_antigravity_plugin_manifest.py:L31).
- `MCP_CONFIG` = `PLUGIN_DIR/mcp_config.json` (tests/test_antigravity_plugin_manifest.py:L32).
- `HOOKS_TMPL` = `PLUGIN_DIR/hooks.json.tmpl` (tests/test_antigravity_plugin_manifest.py:L33).
- `SKILL_MD` = `PLUGIN_DIR/skills/mempalace/SKILL.md` (tests/test_antigravity_plugin_manifest.py:L34).
- `PLUGIN_README` = `PLUGIN_DIR/README.md` (tests/test_antigravity_plugin_manifest.py:L35).
- `RECALL_SKILL_MD` = `PLUGIN_DIR/skills/mempalace-recall/SKILL.md` (tests/test_antigravity_plugin_manifest.py:L38).
- `RECALL_RULE_MD` = `PLUGIN_DIR/rules/mempalace-recall.md` (tests/test_antigravity_plugin_manifest.py:L39).
- `SHARED_PROTOCOL` = `<repo>/integrations/shared/recall-protocol.md` (tests/test_antigravity_plugin_manifest.py:L40).
- `INSTALL_SH` = `<repo>/hooks/antigravity/install.sh` (tests/test_antigravity_plugin_manifest.py:L41).

All file reads use UTF-8 encoding (e.g. tests/test_antigravity_plugin_manifest.py:L76, L102, L190).

## Constants / contract data

`SHARED_PROTOCOL_REF` is the canonical URL
`https://github.com/MemPalace/mempalace/blob/main/integrations/shared/recall-protocol.md`
that the recall skill and rule must both reference (tests/test_antigravity_plugin_manifest.py:L42-L44).

`EXPECTED_HOOKS` declares the two required hook events and their per-event constraints
(tests/test_antigravity_plugin_manifest.py:L46-L57):
- `Stop`: script basename `mempal_save_hook_antigravity.sh`, timeout floor 10, ceiling 60.
- `PreInvocation`: script basename `mempal_wake_hook_antigravity.sh`, timeout floor 1, ceiling 10.

## Contracts validated

### Plugin directory layout
The plugin directory must exist as a directory, and each of `plugin.json`, `mcp_config.json`,
`hooks.json.tmpl`, `skills/mempalace/SKILL.md`, and `README.md` must exist as a regular file
(tests/test_antigravity_plugin_manifest.py:L60-L64).

### `plugin.json` minimal schema
`plugin.json` must parse as a JSON object that equals exactly `{"name": "mempalace"}` — no
additional fields (notably no fabricated `permissions` field) are permitted
(tests/test_antigravity_plugin_manifest.py:L67-L82).

### `mcp_config.json` MCP server registration
`mcp_config.json` must parse as a JSON object containing a top-level `mcpServers` key whose
value is an object. That object must contain a `mempalace` entry which is itself an object,
and that entry's `command` field must equal the string `mempalace-mcp`
(tests/test_antigravity_plugin_manifest.py:L85-L97).

### `hooks.json.tmpl` validity
The hooks template must parse as valid JSON and the top-level value must be a JSON object;
the `__PLUGIN_DIR__` placeholder must be JSON-safe such that parsing succeeds even before
substitution (tests/test_antigravity_plugin_manifest.py:L100-L107).

### `hooks.json.tmpl` placeholder discipline
The template body must contain the literal string `__PLUGIN_DIR__` as its install-directory
placeholder, and must NOT contain any of the hard-coded path segments `/Users/`, `/home/`, or
`~/` (no absolute paths may leak into the template)
(tests/test_antigravity_plugin_manifest.py:L110-L124).

### Per-event hook structure (parametrized over `Stop` and `PreInvocation`)
The template is a JSON object whose top-level keys are hook namespace names (e.g.
`mempalace-save`) mapping to per-namespace objects. For each expected event, exactly one
namespace may declare that event (a namespace declares the event if its value is an object
containing the event key) (tests/test_antigravity_plugin_manifest.py:L127-L141). The value at
that event key must be a list containing exactly one handler entry; duplicate entries are
rejected because they would double-fire the hook (tests/test_antigravity_plugin_manifest.py:L142-L147).
For the single handler:
- Its `type` field, defaulting to `command` when absent, must equal `command`
  (tests/test_antigravity_plugin_manifest.py:L148-L151).
- Its `command` string must start with `__PLUGIN_DIR__/` and end with `/<script_basename>`
  for the event's expected basename (tests/test_antigravity_plugin_manifest.py:L152-L159).
- Its `timeout` must be an integer (a boolean is explicitly NOT accepted as an integer) and
  must lie within the inclusive `[timeout_floor, timeout_ceiling]` range for that event
  (tests/test_antigravity_plugin_manifest.py:L160-L165).

### Skill file is a real file (not a symlink)
`skills/mempalace/SKILL.md` must exist as a regular file and must not be a symlink, so that
installers that copy without dereferencing links still produce a working install
(tests/test_antigravity_plugin_manifest.py:L168-L180).

### Skill frontmatter
`SKILL.md` must begin with the YAML frontmatter opening fence `---\n`, must contain a closing
fence `\n---\n` after it, and within that frontmatter block must contain a `description:` key
whose value, after trimming, is non-empty and at least 30 characters long
(tests/test_antigravity_plugin_manifest.py:L183-L203). The `description` line is matched
line-anchored as `^description:\s*(.+)$` within the frontmatter
(tests/test_antigravity_plugin_manifest.py:L195-L196).

### No symlinks anywhere under the plugin directory
A recursive walk of the entire `.antigravity-plugin/` tree must find no symlinks; every entry
must be a real file or directory (tests/test_antigravity_plugin_manifest.py:L206-L219).

### Plugin README substance
`README.md` inside the plugin directory must be at least 200 bytes (characters) long and must
mention each of the strings `plugin.json`, `mcp_config.json`, and `hooks.json`
(tests/test_antigravity_plugin_manifest.py:L222-L237).

### Shared recall protocol (single source of truth)
`integrations/shared/recall-protocol.md` must exist as a regular file and its contents must
contain the canonical title string `MemPalace Recall Protocol`
(tests/test_antigravity_plugin_manifest.py:L249-L257).

### Recall skill
`skills/mempalace-recall/SKILL.md` must exist as a regular file and not be a symlink
(tests/test_antigravity_plugin_manifest.py:L260-L266). It must begin with `---\n`, have a
closing `\n---\n` fence, and within frontmatter carry a non-empty `description:` value
(tests/test_antigravity_plugin_manifest.py:L269-L282). Its body must contain each of the
section markers `When to recall`, `Protocol`, `Tool selection`, `Unhappy paths`, and
`Anti-patterns` (tests/test_antigravity_plugin_manifest.py:L285-L295). Its body must contain
the `SHARED_PROTOCOL_REF` URL so the protocol stays single-sourced
(tests/test_antigravity_plugin_manifest.py:L298-L303).

### Recall rule (plain markdown, not `.mdc`)
`rules/mempalace-recall.md` must exist as a regular file and not be a symlink
(tests/test_antigravity_plugin_manifest.py:L306-L309). Its file extension must be `.md`, no
sibling `mempalace-recall.mdc` may exist, and its body must NOT begin with `---` (no YAML
frontmatter / no Cursor-style `alwaysApply`) (tests/test_antigravity_plugin_manifest.py:L312-L327).
The rule body must contain the `SHARED_PROTOCOL_REF` URL
(tests/test_antigravity_plugin_manifest.py:L330-L333).

### Installer wiring
`hooks/antigravity/install.sh` must contain the literal strings `"$INSTALL_DIR/rules"` and
`"$INSTALL_DIR/skills/mempalace-recall"` in its mkdir block (it creates those directories)
(tests/test_antigravity_plugin_manifest.py:L336-L342). It must also contain the substrings
`skills/mempalace-recall/SKILL.md` and `rules/mempalace-recall.md`, indicating it copies the
recall skill and rule into the install directory (tests/test_antigravity_plugin_manifest.py:L345-L358).

## Outputs / side effects
The suite has no side effects beyond reading the listed files; each test passes (no output) when
its assertions hold and fails with a descriptive message otherwise (e.g.
tests/test_antigravity_plugin_manifest.py:L62-L64, L78-L82). The hook-event check is run once per
event name (`PreInvocation`, `Stop`) in sorted order as separate parametrized cases
(tests/test_antigravity_plugin_manifest.py:L127-L128).
