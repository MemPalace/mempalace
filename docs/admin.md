# Workspace administration

Sign in as the current workspace's owner and choose **Account → Admin**, or choose
**Admin** in the command palette on a phone. The direct route is `/app/#/admin`.
The page shows the active workspace's members, connections, runtime configuration, effective limits and cards needing attention. It adds no
workspace switching, global account directory, invitation, token reveal or editing
form. **Manage agents and workspace settings** opens the existing Settings page.

## Authority

`GET /api/me` retains its identity fields and adds boolean capabilities:

- `can_admin`: true only for a signed-in person with role `owner` in the active workspace.
- `can_admin_instance`: false during this partial rollout.

Capabilities govern navigation; every inventory request independently checks current
authority. Ownership of the user behind an agent token grants no browser authority.
The instance-administration decision remains signed-in person + active-workspace
owner + server-fetched `users.is_admin=1`, for operational metadata only. Instance
routes and that capability are not implemented in this rollout.

| Caller | Inventory response |
| --- | --- |
| Signed-in owner, current workspace ID | 200 |
| Member, owner's PAT, owner's OAuth connection | 403 `{"error":"a signed-in workspace owner is required"}` |
| Signed-in owner, any other workspace ID | 404 `{"error":"not found"}` before projection reads |
| Signed out | Existing 401 `{"error":"unauthorized"}` with challenge |
| Machine connection | Existing gateway 404 `not found` |
| Worker connection | Existing gateway 403 `tool not allowed for this connection` |

A member's direct Admin visit shows the owner refusal and makes no inventory
requests. Signed-out visits retain sign-in. Static JavaScript is a public SPA asset.

## HTTP inventories

Only these new GET routes ship:

- `/api/admin/workspaces/<workspace-id>/users`
- `/api/admin/workspaces/<workspace-id>/connections`
- `/api/admin/workspaces/<workspace-id>/runtime`
- `/api/admin/workspaces/<workspace-id>/limits`
- `/api/admin/workspaces/<workspace-id>/stuck`

The path ID is decoded once and must equal the authenticated workspace ID.
Only the optional `cursor` query parameter is accepted; unknown or repeated keys,
malformed cursors, and cursors from another section or workspace return 400
`{"error":"Invalid admin query"}`. The routes register no mutation methods.

Every successful response is `Cache-Control: no-store` and includes `workspace_id`,
`recorded_at` (UTC). Each list has full-list `count`, `has_more`, and `next_cursor`
(null when complete). Pages contain at most 100 items. Users/connections and machines
sort by creation time then stable ID; members use membership creation time. Projects
sort by slug then ID; stuck cards sort by number. Pass `next_cursor` unchanged for the next
page. **Load more** appends that page. Counts are calculated before pagination.
These are current recorded facts, not an atomic snapshot across sections.

The users payload has this shape (illustrative empty membership list):

```json
{
  "workspace_id": "ws_0123456789abcdef",
  "recorded_at": "2026-11-07T12:00:00Z",
  "workspace": {"id":"ws_0123456789abcdef","name":"Northwind","created_at":"2026-01-01T00:00:00Z"},
  "members": [],
  "count": 0,
  "has_more": false,
  "next_cursor": null
}
```

Each member has `id`, `name`, `email`, `role`, `created_at`, `joined_at` and
`disabled_at`. `created_at` is user creation; `joined_at` is membership creation.
Only current workspace membership is listed, including disabled members.

Connections use the same envelope with `connections` instead of `workspace` and
`members`. Each connection includes `id`, `name`, `client`, `auth`, `user_id`,
`created_at`, `expires_at`, `revoked_at`, `last_used_at`, `parent_id`, `started_by`,
`read_wings`, `import_read`, `trust`, sorted `scopes`, `activity`, `state`, and
boolean `user_disabled`. Connections are listed only for current workspace members,
and counts use the same membership and user joins as the listed rows. State precedence is revoked, disabled user, expired, active.
Activity uses the existing `unused`, `working`, `idle`, `quiet` classifier. **Last
used** measures credential contact, never runner liveness. All states preserve valid
stored lifecycle timestamps. Malformed policy displays deny-all permissions.

Names, emails, clients and wing labels are redacted for display; URLs are omitted.
Credentials, hints, raw policy, cookies, login links, webhook URLs and OAuth material
are never projected. Invalid typed identifiers/timestamps appear as null. Null means
unknown or absent; a measured empty list/count is represented by `[]` and zero.
Source failures return safe 503 constants: `Users unavailable; try again` or
`Connections unavailable; try again`, without source paths or exception text.

## Runtime, limits and attention

`runtime` has independent `orchestration`, `orchestrators` and `machines` subresults.
Each has `state: "ready"` or `state: "error"` with a safe error constant. A ready
switch has `enabled`; ready lists have `items`, `count`, `has_more`, `next_cursor`.
A failed machines source preserves the switch and orchestrator observations.
Older stores without the optional coordinator or heartbeat columns are read as
not configured/unknown without running migrations. Cursors accept slugs up to 100 characters.

Orchestrators include `id`, `slug`, `name`, `coordinator`, `seat_name`, `source`,
`connected`, `activity`, `last_used_at`, `wakeable`. Only unarchived projects appear.
A configured coordinator takes precedence over a stored seat; an absent seat is
`not_configured` and a seat without a coordinator is configuration only. Current membership,
user state, credential expiry/revocation and runner kind determine connection
visibility. Machine credentials never become orchestrators. Wakeability means
current subscription eligibility for `space_message`, with wildcard/exact-board
filtering and current policy/wing access. It sends no webhook or liveness probe.

Machines include `id`, `label`, `enabled`, `revoked_at`, `binding_state`,
`verified:false`, `machine_last_used_at`, `worker_last_used_at`, `activity`,
`worker_activity`, `max_in_flight`, `max_starts_per_hour`, `created_at`.
Machine activity uses machine contact, with worker contact shown separately.
Working means recent contact; idle/quiet means no recent contact; unused means
never contacted. Attestation never proves a live or verified runner.

`limits` returns `orchestration_enabled`, `workspace_max_agents:null`,
`workspace_limit_source:"not_configured"`, `projects`, `machines`, `pagination`.
Each project includes `id`, `slug`, `name`, `configured_max_agents`,
`accepted_config_max_agents`, `effective_max_agents`, `used`, `counting_mode`.
With orchestration off, distinct doing connection assignees count and a null cap
means unlimited. With it on, existing slot accounting deduplicates cards, ordinary
agents, queued/leased/starting/uncertain jobs and recent started jobs. The effective
cap uses accepted runner configuration and the project cap, or default 3. Proposals
never override it. Existing doing worker cards still count after disabling a machine.
Usage exceeding a cap stays visible. Machine grants contain only `id`,
`max_in_flight`, `max_starts_per_hour`; only enabled, unrevoked machines have active grants.
Disabled/revoked machines remain in runtime inventory and slot accounting. Grants are not a workspace cap. Pagination
metadata for each limits list is under `pagination.projects` / `pagination.machines`.

For example, an empty limits result includes:

```json
{
  "workspace_id":"ws_0123456789abcdef",
  "recorded_at":"2026-11-07T12:00:00Z",
  "orchestration_enabled":false,
  "workspace_max_agents":null,
  "workspace_limit_source":"not_configured",
  "projects":[],
  "machines":[],
  "pagination":{
    "projects":{"count":0,"has_more":false,"next_cursor":null},
    "machines":{"count":0,"has_more":false,"next_cursor":null}
  }
}
```

`stuck` returns `cards`, `count`, `reason_counts`, `blockers_enforced`, `has_more`,
`next_cursor`. Each card has `number`, `board`, `title`, `status`, `assignee`,
`claim_until`, `heartbeat_at`, `updated_at`, `reasons`, `active_blockers`,
`expired_jobs`, and local `open_url:"#/card/<number>"`. It includes non-done children.
Independent facts are `expired_lease`, `blocked`, `waiting_on_person`, `stale_agent`.
Expired active jobs include only `job_id`, `state`, `lease_until`, never lease tokens.
Active blockers include only numbers and redacted titles; done blockers are omitted.
Waiting on a person is a fact, not an unhealthy-agent diagnosis. Raw stale facts use
the existing predicate independently from owner-queue escalation. Cards are deduplicated
by number and per-reason counts cover the full set before pagination. GET never
sweeps leases, renews jobs or alters enforcement.

The adjacent Needs-me panel directly reads existing `GET /api/needs-me`, using its
unique server count and existing buckets. It renders titles and local card links,
not bodies or mutation controls. Either attention panel can succeed independently.
This reused route can initialize legacy stores and bind rule-store metadata; the
strict no-initialization/no-write promise applies to the new Admin GETs.

Pass each list's opaque cursor unchanged to its section URL. The cursor binds the
workspace, section and list; pagination preserves sibling lists. Unknown or corrupt
existing stores produce safe source errors, never zero usage. Missing board stores
produce empty inventories; missing runner stores/tables provide empty job/config
inputs without creation. Missing settings mean disabled; missing subscription tables
mean not wakeable. Safe whole-section errors are `Limits unavailable; try again`
and `Stuck unavailable; try again`; runtime suberrors use `Orchestration`,
`Orchestrators`, or `Machines` followed by `unavailable; try again`.

## Loading and partial rollout

The page mounts before shared boards, needs-me or rules refreshes. Each section
loads independently, with a 5,000 ms deadline covering transport and JSON decoding.
A failed or timed-out section has its own Retry, which repeats the failed GET
with the same pagination cursor and append or replacement mode. Manual refresh retains ready data
while loading; a 403 clears the affected data. Navigation, session expiry, sign-out
or workspace replacement abort and discard pending reads. Event polling does not
remount Admin or start shared refreshes on it. The initial `/api/me` request also
has the bounded deadline. Opening a card from Admin switches to the regular base
view and loads that card without remounting Admin or repeating inventory requests.
Every Admin load, refresh, retry and pagination is a GET.

New projections open the existing control SQLite file with `mode=ro`, `query_only`,
a 2 second monotonic budget, maximum 250 ms connection/busy timeout and progress
interruption. They do not take application locks, initialize stores, discover
memory wings or call external services. UTC is captured once through
`gateway.admin_clock.utc_now`; `gateway.admin_clock.monotonic` governs the budget.
This seam permits fixed-clock tests without changing authentication expiry policy.
Board/runner stores use the encrypted SQLite connection factory at the generation-aware
supervisor path, also read-only with the same budget and query-only setting. Readers
use SELECT-only presence probes, close connections in finally, and never initialize
seats, schema, stores, tokens or subscriptions. They never call the directory-creating
workspace helper, project GET, store constructors, lock-taking settings/registry
wrappers, coordinator wrappers or webhook authorization/delivery wrappers. Already
read runtime subresults survive later source errors; an exhausted budget gives
unattempted siblings explicit errors. SQLite may take bounded ordinary read locks.

Deployment/health and backups remain labeled **Available in a later slice**. They make no requests and
provide no operational data in this rollout. No deploy, backup, restore, runner
control or new enforcement endpoint is present.
