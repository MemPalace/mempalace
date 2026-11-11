## sync

- **Paths:** `commonplace/sync_cli.py`, `commonplace/sync_daemon.py`, `commonplace/sync_feed.py`, `commonplace/sync_protocol.py`, `commonplace/sync_service.py`, `commonplace/sync_sessions.py`, `commonplace/sync_sources.py`, `commonplace/sync_state.py`
- **Owns:**

  - Transcript-only entry consent, legacy read/rewrite guards and filtered active-operation scheduling.

  - Opt-in client synchronization of transcripts and memory, its local state and the content-free live feed.

  - `--source agent-log:ROOT` selects a directory of agent-log JSONL files for any agent; it needs an explicit root and its own `COMMONPLACE_CAPTURE_TOKEN_AGENT_LOG` connection.

- **Promises:**

  - Explicit exact-endpoint consent precedes token/state access. Disabled legacy rows and schema survive unchanged; memory roots are never inspected. Caught-up transcripts finish backfill without legacy allowance use.

  - Sync never opens a server database and never reads credentials; local state holds metadata only.

- **How it fails:**

  - Endpoint consent does not bind workspace/principal identity; a different token at the same hub remains possible. Old services retry rejected configs until explicit reinstall or uninstall.

  - A lost connection keeps the outbox and resumes from the cursor; a corrupt local cache is rebuilt from the server.

- **Tests:**

  - `tests/test_card199_safety.py` covers entry order, each discriminator, direct scanner/attempt/rearm guards, retained rows, backfill, status and private preview output.

  - The `tests/test_commonplace_sync_` files and `tests/test_commonplace_agent_log_sync.py`.
