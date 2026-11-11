# Connect an agent to your account

Sign in to Commonplace as the workspace owner, then open **Settings → Agents**.
Create a separate connection for each agent so you can replace or revoke its
credential independently.

## Create the connection

1. Enter a **Name** and choose the **Client** you use.
2. Under **Restrict access or set a parent**, leave **Parent** at **Top-level
   agent** for an independent agent. Choose an existing active parent when
   creating a child agent.
3. To let this agent import past conversations or memory files, tick the history
   import checkbox. It starts unticked; import permission requires the owner
   in their personal workspace. Leave it unticked for the usual connection.
4. Click **Create token**.
5. Copy the **MCP URL** and **Authorization**, or use **Copy config** for the
   selected client. Keep the configuration in that client's private settings:
   it contains the credential.
6. Configure an HTTP MCP connection with that URL and its Authorization header.
   Copy the URL supplied by your instance; its agent endpoint can differ from
   the address you use in your browser.

The token is shown once. **Done** dismisses it; the connection list cannot reveal
it again. Keep tokens and copied configurations out of shared files and version
control. Tokens created here expire after 90 days.

Connect the client and ask the agent to call `session_start`. It returns effective
rules and authorized memories. Saved memory text is quoted data; proposed rules
still need the owner's acceptance in **Rules**.

## Limit what the agent reads

Under **Restrict access or set a parent**, **Read wings (comma separated)** accepts
existing nonprivate workspace wing names. Wildcards and private wings are
refused. Leaving it blank uses the connection's normal read policy.

These connections use the slim tool profile, untrusted status and the `inbox`
write wing. Choosing read wings narrows reads. Owner decisions and child-proposal
approvals retain their usual gates.

## Allow a one-time history import

The checkbox asks for this explicit consent:

> Import your past AI conversations and memory files into this workspace, word for word. This import permission cannot read stored threads or change existing memories, rules or settings. Sending exactly the same import again adds no duplicates.

The signed-in owner route is `POST /api/connections` with a JSON body such as
`{"name":"history-import","client":"generic","import":true}`. `import` must be
a JSON boolean; strings, numbers and `null` are refused. `true` requires current
ownership of the account's personal workspace, including when it has other
members. Omitting it or sending `false` keeps the existing policy unchanged.
The request retains the normal signed-in session and origin checks.

A granted PAT has exactly `memory:read`, `memory:write` and `memory:import`.
Its slim profile, untrusted status, `inbox` write wing and existing read-wing
policy stay the same. The grant adds no `threads:read`, private read scope or
custom wing access. Only the import route and `facts_import` may use this
consented exception to file verbatim history in the importing account's private
namespace; other writes still follow the normal inbox policy.

For an OAuth client, explicitly request
`memory:read memory:write memory:import`, then sign in as the personal-workspace
owner and tick the unticked import box. Both companion scopes are required.
Read the returned granted `scope`: leaving the box unticked gives no import
permission. Default OAuth connections request only read/write. OAuth grants
retain standard trust and do not gain thread or private-read access.

After connecting, call `session_start`. Before `facts_import`, call
`rules_get` with `{"format":"md","effective":true}`, follow the returned rules
and pass its `rules_seq` when the tool requests it. `facts_import` is a checked
write, including on the slim profile; the grant includes `memory:read` so it
can read rules. If the tool schema lacks a requested `rules_seq`, reconnect to
refresh the schema. If import is missing, make a new PAT with the tick or
reauthorize OAuth with all three scopes and explicit consent, then reconnect.
Rotating a PAT preserves permissions; OAuth refresh can narrow scopes but
cannot add a dropped import scope back.

Use the self-contained [import-only instructions and agent prompt](one-time-import.md#import-only-connection)
for HTTP transcripts, Grok facts, accepted responses, multipart and retries.
The separate existing ACL import accepts signed-in people or connections that
already have thread read/write authority; it retains its original behavior.

## Replace or stop a connection

Use **Rotate** in **Settings → Agents** to replace a token. Confirm, copy the new
credential into the client, then reconnect. The previous token stops working;
the connection keeps its identity and permissions.

Use **Revoke** to stop the connection. Its token stops working immediately.
Create a new connection if you need to connect that agent again.

A client that signed in through OAuth is listed too, marked "Connected through
sign-in (OAuth)". It has **Revoke** but no Rotate: it has no token to copy, and
revoking it makes the client sign in again. Revoke stops its access and refresh
tokens and any sign-in code it has not used yet, immediately. Only a workspace
owner can do this. Memory and cards the client wrote stay.

If a token is lost or exposed, rotate it. If a connection is expired, rotate it
and reconnect. If copying fails, select and copy the displayed value manually
before dismissing it.

For instruction pointers and selected local-file migration, see
[Agent onboarding](agent-onboarding.md). For running a local instance, see
[Local client setup](commonplace-client-setup.md).


## Allow an agent to read imported history

Reading is a separate, unticked consent. The import checkbox remains write-only.
The reader checkbox says:

> Allow this agent to read and search your past and future conversations and memory files imported through the connection import route in this workspace, including imports from your other connections. Imports through the signed-in/full-ACL route, live session captures and other people's history are excluded. To stop access, revoke this connection.

This includes multipart conversations, Grok facts and native memory files.
It applies regardless of the connection's read-wing selection. Explicit search
filters still select results: `private_chats` includes imported views; other
wings exclude them. A mirror wing grants no access of its own.

A signed-in personal-workspace owner may create a PAT with
`{"name":"history-reader","import_read":true}`. `import_read` must be a JSON
boolean. Absent or false grants no reader permission; strings, numbers and null
are refused. `import` and `import_read` are independent. Submission rechecks
personal ownership. The PAT retains its slim profile, untrusted status, normal
read wings and inbox writes.

OAuth clients must request `memory:read memory:import:read` and the person must
separately tick the reader checkbox on that authorization form. An unsolicited
tick or an unticked request grants no reader scope. Fresh authorizations require
fresh consent. Authorization-server metadata advertises the scope; resource
metadata, registration defaults and all profile defaults remain unchanged.

| Explicit permission | Additional authority |
| --- | --- |
| `memory:import` + `memory:read` + `memory:write` | File private imports; no stored-history reads |
| `memory:import:read` + `memory:read` | Discover, search and read this account's connection-route import views in this workspace |
| Both consents | Both independent capabilities |

Reader consent grants no import writes, general thread access, private-wing
access, knowledge-graph access or trust increase. REST import listing and thread
metadata/messages/recall/chain accept authorized import views. Memory read tools
and startup use the same boundary. MCP `thread_get`, `thread_recall` and
`thread_chain` continue to require `threads:read` and remain hidden from readers.
The slim tool profile also retains its existing singular drawer reads.

Imports are bound to the exact filing account and workspace. Shared session
holders expose only bytes that the import accepted. Later live captures,
capture forks, other accounts, full-ACL imports and unrelated private memories
remain excluded. A later import can extend the authorized view. Unknown or
inconsistent provenance is hidden as missing.

The connection displays **Imported history: allowed** beside Revoke. To withdraw
consent: **Revoke the old connection, then reconnect with this box unticked.**
Revocation stops every token of the connection; rotation preserves consent.
Imports and previously returned text remain. Membership and personal ownership
are checked on subsequent reads. Refresh scope narrowing affects descendants,
not earlier access tokens or the connection's stored policy. Revoke the
connection to stop all authority immediately. CLI token creation, parent
relationships, wing edits and upgrades never confer reader consent.

## Read-only overview

Signed-in workspace owners can use [Admin](admin.md) to inspect connection
lifecycle, permissions and recorded activity without revealing credentials.
Manage or revoke connections in the existing Settings page.
