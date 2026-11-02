# Offline repeated-failure audit

This optional developer tool proposes improvements from local structured review
artifacts. It makes no model/API requests, loads no review key, and never changes
rules. Publication uses only the ordinary authenticated MCP board route.

Run with Python 3.10+ on POSIX. The offline command needs only the standard library:

```sh
python tools/review_audit.py --verdict-root /private/review-verdicts
```

Dry-run is the default. It reads neither the MCP token file nor receipt state,
imports no MCP SDK, and performs no writes or network calls, even when connection
options are supplied. Output contains private review evidence; keep it private.

## Input and matching

Use a dedicated, owned, mode-0700 verdict directory. Only immediate `.json` and
`.md` files are inspected, in filename order. No recursion or symlinks are allowed.
Files are opened relative to the root with no-follow semantics, checked against
their resolved path and inode at open, and limited to 1 MiB each, at most 10,000
artifacts. JSON duplicate keys, non-finite numbers, invalid UTF-8 and malformed
structures are rejected. A Markdown artifact must have an explicit `json` fence;
only the **last** such block is considered. A malformed final block never falls
back to an earlier verdict. Unsupported artifacts are reported without guessing.

The structured record must contain:

```json
{
  "ts": "2026-01-08T12:00:00Z",
  "target": "example/project#12",
  "repository": "example/project",
  "head": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "diff_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "mode": "blind",
  "parsed": {
    "verdict": "FAIL",
    "findings": [{
      "severity": "major", "file": "example.py", "line": 8,
      "checklist": "error handling", "issue": "A failed write is retried.",
      "fix": "Keep the uncertain write blocked."
    }]
  }
}
```

`ts` must be ISO8601 UTC. Code mode is `blind`; code identity is `(repository,
full head)`. SHA-1 and SHA-256 Git object IDs are supported. When `repository` is
omitted it can be derived only from an unambiguous `owner/repository`,
`owner/repository#number`, or GitHub pull-request URL target. A local target
without repository identity is unsupported. Plan mode is `plan`, with identity
`(target, diff_sha256)` and no required head. Legacy artifacts qualify only if
these explicit fields and identities are present; prose metadata is never mined.

Finding locations require nonempty file text; null files remain unsupported. Line references may be null, a positive integer, or nonempty printable text of at most512characters (for example `8-10` or `Step2`). They are preserved as references, never guessed into numeric positions.

Only `FAIL` findings with `major` or `blocker` severity qualify. Fingerprints hash
severity, checklist, file and exact issue after whitespace normalization. Case,
punctuation and different files remain distinct; line and suggested fix are not
part of the fingerprint. Two distinct input identities within the inclusive
rolling seven-day window trigger one proposal. Future records are excluded.
Same-head reruns count once even if their diff changes. Output preserves verbatim
issue quotes, source artifact filenames and identities in stable order. Proposals
recommend a lint/check/rule and require owner approval; nothing is auto-enabled.

## Explicit publication and receipts

Install the optional official SDK in a dedicated tooling environment:

```sh
python -m pip install 'mcp>=1.26,<2'
```

Have a signed-in workspace owner create an ordinary agent connection through
the application's connection UI/API. Store its MCP connection token in a separate
owned mode-0600 file, inside an owned mode-0700 directory. Do not put tokens in
arguments, environment variables, logs, repository files or unit files. The file
must be distinct from any review-provider key; review environment/key files are
not accepted. The tool never seeds authentication or writes workspace databases.

First inspect the offline output, then explicitly publish to the intended board:

```sh
python tools/review_audit.py --verdict-root /private/review-verdicts \
  --state-dir /private/review-audit-state --endpoint https://board.example/mcp \
  --token-file /private/board-auth/connection.token --board improvements --publish
```

HTTPS is required except for loopback synthetic testing. URL credentials,
queries and fragments are refused; proxy environment and redirects are disabled.
Only `task_add` and, during explicit reconciliation, `task_show` are called. Cards
start as ordinary todo proposals; no claims, messages, rule writes or approval
calls are sent. The SDK is bounded by a 15-second operation deadline; the HTTP
transport has retries disabled. Errors never print token or server error text.

The owned mode-0700 state directory is bound to one endpoint and board. Keep it
separate from verdicts and token storage. A POSIX lock serializes invocation,
including network work. An atomic, fsynced mode-0600 intent is saved **before**
each create. A confirmed result saves the card number as its receipt. A rerun
skips receipts. A timeout, refusal, malformed receipt, or crash after intent is
conservatively `blocked`; other fingerprints may still publish. An interrupted
intent becomes blocked on the next publication, including when its evidence has
aged out. Dry-run does not recover or change state. Exit 2 means blocked writes;
exit 1 means a fatal input, state or connection error. This is not exactly-once
delivery. Never delete/reset state to retry an uncertain create.

## Reconcile an existing card

Inspect the ordinary board UI or normal board reads for the fingerprint shown in
the title/body. Then confirm its existing card number through this read-only MCP
path (the only write is the local receipt):

```sh
python tools/review_audit.py reconcile-existing-card \
  --state-dir /private/review-audit-state --endpoint https://board.example/mcp \
  --token-file /private/board-auth/connection.token --board improvements \
  --fingerprint FULL_64_CHARACTER_FINGERPRINT --card-id 42
```

`task_show` must return the intended board, card number, exact saved title and
body digest. A match replaces the blocked entry with a receipt. Future publish
runs skip it without retrying create. A mismatch remains blocked. If no matching
card exists, leave the entry blocked and investigate; this tool has no retry or
"assume absent" escape hatch. Preserve state backups with the same protections.

## Opt-in weekly schedule

`tools/review-audit.service` and `tools/review-audit.timer` are inert generic
templates. Repository publication does not install, enable or start either unit.
Rollout remains pending until an operator does all of the following:

1. Configure and verify the ordinary owner-created connection and protected paths.
2. Run the offline command and inspect the proposed-only output.
3. Adapt the service interpreter/source paths and its environment-file path. Put
   only `REVIEW_AUDIT_VERDICT_ROOT`, `REVIEW_AUDIT_STATE_DIR`,
   `REVIEW_AUDIT_ENDPOINT`, `REVIEW_AUDIT_TOKEN_FILE` (a path), and
   `REVIEW_AUDIT_BOARD` in the protected operator environment file.
4. Review the explicit `--publish` command and create the service's approval
   marker only after accepting publication to that board.
5. Install the templates in the operator's chosen systemd unit directory, then
   explicitly enable/start the timer. User units require adapting `/etc` paths.

The timer schedules weekly, with a randomized delay and missed-run catch-up.
It has no dependency on a model provider or the review credential file. Review
the next scheduled time and monitor local exit status/receipts after activation.
