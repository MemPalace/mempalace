# MemPalace Init

Guide the user through a complete MemPalace setup. Follow each step in order,
stopping to report errors and attempt remediation before proceeding.

## Step 1: Check Python version

Run `python3 --version` (or `python --version` on Windows) and confirm the
version is 3.9 or higher. If Python is not found or the version is too old,
tell the user they need Python 3.9+ installed and stop.

## Step 2: Check if mempalace is already installed

Run `mempalace --version`. If it succeeds, the CLI is on PATH — report
the installed version and skip to Step 4.

If `mempalace --version` fails, **do not** skip to Step 4 just because
`pip show mempalace` or `uv tool list` reports the package as installed:
the package may live inside a venv that isn't activated, in which case
Step 5 (`mempalace init ...`) will fail with `command not found`. Treat
that case as not-installed and continue to Step 3, which will (re)install
into a PATH-visible location via `uv tool install` or `pip`.

## Step 3: Install mempalace

Prefer [`uv`](https://docs.astral.sh/uv/) — it isolates the CLI from system
Python and avoids most environment-related failures:

1. If `uv` is on PATH (`uv --version`), run `uv tool install mempalace`.
2. Otherwise run `pip install mempalace`.

### Error handling -- install failures

If the install command fails, try these fallbacks in order:

1. If `uv tool install` failed, try `pip install mempalace` (or vice versa).
2. Try `pip3 install mempalace`.
3. Try `python -m pip install mempalace` (or `python3 -m pip install mempalace`).
4. If the error mentions missing build tools or compilation failures (commonly
   from chromadb or its native dependencies):
   - On Linux/macOS: suggest `sudo apt-get install build-essential python3-dev`
     (Debian/Ubuntu) or `xcode-select --install` (macOS)
   - On Windows: suggest installing Microsoft C++ Build Tools from
     https://visualstudio.microsoft.com/visual-cpp-build-tools/
   - Then retry the install command
5. If all attempts fail, report the error clearly and stop.

## Step 4: Ask for project directory

Ask the user which project directory they want to initialize with MemPalace.
Offer the current working directory as the default. Wait for their response
before continuing.

## Step 5: Initialize the palace

Run `mempalace init --yes <dir>` where `<dir>` is the directory from Step 4.

If this fails, report the error and stop.

## Step 6: Configure MCP server

Run the following command to register the MemPalace MCP server with Claude:

    claude mcp add mempalace -- mempalace-mcp

If this fails, report the error but continue to the next step (MCP
configuration can be done manually later).

## Step 7: Verify installation

Run `mempalace status` and confirm the output shows a healthy palace.

If the command fails or reports errors, walk the user through troubleshooting
based on the output.

## Step 8: Show next steps

Tell the user setup is complete and suggest these next actions:

- Use /mempalace:mine to start adding data to their palace
- Use /mempalace:search to query their palace and retrieve stored knowledge
