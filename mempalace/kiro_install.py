"""
Kiro IDE integration for MemPalace.

Kiro (the AI IDE, https://kiro.dev) does not use Claude-Code/Codex-style
plugins. Its two first-class extension points are:

  1. MCP servers, registered in ``~/.kiro/settings/mcp.json`` (global) or
     ``<workspace>/.kiro/settings/mcp.json`` (workspace-local).
  2. Steering files, Markdown under ``~/.kiro/steering/`` (or workspace
     ``.kiro/steering/``) that are injected into the agent's context.

Kiro also has no live Stop/PreCompact hook mechanism the way Claude Code and
Codex do, so MemPalace captures Kiro history by *reading the session
transcripts Kiro already writes to disk* — the same approach used by the
kiro-recall project — via ``mempalace mine <sessions-dir> --mode convos``.

This module is intentionally dependency-free (stdlib only) so ``mempalace
kiro install`` works even before the heavier ChromaDB stack is imported.

Public entry points (all return a list of human-readable status lines):
    install(local=None, palace=None, command=None)
    uninstall(local=None)
    sync(palace=None, agent_dir=None, dry_run=False)
    status(local=None, agent_dir=None)
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Optional

from .version import __version__

# Key under which MemPalace registers itself in Kiro's mcp.json. Matches the
# server name used everywhere else (.claude-plugin / .codex-plugin).
SERVER_KEY = "mempalace"

# Kiro's per-user agent directory name inside globalStorage.
_AGENT_DIR = "kiro.kiroagent"

# Every MCP tool MemPalace exposes (see mcp_server.TOOLS). We auto-approve all
# of them EXCEPT the two destructive deletes, so memory recall + saving is
# seamless while data loss still requires an explicit confirmation in Kiro.
_ALL_TOOLS = (
    "mempalace_status",
    "mempalace_list_wings",
    "mempalace_list_rooms",
    "mempalace_get_taxonomy",
    "mempalace_get_aaak_spec",
    "mempalace_kg_query",
    "mempalace_kg_add",
    "mempalace_kg_invalidate",
    "mempalace_kg_timeline",
    "mempalace_kg_stats",
    "mempalace_traverse",
    "mempalace_find_tunnels",
    "mempalace_graph_stats",
    "mempalace_create_tunnel",
    "mempalace_list_tunnels",
    "mempalace_follow_tunnels",
    "mempalace_search",
    "mempalace_check_duplicate",
    "mempalace_add_drawer",
    "mempalace_get_drawer",
    "mempalace_list_drawers",
    "mempalace_update_drawer",
    "mempalace_diary_write",
    "mempalace_diary_read",
    "mempalace_hook_settings",
    "mempalace_memories_filed_away",
    "mempalace_sync",
    "mempalace_reconnect",
)

# Destructive tools deliberately left OUT of autoApprove.
_DELETE_TOOLS = ("mempalace_delete_drawer", "mempalace_delete_tunnel")

AUTO_APPROVE = [t for t in _ALL_TOOLS if t not in _DELETE_TOOLS]

STEERING_FILENAME = "mempalace.md"

STEERING_CONTENT = """\
---
inclusion: always
---

# MemPalace — persistent AI memory

You have a persistent, searchable memory of past work and conversations,
exposed through the `mempalace` MCP server. Treat it as your long-term
memory: consult it before guessing, and record what matters after doing
non-trivial work.

## Recall (read) — do this proactively
Before answering questions about a person, project, past decision, or prior
work — and before starting non-trivial work on a feature/file you have not
discussed this session — search your memory first:

- `mempalace_search` — semantic search across everything you've stored.
  Pass ONLY keywords in `query`; put background in `context`.
- `mempalace_status` / `mempalace_list_wings` / `mempalace_get_taxonomy` —
  see what's stored (wings = people/projects, rooms = days/topics).
- `mempalace_kg_query` / `mempalace_kg_timeline` — look up facts and how
  they changed over time.
- `mempalace_get_drawer` / `mempalace_list_drawers` — open verbatim content.

Triggers: "like we discussed", "as we decided", "remember when", "how did
we fix", "what's my usual approach", or any reference to prior context.

## Record (write) — after meaningful work
- `mempalace_diary_write` — a short session summary of what happened, what
  was decided, and what was learned.
- `mempalace_add_drawer` — file verbatim quotes, decisions, and code into
  the right wing/room. Call `mempalace_check_duplicate` first to avoid
  re-filing.
- `mempalace_kg_add` / `mempalace_kg_invalidate` — when facts are
  established or change.

## Principles
- Verbatim is sacred — store exact words, never paraphrase user content.
- Be quiet about bookkeeping: don't narrate memory lookups or saves unless
  asked. If a search finds nothing useful, just continue.
- Everything stays local — MemPalace never sends your data anywhere.

## Backfilling history
MemPalace also reads Kiro's own session transcripts. To import past Kiro
chats into the palace, run `mempalace kiro sync` (or
`mempalace mine <kiro-sessions-dir> --mode convos`).
"""


# ── path resolution ────────────────────────────────────────────────────


def kiro_base(local: Optional[str]) -> Path:
    """Resolve the ``.kiro`` config dir: global ``~/.kiro`` or a workspace dir.

    ``local`` may be a workspace path (``--local DIR``) or the current working
    directory when ``--local`` is passed with no argument.
    """
    if local:
        return Path(local).expanduser().resolve() / ".kiro"
    return Path.home() / ".kiro"


def _kiro_user_dirs() -> list[Path]:
    """Candidate roots for Kiro user data, by platform (mirrors kiro-recall)."""
    home = Path.home()
    if sys.platform == "darwin":
        return [home / "Library" / "Application Support" / "Kiro" / "User"]
    if sys.platform.startswith("win") or os.name == "nt":
        appdata = os.environ.get("APPDATA") or str(home / "AppData" / "Roaming")
        return [Path(appdata) / "Kiro" / "User"]
    xdg = os.environ.get("XDG_CONFIG_HOME") or str(home / ".config")
    return [Path(xdg) / "Kiro" / "User"]


def kiro_agent_dir(override: Optional[str] = None) -> Optional[Path]:
    """Locate Kiro's ``globalStorage/kiro.kiroagent`` directory, or None.

    Honors an explicit ``override`` (or the ``MEMPALACE_KIRO_AGENT_DIR`` env
    var) for tests / non-standard installs.
    """
    override = override or os.environ.get("MEMPALACE_KIRO_AGENT_DIR")
    if override:
        cand = Path(override).expanduser()
        return cand if cand.is_dir() else None
    for user_dir in _kiro_user_dirs():
        candidate = user_dir / "globalStorage" / _AGENT_DIR
        if candidate.is_dir():
            return candidate
    return None


def kiro_sessions_dir(override: Optional[str] = None) -> Optional[Path]:
    """Return ``<agentDir>/workspace-sessions`` if it exists, else None."""
    agent = kiro_agent_dir(override)
    if agent is None:
        return None
    sessions = agent / "workspace-sessions"
    return sessions if sessions.is_dir() else None


# ── mcp.json + steering read/write ───────────────────────────────────────


def _mcp_entry(command: str, palace: Optional[str]) -> dict:
    """Build the Kiro mcp.json entry for the MemPalace server."""
    args: list[str] = []
    if palace:
        args = ["--palace", str(Path(palace).expanduser())]
    return {
        "type": "stdio",
        "command": command,
        "args": args,
        "disabled": False,
        "autoApprove": list(AUTO_APPROVE),
        "description": (
            "MemPalace: persistent, searchable AI memory. Recall past work and "
            "conversations; file verbatim memories. Local-only, no API key."
        ),
    }


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def register_mcp(base: Path, command: str, palace: Optional[str]) -> str:
    """Merge the MemPalace server into ``<base>/settings/mcp.json``.

    Never clobbers other servers; only the ``mempalace`` key is rewritten.
    """
    cfg_path = base / "settings" / "mcp.json"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    config = _read_json(cfg_path)
    if not isinstance(config, dict):
        config = {}
    servers = config.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
        config["mcpServers"] = servers
    servers[SERVER_KEY] = _mcp_entry(command, palace)
    cfg_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return f'registered MCP server "{SERVER_KEY}" in {cfg_path}'


def unregister_mcp(base: Path) -> str:
    """Remove only the MemPalace server entry from ``<base>/settings/mcp.json``."""
    cfg_path = base / "settings" / "mcp.json"
    config = _read_json(cfg_path)
    if not isinstance(config, dict) or SERVER_KEY not in (config.get("mcpServers") or {}):
        return "MCP entry not present"
    del config["mcpServers"][SERVER_KEY]
    cfg_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return f'removed MCP server "{SERVER_KEY}" from {cfg_path}'


def write_steering(base: Path) -> str:
    """Write the MemPalace steering file under ``<base>/steering/``."""
    steering_dir = base / "steering"
    steering_dir.mkdir(parents=True, exist_ok=True)
    file = steering_dir / STEERING_FILENAME
    file.write_text(STEERING_CONTENT, encoding="utf-8")
    return f"wrote steering {file}"


def remove_steering(base: Path) -> str:
    file = base / "steering" / STEERING_FILENAME
    if file.exists():
        file.unlink()
        return f"removed steering {file}"
    return "steering not present"


# ── public commands ──────────────────────────────────────────────────────


def install(
    local: Optional[str] = None,
    palace: Optional[str] = None,
    command: str = "mempalace-mcp",
) -> list[str]:
    base = kiro_base(local)
    scope = f"local ({base})" if local else "global (~/.kiro)"
    lines = [f"MemPalace {__version__} → Kiro", f"scope: {scope}"]
    lines.append(register_mcp(base, command=command, palace=palace))
    lines.append(write_steering(base))
    lines.append("")
    lines.append("Next: reload Kiro (Command Palette → 'Developer: Reload Window').")
    lines.append("The mempalace MCP server starts automatically on the next session.")
    sessions = kiro_sessions_dir()
    if sessions is not None:
        lines.append("")
        lines.append(
            "Tip: backfill past Kiro chats with `mempalace kiro sync` "
            f"(found sessions at {sessions})."
        )
    return lines


def uninstall(local: Optional[str] = None) -> list[str]:
    base = kiro_base(local)
    scope = f"local ({base})" if local else "global (~/.kiro)"
    lines = [f"scope: {scope}"]
    lines.append(unregister_mcp(base))
    lines.append(remove_steering(base))
    lines.append("Note: your palace data is left intact (~/.mempalace).")
    return lines


def sync(
    palace: Optional[str] = None,
    agent_dir: Optional[str] = None,
    dry_run: bool = False,
) -> list[str]:
    """Mine Kiro's session transcripts into the palace (``--mode convos``)."""
    sessions = kiro_sessions_dir(agent_dir)
    if sessions is None:
        return [
            "Could not locate Kiro's session transcripts.",
            "Looked under each platform's globalStorage/kiro.kiroagent/workspace-sessions.",
            "If Kiro is installed in a non-standard location, set "
            "MEMPALACE_KIRO_AGENT_DIR to its globalStorage/kiro.kiroagent path, or run:",
            "  mempalace mine <path-to-kiro-sessions> --mode convos",
        ]
    # Import here so `mempalace kiro install` stays dependency-free.
    from .config import MempalaceConfig
    from .convo_miner import mine_convos

    palace_path = os.path.expanduser(palace) if palace else MempalaceConfig().palace_path
    mine_convos(
        convo_dir=str(sessions),
        palace_path=palace_path,
        dry_run=dry_run,
    )
    return [f"Synced Kiro sessions from {sessions} into {palace_path}"]


def status(local: Optional[str] = None, agent_dir: Optional[str] = None) -> list[str]:
    base = kiro_base(local)
    cfg_path = base / "settings" / "mcp.json"
    steering = base / "steering" / STEERING_FILENAME
    config = _read_json(cfg_path)
    registered = isinstance(config, dict) and SERVER_KEY in (config.get("mcpServers") or {})

    lines = [f"MemPalace {__version__} — Kiro integration status", f"  scope:    {base}"]
    lines.append(f"  mcp.json: {'registered' if registered else 'not registered'} ({cfg_path})")
    lines.append(f"  steering: {'present' if steering.exists() else 'absent'} ({steering})")
    sessions = kiro_sessions_dir(agent_dir)
    lines.append(f"  sessions: {sessions if sessions else 'not found'}")
    return lines
