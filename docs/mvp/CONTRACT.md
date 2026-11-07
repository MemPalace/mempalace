# Commonplace MVP: build contract

This file defines the module contracts. Product rules: the UI says Memory / memories / themes / people / cards. No palace jargon (wing, room, drawer, palace, closet) in anything a user sees. No taglines, helper text or secondary labels. Python: ruff (E/F/W), double quotes, snake_case. Tests: `tests/test_commonplace_<area>.py`; iterate with `uv run pytest -q tests/test_commonplace_<area>*.py`. Run integration checks before release. No GitHub Actions.

## The seam (already on feat/mvp, don't change without the coordinator)

- `commonplace/extensions.py`: `LocalTool`, `register()`, `ToolContext`, `ToolError`. A module listed in `extensions.MODULES` registers its MCP tools at import time and its HTTP routes in `install(gateway)`.
  - `ToolContext.principal` (a `control_db.Principal`), `.db` (ControlDB), `.workspace_dir()` (str path of the resolved active workspace generation), `.memory(name, args)` (runs a `mempalace_*` tool through the caller's policy; raises `ToolError` on refusal).
  - Handlers return `str` (sent as text) or JSON-able data. Raise `ToolError("no: …")` for refusals.
  - Profiles: `ALL_PROFILES` (slim, standard, full) or `WORK_PROFILES` (standard, full). `trusted_only=True` refuses untrusted tokens. `owner_only=True` hides the tool from non-owners.
- `commonplace/events.py`: `emit(db, workspace_id, kind, subject=, actor=, data=)`, `after(db, ws, seq, limit)`, `latest_seq`, `add_listener(fn)`. Kinds listed in the module docstring. `data` never holds memory text.
- `commonplace/sessions.py`: login links + session cookie (`cmmn_session`). `person_principal()` gives a signed-in person `user:<id>` with full view of their workspace.
- `Gateway.add_prefix_route(method, prefix, handler)`, `Gateway.principal_for(handler)` (Bearer `cmmn_…` or session cookie → Principal or None), `Gateway.call_memory_tool(principal, name, args)`, `Gateway.workspace_dir(ws_id)`, `Gateway.secure_cookies`.
- `Principal.actor` is `conn:<connection>` for tokens and `user:<id>` for people. `Principal.person` is True for web sessions.

## Shared HTTP helpers: `commonplace/http_util.py` (owned by the web worker, W4; others may import)

```python
read_json(h) -> dict            # 400 on bad JSON; max 1 MiB
send_json(h, status, obj)
send_text(h, status, text, content_type)
require_principal(gateway, h) -> Principal | None   # sends 401 JSON and returns None when missing
path_parts(h, prefix) -> list[str]                  # "/api/boards/x/cards/7" with prefix "/api/" -> ["boards","x","cards","7"]
query(h) -> dict[str, str]
```
Until W4 lands it, each worker may keep a private copy with the same names; the coordinator dedupes at merge.

## Boards (W1): `commonplace/boards/`

Store: `<workspace_dir>/boards.sqlite3`, schema from `docs/boards/schema.sql` plus these task columns: `repo TEXT`, `branch TEXT`, `pr_url TEXT`, `pr_number INTEGER`, `checks TEXT` (`pending|passing|failing|` empty), `status_line TEXT`, `heartbeat_at TEXT`, `labels TEXT` (JSON array). Move rules are in docs/boards/API.md: a board is created by `board_add` (signed-in member or trusted token; default shared scope) or on first `task_add` naming an unknown slug by a trusted token.

Service API (`commonplace/boards/service.py`), used by tools, REST and the other workers:

```python
open_store(workspace_dir) -> BoardStore
BoardStore.link_drawer(task_ref: str|int, drawer_id: str, actor: str, title: str = "") -> None   # used by memory_save(task=…)
BoardStore.task_links(task_ref) -> list[dict]   # [{"drawer_id","title","invalidated"}]
BoardStore.cards_for_drawer(drawer_id) -> list[dict]  # [{"number","title","status","board"}]
needs_me(store, principal) -> dict  # see below
```

MCP tools (all `profiles` per API.md; text replies, 10–60 tokens, errors start `no:`):

| Tool | Args | Notes |
|---|---|---|
| `board_list` | — | |
| `board_add` | `slug`, `name?`, `wing?`, `repo?` | trusted_only, all profiles |
| `board_set` | `board`, `coordinator` (connection or null) | standard/full, owner_only, trusted_only; active PAT agent with chat access; warns without a matching active webhook |
| `space_read` | `board`, `after?` or `last?` (1–100), `before?` | standard/full; board wing + memory:read; JSON with chronological verbatim messages, cursor, oldest and has_more |
| `space_post` | `board`, `text` | standard/full, trusted_only; board read/write wing + memory:write; append-only, verbatim |
| `task_list` | `board?`, `status?`, `mine?` | |
| `task_heartbeat` | `task` | holder-only claim renewal, including after expiry |
| `task_approve` | `task`, `approve?`, `version?`, `reason?`, `escalate?` | direct parent, top-level agent, board coordinator or owner decides proposals; direct parent, creator-parent, board coordinator or owner decides plan reviews; agents give a reason or escalate |
| `task_show` | `task` | includes branch / PR / checks / status line / links |
| `task_add` | `title`, `body?`, `board?`, `status?`, `assignee?`, `labels?`, `message_id?`, `claim?` | top-level defaults todo; claim=true or assignee=self starts doing; child-created cards are pending; parent-created child assignments are ready |
| `task_comment` | `task`, `text` | |
| `task_take_next` | `board?`, `task?` | |
| `task_update` | `task`, `title?`, `body?`, `status?`, `before?`, `after?`, `link?`, `unlink?`, `lesson?`, `repo?`, `branch?`, `pr?` (URL or number), `checks?`, `status_line?` | `status_line` also renews the claim and sets `heartbeat_at`; review requires `question`, `context` and at least two `options` |
| `task_handoff` | `task`, `to`, `note`, `options?` | `to` = `anyone`, `conn:<name>` / `<name>`, or a person (`avery`, an email, `user:<id>`). To a person = card goes to `review` = the inline decision card |
| `ask` | `question`, `options?`, `board?`, `task?` | Shortcut: without `task` it creates a card in `review` asking the board owner; with `task` it is `task_handoff` to the owner. Returns `#n waiting on <person>` |
| `decide` | `task`, `choice?`, `reply?`, `then?` (`return`/`todo`/`done`/`backlog`) | signed-in workspace owner only, including legacy agent-directed rows; all agent connections are refused. Answers the ask; default `then=return` |
| `needs_me` | — | owner_only. Text digest of `needs_me()` |

`needs_me()` returns:

```json
{"asks": [card…], "done_review": [card…], "backlog_from_agents": [card…],
 "ready_to_merge": [card…], "stale": [card…], "count": 7}
```
`card` (REST and needs_me):
```json
{"number": 145, "board": "commonplace", "title": "…", "body": "…", "status": "review",
 "assignee": "conn:codex:example", "claim_until": null, "ask": "…", "ask_options": ["1y","90d"],
 "ask_to": "user:usr_x", "return_to": "conn:claude-code:example", "flags": [],
 "repo": "github.com/example/project", "branch": "feat/x", "pr_url": "…", "pr_number": 12,
 "checks": "passing", "status_line": "Fixing the two failing tests", "heartbeat_at": "…",
 "labels": [], "links": 2, "version": 3, "created_by": "conn:…", "created_at": "…", "updated_at": "…",
 "position": 1.0, "message_id": null}
```

Events (via `events.emit`, subject `#<n>`): `card.created`, `card.updated`, `card.moved`, `card.commented`, `card.claimed`, `card.expired`, `card.handoff`, `card.review_needed` (a card enters review; `data.ask_to`), `card.decided`. `data` = `{"board","number","title","status","from","to","assignee","ask_to","actor"}` as applicable.

REST (people via cookie, agents via Bearer; same service, same rules):

```
GET    /api/boards                                  -> {"boards":[{"slug","name","wing","counts":{status:n}}]}
POST   /api/boards                                  {"slug","name?","wing?","repo?"}
PATCH  /api/boards/<slug>                           {"coordinator": connection or null} -> {"board","coordinator"}
GET    /api/boards/<slug>/chat?after=0               -> {"board","coordinator","messages":[message…],"cursor"}
POST   /api/boards/<slug>/chat                      {"text"} -> message (verified author stamps)
GET    /api/boards/<slug>/cards?status=             -> {"cards":[card…]}
POST   /api/boards/<slug>/cards                     {"title","body?","status?","assignee?","labels?","message_id?"} -> card
GET    /api/cards/<n>                               -> card + {"events":[{"kind","actor","text","at","choice","target"}],"linked":[{"drawer_id","title","invalidated"}]}
PATCH  /api/cards/<n>                               {"version", any task_update field} -> card (409 + current card on version clash)
POST   /api/cards/<n>/comments                      {"text"}
POST   /api/cards/<n>/handoff                       {"to","note","options?"}
POST   /api/cards/<n>/decide                        {"choice?","reply?","then?"}
GET    /api/needs-me                                -> needs_me()
```

## Memory (W2): `commonplace/memory_tools.py`

MCP tools (aliases over the `mempalace_*` tools through `ctx.memory`, so policy applies):

| Tool | Args | Maps to |
|---|---|---|
| `session_start` | `query?`, `board?`, `repo?`, `limit?` (default 3, maximum 5) | Effective rules plus policy-visible relevant quoted memories; all profiles, `memory:read`, underlying tool allowlists and visibility remain in force. Explicit read follows normal rules-served behavior. |
| `rules_publish` | `rules: [{key?, text, summary?, kind, scope?, board?, repo?}]`, `replace?`, `agent?: self \| '*'` (default `self`) | Verbatim proposals for the caller or All agents. Shared proposals need owner approval; no agent self-accept API. See [Rules](../design/RULES.md). |
| `memory_save` | `text`, `theme?` (wing; default: the token's write wing, else `shared`), `topic?` (room, default `general`), `task?`, `source?` (`{thread, seq_start?, seq_end?}`: the chat messages the words came from) | `mempalace_add_drawer`; then `link_drawer` when `task` given. Returns `saved <id>` (+ `linked to #n`) |
| `fact_save` | `subject`, `predicate`, `object`, `theme?` (wing), `source` (required: `{thread, seq_start?, seq_end?, url?}`; `url` required for chats outside Commonplace or T3), `valid_from?`, `sensitivity?`, `single_valued?` | `mempalace_kg_save` under the token's KG wing rules; refuses text the privacy scan flags as a secret. Returns `saved <id>`, `merged into <id>`, `saved <id>, replacing "<old>"` or `saved <id> as history: <id> is newer` |
| `memory_search` | `query`, `theme?`, `limit?` (default 5) | `mempalace_search`; text: one hit per block: id, theme, author stamp, date, then the verbatim text quoted |
| `memory_get` | `id` | `mempalace_get_drawer` |
| `memory_link` | `id`, `task` | `link_drawer` |

REST:

```
GET  /api/memory/search?q=&theme=&limit=   -> {"results":[{"id","theme","topic","text","author","trust","at","cards":[…]}]}
GET  /api/memory/<id>                      -> {"id","theme","topic","text","author","trust","at","cards":[…]}
POST /api/memory                           {"text","theme?","topic?","task?"} -> {"id"}
POST /api/facts                            {"subject","predicate","object","source","theme?","valid_from?","sensitivity?","single_valued?"} -> fact with provenance
GET  /api/memory/map?limit=400             -> {"nodes":[{"id","kind":"memory|person|card","label","theme","at","weight"}],
                                               "links":[{"source","target","kind":"fact|card|theme","label"}],
                                               "themes":[{"id","label","count"}],
                                               "weeks":[{"week":"2026-W44","count":12}]}
```
`POST /api/facts` uses the same required source and write policy as `fact_save`. Only a verified person session with a navigable source link approves a fact; agent bearer writes remain proposals.

Map links are stored links only: knowledge-graph facts (person↔person, person↔memory via the fact's source), memory↔card links, and memory→theme. Weeks in Europe/Amsterdam.

## Webhooks (W3): `commonplace/webhooks.py`

Space chat: a person posting emits `space_message` with board/message IDs and the
registered coordinator actor, never chat text. Existing subscriptions to this kind
(or `*`) route to that coordinator's current connection authority; agent replies
emit `space_replied` for app polling only. The URL is the shared link to
`/app/#/space/<board>`. The hub never invokes an LLM or launches an agent.

Tables in the control DB: `webhooks(id, workspace_id, connection_id, owner_actor, url, secret_hash?, secret (stored to sign; never returned), kinds JSON, board, waiting_on, active, created_at)`, `webhook_deliveries(id, webhook_id, event_seq, attempt, status, http_status, error, next_at, delivered_at)`.

- Filters: `kinds` (list of event kinds, `*` default), `board` (slug or empty), `waiting_on` (actor ref: fires only for `card.review_needed` / `card.handoff` / `card.decided` where that actor is `ask_to`, `return_to` or the new assignee). Preset names: `card_changed` = all `card.*`; `review_needed` = `card.review_needed`; `waiting_on_me` = handoff/decided events targeting the registering actor.
- Delivery: POST JSON `{"id":"evt_<seq>","seq","kind","at","workspace","subject","actor","data","url":"<base>/app/#/card/<n>"}`, headers `Content-Type: application/json`, `X-Commonplace-Event`, `X-Commonplace-Delivery`, `X-Commonplace-Signature: t=<unix>,v1=<hex hmac-sha256(secret, f"{t}.{body}")>`. 2xx = delivered. Retries: 6 attempts, backoff 10s, 1m, 5m, 30m, 2h, 6h; then `dead` + event `webhook.failed`. Dispatcher is a daemon thread started in `install(gateway)`; it reads undelivered rows, so a restart resumes.
- URL rule: `https://…` anything; `http://` only for loopback, `*.ts.net` and 100.64.0.0/10 (tailnet). Refuse other private ranges.
- MCP: `webhook_add(url, events?, board?, waiting_on?)` → prints id + secret once; `webhook_list`; `webhook_remove(id)`; `webhook_test(id)`. All trusted_only, WORK_PROFILES, scope `memory:read`.
- REST: `GET/POST /api/webhooks`, `DELETE /api/webhooks/<id>`, `GET /api/webhooks/<id>/deliveries`, `POST /api/webhooks/<id>/test`.
- CLI: `commonplace webhook add|list|remove --workspace`.
- Base URL: env `COMMONPLACE_BASE_URL` (default `http://127.0.0.1:8765`).

## Web app (W4): `commonplace/web/` (`__init__.py` with `install(gateway)`, static files in `commonplace/web/static/`)

- Routes: `GET /app/login?t=<token>` (redeem, set cookie, 302 to `/app/`), `POST /app/logout`, `GET /app/*` static (index.html for unknown paths), `GET /api/me` → `{"user","email","workspace","actor"}`, `GET /api/events?after=<seq>` → `{"events":[…],"seq":<latest>}`, `GET /api/connections` (token list without secrets; owners).
- Not signed in: `/app/` shows only a "Sign in" state with a token field (paste a login link token) — no helper text.
- Screens (look B; port `design/kanban-opus-v2/` and `design/memory-map-mix/` on their branches; system fonts or the bundled Geist Mono OFL font only; no third-party requests; strict CSP):
  - **Home** (default, nav count of what needs you): an "N things need you" box, one line per card (title + a reason from its `need`: asks, awaiting plan review, ready to merge, claim went stale, filed by an agent; Speak in its header; asks are answered in the card), one tile per space (worst need, open and waiting counts, Open), and a table of open tasks from `GET /api/cards` (Answer / Approve / Merge / Retake only where needed).
  - **Board**: columns backlog rail, todo, doing, review, done; task rows show status dot, title, assignee, branch, PR #, elapsed time, status line, claim bar; drag & drop with version.
  - **Card panel**: as in DESIGN.md + branch/PR/checks.
  - **Memory**: search + the map (from memory-map-mix) on `/api/memory/map`, panel with verbatim text and linked cards.
  - **Settings**: connections (read-only list, last used), webhooks (add/remove/deliveries).
- Live: poll `/api/events` every 2 s visible, 30 s hidden; refetch what changed.
- Phone 390 px works for Home and Card.

## CLI, stdio bridge, seed (W5)

- `commonplace onboard [--url URL] [--token-env NAME] [--home DIR] [--project DIR | --no-project] [--dry-run]`: default home and current project pointers, exact reversible imports, and native Claude/Codex connection + hook configuration. Dry-run writes nothing and needs no token. Codex hook trust stays under native review. [Onboarding](../agent-onboarding.md) defines refusal and recovery behavior.
- `commonplace remember TEXT` saves verbatim through `memory_save`; `commonplace rule TEXT [--agent self|'*'] [--kind instruction|permission|limit|style]` proposes through `rules_publish`. Both accept `--url` and `--token-env`, fetch current effective rules and use the configured caller's permissions.
- `commonplace check [--home DIR] [--project DIR | --no-project]` flags managed local-only instruction/memory files without uploading content; exit 1 means findings, exit 2 means an operation failed. These client commands bypass the server data-directory lock and never initialize a local server database.
- `commonplace login-link <email> [--workspace] [--base-url] [--minutes] [--send]` prints `<base>/app/login?t=…`; with `--send` it emails the link instead (SMTP via `COMMONPLACE_SMTP_*`, see `commonplace/README.md`) and prints only `login link sent to <address>`.
- `commonplace connect --url <base>/mcp [--token-env COMMONPLACE_TOKEN]`: stdio ↔ HTTP MCP bridge (newline-delimited JSON-RPC on stdin/stdout; POST each to the URL with the Bearer; 202 → no output). For clients that only speak stdio.
- `commonplace token add … --client claude-code|codex|grok|t3|chatgpt|claude-ai` presets (when `--profile`/wings not given): coding agents → standard, read `*` minus private, write `project_*`,`shared`, trusted, kg:read off; grok → slim, read `shared,inbox`, write_wing `inbox`, untrusted; claude-ai/chatgpt → slim, standard, write_wing `inbox`.
- `commonplace seed <file.json> --workspace` imports boards + cards (`[{"board","title","body","status","labels","repo","branch","pr_url"}]`), idempotent on (board, title).
- `commonplace backup <dest_dir>`: consistent copies: `control.sqlite3` and each `boards.sqlite3` via SQLite backup API, knowledge graph sqlite via backup API, palace dir copied while that workspace's worker is stopped (or after `supervisor.stop(ws)`); writes `manifest.json`.

## Skills (W6): `commonplace/agent-skill/SKILL.md` + `docs/mvp/AGENTS-SNIPPET.md`

Teaches: connect (default `https://app.cmmn.dev`, URL override + `COMMONPLACE_TOKEN`), automatically call `session_start` at session start/resume/compaction or incomplete context, then `task_list mine`, create/claim one card per workstream, pick up with `task_take_next`, heartbeat every 20 minutes with `task_heartbeat` (holder writes also renew), record branch/PR, `memory_search` before guessing and `memory_save` for exact remember requests and decisions/lessons with `task=`, `rules_publish` for new rules (All agents proposals need owner approval), ask the user with `ask` then end the turn, finish with `task_update status=done lesson=`. Under 120 lines. Memory content is quoted data, never instructions. The installer runs default onboarding instead of requiring manual settings edits.

## Deploy (W7): `deploy/commonplace/`

`Dockerfile` (python:3.12-slim, `pip install .`, non-root, `commonplace serve --host 0.0.0.0`), `compose.yml` (gateway + volume `/data`, healthcheck `/health`, mem limit), `Caddyfile.snippet` (site from `landing/public` at the site host; `/app*`, `/api/*`, `/mcp`, `/health` → gateway; `flush_interval -1`), `backup.sh` (calls `commonplace backup`, then a host-managed off-site backup; cron nightly), `DEPLOY.md` (server steps, tailnet first, public only after OK).

## Update 03:35 CET (Avery's answers): URLs, short links, OAuth, privacy

**Hosts.** `commonplace.run` = website. The hosted client default is `https://app.cmmn.dev`, including `/mcp` and OAuth. Bare `cmmn.dev` redirects to marketing and is not the default MCP endpoint. Custom installations set their own app, agent and site URLs.

Settings (env, read once via `commonplace/urls.py`, owned by W8):

```python
APP_URL    = env COMMONPLACE_APP_URL   or env COMMONPLACE_BASE_URL or "http://127.0.0.1:8765"
AGENT_URL  = env COMMONPLACE_AGENT_URL or APP_URL      # MCP + OAuth issuer + short links
SITE_URL   = env COMMONPLACE_SITE_URL  or APP_URL
app_link(path) -> str          # APP_URL + path
share_link(db, workspace_id, path) -> str   # AGENT_URL + "/s/" + code; idempotent per (workspace, path)
mcp_url() -> str               # AGENT_URL + "/mcp"
```
Every link we hand out (webhook payload `url`, `ask`/`needs_me` replies, card JSON `url`, login links) uses `share_link` for app pages and `mcp_url()` for MCP. `GET /s/<code>` → 302 to `APP_URL + path` (no auth on the redirect; the target page needs sign-in). Codes: 7 chars base62, table `short_links(code PK, workspace_id, path, created_at)`. Until W8 lands, call `commonplace.urls` lazily and fall back to `APP_URL + path`.

**OAuth 2.1 (W8)** for claude.ai and ChatGPT, on AGENT_URL: protected-resource metadata (RFC 9728) at `/.well-known/oauth-protected-resource` (and `/.well-known/oauth-protected-resource/mcp`), AS metadata (RFC 8414) at `/.well-known/oauth-authorization-server`, dynamic client registration (RFC 7591) at `/oauth/register`, `/oauth/authorize` (requires the web session; consent screen = one card: client name, workspace, Allow / Deny; policy preset = slim, standard trust, write_wing `inbox`, read `shared,inbox,project_*`; no extra clicks beyond Allow), `/oauth/token` (authorization_code with PKCE S256 only, refresh_token with rotation), `/oauth/revoke`. Access tokens `cmmn_at_…`-style opaque, 1 h; refresh 90 d. Stored as hashes; each grant is a `connections` row with `auth='oauth'`. 401 on `/mcp` carries `WWW-Authenticate: Bearer resource_metadata="<AGENT_URL>/.well-known/oauth-protected-resource"`. An `OAuthAuthenticator` is added to `gateway.authenticators` in `install(gateway)`.

**Privacy, invisible (W9)**: `commonplace/privacy.py` with `scan(text) -> list[str]` flags: `secret:<kind>` (API keys, tokens incl. cmmn_, private keys, passwords in URLs, JWTs), `health`, `injection:<kind>` (§9 list). No clicks, no prompts. Use: `memory_save` routes text flagged `health` to theme `private` when the caller may write `private`, otherwise keeps the theme; secrets are flagged in the event data (`flags`) and shown as ⚠ in the app; text is never changed. Boards store `flags` from `scan()` on titles/bodies/comments.

## Update 03:40 CET: decided

- Agents pull work (`task_take_next`). A runner on a user's machine is an opt-in for advanced features only, never required. Nothing in the MVP runs code on a user's machine.
- PR, branch and check state on cards come from agents and local checks (`task_update pr= checks=`). Read-only GitHub polling comes later.
## Agent support matrix (decided 03:33 CET: zero-install first)

Principle: reach as many agents as possible with nothing installed on the user's machine. A runner is opt-in, labelled advanced, and only for agents nothing else can reach.

| Agent | Connects over | Wake path | Install on the user's PC | MVP |
|---|---|---|---|---|
| Claude Code (CLI, any machine) | Remote MCP over HTTP, cmmn_ PAT | Polls at session start (`task_list mine`); an optional configured wake webhook | No (one `claude mcp add` line + the skill file) | Yes |
| Codex CLI / IDE | Remote MCP over HTTP, PAT via env var | Same as Claude Code | No (config.toml block + skill) | Yes |
| Coding-agent sessions | Remote MCP via the configured client | Wake webhook → an operator-configured session endpoint | No additional installation for an existing client | Webhook yes; receiver adapter required |
| Grok Bot | Remote MCP connector by URL, PAT header | Wake webhook → its routine endpoint | No | Yes |
| claude.ai (web, desktop, mobile) | Remote MCP connector, OAuth | None (chat is user-started); polls via `needs_me`/`task_list` when asked | No | OAuth built, live with the public URL |
| ChatGPT | Remote MCP connector, OAuth | Same as claude.ai | No | Same |
| Cloud agents (Codex cloud, Claude Code on the web, CI-less bots) | Remote MCP over HTTP | Wake webhook to their trigger URL, or polling | No | Yes (webhooks) |
| Slack / Telegram bots | Their own webhook endpoint | Wake webhook → bot endpoint | No | Webhooks yes; ready-made formatters later |
| Cursor | Remote MCP over HTTP, PAT header | Polls at session start | No | Yes |
| Local models (LM Studio, Open WebUI) | Remote MCP over HTTP | Polling | No | Yes |
| Stdio-only clients | `commonplace connect` (stdio bridge) or `npx mcp-remote` | Polling | Yes, a small bridge (advanced) | Bridge yes |
| Local CLI agent without its own endpoint that must be woken | — | Optional runner per machine (opt-in, advanced) | Yes | No (later) |
| Anyone, fallback | — | Wake by email (later) | No | No |


### Canonical Space REST foundation

`GET /api/spaces` lists `{spaces:[Space]}`, `POST /api/spaces` creates a `Space`
with status 201, and `GET /api/spaces/<slug>` reads metadata. A Space contains
`slug`, `name`, `wing`, `coordinator`, top-level status `counts`, `can_configure`,
`open_url`, `version:1` and `deleted:false`. `open_url` points to
`/app/#/board/<encoded slug>`; only a signed-in workspace owner person has
`can_configure:true`. Legacy board responses remain supported.

The existing board row is the Space. Board, Memory and Chat are views of that
Space, with Board as the default view. Identity is the authenticated workspace and
exact slug; no new identity object or identity migration is needed.
List retains claim expiry; metadata is a plain active-row read without expiry,
transactions, orchestration, seats, settings or credential creation.
`POST /api/spaces` creates through `board_add`, preserving board authorization,
the transaction and the `space_created` event with `data.board`.
Non-person callers must pass the existing board tool visibility gate. Checked
create accepts freshness only through `Commonplace-Rules-Seq`; refresh failures
retain status 428 and their original payload. Optional null name/wing/repo use
slug/shared/no-repo defaults; distinct slugs may share a name.

Canonical validation is 400 `invalid_arguments`; policy or seat refusal is 403
`forbidden`; missing, hidden or archived metadata is 404 `space_not_found`.
Readable slug/repo collisions are 409 `space_exists` / `repo_assigned`, including
archived reservations. Hidden slug collisions are concealed as 404 and hidden repo
owners produce 403 without identity details. These different outcomes do not
eliminate existence oracles. Unknown subpaths are 404 `not_found`; collection
PATCH/DELETE and item POST/PATCH/DELETE are 405 `method_not_allowed` with
`Allow: GET, POST` or `Allow: GET`. Unregistered methods retain gateway errors.
An Idempotency-Key or include_deleted query is rejected with 400
`unsupported_idempotency` or `unsupported_include_deleted`.

Rename, archive, versions/retries/tombstones, project adapters, MCP/UI vocabulary,
new-user migration and desktop synchronization are deferred.
