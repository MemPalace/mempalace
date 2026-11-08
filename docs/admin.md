# Workspace administration

Sign in as the current workspace's owner and choose **Account → Admin**, or choose
**Admin** in the command palette on a phone. The direct route is `/app/#/admin`.
The page shows the active workspace's members and agent connections. It adds no
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

The path ID is decoded once and must equal the authenticated workspace ID.
Only the optional `cursor` query parameter is accepted; unknown or repeated keys,
malformed cursors, and cursors from another section or workspace return 400
`{"error":"Invalid admin query"}`. The routes register no mutation methods.

Every successful response is `Cache-Control: no-store` and includes `workspace_id`,
`recorded_at` (UTC), full-section `count`, `has_more`, and `next_cursor` (null when
complete). Pages contain at most 100 items, ordered by creation time then stable ID;
members use membership creation time. Pass `next_cursor` unchanged for the next
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

Orchestrators/runners, agent limits, needs-me/stuck cards, deployment/health and
backups are labeled **Available in a later slice**. They make no requests and
provide no operational data in this rollout. No deploy, backup, restore, runner
control or new enforcement endpoint is present.
