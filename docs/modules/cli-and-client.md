## cli-and-client

- **Paths:** `commonplace/cli.py`, `commonplace/client.py`, `commonplace/client_hooks.py`, `commonplace/hook_spool.py`, `commonplace/client_install.py`, `commonplace/review_rules.py`, `commonplace/bridge.py`, `commonplace/onboarding.py`, `commonplace/seed.py`, `commonplace/README.md`, `commonplace/SETUP.md`, `commonplace/agent-skill/`
- **Owns:**

  - The `runner report` argument parser; the command itself lives in `commonplace/runner/report.py`.

  - `commonplace waitlist list|admit` (instance admin, `--by` as for `plan set`) and the `COMMONPLACE_MAX_ACCOUNTS` setup note in `SETUP.md`.

  - The `commonplace` command line, the credential-scoped client, native hooks and installers, the stdio bridge and the agent skill.

- **Promises:**

  - `runner report` sends only the values it was given for one started run; it never opens the data directory.

  - `commonplace token add` refuses `cards:decide`; answering Needs-me questions needs the owner's browser consent.

  - Local search-index status and run pass an explicit unscoped principal and retain their printed count keys and capture enrolment behavior.
  - Opt-in backup reporting records only the final selected mode; check/cleanup/restore never report. Receipt failure preserves backup return/exception; test_backup_status.py covers both outcomes and fallback.

  - Serve always closes its control database on exit, including SIGTERM unwind, releasing owned resources through pre-close callbacks.

  - Imported-history consent: CLI token creation rejects the imported-history reader scope before issuing a credential; browser consent is required.

  - The agent skill stays below 120 lines and links to the full opt-in splitting guide for delivery details.

  - The shipped agent skill routes routine questions to the authorized board coordinator, documents explicit person_reason and owner fallback, and permits only the coordinator to withdraw its own agent-directed decision while preserving history. Person decisions and self-approval remain protected.

  - The agent playbook requires inbox reads at session start, on wakes and every 10 minutes, stable keys, and completion markers.

  - The commonplace README documents the server voice settings (`COMMONPLACE_VOICE`, `COMMONPLACE_VOICE_MODEL_EN`, `COMMONPLACE_VOICE_MODEL_NL`) with their defaults.

  - CLI commands act through the same services as the network routes; installers never change trust policy; hooks fail open.

  - The native startup hook gives the hub `client_hooks.HUB_TIMEOUT_S` (0.4 s) and then fails open to the follow-up notice.

  - The capture hook only writes its payload verbatim to a private local spool (`hook_spool.py`: atomic, owner-only, bounded by item count and bytes) and starts a detached uploader; it never waits on the network. Uploads go oldest first, retry with exponential backoff, send a `client_op_id` per item and offset, and remove an item only after the hub confirmed it. A full spool drops the oldest items and logs each drop; unuploadable items are kept under `rejected/`.

  - The native startup hook adds a missing-rules notice unless its context opens with a complete effective rule set.

  - `commonplace review-rules` prints the effective rules as a reviewer prompt block that is never empty, and `--verify` accepts a verdict only when its last `Rules checked:` line names the current complete set.

- **How it fails:**

  - `runner report` exits 4 on an invalid config or report, 3 on a revoked credential and 5 when the hub refuses or is unavailable.

  - Local data-directory access remains operator authority; authenticated progress never inherits it.

  - The CLI runs with data-dir access, so anyone with the service's OS user can use it (residual in `SYSTEM.md`).

- **Tests:**

  - `tests/test_run_ledger.py` runs `runner report` against a hub.

  - `tests/test_search_index_progress.py` covers local status, run and explicit capture enrolment.

  - `tests/test_commonplace_cli.py`, `tests/test_commonplace_client.py`, `tests/test_commonplace_client_hooks.py`, `tests/test_commonplace_hook_spool.py`, `tests/test_commonplace_client_install.py`, `tests/test_commonplace_onboarding.py`.

  - `tests/test_rules_enforcement_points.py` covers the hook's missing-rules notice and the `review-rules` block, verdict check and exit statuses.
