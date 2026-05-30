# Multi-Session MCP Daemon — Persistent MemPalace for All Your AI Agents

**Fixes:** #1229 — zombie MCP server processes blocking sessions and corrupting ChromaDB

## The Problem

The default `mempalace-mcp` command (which calls `mempalace.mcp_server`) is designed
to run as one process per agent session. When you run Claude Code, Codex, Gemini CLI,
and GG simultaneously, each session spawns its own Python process — and each process
holds an open ChromaDB `PersistentClient`.

This creates two compounding failure modes:

### 1. Zombie processes after SIGKILL

MCP host applications (Claude Desktop, VS Code, terminal multiplexers) sometimes
force-quit sessions with `SIGKILL` rather than `SIGTERM`. Python's `atexit` handlers
and `signal.signal(SIGTERM, ...)` cleanup traps never fire on `SIGKILL`. The result
is a Python process that exits without releasing its file descriptor on
`~/.mempalace/mcp-server.pid`.

On the next session start, a PID-file guard sees a stale PID file, decides another
instance is running, and refuses to start. The session connects to nothing, and every
MCP tool call returns "Connection closed".

Removing the stale PID file by hand is the only recovery — until it happens again.

### 2. Concurrent ChromaDB writers corrupt HNSW

ChromaDB's HNSW index uses memory-mapped segment files. When two processes both hold
a `PersistentClient` against the same `chroma.sqlite3` and both call `upsert()`, the
writes interleave at the mmap level. This causes the in-memory HNSW tree and the on-disk
sqlite metadata to diverge — exactly the divergence issue #1222 documents and detects.

The `hnsw_capacity_status` probe (introduced in #1222) can detect this after the fact,
but it cannot prevent it. The only safe fix is ensuring **a single process owns the
ChromaDB connection** at all times.

## The Solution: Daemon + Bridge Architecture

Instead of each session spawning its own `mcp_server.py` process, a single **daemon**
process runs continuously (managed by macOS LaunchAgent), holds the ChromaDB connection,
and serves all sessions over a Unix socket. Each agent session runs a tiny **bridge**
script that relays its stdio to/from the daemon socket.

```
macOS LaunchAgent
  └── mempalace-daemon.py    (one process, holds ChromaDB, listens on ~/.mempalace/mcp.sock)
        ├── Claude Code session  ←→  mempalace-bridge.py  ←→  socket
        ├── Codex session        ←→  mempalace-bridge.py  ←→  socket
        ├── Gemini CLI session   ←→  mempalace-bridge.py  ←→  socket
        └── GG session           ←→  mempalace-bridge.py  ←→  socket
```

**Benefits:**

- **No zombie problem.** If a session is SIGKILL'd, only the bridge dies. The daemon
  keeps running; the socket stays open; the next session connects immediately.
- **No concurrent writer corruption.** All `tools/call` requests are serialised through
  a single `threading.Lock` inside the daemon. Protocol messages (`initialize`,
  `tools/list`, `ping`) are lock-free for speed.
- **Auto-start on first use.** The bridge detects a missing socket and starts the
  daemon automatically, so you don't have to think about it.
- **LaunchAgent keeps it alive.** If the daemon crashes (e.g. OOM), launchd restarts
  it within `ThrottleInterval` seconds (default: 5).

## Files

Three files are provided in the `examples/` directory:

| File | Purpose |
|------|---------|
| `mempalace-daemon.py` | The persistent server process — run once via LaunchAgent |
| `mempalace-bridge.py` | Per-session stdio relay — this is the MCP command you configure |
| `com.mempalace.daemon.plist` | macOS LaunchAgent template — edit paths, then `launchctl load` |

## Installation

### Step 1 — Copy the scripts

```bash
# Copy to wherever you keep your local tooling.
# The bridge looks for the daemon script relative to its own location,
# so keep both files in the same directory.
cp examples/mempalace-daemon.py ~/bin/mempalace-daemon.py
cp examples/mempalace-bridge.py ~/bin/mempalace-bridge.py
chmod +x ~/bin/mempalace-daemon.py ~/bin/mempalace-bridge.py
```

### Step 2 — Edit the LaunchAgent plist

Open `examples/com.mempalace.daemon.plist` and replace:

- `/path/to/mempalace/venv/bin/python` — the Python interpreter in your MemPalace
  virtualenv (run `which python` inside the venv to get the path)
- `/path/to/mempalace-daemon.py` — the absolute path where you copied the daemon script
- `YOUR_USERNAME` — your macOS username (`echo $USER`)

### Step 3 — Install and load the LaunchAgent

```bash
cp examples/com.mempalace.daemon.plist ~/Library/LaunchAgents/com.mempalace.daemon.plist
launchctl load ~/Library/LaunchAgents/com.mempalace.daemon.plist
```

Verify it started:

```bash
# The socket should exist within a few seconds
ls -la ~/.mempalace/mcp.sock

# Check the log
tail -f ~/.mempalace/daemon.log
```

### Step 4 — Configure each agent to use the bridge

Replace every `mempalace-mcp` / `mempalace.mcp_server` invocation with the bridge.

#### Claude Code (`~/.claude.json`)

```json
{
  "mcpServers": {
    "mempalace": {
      "type": "stdio",
      "command": "/absolute/path/to/python",
      "args": ["/absolute/path/to/mempalace-bridge.py"]
    }
  }
}
```

Or register via CLI:

```bash
claude mcp add mempalace -- /path/to/python /path/to/mempalace-bridge.py
```

#### Codex (`~/.codex/config.toml`)

```toml
[mcp_servers.mempalace]
command = "/absolute/path/to/python"
args    = ["/absolute/path/to/mempalace-bridge.py"]
```

#### Gemini CLI (`~/.gemini/settings.json`)

```json
{
  "mcpServers": {
    "mempalace": {
      "command": "/absolute/path/to/python",
      "args": ["/absolute/path/to/mempalace-bridge.py"]
    }
  }
}
```

Or via CLI:

```bash
gemini mcp add mempalace /absolute/path/to/python /absolute/path/to/mempalace-bridge.py --scope user
```

#### Any other stdio MCP client

The bridge is a generic stdio relay. Any client that accepts a `command` + `args`
MCP configuration can use it:

```
command: /path/to/python
args:    ["/path/to/mempalace-bridge.py"]
```

## Troubleshooting

### "MemPalace daemon not reachable"

The bridge tried 20 times (5 seconds total) to connect and failed.

1. Check the daemon log: `tail ~/.mempalace/daemon.log`
2. Check launchd status: `launchctl list | grep mempalace`
3. Try starting the daemon manually to see startup errors:
   ```bash
   /path/to/python /path/to/mempalace-daemon.py
   ```

### LaunchAgent not starting

- Verify plist syntax: `plutil ~/Library/LaunchAgents/com.mempalace.daemon.plist`
- Reload: `launchctl unload ~/Library/LaunchAgents/com.mempalace.daemon.plist && launchctl load ~/Library/LaunchAgents/com.mempalace.daemon.plist`
- Check Console.app for launchd errors.

### Stale socket from a previous crash

If the daemon crashed without cleanup, the socket file may still exist but be dead.
The daemon removes a stale socket automatically on startup (`SOCK_PATH.unlink()` before
`server.bind()`), so restarting the daemon is sufficient:

```bash
launchctl kickstart -k gui/$(id -u)/com.mempalace.daemon
```

### Reverting to the single-process mode

Remove or unload the LaunchAgent, then update your MCP configurations back to
`mempalace-mcp` or `python -m mempalace.mcp_server`. The daemon and bridge are
additive — they do not modify the core `mcp_server.py`.

## Tested on

- macOS 14 Sonoma
- macOS 15 Sequoia
- Python 3.11 / 3.12
- MemPalace 3.3.x (ChromaDB 0.6.x)
- Concurrent sessions: Claude Code + Codex + Gemini CLI + GG
