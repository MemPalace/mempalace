# Optional machine runner

The runner lets a signed-in workspace owner or a granted coordinator request an agent start for a card the machine's worker could claim. It is inert by default: no machine means no change; an `unbound` machine refuses every start, including the owner's, and leases return 204. No config or service activation ships enabled. D1–D3 below remain open.

## Trust and authority

Only a signed-in person owner can enroll, attest, change grants, rotate credentials or revoke a machine through `/api/runners`. Enrollment returns separate machine and worker tokens once. Attestation records an owner statement as `owner_attested`, always with `verified:false`: **attested, not verified**. Topic or wing changes, worker rotation and revocation reset the binding to `unbound`; queued and leased jobs are cancelled on the next sweep.

Coordinators are CLI-created trusted PATs explicitly listed in the machine's `requesters`; OAuth and ordinary standard or untrusted connections do not qualify. A start checks both the requester's card access and whether the worker could claim the unassigned, released `todo` card in its write wing. It does not assign or claim the card.

The worker PAT bounds tools and wing access. Its tools are `task_show`, `task_take_next`, `task_update`, `task_comment`, `task_heartbeat`, `task_handoff` and `ask`; it cannot coordinate starts or perform owner actions. The fixed `SPAWN_MESSAGE` tells the agent to claim only the requested card, stop if refused, and touch no other card. Card scope from the spawn message is guidance only. A worker can claim any startable todo card in its write wing. Also, a card manually assigned to the worker by an owner remains editable outside its write wing under the existing assigned-card rule. The runner never assigns cards.

The T3 grant supplied through `t3_grant_env` is outside the worker PAT ceiling. It reaches whatever T3 permits; local runtime ceilings constrain an honest runner only. The default runtime ceiling is `approval-required`; a higher local ceiling needs `runtime_ack == "<label>:<mode>"`, and requests must also fit the hub grant.

## Local checks and checkout

Use `python -m commonplace runner check --config F` before `python -m commonplace runner run --config F [--once]`. The owner writes the JSON config with mode 0600 on POSIX. `check` never contacts the hub. Every topic needs a local `binding_acks` entry, `"<label>:<topic>"`.

Every configured project must have `shared_checkout_ack == "<label>:shared-checkout"`. A missing or different value makes both commands exit 4 before any hub contact, lease or spawn, with `runner: config_invalid: project <label> needs shared_checkout_ack "<label>:shared-checkout"`. Two project labels sharing one `project_id` also cause exit 4 (`config_invalid`): exclusivity is per distinct `project_id` on each machine.

The fixed workspace strategy is `{"type":"root"}`, **unverified until D2**. Its assumed meaning is that agents share the T3 project's checkout over time. It is not configurable. For isolation, point each project at a distinct T3 project rooted in a dedicated clone. `runner check` prints the shared-checkout warning and the uncaptured-launch-shape notice; passing it does not verify T3 behavior or worker identity.

## Capacity and uncertain launches

`max_in_flight` (default 1, range 1–4) counts this machine's `leased`, `starting` and `uncertain` jobs, plus `started` jobs held for `STARTED_HOLD_SECONDS = 3600` after `started_at`. Lease selection enforces both that machine cap and one in-flight agent per project. This bounds “agents started in the last hour or starting.” An agent running past the one-hour hold is uncounted, so another agent may start in its checkout. A real end-of-thread bound needs D3. The hold is fixed, not a config key; hourly starts and the queue have separate limits.

An ambiguous launch becomes `uncertain`, never automatically queued again. It keeps its machine/project slot and blocks another start on the card. An owner sees it with `card_start_status(task)` (or `GET /api/card-starts?task=<n>`), and inspects the machine's grant and binding through `GET /api/runners`. A person owner must investigate and use `card_start_resolve(job, outcome, thread_id?)`: `started` requires a thread id; `failed` releases the slot. A late `started` result from the same machine with its stored lease token can also resolve uncertainty.

`card_start_cancel` on `uncertain` returns 409 for everyone, including the owner: `start outcome is uncertain; a workspace owner must resolve it`. Cancelling a `starting` job only records the request; it does not stop a remote thread or free the slot. Resolve spans the control and workspace databases: a crash between commits can leave a resolution without its audit row, although the job retains `resolved_by` and `resolved_at`.

## Hub failures

Continuous mode backs off on disabled-machine 404s and transient failures (429, 5xx, redirects, timeouts, connection errors or non-JSON success bodies). The delay is `min(poll_seconds * 2**k, 300)` seconds for consecutive non-ok calls, reset by success. Redirects are never followed. Journals replay outcomes before new leases and never re-spawn a recorded launch. The hub timeout is at least 10 seconds to cover the plan delay of up to 2 seconds.

| Exit | Meaning |
|---|---|
| 0 | Success or no job; a fenced job (409) is dropped. |
| 2 | CLI usage/error path. |
| 3 | Machine credential revoked or expired; journal kept. |
| 4 | `config_invalid`, including missing checkout acknowledgement or duplicate project ids. |
| 5 | Hub unavailable or refused in `--once` mode; continuous mode backs off. |

## Chat answers

An enrolled machine can answer its enroller's own project-chat messages using
their CLI and login. This is a separate opt-in loop: no chat config or service
ships enabled. It needs an enabled, non-revoked machine, but no card-runner
binding attestation or agent-start grant. Members receive the basic receipt.
Answers are redacted and private to the poster, labelled
`via your {cli} login · {model}`; they never claim work or replace the card fallback.

### Config and commands

Both config shapes must be owner-written with mode 0600 on POSIX:

- **Chat-only:** exactly the three top-level keys `hub_url`, `token_env` and
  `chat`, as below. No card-runner or agent-launch configuration is needed.
- **Full card runner plus chat:** keep the existing full card-runner config and
  add the same top-level `chat` block. All its card-runner fields and
  acknowledgements remain required. `runner run` runs card starts;
  `runner chat` runs chat answers. Run the loops separately if both are wanted.

Replace the hub placeholder and absolute executable paths with your own values;
use models offered by your own CLI login. The Codex example uses `your-codex-model`.
`hub_url` requires HTTPS, except that loopback HTTP is allowed.

```json
{
  "hub_url": "https://<your-hub>",
  "token_env": "COMMONPLACE_RUNNER_TOKEN",
  "chat": {
    "default_cli": "claude",
    "context": "minimal",
    "relay_always": false,
    "clis": {
      "claude": {
        "argv": ["/absolute/path/claude", "-p", "--tools", "", "--strict-mcp-config", "--no-session-persistence", "--model", "{model}"],
        "default_model": "claude-haiku-5-5"
      },
      "codex": {
        "argv": ["/absolute/path/codex", "exec", "--sandbox", "read-only", "--ignore-user-config", "--ephemeral", "--skip-git-repo-check", "-m", "{model}", "-"],
        "default_model": "your-codex-model",
        "tools": true
      }
    }
  }
}
```

Supply the machine token through the environment variable named by `token_env`,
never in the JSON or argv. Validate locally, then start the chat loop:

```sh
python -m commonplace runner check --config F
python -m commonplace runner chat --config F
```

`check` contacts neither the hub nor a model. Both commands accept either shape;
`runner run` refuses a chat-only file with exit 4. `runner chat --config F --once`
makes one lease request, marks the machine online, answers at most one job and
exits: 0 after a job or none (a fenced job is dropped), 3 when the hub is unreachable or the
machine credential is missing, revoked or expired, and 5 when the hub answers with another
HTTP failure. An answer uses at most three POSTs total (the initial POST plus two retries)
on HTTP status 500 or higher while the lease is valid. Failure reports use the
same bounded retry. The project's `main` model choice selects the CLI label and model; without
one, `default_cli` and its `default_model` apply. An unavailable CLI receives no
job; there is no silent provider fallback.

`chat` accepts only `default_cli`, `clis`, `context` and `relay_always`.
`default_cli` must name one of the 1–8 `clis` entries. Each entry requires
`argv` and `default_model`, with optional boolean `tools`. The first argv element
must be an absolute path to an existing executable file. `{model}` must be a
whole element, never embedded in `--model={model}`. Unknown keys are rejected;
polling and timeout are fixed constants.

### Tools and CLI versions

The Claude tools-off argv above was checked against **Claude Code 2.1.308**
`--help`. Keep the empty string after `--tools`: it disables built-in tools.
`--strict-mcp-config` with no `--mcp-config` prevents MCP servers from loading;
`--no-session-persistence` disables session persistence. Use `-p` or `--print`.
Do not use `--bare`: it disables Claude Code's subscription (OAuth) login.
Re-check the flags after upgrading the CLI; a real subscription-backed answer
still needs verification on your machine.

Without `tools: true`, validation requires that tools-off shape and rejects
`--dangerously-skip-permissions`, `--allow-dangerously-skip-permissions`,
`--allowedTools`, `--allowed-tools`, `--mcp-config`, `--add-dir`, `--settings`,
`--plugin-dir`, `--agents`, `--agent` and `--permission-mode`, including their
`--flag=value` forms.

**Every executable except `claude`, including Codex, requires explicit
`"tools": true`.** Validation uses the executable's basename, not its CLI label. This
acknowledgement bypasses tools-off validation, including for a Claude entry.
The Codex flags were checked against **codex-cli 0.163.0** `--help` and are
hardening advice: `--sandbox read-only` still permits model-generated shell
commands and local reads. `--ignore-user-config`, `--ephemeral` and an empty
working directory reduce inherited configuration and persistence. Re-check
these flags after upgrades. If your CLI mixes progress with answer text on
stdout, use its `--json` output and an owner-written wrapper that emits only the
answer; that wrapper also requires `tools: true`.

### Context and egress

By default, `"context": "minimal"` sends your new message, the project name and
up to 15 trusted-origin card titles with status. It excludes other participants'
chat, assignees, start jobs and untrusted-origin card text. The context is
redacted, bounded and built only when your own machine leases a job; it is not
stored in the relay queue.

An enrolling owner's local `"context": "full"` opt-in adds the last 10 chat
lines from all participants, up to 15 visible card titles and assignees, start jobs
and the decisions count. These settings have no hub-side web controls.

`runner check` and chat startup print this egress statement:

```text
chat: answers run your configured CLI under your own login; each prompt (your message, the project name and up to 15 trusted card titles) goes to that CLI's provider
```

With full context they also print:

```text
chat: context full: prompts also carry recent chat lines from everyone in the project and up to 15 visible card titles
```

For each acknowledged CLI they print the following, replacing `{label}`:

```text
chat: {label} runs with its tools on (tools: true); chat text can steer those tools, including reading local files and sending them to the CLI's provider
```

The hub makes no model call for runner answers. Machine-only chat relay routes
under `/api/runner/chat/` carry redacted chat content to the poster's own runner
and answer text back; the card-runner actions carry no user content.
The runner launches argv without a shell in a fresh empty temporary directory,
passing the prompt through stdin. It retains your CLI login environment but
removes Commonplace credentials. The runner never prints or writes prompts or
answers to disk, its journal or logs. With `tools: true`, the CLI's own file
access and session persistence are your responsibility. Redaction is not a
guarantee that every sensitive detail is detected.

### Routing and limits

Chat answers require front answers enabled with `provider: "none"`. An
explicit server provider takes precedence, with no fallback in either direction;
server-key mode stays off by default. The relay steps aside while the project's
main orchestrator is connected, wakeable through a space webhook and used within
the last 600 seconds. Local `"relay_always": true` overrides that precedence;
its default is false. A later public orchestrator reply can coexist with the
private runner answer.

| Limit | Value |
|---|---|
| Poll interval | 3 seconds (`POLL_S`). |
| CLI timeout | 60 seconds (`CLI_TIMEOUT_S`). |
| CLI stdout | At most 64 KiB; an overrun kills the CLI and records `bad_output`. |
| Online window since last lease request | 90 seconds (`CHAT_ONLINE_S`). |
| Queue wait | 120 seconds (`CHAT_WAIT_S`), then `expired/no_runner`. |
| Lease duration | 120 seconds (`CHAT_LEASE_S`), then `failed/lease_expired`. |
| Main freshness | 600 seconds (`CHAT_MAIN_FRESH_S`). |
| Daily cap | 200 jobs per user per workspace per UTC day (`DAILY_CAP`); skipped jobs do not count. Subscription limits also apply. |
| Concurrent leases | One per machine; the first of several runners to lease wins. |
| Context JSON | 49,152 UTF-8 bytes (`MAX_CONTEXT_JSON_BYTES`). |
| Answer request text | At most 16,384 UTF-8 bytes (`MAX_ANSWER_BYTES`). |
| Route request body | At most 20,480 bytes (`MAX_BODY_BYTES`). |
| Returned answer | Cut to 2,000 characters before redaction; returned answer at most 8,000 bytes. |

Latency is roughly one poll interval plus CLI runtime. Failed or expired jobs
are never re-queued. The receipt remains on failure, and chat status shows
today's usage and the latest error, such as `cli_failed`, `cli_timeout`,
`cli_not_configured`, `bad_output`, `no_access` or `daily_limit`. Late, duplicate
or wrongly credentialed answers get 409 and are discarded. Chat command exits
are 0 for success/no job, 3 for missing or invalid credentials, 4 for invalid
config and 5 for a hub failure in `--once` mode.

## Owner checklists (open; no agent performs any item)

**D1–D3 (decisions).**
- [ ] **D1 worker identity.** The owner decides whether "attested, not verified" is acceptable. The setup would be a dedicated T3 provider instance whose Commonplace MCP entry uses the worker token, mapped to the granted topics, recorded through attest. Anything stronger needs a future T3 per-thread credential field. The card goal is not done until D1 is decided.
- [ ] **D2 launch shape.** One owner-approved real `t3_thread_launch` capture on an owner machine. It settles `structuredContent.threadId/runId/status`, `workspaceStrategy` `root` vs. `existing_worktree`, `modelSelection`, and what "modes cannot exceed yours" means for the T3 grant. Record the artifact path. An optional later detective read-back (the `t3.py:115–148` pattern) is design only.
- [ ] **D3 recovery.** Capture the `thread list --json` keys before any reconcile. Until then, `uncertain` goes to a person resolve.

**Activation (F10; strict order, each a separate owner act).**
1. [ ] D1 decision recorded.
2. [ ] D2 capture artifact path recorded; the parser contract **and the `root` workspace strategy** re-checked against it (OD-B).
3. [ ] T3 provider instance and topic mapping set up by the owner; T3 grant scoping known, or its absence accepted.
4. [ ] Only then: a real `POST /api/runners` enrollment and `attest` in the owner's own session.
5. [ ] Runner config written by the owner (0600), with `shared_checkout_ack` for each project only after the owner accepts that agents work in that project's checkout (or points the project at a dedicated clone, OD-B); `runner check` exit 0. Any service install or activation is a separate approval.
