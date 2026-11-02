# Commonplace gateway

One gateway in front of MemPalace: accounts, workspaces, scoped tokens, verified authorship and an audit log. Each workspace gets its own `mempalace` worker on loopback, started on first use and stopped when idle. Design: [PRODUCT-DESIGN.md](../PRODUCT-DESIGN.md) §4–7.

```bash
commonplace init --admin-email you@example.com
commonplace token add grok:coordinator --workspace personal --client grok \
  --profile slim --read-wings shared,inbox --write-wing inbox --trust untrusted
commonplace serve --host 127.0.0.1 --port 8765
```

`token add` prints the `cmmn_…` token once on stdout. Clients send it as `Authorization: Bearer cmmn_…` to `POST /mcp`. Data lives in `$COMMONPLACE_DATA` (default `~/.commonplace`): `control.sqlite3` plus workspace generation directories. `workspaces/<id>` is the stable generation-pointer container; use `commonplace doctor --workspace <workspace>` to find the active path.

Social sign-in: [provider setup](SETUP.md).

## Mail

The signed-out app accepts an email address and sends a single-use sign-in link, valid for 15 minutes. New accounts and personal workspaces are created only after verification with `COMMONPLACE_SIGNUP=open`. Signup is closed by default; existing accounts can still sign in. The request gives the same neutral response for unknown addresses and delivery failures.

`POST /app/auth/email` accepts JSON or form data with `email` and optional `next`. The mailed link opens `/app/auth/email/verify?t=…` and starts a normal web session. Only safe app or OAuth return paths are accepted. Requests share the credential IP limit (10/minute) and a normalized-email limit (3/15 minutes).

`commonplace login-link <email> --send` emails the sign-in link to the account's address and prints only `login link sent to <address>`; the link itself is never printed. Without `--send` the link is printed as before. The link uses `--base-url`, else `COMMONPLACE_APP_URL`, else `COMMONPLACE_BASE_URL`.

Mail is off until `COMMONPLACE_SMTP_HOST` and `COMMONPLACE_SMTP_FROM` are set. With mail off, `--send` exits with `mail not configured` and creates no link. If sending fails, the new link is deleted.

| Variable | Default | Meaning |
|---|---|---|
| `COMMONPLACE_SMTP_HOST` | unset (mail off) | SMTP server |
| `COMMONPLACE_SMTP_PORT` | 587, or 465 with SSL, or 25 without TLS | SMTP port |
| `COMMONPLACE_SMTP_FROM` | required with a host | From address, e.g. `Commonplace <noreply@example.com>` |
| `COMMONPLACE_SMTP_USER`, `COMMONPLACE_SMTP_PASSWORD` | unset (no auth) | Set both or neither |
| `COMMONPLACE_SMTP_STARTTLS` | `true` | `false` sends without TLS, e.g. to a relay without auth; with credentials only when the host is loopback |
| `COMMONPLACE_SMTP_SSL` | `true` on port 465, else `false` | Implicit TLS; set only one of SSL and STARTTLS |

Certificates are always verified. The password is never logged or shown in errors.

Claude Code: [local setup and repeatable fresh-install check](../docs/commonplace-client-setup.md).

## Hosted plans

Set `COMMONPLACE_PLANS=on` to enable hosted plans. New email and social sign-ups receive Free:
250 MB storage per account and 10 minutes of server voice transcription per UTC day.
The chat-sync interval hook is every 15 minutes for Free and live for Paid; sync is not built here.
Paid has *unlimited*\*\* storage and transcription and a server-load throttle hook.

** zolang de server het aankan / as long as the server can handle it

Admins manage plans with `commonplace plan set <user email|id> free|paid|none` and inspect
usage with `commonplace plan show <user email|id>`. Payment is not built. The transcription
meter is a hook for a future endpoint. Accounts without a plan have no limits and every feature.
Unset, empty or `off` keeps plans off: self-hosted installations have no limits or plan UI.

## Context handoff

Optional handoff saves a full thread in Memory and starts a fresh agent with a pointer to it. It is installed with the gateway and off by default. Thread tools appear once a workspace enables handoff or has stored threads. Pages use the hub's web sign-in; events use its signed webhooks with the `handoff` preset. See [setup and API](../docs/handoff.md).

## Policy

| Field | Meaning |
|---|---|
| `--profile` | `slim`, `standard` or `full`; or `--tools` with an explicit list |
| `--scopes` | `memory:read`, `memory:write`, `memory:delete`, `kg:read`, `kg:write`, `events:read`, `events:write`, `private:read`, `private:write`, `admin` |
| `--read-wings` / `--write-wings` | wing names or patterns (`project_*`, `*`) |
| `--write-wing` | where an `untrusted` token's writes always land |
| `--trust` | `untrusted` (memory inbox writes only, no memory update, delete or graph writes; own board cards remain manageable), `standard` (no delete), `trusted` |

- `mine`, `sync`, `hook_settings` and `reconnect` are never served through the gateway.
- Wings named `private` or `private_*` also need `private:read` / `private:write`, even under `*`.
- Tools that read or change data across wings (status, graph, tunnels, events) need every wing.
- The knowledge graph has no wings, so reading it, or invalidating or superseding a fact, needs every wing. `kg:write` alone allows adding facts. Untrusted tokens never write the graph.
- A hidden drawer answers exactly like a missing one in every tool.
- A tool the token can't use is missing from `tools/list` and answers `Unknown tool`. A drawer it can't read answers `Drawer not found`.
- Searches without a wing run once per readable wing and merge by score.

The canary suite (`tests/test_commonplace_canary.py`) seeds forbidden wings and calls every MemPalace tool with scoped tokens; run it before every push.

Opt-in compact workspace storage uses a verified offline backfill and rollback. See [compact store switching](../docs/compact-store-switch.md) for backup requirements, commands and retention.

Agents create and claim their own board cards with every client preset. Child-created cards need direct-parent or signed-in-owner approval; orchestrators can create assigned child cards directly. See [the board API](../docs/boards/API.md) for `task_approve`, `task_heartbeat`, expiry, review requirements, and space creation.
