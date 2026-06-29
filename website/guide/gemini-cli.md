# Gemini CLI

MemPalace works natively with [Gemini CLI](https://github.com/google/gemini-cli), which handles the MCP server and save hooks automatically.

## Prerequisites

- Python 3.9+
- Gemini CLI installed and configured

## Installation

We recommend [`uv`](https://docs.astral.sh/uv/) — it creates and manages the
virtual environment for you:

```bash
# Clone the repository
git clone https://github.com/MemPalace/mempalace.git
cd mempalace

# Create the venv and install MemPalace + dependencies
uv sync
```

This produces a `.venv/` directory with the project installed in editable
mode. If you prefer plain pip, the equivalent is:

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
```

## Initialize the Palace

```bash
uv run python -m mempalace init .
```

### Identity and Project Configuration (Optional)

You can optionally create or edit:

- **`~/.mempalace/identity.txt`** — plain text describing your role and focus
- **`./mempalace.yaml`** — per-project MemPalace configuration created by `mempalace init`
- **`./entities.json`** — per-project entity mappings used by AAAK compression

## Connect to Gemini CLI

Register MemPalace as an MCP server:

```bash
gemini mcp add --scope user mempalace \
  -- /absolute/path/to/mempalace/.venv/bin/python -m mempalace.mcp_server
```

::: warning
Use the **absolute path** to the Python binary so the server starts from any
working directory. The `--` separator prevents Gemini from parsing
`-m mempalace.mcp_server` as its own flags.
:::

## Enable Auto-Saving

Add a `PreCompress` hook to `~/.gemini/settings.json`:

```json
{
  "hooks": {
    "PreCompress": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "/absolute/path/to/mempalace/hooks/mempal_precompact_hook.sh"
          }
        ]
      }
    ]
  }
}
```

Make sure the hook scripts are executable:
```bash
chmod +x hooks/*.sh
```

## Usage

Once connected, Gemini CLI will automatically:
- Start the MemPalace server on launch
- Use `mempalace_search` to find relevant past discussions
- Use the `PreCompress` hook to save memories before context compression

### Manual Mining

Mine existing code or docs:
```bash
uv run python -m mempalace mine /path/to/your/project
```

### Verification

In a Gemini CLI session:
- `/mcp list` — verify `mempalace` is `CONNECTED`
- `/hooks panel` — verify the `PreCompress` hook is active
