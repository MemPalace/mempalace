# OpenClaw setup and connection diagnosis

For requested setup/diagnosis only. Obtain approval before installation, config
changes, or imports; a skill grants no permissions.

## 1. Reuse the intended palace

Reuse exposed tools and existing registrations. A remote-only client needs no
local MemPalace/Python binary, initialization, watcher, or second palace; never
substitute local data when its endpoint fails. Agree on private local, local/shared
hub, or remote-only topology.
One hub owns a shared palace; do not launch competing writer servers.

Prefer light MCP for new model-facing use where available. Recall calls light
preferred/full legacy; upstream setup defaults to full,
which remains supported. Preserve working integrations unless a change is
requested. `mempalace-mcp` is the thin full stdio proxy with same-palace local
fallback, NOT `mempalace-light-mcp`; that fallback is not a remote-only strategy.
Discover actual HTTP tools; HTTP does not imply light.

For hub hosting, consult only [start the hub][hub]; for network authentication
and TLS, [serve MCP over HTTP][remote]. Loopback reaches only this machine.
Choose before installation/registration.

## 2. Verify the bundle and runtime

Import the complete `integrations/openclaw/` folder/ZIP: `SKILL.md` plus
`references/{setup,tool-selection}.md`. Resolve references from `{baseDir}`;
copying only `SKILL.md` breaks them. The Python wheel does not install this skill.
Canonical links need network access, not sibling files.

For local stdio, reuse the installation; if a new one is requested, consult
[local installation][install]. Use its console entry point, not unrelated Python.
Check Gateway PATH, not just terminal PATH; use the actual absolute path if needed.
Initialize only an explicitly new local
palace/corpus using `mempalace instructions init`; verify with `mempalace status`.
Never reinitialize an existing palace. Remote-only clients skip these local steps.

## 3. Inspect and merge native configuration

Consult running-version OpenClaw docs; public equivalents:
[MCP](https://docs.openclaw.ai/tools/mcp),
[MCP registry](https://docs.openclaw.ai/cli/mcp/registry), and
[Skills](https://docs.openclaw.ai/tools/skills). Inspect
`openclaw mcp show mempalace --json` or `openclaw mcp list` first. Preserve existing
transport, environment, timeouts, and filters. Merge into native `mcp.servers`,
NOT another client's `mcpServers`; do not overwrite with a minimal example.

For a NEW light stdio entry after executable checks:

```bash
openclaw mcp set mempalace '{"command":"mempalace-light-mcp","args":[]}'
```

Full stdio alternative, not another writer:

```bash
openclaw mcp set mempalace '{"command":"mempalace-mcp","args":[]}'
```

Existing local hub HTTP alternative:

```bash
openclaw mcp set mempalace '{"url":"http://127.0.0.1:8765/mcp","transport":"streamable-http"}'
```

For remote HTTPS, use Settings → MCP → Add server → Streamable HTTP with the
approved endpoint. Use current supported secret-backed header/SecretRef fields
from the running-version docs, not guessed JSON. Keep credentials out of chat,
skill text, and config literals.
Do not loosen tool policy to make a connection work.

## 4. Prove three separate outcomes

1. Saved definition: `set`/`show` (or doctor without `--probe`) is configuration,
   not proof of connection.
2. Live connection/discovery: `openclaw mcp doctor mempalace --probe`.
3. Intended agent: verify exposed tools and an authorized read, such as
   `palace_query` with `{"query":"STATUS"}` or full `mempalace_status`.
   Host policy/filters may hide tools despite a successful probe.

Report these outcomes separately. Never use init, mine, or test writes as probes.
Finish only when bundle resolution and connection/agent reads pass.

## 5. Deliberate CLI use; separate automation

CLI remains supported for explicit operations when shell execution is authorized
and its route targets the intended palace or same-palace local hub. Use installed
`mempalace instructions <command>` for supported operations; CLI is not a way
around missing tools or policy. If neither exposed tools nor an authorized
matching CLI route exists, report the limitation and offer setup.

CLI can route through a daemon/hub; consult [routing safety][routing] when needed.
Never replay failed/ambiguous submissions locally or bypass the writer lease.
Exclusive maintenance follows its own procedure. Hooks and recall injection are
separate, harness-specific integrations: this skill neither installs nor proves
them. Verify any requested automation separately.

[hub]: https://mempalaceofficial.com/guide/shared-brain.html#_1-start-the-hub
[remote]: https://mempalaceofficial.com/guide/remote-server.html#_3-serve-mcp-over-http
[install]: https://github.com/MemPalace/mempalace/blob/develop/skills/mempalace/SKILL.md#2-install-the-cli-when-necessary
[routing]: https://github.com/MemPalace/mempalace/blob/develop/docs/cli-write-routing.md#safety-properties
