# Gemini CLI Integration Guide

This guide explains how to set up MemPalace as a permanent memory for the [Gemini CLI](https://github.com/google/gemini-cli).

## Prerequisites

- Python 3.9+
- Gemini CLI installed and configured

## 1. Installation

On many Linux systems, installing Python packages globally is restricted. We
recommend [`uv`](https://docs.astral.sh/uv/), which creates and manages a
local virtual environment for you.

```bash
# Clone the repository (if you haven't already)
git clone https://github.com/MemPalace/mempalace.git
cd mempalace

# Create the venv and install MemPalace + dependencies in editable mode
uv sync
```

This produces a `.venv/` directory inside the repo with everything installed.
If you prefer plain pip:

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
```

## 2. Initialization

Set up your "Palace" (the database) and configure your identity.

```bash
# Initialize the palace in the current directory
uv run python -m mempalace init .
```

### Identity and Wings (Optional but Recommended)
You can manually define who you are and what projects you work on by creating/editing these files in `~/.mempalace/`:

- **`~/.mempalace/identity.txt`**: A plain text file describing your role and focus.
- **`~/.mempalace/wing_config.json`**: A JSON file mapping projects and name variants to "Wings".

## 3. Connect to Gemini CLI (MCP)

Register MemPalace as an MCP server so Gemini CLI can use its tools.

```bash
gemini mcp add mempalace /absolute/path/to/mempalace/.venv/bin/python3 -m mempalace.mcp_server --scope user
```
*Note: Use the absolute path to ensure it works from any directory.*

## 4. Enable Auto-Saving (Hooks)

To ensure the AI saves memories automatically when conversation history becomes too long, add a `PreCompress` hook to your Gemini CLI settings.

Edit your `~/.gemini/settings.json` and add the following:

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

## 5. Usage

Once connected, Gemini CLI will automatically:
- Start the MemPalace server on launch.
- Use `mempalace_search` to find relevant past discussions.
- Use the `PreCompress` hook to save new memories before they are lost.

### Manual Mining
If you want the AI to learn from your existing code or docs immediately, run the "mine" command:
```bash
uv run python -m mempalace mine /path/to/your/project
```

### Verification
In a Gemini CLI session, you can run:
- `/mcp list`: Verify `mempalace` is `CONNECTED`.
- `/hooks panel`: Verify the `PreCompress` hook is active.
