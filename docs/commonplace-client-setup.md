# Local Claude Code setup

To connect to an existing Commonplace account, follow
[Connect an agent to your account](agent-connections.md).

Requires Python 3.10+, [uv](https://docs.astral.sh/uv/) and Claude Code already installed.
From a checkout, install and create a local workspace:

```sh
uv sync --extra dev
.venv/bin/commonplace init --admin-email you@example.com
```

Start the gateway in another terminal from the same checkout:

```sh
.venv/bin/commonplace serve --host 127.0.0.1 --port 8765
```

Keep it running. Back in the first terminal, issue a token and register the
HTTP endpoint:

```sh
export COMMONPLACE_TOKEN="$(.venv/bin/commonplace token add claude-code:local \
  --workspace personal --client claude-code)"
claude mcp add --transport http commonplace http://127.0.0.1:8765/mcp \
  --header "Authorization: Bearer $COMMONPLACE_TOKEN"
```

The equivalent project `.mcp.json` is below. Replace the placeholder with the
token printed once by `token add`. Keep that credential out of version control.

```json
{
  "mcpServers": {
    "commonplace": {
      "type": "http",
      "url": "http://127.0.0.1:8765/mcp",
      "headers": {"Authorization": "Bearer <your-token>"}
    }
  }
}
```

## Repeatable offline check

On Linux or macOS, after `uv sync --extra dev` has prepared the dependency and
build cache, run:

```sh
python3 scripts/check_commonplace_setup.py
```

The check creates a fresh, non-editable installation from this checkout using
`uv sync --offline --frozen --no-dev --no-editable`. Missing cached dependencies
fail the check; it never falls back to downloading packages or Python.
It uses a temporary home, workspace, data directory and `.mcp.json`, with only
test-generated credentials and an `example.invalid` email. Existing environment
credentials and application settings are excluded.

It runs the installed `commonplace init`, `token add --client claude-code` and
`serve` commands, starts the real memory worker, and uses the temporary MCP
configuration to send `initialize`, `notifications/initialized` and `tools/list`
over loopback HTTP. Both memory and board tools must appear; missing and invalid
tokens must receive HTTP 401. No Claude process or account is needed. Python
socket access outside loopback is blocked inside the disposable environment;
no embedding model or external provider is used. The gateway, workers and
temporary files are removed on success or failure.

The check exits nonzero if setup exceeds 300 seconds. Timing starts before the
fresh installation and ends after authenticated tool discovery; queueing and
cleanup are excluded. A local Linux run on 2026-10-31 measured **1.11 seconds to
install, 3.38 seconds to connect**, exposing 33 tools. This measures an empty
client environment with a warm package cache. Installing Python, uv or Claude
Code, cold dependency downloads, interactive client approval and account sign-in
are outside this measurement; it does not establish their end-to-end time.
