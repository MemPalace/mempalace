## browser-github-grants

- **Paths:** `commonplace/github/`
- **Owns:**

  - Explicit per-person GitHub browser grants, one-use connect flows, encrypted pending consent and direct bounded transport.
- **Promises:**

  - Never-owner workers retry claim and recovery on heartbeat ticks; former owners stay fenced. Serialized, idempotent shutdown deletes only the matching owner lease once, before server teardown returns or SQLite closes; standby and lost-lease logs explain readiness.

  - Disconnect purges grants and confirmations even when ciphertext or the wrapping key is unreadable, marking remote revocation unconfirmed without retrying unavailable credentials.

  - Browser sessions use exact Origin and session-derived CSRF; scope evidence and expiry fail closed; GitHub login and connect share subject ownership.
  - **Browser GitHub connection:** Default-off consent requests an empty scope, reads only the GitHub id/login and never creates login identities.
  - **Connection lifecycle proof:** Durable remote-work accounting holds the reconnect fence until revocation and older token cleanup both settle; only HTTPS or loopback development may start consent.
  - **Connection recovery:** Known expiry purges locally and reconnects without revocation acknowledgement; duplicate scope headers are all checked, and cleanup settles exchange counters even on failure. Own-person disconnect remains available across workspaces and while consent is disabled.

- **How it fails:**

  - Unknown expiry, scope conflict or missing keys refuse connection. Startup recovery requires an absent or expired shared-database lease; a competing serve cannot write GitHub state until its periodic retry claims after release/expiry; live OAuth behavior and production ingress logging remain unproven.
- **Tests:**

  - `tests/test_github_grants.py`, `tests/test_github_ui.py`, `tests/github_render.mjs`.

  - `tests/github_lifecycle.mjs` checks connected rendering, card switches and logout; a shared-volume lease fences recovery across private temporary directories without any lifetime lock file. Heartbeats renew the 30-second lease every 5 seconds, and expired owners fail closed on state writes.
