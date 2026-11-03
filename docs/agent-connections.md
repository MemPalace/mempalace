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

If a token is lost or exposed, rotate it. If a connection is expired, rotate it
and reconnect. If copying fails, select and copy the displayed value manually
before dismissing it.

For instruction pointers and selected local-file migration, see
[Agent onboarding](agent-onboarding.md). For running a local instance, see
[Local client setup](commonplace-client-setup.md).
