# Going public: checklist

Enable public access only after the operator verifies authentication, backups, DNS and TLS.

## Hosts

| Host | Serves | Proxied to the gateway |
|---|---|---|
| commonplace.run (+ www → 308) | Website (static, Caddy) | nothing |
| app.cmmn.dev | The app | `/app*`, `/api/*`, `/health*` |
| cmmn.dev | MCP, OAuth, short links, short-name redirects | `/mcp`, `/oauth/*`, `/.well-known/*`, `/s/*`, `/health*` |

Caddy template: `deploy/commonplace-public.caddy.example`. Copy it to an operator-owned file, replace documentation bind addresses and the static root, and validate the host configuration before reloading Caddy. The private site is configured separately.

## DNS (Cloudflare, DNS only: grey cloud, so Cloudflare never sees plaintext)

| Zone | Name | Type | Value |
|---|---|---|---|
| commonplace.run | @ | A | 192.0.2.30 |
| commonplace.run | @ | AAAA | 2001:db8::30 |
| commonplace.run | www | CNAME | commonplace.run |
| cmmn.dev | @ | A | 192.0.2.30 |
| cmmn.dev | @ | AAAA | 2001:db8::30 |
| cmmn.dev | app | A | 192.0.2.30 |
| cmmn.dev | app | AAAA | 2001:db8::30 |

Preserve unrelated DNS records. Add CAA `0 issue "letsencrypt.org"` on both zones (optional).

## Operator environment

Use the operator-owned environment file described in `commonplace/DEPLOY.md`.

```
COMMONPLACE_SITE_URL=https://commonplace.run
COMMONPLACE_APP_URL=https://app.cmmn.dev
COMMONPLACE_AGENT_URL=https://cmmn.dev
COMMONPLACE_COOKIE_DOMAIN=cmmn.dev
COMMONPLACE_ALLOWED_HOSTS=app.cmmn.dev,cmmn.dev,hub.example.com,192.0.2.10,127.0.0.1:8765
# Optional social sign-in (set every variable for each enabled provider):
# COMMONPLACE_GOOGLE_CLIENT_ID, COMMONPLACE_GOOGLE_CLIENT_SECRET
# COMMONPLACE_GITHUB_CLIENT_ID, COMMONPLACE_GITHUB_CLIENT_SECRET
# COMMONPLACE_APPLE_CLIENT_ID, COMMONPLACE_APPLE_TEAM_ID,
# COMMONPLACE_APPLE_KEY_ID, COMMONPLACE_APPLE_PRIVATE_KEY
# COMMONPLACE_X_CLIENT_ID, COMMONPLACE_X_CLIENT_SECRET
# COMMONPLACE_X_DAILY_CAP (optional, default 6)
# COMMONPLACE_DISCORD_CLIENT_ID, COMMONPLACE_DISCORD_CLIENT_SECRET
# COMMONPLACE_SIGNUP=closed turns off new-account sign-up.
```

Provider registration and signup policy: [social sign-in setup](../commonplace/SETUP.md). Apply optional variables through the gateway's environment and restart it; no provider variables means no change to sign-in.

OAuth issuer and every metadata URL then come from `https://cmmn.dev`; sign-in and app links from `https://app.cmmn.dev`. Existing tokens keep working. Short links retain the base URL used when they were created; links using a private host resolve only where that host is reachable.

## Phone notifications (optional)

Phone notifications are off unless the hub has valid configuration. With no
`COMMONPLACE_NTFY_URL`, no dispatcher thread or outbound call runs. The owner
must complete the checklist below before activating a real hub; agents do not
provision or activate this service.

| Variable | Value |
|---|---|
| `COMMONPLACE_NTFY_URL` | Self-hosted ntfy base, e.g. `https://ntfy.example`; no credentials, query, fragment, `;`, `@` or path beyond `/` |
| `COMMONPLACE_NTFY_TOKEN` | Optional raw token matching `[A-Za-z0-9_-]{1,128}`; no `Bearer ` prefix, spaces or line breaks; hub only |
| `COMMONPLACE_APP_URL` | Phone-reachable app base, e.g. `https://cp.example` |
| `COMMONPLACE_AGENT_URL` | Phone-reachable short-link base, e.g. `https://cp.example` |

The app and agent hosts must not be localhost, loopback or unspecified
addresses. Use HTTPS for the ntfy server; plain HTTP is accepted only for the
local and private addresses the webhook URL rule allows. Invalid configuration disables phone push and logs one
fixed warning naming only the variable. Apply configuration in the hub's
operator-owned environment and restart only after the activation approval.

In Settings, signed-in owners opt in for their home workspace; members cannot.
All three categories (`needs_you`, `blocked`, `done`) are enabled together.
Read the [behavior, privacy disclosure and delivery limits](../docs/boards/API.md#phone-notifications)
before activation. Server acceptance is separate from phone receipt.

Record each item as a separate explicit owner act before device acceptance:

- [ ] **O1 — Server.** Record the self-hosted ntfy version. Configure `auth-file`
  (or `database-url`) authentication and `auth-default-access: deny-all`.
- [ ] **O2 — Hub credential.** Create a dedicated non-admin ntfy user with only
  `write-only` on `cp*`, no read access and no other topic under deny-all. A
  token inherits the account's full authority; never use an admin or shared
  account. The owner creates the user and token; agents create no user, ACL,
  token or topic. Set `COMMONPLACE_NTFY_TOKEN` only on the hub.
- [ ] **O3 — Phone read credential.** Give each person a separate non-admin user
  with exact-topic read access. Name who provisions it after each Turn on
  (the topic changes), and how it is revoked at Turn off.
- [ ] **O4 — Activation.** Explicitly approve setting `COMMONPLACE_NTFY_URL` on
  the real hub and any Caddy or DNS route.
- [ ] **O5 — Product.** Confirm the built default, pause after sign-out or
  session expiry, and home-workspace-only coverage. The pause default stands
  until the owner answers. Choosing “keep pushing after sign-out” requires an
  explicit product change before activation.
- [ ] **O6 — iOS upstream.** Keep `upstream-base-url` unset unless the owner
  approves the disclosure described in the privacy section.
- [ ] **O7 — Device acceptance.** Verify one real needs-you, one blocked and one
  done notification arrive, and a tap opens the right card. Record this
  separately from server acceptance.

## Public surface

- [ ] Every gateway route answers 401 without credentials, except: `/health`, `/healthz`, `/app/` static files, `/app/login`, `/app/auth/*`, `/s/<code>`, `/.well-known/*`, `/oauth/register|authorize|token|revoke` (test: `tests/test_commonplace_public_surface.py`).
- [ ] Rate limits on (default): unauthenticated and failed auth 30/min per IP; login and OAuth endpoints 10/min per IP; 600/min per token; untrusted tokens 60/min and 120 writes/hour; short links 120/min per IP.
- [ ] Client IP from Caddy's `X-Forwarded-For` only (with no trusted proxies configured, Caddy replaces any client-sent value with the real peer).
- [ ] Host check on (`COMMONPLACE_ALLOWED_HOSTS`): other Host headers get 421.
- [ ] No debug endpoints, no tracebacks in responses, no tokens or memory text in logs.
- [ ] Request bodies capped (8 MiB MCP, 1 MiB REST).
- [ ] Secure, HttpOnly, SameSite=Lax session cookie for `.cmmn.dev`; login links are direct (never short links) and work once.
- [ ] `mine`, `sync`, `hook_settings`, `reconnect` are never served through the gateway (hub core).
- [ ] Grok Bot tokens: slim, untrusted, write only to `inbox`.
- [ ] Backups: nightly hook ran at least once and a restore was tried.

## Go steps

1. DNS records above (Cloudflare API token or by hand).
2. Add the environment lines to the operator-owned `.env`; deploy with the external `COMPOSE_FILE` and env paths described in `commonplace/DEPLOY.md`.
3. Install the filled-in Caddy site in the operator-owned configuration, validate the full configuration and reload the host service.
4. Check: `curl -I https://commonplace.run`, `curl https://cmmn.dev/.well-known/oauth-protected-resource`, `curl -i https://cmmn.dev/mcp` (401 with `resource_metadata`), sign in on `https://app.cmmn.dev/app/`.
5. claude.ai: Settings → Connectors → Add custom connector → `https://cmmn.dev/mcp`. ChatGPT: same URL as a developer-mode connector.

Rollback: restore the previous operator-owned public Caddy site and reload Caddy. DNS can stay.
