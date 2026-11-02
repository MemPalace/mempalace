# Developer review tools

These optional POSIX command-line tools help review repository changes. They are not imported by the memory service and do not enable external processing of stored memories. The review client sends only the explicitly selected review input to the configured review provider. Review requires deliberate configuration and promotional credit; installing the source does not authorize a paid request.

## Blind code review

Code review defaults to `--mode blind`. Supply the original problem in a separate file with `--problem`; do not use the builder's explanation as the original problem. The model receives the problem, the diff and review policy. PR descriptions, titles, branch names, commit messages, earlier feedback and check results stay outside its input. `--context` is refused. Comments inside the diff remain visible; this is a reduction in confirmation bias, not a prompt-injection guarantee. The problem digest records what was supplied, not who originally wrote it.

For a PR, pass `--repo`, `--pr`, `--repo-path` and a full forty-character `--head`. The local clone must contain the pinned head and target-base commit objects. The tool derives its review diff and regression baseline from their immutable merge-base. It never uses a separately fetched, mutable PR diff in blind mode. The verdict names the full reviewed head. Integration checks against the current target branch remain separate requirements.

For a local code gate, pass `--diff`, `--repo-path`, `--base` and `--head`. The supplied diff must exactly match the Git-derived diff, including full object identifiers. Generate it with `git --no-replace-objects diff --no-ext-diff --no-textconv --binary --full-index BASE...HEAD`. Object resolution ignores replacement refs, and review diff generation never invokes external diff or text conversion commands.

Plan reviews keep the contextual workflow with explicit `--mode plan --purpose review`. Plan mode accepts `--context` (at most 8000 bytes) and a plan-specific checklist. It cannot be used as a merge gate.

## Regression evidence

The default change kind is `bugfix`. A bugfix requires a trusted external runner configuration, explicit changed test paths, pinned commits, a private evidence destination and explicit permission to execute the runner. A truthful `--change-kind feature` is available for work that does not correct a bug; the model independently classifies the original problem and diff without seeing that declaration. A bugfix classification with no verified regression evidence forces FAIL; a missing classification forces ERROR. Intent classification is a review responsibility, not a machine-verifiable fact.

See [regression runner contract](review_support/REGRESSION.md). The runner owns process/network isolation and dependency provisioning. PR-controlled tests execute on that runner's host. The review client does not provide a sandbox or execute a command selected by the PR. Missing or unsupported runner configuration blocks the bugfix before review credentials are loaded. Both-pass, skips, collection/setup errors, timeouts and mismatched revisions cannot prove a regression.

The recorded baseline is the branch's immutable merge-base, not a claim that the current target branch was tested. A separate integration check must still test the change merged onto the current target branch.

## Promotional credit policy

The portable client refuses paid requests until an operator supplies a policy with `--policy`. The policy is local configuration, outside any checkout; do not commit real credit/account values or credentials. It contains `cap_usd`, `free_credit_usd`, and an ISO date `expires`. The cap must be positive and cannot exceed the configured free credit. An optional `session` specifies timezone-aware `start` and `end`, `cap_usd` and the shared ledger's `baseline_usd`; requests outside that window or above its remaining allowance are refused. The global cap includes actual ledger spend plus in-flight worst-case reservations. No payment method or credit reload is configured by this tool.

Review credentials remain in the protected local credential file used by the client. They are loaded only after input and regression preflight and credit checks. They are not exported to tests, passed to helpers or written to logs. Verdicts and ledger timestamps use UTC. Default verdict/evidence directories are created with mode0700; existing nonprivate or symlink directories are refused before credential loading. Default regression evidence gets a fresh name per invocation; existing caller-supplied evidence is never accepted as proof. Verdicts and regression evidence are private artifacts. The executor sends the frozen prepared payload; later edits of input files cannot change the serialized review request. Do not commit them or post them to a public PR. A public review comment contains only the reviewed full SHA and PASS or CHANGES.

The source is staged developer tooling. It does not replace an installed client, migrate a ledger, install a runner or activate a schedule. Before any installation, preserve existing account/session constraints and shared ledger paths in the local operator policy. A dry run produces no verdict and sends no paid request.

## Repeat-failure audit

See [audit operation](review_support/AUDIT.md). Audit runs offline by default, groups exact structured failure reasons across independent reviewed inputs, and proposes a check or rule rather than activating one. Publication uses an ordinary configured agent connection, not database writes. The weekly service/timer files are inert templates; configuration and installation remain an explicit rollout step.
