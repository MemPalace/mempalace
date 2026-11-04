# Projects and orchestrators

Every project (board) gets one main orchestrator seat automatically, normally
named `orchestrator:<project>`. Older projects get their seat when first viewed.
An existing coordinator stays the main orchestrator until an owner replaces it.

## Connect an agent

Connect attaches an agent to the project's automatic orchestrator seat. A
signed-in workspace owner uses **Connect agent** in the chat header or project
settings, then copies the token and MCP URL into the agent's configuration.
The token is shown once; closing the result hides it. Connect does not start
an agent process. If another active coordinator holds the project, replacing
it requires confirmation.

The connection has standard trust and the standard profile, with memory read
and write access to the project's wing. A wing can serve several boards, so
this is a memory-access boundary, not a guarantee of access to only one board.
For a project using the shared wing, Connect requires explicit confirmation
that the agent will have read and write access to all shared memory.

Tokens expire after 90 days. Connecting again rotates an active token and
invalidates the old one. After revocation or expiry, Connect creates a fresh
connection. Connections can also be rotated or revoked on the Agents screen.

Register a `space_message` webhook for the project if the agent should wake
when someone posts; Connect supplies setup details but does not subscribe it.
Without a webhook, the agent reads messages on its next check.

The header shows the main orchestrator and its connection state. “Connected ·
waiting for the agent” means the credentials exist but the agent has not used
them yet. Private status stays private. An unconnected seat leaves release and
supervision with the existing people; a seat takes those duties only after its
agent has used the connection within the last 24 hours.

## Model choices

Owners can save provider and model labels for the main orchestrator and workers
in project settings. These choices are stored only: saving them does not call a
provider or start work. Applying them through the runner comes later. They do
not change the separate workspace chat model settings.

Model resolution uses the project's choice, then the workspace default for
that role, then the runner's own default. Clearing a project choice restores
that fallback. No API key is stored with these labels.

## Events and API

Creating a board emits `space_created` with `data.board` set to its slug and no
chat text. Wildcard webhook subscribers receive this event too. A failed seat
listener does not block board creation; the next project view repairs the seat.

See [project routes](boards/API.md#project-routes) for the API contract and
[project chat](chat.md#coordinator-contract-and-card-fallback) for message routing.
