# Context handoff

## What it does

- When a thread nears its context limit, handoff saves it in Memory.
- Every message, tool call and tool result stays verbatim.
- A fresh agent starts with a short handoff and a pointer to that thread.
- It can retrieve the earlier words when it needs them.

## Turn it on

Handoff is off by default. With it off, existing behaviour and normal
compaction stay unchanged.

Create a web sign-in link for an existing workspace member:

```bash
commonplace login-link you@example.com --workspace personal --base-url http://127.0.0.1:8765
```

Open the printed `/app/login?t=…` link, then `/app/settings/memory`.
The link works once and expires after 15 minutes by default. Sign-in sets
the web app's `cmmn_session` cookie. Forms require a CSRF token derived
from that session and reject cross-site submissions.

Workspace owners can turn on **Handoff** and change the threshold and
agent or task overrides. Other members can view the settings. Signed-in
people can read stored threads in their workspace.

From the CLI, use your workspace and connection names:

```bash
commonplace handoff enable --workspace personal --threshold 0.80
commonplace handoff status --workspace personal
commonplace handoff disable --workspace personal --agent codex:local
commonplace handoff enable --workspace personal --task '#142'
```

Each field inherits separately: task overrides agent, agent overrides
workspace, and workspace overrides the default. A task can stay on while
its workspace is off. Remove that task override with:

```bash
commonplace handoff clear --workspace personal --task '#142'
```

Set these in the environment that starts the agent:

```bash
export COMMONPLACE_URL=http://127.0.0.1:8765
export COMMONPLACE_TOKEN=cmmn_REPLACE_WITH_YOUR_TOKEN
export COMMONPLACE_TASK='#142'
```

`COMMONPLACE_TASK` is optional. The hook needs `handoff:write` and `trusted`
trust to request a handoff. Registering its successor needs `handoff:write`.
The successor needs `threads:read`
and access to the stored thread.

Use HTTPS for a remote gateway. The hook warns on stderr when
`COMMONPLACE_URL` uses plain HTTP with a non-loopback destination, then
continues normally.

The API uses the workspace attached to the bearer token. For example:

```bash
curl --request PUT "$COMMONPLACE_URL/api/settings/handoff" \
  --header "Authorization: Bearer $COMMONPLACE_TOKEN" \
  --header 'Content-Type: application/json' \
  --data '{"scope":"workspace","ref":"","enabled":true,"threshold":0.80}'
```

For an agent override, use `"scope":"agent"` and its connection name as
`ref`. For a task override, use `"scope":"task"` and `"ref":"#142"`.
An omitted or `null` setting inherits. Owners control workspace and agent
settings and launchers. Members may change only a task's `enabled` field.
Agent tokens also need `handoff:write` and `trusted` trust for settings
writes, deletes and launches, including relaunch and `handoff_start`.
Signed-in person sessions keep their existing workspace role permissions.

To force handoff off for one run, regardless of saved settings:

```bash
COMMONPLACE_HANDOFF=off claude
```

## Start a handoff by hand

Run this on the machine hosting the source T3 thread and the Commonplace
gateway, using the gateway's `COMMONPLACE_DATA` directory. This also works
when the source model has hit its session limit: it reads the saved native
transcript without calling that model.

Find the workspace name or ID in the gateway's local data directory:

```bash
commonplace workspace list
# For a different gateway data directory:
commonplace --data /path/to/gateway-data workspace list
```

Use a printed ID with `--workspace` to avoid ambiguous names. `personal`
in the examples is a workspace name, not an alias for your personal
workspace; replace it with your actual name or ID. Missing or invalid
workspace selections show the available local names and IDs.

Claude Code → Codex:

```bash
commonplace handoff start --thread '<t3-thread-id>' --to codex/gpt-6.1-sol --effort max --workspace personal
```

Codex → Claude Code:

```bash
commonplace handoff start --thread '<t3-thread-id>' --to claude-code/claude-opus-5-5 --effort high --workspace personal
```

Replace the thread ID with the source T3 ID, including `mcp:` when present.
`claude-code` and `claude` map to T3's default `claudeAgent` instance;
`codex` stays `codex`. Other configured T3 instance IDs pass through.
Models use their exact T3 catalog slug; the examples select explicit model
IDs. T3 validates the selected instance, model and effort.

Add `--note 'your exact continuation instructions'` for a verbatim note.
`--dry-run` prints the resolved checkout, native transcript paths, settings
and destination without saving a handoff or starting or settling a thread.
`--workspace` can be omitted when `COMMONPLACE_WORKSPACE` or the local
checkout map below selects one. `--data` before `handoff` selects another
gateway data directory, as with the other local administration commands.

To bind checkouts, create `~/.commonplace/handoff-client.json` on the source
machine (this file is separate from the gateway data directory):

```json
{
  "workspaces": {
    "~/projects/example": "ws_REPLACE_WITH_PRINTED_ID",
    "~/projects/example/other-checkout": "another-workspace-name"
  }
}
```

Keys are checkout directories; `~` is expanded. Values are Commonplace
workspace names or IDs, not T3 project IDs. The most specific matching
directory or ancestor wins. Selection order is `--workspace`, then
`COMMONPLACE_WORKSPACE`, then a mapping for the source thread's checkout,
then one for its project root. This uses the source thread's paths, not
the shell's current directory. The map contains no credentials and does
not enable handoff; enable the selected workspace or override separately.

Manual handoff respects the same task → agent → workspace settings. Use
`--agent <connection-name>` and `--task '#142'` when the source has those
overrides; they default to `COMMONPLACE_AGENT` / `COMMONPLACE_TASK`, then
the caller's connection for the agent. It refuses when the effective
setting is off, or `COMMONPLACE_HANDOFF=off` applies. There is no `--force`:
enable the intended scope first. The token and settings of the successor
must permit `thread_recall` and `thread_get` as described below.

The full source is saved through `HandoffService`, with the same
`thread:<source T3 ID>` idempotency key and atomic launch claim used by
`POST /api/handoff`. The short handoff and prompt stay in the existing
private stores. Manual selection overrides the launcher for this call;
it does not change `successor_argv` or any saved launcher setting.

For a project-root source, the client runs
`t3 thread start <cwd> - --provider <p> --model <m> [--effort <e>] --json`,
passing the prompt over stdin. This CLI route uses the project's default
permission mode, which may differ from the source's mode. For an existing
worktree, the installed T3 CLI cannot bind that checkout: its path argument
selects a project and otherwise starts at the root. The adapter uses T3's `acp-mcp-call`
bridge to `t3_thread_launch` with
`workspaceStrategy={"type":"existing_worktree","worktreePath":"…"}`.
The bridge requires Commonplace's own explicitly approved OAuth MCP grant
in `COMMONPLACE_T3_MCP_TOKEN`. It requests the source's `runtimeMode` so T3
refuses an insufficient grant instead of lowering permissions. There is no
public headless auth CLI flag for a managed MCP runtime ceiling; generic
`t3 auth session issue` tokens are not a substitute. No provider credential
is read or reused, and the grant never enters the prompt, journal or
successor environment. Without a grant, the command reports an unsupported
launch before storage or launch; an attempted launch refused by T3 records
`launch_failed` and leaves the source running. It never creates another
worktree. The prompt ends with `commonplace-handoff-id: hof_…`,
which the successor's existing hook uses to recover its chain ID without
needing T3 to pass environment variables.

Only a confirmed T3 start is registered as `t3:<new thread ID>`. Registration
is followed by `t3 thread settle <source ID>`, which settles a running
source when its turn ends. Storage or launch failures leave the source
running; a failed launch records `launch_failed`. Repeating the command
returns the existing successor, even after the old native session is gone
(pass `--workspace` if its checkout is no longer available). Completed manual
handoffs return without rereading that session. Concurrent manual calls
serialize, and the database launch claim also excludes competing hook requests with the same
key. A private launch journal recovers registration after a confirmed start;
an unconfirmed start is never retried automatically. Inspect T3 before an
owner releases a failed launch claim. A crash after claiming a launch but
before starting T3 or writing the journal can leave `launched` without a
successor; this requires operator inspection and recovery, never an automatic
retry. If source settlement fails, the result
has `source_settled:false`; retry the same command to retry settlement.

### How the source is resolved

`t3 thread show <id> --json --messages 0` supplies the provider instance,
model, project ID, branch and worktree. It does **not** supply a transcript.
The adapter opens `$T3CODE_HOME/userdata/statev2.sqlite` read-only (default
`~/.t3/userdata/statev2.sqlite`) and reads:

- `projection_projects.workspace_root` for a project-root thread's cwd.
- `orchestration_v2_projection_provider_threads.payload_json`, specifically
  each main thread's `driver` and `nativeThreadRef.nativeId` in run order.

It locates the exact native ID in Claude Code's `projects/*/<id>.jsonl`
or Codex's `sessions/**/*.jsonl` / `archived_sessions/**/*.jsonl`. Provider
`homePath` in T3's `userdata/settings.json` takes precedence, followed by
`CLAUDE_CONFIG_DIR` / `CODEX_HOME`, then the standard home directories.
Tool results and raw records are retained, including CR/LF. The adapter
refuses missing or ambiguous transcripts, unavailable checkouts, unsupported
drivers, and mixed native formats instead of handing over a partial
latest-message view. This state-schema dependency is covered by fake-T3
tests and may need updating after a T3 schema change.

### Local setup

This is an opt-in setup recipe; installing the feature does not run it.
Use the existing local gateway's data directory and workspace name in
these commands. The `personal` workspace below is an example.

```bash
commonplace handoff enable --workspace personal --threshold 0.80
commonplace handoff status --workspace personal
```

Merge this checkout map into `~/.commonplace/handoff-client.json` (preserve
any existing `successor_argv`):

```json
{
  "workspaces": {
    "~/projects/example-app": "personal",
    "~/projects/example-library": "personal"
  }
}
```

The deepest mapped checkout wins. T3 worktrees inherit their source
project's mapping. Manual CLI starts need no hook or `successor_argv`.
Existing-worktree starts also need a dedicated Commonplace MCP client grant,
created through T3's supported OAuth authorize/approval/exchange flow, with
access sufficient for the source permission mode. Set its bearer as
`COMMONPLACE_T3_MCP_TOKEN` in the local CLI/gateway environment. This recipe
does not mint that grant or enable it on the machine. Until the owner
configures it, worktree starts refuse cleanly and the source keeps working;
project-root starts use the supported thread CLI without this extra grant.
The `handoff_start` MCP tool runs on the gateway machine and requires this
map to bind the source checkout to the token's workspace. It accepts
`thread_id`, `to`, and optional `effort`, `note`, `agent`, `task`, `dry_run`;
it needs `handoff:write`, a `standard` or `full` tool profile (or an explicit
`handoff_start` grant), and `trusted` trust. It cannot
select another workspace. For example:

```json
{"thread_id":"mcp:source-id","to":"codex/gpt-6.1-sol","effort":"max","note":"Continue the deployment checks."}
```

For automatic threshold handoffs too, set `COMMONPLACE_URL` and a
workspace-scoped `COMMONPLACE_TOKEN` in the environment that starts T3's
agents, then merge the Claude and Codex hook definitions in the next section
into **user-level** hook configuration. Use the absolute wrapper path
`/path/to/commonplace/hooks/commonplace_handoff_hook.sh` and the
appropriate `--client`. Review Codex's hook definitions as described below.
Choose `successor_argv` only if automatic hooks should launch a static
successor. Mapped checkouts then use those user-level hooks and the
workspace setting. Keep credentials and local setup outside public
repositories; do not commit hook configuration or machine-specific paths.

## When it triggers

The default threshold is `0.80`. The allowed range is `0.50` to `0.95`.
The hook compares the last model call's context use with the model's window.
It triggers when the fraction reaches or exceeds the threshold.

For Claude Code, usage includes input, cache-read, cache-creation and output
tokens. For Codex, it comes from `token_count` events. The hook reads the
transcript tail rather than the whole file for this check.

Windows come from a model-name table; the default is 200,000 tokens. A
Claude model runs with a 1,000,000-token window when its name ends in
`[1m]` or `-1m`, is on the known list (add more with
`COMMONPLACE_1M_MODELS=name,prefix`), or is the model Claude Code was told
to use (`ANTHROPIC_MODEL`, or `model` in `.claude/settings.json`) and that
has the marker. A transcript that already uses more than the table window
is on 1M too. `--window` on `handoff enable` (or `context_window` through
the API) always wins.

Unknown usage does not trigger a threshold handoff. `PreCompact` is the
backstop: when handoff is enabled, it requests a handoff even if the
threshold has not been reached or usage is unavailable.

The hook caches effective settings. A saved setting change may take up to
60 seconds to reach a running hook. `COMMONPLACE_HANDOFF=off` takes effect
locally without a settings request.

## Set up the hook

Install Commonplace in the Python environment used by the hook. Set
`COMMONPLACE_PYTHON` to that interpreter's absolute path if `python3` uses
a different environment. Set the gateway URL and token before starting
the agent. Configure a successor command as described below.

### Claude Code

Merge this `hooks` block into the project's `.claude/settings.json`.
Replace `/absolute/path/to/repo` with this repository's path. Keep the
escaped quotes if the path contains spaces. Keep any existing handlers.

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "bash \"/absolute/path/to/repo/hooks/commonplace_handoff_hook.sh\" --client claude-code",
            "timeout": 60
          }
        ]
      }
    ],
    "PreCompact": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "bash \"/absolute/path/to/repo/hooks/commonplace_handoff_hook.sh\" --client claude-code",
            "timeout": 60
          }
        ]
      }
    ]
  }
}
```

The wrapper passes the hook JSON from stdin to
`python3 -m commonplace.handoff.hook --client claude-code`.
After a handoff, the hook returns `{"continue":false,"stopReason":"…"}`
to stop the old agent.

### Codex

Use the repository's Codex hook mechanism: `.codex/hooks.json`, with a
nested `hooks` object and handler groups. Register the hook for `Stop`
(the check), `PreCompact` (the backstop) and `UserPromptSubmit` (where the
handoff happens).

```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "bash \"/absolute/path/to/repo/hooks/commonplace_handoff_hook.sh\" --client codex",
            "timeout": 60
          }
        ]
      }
    ],
    "PreCompact": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "bash \"/absolute/path/to/repo/hooks/commonplace_handoff_hook.sh\" --client codex",
            "timeout": 60
          }
        ]
      }
    ],
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "bash \"/absolute/path/to/repo/hooks/commonplace_handoff_hook.sh\" --client codex",
            "timeout": 60
          }
        ]
      }
    ]
  }
}
```

Codex must trust the project configuration. Then review each definition
with `/hooks`; project trust alone does not approve hook commands. Use
either JSON or TOML in a config layer so hooks do not run twice. See the
[Codex hook guide](../website/guide/hooks.md#install--codex-cli) for the
existing configuration and trust flow. `mempalace status` checks project
trust; it cannot confirm that each definition was reviewed.

A Codex hook can't stop a running turn. So over the threshold the hook
only marks the session and shows "Context is nearly full. It hands off at
the start of the next turn." When your next message arrives
(`UserPromptSubmit`), it stores the thread, writes the handoff and starts
the successor with your message passed on, word for word. The old session
refuses that message with "Context handed off. Successor: …". If the hub
can't be reached, the mark stays and the next turn tries again.

## Choose the successor command

The default launcher is `client`: the hook starts the successor on the
agent's machine. Put one command in `~/.commonplace/handoff-client.json`.
For Claude Code:

```json
{
  "successor_argv": ["claude", "-p", "{prompt}"]
}
```

For a T3 project:

```json
{
  "successor_argv": ["t3", "thread", "start", "{cwd}", "{prompt}"]
}
```

`{cwd}` must identify a project known to T3. This uses
`t3 thread start <project> <message>` and starts a fresh thread. Give that
project's agent access to the Commonplace thread tools.

Each JSON array element is one argument. Placeholders are replaced inside
that argument. No shell evaluates the command: shell quotes, `~`, pipes
and redirections are not expanded.

| Placeholder | Value |
|---|---|
| `{prompt}` | The handoff Markdown and retrieval instructions |
| `{prompt_file}` | Path to a file containing that prompt |
| `{handoff_id}` | This handoff's ID |
| `{thread_id}` | The stored thread ID, not the successor's client ID |
| `{cwd}` | The source thread's working directory |

The launched process inherits the environment and receives
`COMMONPLACE_HANDOFF_ID`. Keep that value so its next handoff can link
back to this one. The server's `command` launcher also supplies
`COMMONPLACE_THREAD_ID` with the stored thread ID.

The local hook observes the successor for 50 ms and reports a nonzero exit
seen during that window. A process still running then is registered as
launched; this startup observation does not guarantee continued liveness.

Gateway `command` and `webhook` launchers are disabled by default. An operator
can enable either kind with `COMMONPLACE_HANDOFF_LAUNCHERS=command`,
`COMMONPLACE_HANDOFF_LAUNCHERS=webhook`, or both:

```bash
export COMMONPLACE_HANDOFF_LAUNCHERS=command,webhook
```

Set this in the gateway's environment. Tokens and workspace settings cannot
enable launcher kinds. Saving a disabled kind is refused; an already stored
launcher records `launch_failed` without starting a command or sending a request
if its kind is disabled when the handoff runs. The default `client` launcher
and the hook's local `successor_argv` do not require this gateway opt-in.

Command and webhook launcher rows stored before this fix stay inert while
their kinds are disabled. Review those rows, including their `updated_by`
actors, before setting `COMMONPLACE_HANDOFF_LAUNCHERS`: enabling a kind can
activate an older stored launcher.

With operator opt-in, owners can configure a `command` launcher on the gateway,
or a `webhook` launcher at a public destination. Webhook URLs must pass the event
webhook URL rules and resolve only to public addresses. Private and other
non-public addresses are refused when saving and launching, including when
DNS changes after validation. Redirects are never followed.
The command launcher also uses
an argument array without a shell. A webhook launcher receives the prompt
and must return an `agent_ref`, with optional `url` and `thread_id`.
Event webhooks, described below, do not receive the prompt.

Without `successor_argv`, the hook starts nothing and names the prompt
file in its stop message, so you can start the successor yourself.

The hook registers the successor as `pid:<pid>` of the command it ran.

The successor prompt ends with `commonplace-handoff-id: hof_…`. A
successor whose environment has no `COMMONPLACE_HANDOFF_ID` (a T3 thread:
`t3 thread start` can't pass environment variables to the thread's agent)
gets it from that line: its own hook reads it from the start of its
transcript, so its next handoff links into the chain right away. The hub
checks that the parent is in the same workspace.

## What the successor gets

The new agent receives the handoff and a stored thread pointer. It does
not load the full old conversation into its context.

The handoff quotes the first user message as the goal, the last assistant
message as the status, unfinished todo items as open points, and later
user messages as decisions. It includes available task, branch and PR
references, an optional verbatim agent note, and earlier thread pointers.
No model rewrites the thread. Long quotes may end with `…` and a
`thread_get` range for the full words. Stored and retrieved messages
remain verbatim.

### Thread tools

The three tools are gateway extension tools. They stay hidden until a
workspace enables handoff at any scope or has stored threads. Turning
handoff off keeps the tools available for threads already stored.

Tokens need `threads:read`, an allowed tool name, and permission to read
the stored thread. The `standard` and `full` profiles include the names;
explicit `--tools` lists can name them too. Untrusted tokens cannot use
them. Hidden tools are absent from `tools/list` and answer `Unknown tool`.

Handler errors return `isError: true` with text starting `no:`. Missing
and inaccessible threads both return `no: thread not found`.

Use the stored ID (`thr_…`) as `thread_id` in all three tools.

`thread_recall` searches the thread and, by default, its predecessors.

| Argument | Type | Default | Meaning |
|---|---|---|---|
| `thread_id` | string | required | Stored thread to search |
| `query` | string | required | Words or facts to find |
| `limit` | integer | `8` | Maximum hits; 1–50 |
| `follow_chain` | boolean | `true` | Include readable earlier threads |

Each hit identifies its thread, chunk and inclusive message range, with
a score and verbatim text. Use the range to fetch more context.

`thread_get` returns messages in source order.

| Argument | Type | Default | Meaning |
|---|---|---|---|
| `thread_id` | string | required | Stored thread to read |
| `start` | integer | `0` | First message number, inclusive |
| `end` | integer | omitted | Last message number, inclusive; omit to read up to the call limit |
| `include_raw` | boolean | `false` | Include each message's original source record |

Message numbers start at zero. Each call returns at most 200 messages.
Request further ranges to read a longer thread. For example, `start=20,
end=24` reads five messages.

`thread_chain` returns the requested thread followed by its predecessors.

| Argument | Type | Default | Meaning |
|---|---|---|---|
| `thread_id` | string | required | Stored thread at which to start |

The order is newest first. Traversal stops at the first thread the token
cannot read and stops safely if it encounters a cycle.

## REST API

Send `Authorization: Bearer cmmn_…`. Request and response bodies are JSON.
The token selects the workspace. Thread and handoff resources outside its
access return `404` with `{"error":"not found"}`, just like missing IDs.
For agent tokens, every launch and settings write or delete also requires
`handoff:write` and `trusted` trust. Missing this authority returns `403`.

| Method | Path | Access | Body or result |
|---|---|---|---|
| POST | `/api/handoff` | agent tokens: `handoff:write` + `trusted` | Handoff request → action, record, launch flag and prompt |
| GET | `/api/handoffs?task=%23142` | `threads:read` | Handoffs for the task; task filter is optional |
| GET | `/api/handoffs/<id>` | `threads:read` | Handoff record |
| POST | `/api/handoffs/<id>/successor` | `handoff:write` | `{agent_ref, url?, thread_id?}` |
| POST | `/api/handoffs/<id>/launch-failed` | `handoff:write` | `{error}` |
| POST | `/api/handoffs/<id>/relaunch` | person owner, owner-role connection, or original launcher connection; agent tokens: `handoff:write` + `trusted`, unrestricted | Retry a `launch_failed` handoff |
| GET | `/api/threads/<id>` | `threads:read` | Stored thread metadata |
| GET | `/api/threads/<id>/messages?start=0&end=99` | `threads:read` | Verbatim messages in the inclusive range |
| POST | `/api/threads/<id>/recall` | `threads:read` | `{query, limit?, follow_chain?}` → search hits |
| GET | `/api/threads/<id>/chain` | `threads:read` | This thread and readable predecessors |
| GET | `/api/settings/handoff?agent=&task=` | authenticated token | Saved rows and effective settings |
| PUT | `/api/settings/handoff` | owner; members: task `enabled` only | `{scope, ref, enabled?, threshold?, context_window?, launcher?}` |
| DELETE | `/api/settings/handoff?scope=&ref=` | owner | Remove an override |

Encode `#` as `%23` in URL query values. An unencoded `#` starts a URL
fragment and never reaches the server.

A handoff request has these fields:

| Field | Meaning |
|---|---|
| `meta` | Required object with the source `thread_id`; optional `agent`, `client`, `model`, `task_ref`, `branch`, `pr_url`, `cwd`, `parent_handoff_id` |
| `transcript` | Required full transcript as a string |
| `transcript_format` | `auto` (default), `claude-code`, `codex`, or `messages` |
| `usage` | Optional `{used_tokens, window_tokens, model}` |
| `note` | Optional agent words, preserved verbatim |
| `idempotency_key` | Optional retry key; defaults to `thread:<source thread_id>` within the workspace |

The `messages` format is a JSON list of `{role, content}` records, encoded
in the `transcript` string. The other formats are the clients' JSONL.

An `action` of `compact` leaves the client on its normal path. `handoff`
tells the old agent to stop. Only the response with `launch:true` tells
the client to start a successor; other launchers start it on the server.

## Events and webhooks

Handoffs write audit entries and append to the hub's event log. The hub's
existing webhook service delivers matching events in the background.
As a workspace owner, subscribe to the `handoff` preset:

```bash
commonplace webhook add http://127.0.0.1:9000/handoff --workspace personal --events handoff
```

Or call the `webhook_add` extension tool with:

```json
{"url":"http://127.0.0.1:9000/handoff","events":["handoff"]}
```

The tool needs `memory:read`, an allowed tool name and a connection that
is not untrusted. Both methods return the signing secret once; store it
at the receiver. The hub also accepts these arguments at
`POST /api/webhooks`; its existing list, remove, test and delivery routes
manage subscriptions. Without an event filter, a subscription receives
all permitted event kinds.

| Event | Meaning |
|---|---|
| `handoff.started` | A handoff began |
| `handoff.stored` | The full thread and handoff are saved |
| `handoff.completed` | The successor was registered |
| `handoff.failed` | Storage failed; use normal compaction |
| `handoff.launch_failed` | The thread is saved but successor launch failed |
| `handoff.settings_changed` | A saved setting changed |

The hub wraps each event in this envelope (example values):

```json
{
  "agent": "commonplace",
  "task": "#142",
  "need": "info",
  "message": "#142 context handed off http://127.0.0.1:8765/s/A1b2C3d",
  "id": "evt_42",
  "seq": 42,
  "kind": "handoff.completed",
  "at": "2026-10-30T12:00:00Z",
  "workspace": "ws_example",
  "subject": "#142",
  "actor": "conn:example",
  "data": {
    "handoff_id": "hof_example",
    "stored_thread_id": "thr_example",
    "task_ref": "#142",
    "successor_agent_ref": "t3:example",
    "number": 142
  },
  "url": "http://127.0.0.1:8765/s/A1b2C3d"
}
```

Handoff `data` contains available IDs and references only, plus `number`
for a numeric task reference and `decision` on failures. Empty references
are omitted. The webhook filter excludes `reason`, thread text and the
handoff prompt. `subject` is the task reference, or the handoff ID when
there is no task. `url` links to the app; `id` identifies the hub event.

The receiver gets a JSON POST with `X-Commonplace-Event`,
`X-Commonplace-Delivery` (the hub delivery ID), and
`X-Commonplace-Signature: t=<unix seconds>,v1=<hex digest>`. The digest is
HMAC-SHA256 with the UTF-8 secret over the timestamp, a dot and the exact
body bytes: `timestamp + b"." + body`. Verify those bytes before parsing
the JSON. For example, with a receiver-chosen five-minute age limit:

```python
import hashlib
import hmac
import json
import time


def verify_handoff(body: bytes, signature: str, secret: str) -> dict:
    fields = dict(part.split("=", 1) for part in signature.split(","))
    timestamp = fields["t"]
    expected = hmac.new(
        secret.encode("utf-8"), timestamp.encode("ascii") + b"." + body, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(fields["v1"], expected):
        raise ValueError("invalid signature")
    if abs(time.time() - int(timestamp)) > 300:
        raise ValueError("stale signature")
    return json.loads(body)
```

Pass the unmodified body and the `X-Commonplace-Signature` header to this
function. Read `kind` and `id` from the verified body; the event and
delivery headers are not signed. Deduplicate by event `id` to avoid
repeating an action, including replays within the age limit. Each delivery
attempt gets a fresh signature timestamp.

Delivery makes at most six attempts, with retry delays of 10 seconds,
1 minute, 5 minutes, 30 minutes and 2 hours. A `2xx` response succeeds;
other responses retry until the limit. Redirects are not followed.

## Fact indexing

After a thread is stored, a separate background job extracts complete named relations. It does not infer a speaker from the token account: first-person decisions and preferences remain in the verbatim chat until their speaker can be verified. It runs locally with deterministic rules. Extracted facts point to the exact stored messages and live in `private_threads` with private sensitivity. Secret-like messages, injected AGENTS.md or skill/command expansions, isMeta records, and messages containing XML/HTML wrappers are excluded from the fact index; the lossless chat stays verbatim.

The connection needs permission to read the source thread (`threads:read` and its source wing), plus the existing `fact_save` permissions for `private_threads`. Extraction uses the gateway's normal policy and privacy checks. It runs independently of mirroring and successor launch, so an unavailable fact worker or a slow embedding job does not block the handoff.

Extraction progress is stored beside the chat. A repeated or extended handoff resumes unfinished messages. A crash between saving a fact and committing progress may replay the save; the fact row and source pointer are deduplicated. Unsupported wording remains in the full chat and its search mirror. Past-tense employers and ambiguous location statements are not indexed. Named subjects must be proper names, and values with abbreviations, grouping commas, joined clauses or trailing scope qualifiers are skipped in full. Present assertions may carry an explicit start date. Automatic extraction makes no model or external API calls.

Run the offline extraction eval with `python benchmarks/fact_recall_bench.py --saver extract --threads 120`. Its gold fixture includes old-thread recall, held-out paraphrases, changed values, exact merged sources, private facts and a two-message fact. Negative fixtures cover injected wrappers, agent relays, code prose, abbreviations, grouped numbers, past-tense employers and scope qualifiers. `false_facts` must be zero and `extraction_precision` must be 1.0; every filed subject is scored, including subjects absent from the gold questions. The extraction saver uses the production cursor and argument mapping; real-gateway tests separately cover policy and webhook privacy. Fact change events carry opaque references, never text-bearing triple IDs.

## Failure behaviour

| Failure | Result |
|---|---|
| Handoff is off | Nothing is stored or launched; normal compaction continues |
| Transcript parsing or storage fails | No successor starts; `handoff.failed`; normal compaction continues |
| The additional Memory search index cannot be updated | The full thread stays saved; handoff can continue |
| Server command or webhook launcher fails | The thread stays saved; `handoff.launch_failed`; the old agent is told to stop |
| Hook configuration, HTTP, timeout or response error | The hook exits successfully and silently; the agent continues on its normal path |

Person owners, owner-role connections, or the original launcher connection can retry a recorded launch failure with
`commonplace handoff relaunch <handoff_id>`. Inspect the saved handoff
before starting a successor manually. Every agent connection must still have
`handoff:write`, trusted trust and unrestricted access. Standard-trust, untrusted
and inbox-restricted tokens cannot relaunch. A member connection may relaunch only
its own handoff; an owner-role connection retains its existing ability to relaunch
another connection's handoff. Retries outside `launch_failed` are refused.

When the hook's own successor command fails, it reports
`POST /api/handoffs/<id>/launch-failed`, which records `launch_failed`
and sends `handoff.launch_failed`; the old agent keeps working and
compacts as usual.

Thread storage stays local. Requests go to your configured Commonplace
gateway and your configured webhook or launcher URLs. Event webhooks
carry references only. A webhook launcher receives the handoff prompt
because it must start the successor.

## Idempotency and the chain

The default idempotency key is `thread:<client thread id>`, unique within
the workspace. Retries use the same key. Only one response claims the
right to launch the successor. Registering the same successor twice is
a no-op; registering a different successor for that handoff is rejected.

Saving the same source thread and content returns the existing stored
thread. When a retry carries the same thread with more messages at the
end (the agent kept working), only the new messages are appended to the
stored thread; the handoff and its successor stay the same. Other
different content under the same source ID is a conflict. A failed
storage attempt can be retried. A handoff stuck mid-store after a crash
is taken over by the next retry once its 10-minute lease runs out. A
recorded launch failure needs an explicit relaunch by a person owner, an owner-role
connection, or the original launcher connection to release its launch claim.
Agent connections still need the launch authority described above.

When a successor's own thread ID was registered with its handoff, its
next handoff links into the chain even without `COMMONPLACE_HANDOFF_ID`.

When the successor later hands off, its hook sends
`COMMONPLACE_HANDOFF_ID` as `parent_handoff_id`. This links the stored
threads. `thread_chain` lists the current thread and then older ones;
`thread_recall` searches that same readable chain by default. The handoff
document's earlier-thread pointers are also newest first.

The card view at `/app/cards/142` shows handoff state for `#142`, with the
old thread and the successor reference or URL.

## Limits and open questions

- A single `thread_get` reads at most 200 messages. Continue with another
  range; stored messages are never shortened.
- Handoff quotes are limited to 1,200 characters and at most eight
  decision quotes. Use their full-range pointers for the rest.
- Request limits are 64 MB for `/api/handoff` and 1 MB for
  other JSON requests.
- A Codex handoff happens at the start of the next turn, not mid-turn;
  see the Codex section. If that successor fails to start and the gateway
  restarts before it is explicitly relaunched, the relaunched successor doesn't
  get the user's unanswered message (the control database holds no text);
  the old session showed it was refused.
- Each stored thread is also written into Memory search in the background, in a private
  section. Structured mirror copies of private stored threads, facts derived from them,
  and their tunnel previews are returned only to callers who can read their source
  thread. This covers structured Memory search, `memory_get` and session start context.
  CLI-format (`cli_compatible`) search excludes mirror drawer copies for everyone, including owners; its `facts` field is still returned only to callers who can read each fact's source thread. A `superseded_by` id is returned only when its target is another kept fact in
  the same response. Mirror copies do not count as duplicates for anyone. Similarity
  dedup exclusion (D5) is verified on compact, sqlite_exact and chroma. Adding,
  updating, deleting or linking drawers in a stored-thread room requires permission to
  read that thread. Known exceptions: `delete_by_source`, `delete_tunnel` and
  `delete_hallway` take no room and are not checked yet. Fact mutations
  (`kg_invalidate`, `kg_supersede`, `kg_save` merges, and single-valued supersedes through `kg_save` or `fact_save`) on thread-derived facts are not
  room-checked. A member with fact-write permissions but no source-thread read access
  can mutate those hidden facts. Invalidate's `closed` count is a yes/no oracle for
  hidden values. This pre-existing write-side exposure is accepted for follow-up alongside `delete_by_source`, `delete_tunnel` and `delete_hallway`. Presence is not hidden either: taxonomy and aggregate counts can show stored-thread ids and per-thread counts to callers who can read the private section, fan-out ranks and counts can reflect hidden hits, and gating runs after a bounded fetch of at most 100 candidates, so hidden hits can take candidate slots.
- `handoff.completed` is queued in the same transaction that records the
  successor and delivered exactly once, also after a crash (it goes out
  when the gateway starts again) and with several gateways on one control
  database. A webhook receiver may still see one delivery twice if the
  hub crashed after the receiver accepted it; deduplicate by delivery id.
