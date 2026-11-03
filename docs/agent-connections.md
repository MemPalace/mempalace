# Connect an agent to your account

Sign in to Commonplace as the workspace owner, then open **Settings → Agents**.
Create a separate connection for each agent so you can replace or revoke its
credential independently.

## Create the connection

1. Enter a **Name** and choose the **Client** you use.
2. Under **Restrict access or set a parent**, leave **Parent** at **Top-level
   agent** for an independent agent. Choose an existing active parent when
   creating a child agent.
3. Click **Create token**.
4. Copy the **MCP URL** and **Authorization**, or use **Copy config** for the
   selected client. Keep the configuration in that client's private settings:
   it contains the credential.
5. Configure an HTTP MCP connection with that URL and its Authorization header.
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
