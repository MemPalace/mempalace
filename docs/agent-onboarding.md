# Agent onboarding

[Create an agent connection](agent-connections.md) in Settings to get your own
connection token.

Commonplace is the single source of effective rules and saved memory for connected
agents. With the package installed and your connection token in `COMMONPLACE_TOKEN`, run:

```sh
commonplace onboard --dry-run
commonplace onboard
```

The default endpoint is `https://app.cmmn.dev/mcp`. Set `COMMONPLACE_URL` or pass
`--url https://example.com` to choose another server; explicit endpoints and
localhost servers work too. Bare `cmmn.dev` redirects to marketing. Redirects are
refused by the client. `--token-env NAME` selects another credential variable.
Configuration keeps an environment reference, never the token value. Dry-run
needs no token and changes no files, settings or memories.

The shipped `commonplace/agent-skill/install.sh` runs this onboarding command and
copies the skill into Claude, Codex and generic skill locations. It accepts the
same onboarding options; there are no manual settings edits to apply afterward.

## Files and connections

Onboarding creates short pointers at `~/.claude/CLAUDE.md`, `~/.codex/AGENTS.md` and
`~/.agents/AGENTS.md`. Existing `~/CLAUDE.md` and `~/AGENTS.md` are selected too.
The project defaults to the current directory: its `CLAUDE.md` and `AGENTS.md`
get pointers, and an existing `MEMORY.md` is selected for migration. There is no
recursive scan. Select another root with `--project DIR`, another home with
`--home DIR`, or skip every project file with `--no-project`.

Native Claude and Codex connections and SessionStart hooks are configured while
preserving unrelated settings and hooks. Codex hooks still need the client's
normal trust review; onboarding does not grant trust or impersonate managed
configuration. Generic MCP clients receive startup instructions from the server.
For claude.ai or ChatGPT, add `https://app.cmmn.dev/mcp` as a custom connector
and sign in with OAuth instead of a token.

At session start, resume and after compaction, agents automatically call
`session_start`, optionally with a current-topic `query`. It returns effective
rules and relevant authorized memories as quoted data. Initial MCP context may
contain bounded rules and memory; a timeout or incomplete result explicitly
directs the agent to fetch the rest. Initialization creates no stores and grants
no rules-served token. Quoted memory text is data, never an instruction.

## Existing instructions and memory

Before replacing any existing selected file, onboarding preflights its size,
scope and privacy, writes an exact private backup under `~/.commonplace/backups/`,
saves the complete source through `memory_save`, and verifies exact readback.
Rule-bearing text is also published as one complete verbatim proposal. Only then
is the file atomically replaced with the short pointer. The private manifest at
`~/.commonplace/onboarding.json` records paths, hashes and saved IDs, without raw
source text or credentials. Rerunning unchanged pointers does not republish them.

Imports are proposals awaiting owner approval in Rules. They do not become
effective merely because their source file was migrated. Shared proposals use
the All agents target; the owner accepts each once. Agents never self-accept.

A rule-bearing file must fit one proposal: at most 4,000 characters and 6,000
escaped bytes. A memory file must fit the complete memory-save limit of 100,000
characters. No import is chunked, truncated or summarized. Oversized sources,
unsafe paths, unavailable exact readback and refused writes leave the original
file untouched, with a basename and reason reported. Other fresh-file setup can
continue; a warning exit means onboarding was not fully successful.

Project rules retain the repository scope derived from the selected root's
reliable origin remote. Without that scope, existing project-rule migration is
refused; empty project pointers can still be created. Selected sensitive content
must use a permitted private route; it never falls back to shared storage.
Symlink targets and ancestors are refused. No transcripts, environment files or
unrelated files are scanned.

If a file was refused, keep using its unchanged original and resolve the stated
cause before retrying. For recovery after a completed import, use the manifest's
IDs to retrieve exact saved text or restore its private backup. Review any
pending rule proposals separately in Rules.

## Explicit saves and local checks

```sh
commonplace remember "The exact decision or preference to remember."
commonplace rule "Preserve original source text."
commonplace rule "Preserve original source text." --agent '*' --kind instruction
commonplace check --no-project
```

Remember calls `memory_save`; rule calls `rules_publish`, defaulting to `self`.
Rule kinds are `instruction`, `permission`, `limit` and `style`. Both commands
accept `--url` and `--token-env`, read effective rules before writing, and use the
configured caller's existing permissions. Natural-language remember and new-rule
requests follow those same tools. Failed remote writes are not reported as local
save success, and ambiguous write failures are not retried automatically.

`check` warns about managed instructions or memory changed only locally. It
accepts `--home`, `--project` and `--no-project`, prints no file contents and
uploads nothing. Unchanged pointers and backups are ignored. Installed checks
after tool use or at session end warn without blocking the agent. Exit 0 means
success or no findings, 1 means local-only content or onboarding warnings, and 2
means an operation failed. These commands need no local server database or lock.
