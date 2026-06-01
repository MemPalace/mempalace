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
    install(local=None, palace=None, command=None, autosync=True, retention_days=30)
    uninstall(local=None)
    sync(palace=None, agent_dir=None, dry_run=False, retention_days=None)
    status(local=None, agent_dir=None)

Plus retention + auto-sync helpers:
    prune_expired_sessions(palace_path, retention_days, dry_run=False)
    maybe_autosync()   — debounced detached background sync (called on MCP boot)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from .version import __version__

# Key under which MemPalace registers itself in Kiro's mcp.json. Matches the
# server name used everywhere else (.claude-plugin / .codex-plugin).
SERVER_KEY = "mempalace"

# Kiro's per-user agent directory name inside globalStorage.
_AGENT_DIR = "kiro.kiroagent"

# Rolling-retention default: prune Kiro sessions whose transcript file hasn't
# changed in this many days. 0 disables retention (keep everything forever).
DEFAULT_RETENTION_DAYS = 30
RETENTION_ENV = "MEMPALACE_KIRO_RETENTION_DAYS"

# Auto-sync: when Kiro launches the MCP server with AUTOSYNC_ENV truthy, the
# server kicks off a debounced background `mempalace kiro sync`. The debounce
# interval (minutes) keeps repeated session-opens from re-syncing constantly.
AUTOSYNC_ENV = "MEMPALACE_KIRO_AUTOSYNC"
AUTOSYNC_INTERVAL_ENV = "MEMPALACE_KIRO_AUTOSYNC_INTERVAL_MIN"
DEFAULT_AUTOSYNC_INTERVAL_MIN = 30
_TRUTHY = {"1", "true", "yes", "on"}

# Debounce marker + log for background auto-sync. Under ~/.mempalace so it sits
# alongside the palace/hook state, independent of the (possibly custom) palace
# path — it only gates how often the background sync fires.
_STATE_DIR = Path.home() / ".mempalace"
_AUTOSYNC_STATE = _STATE_DIR / "kiro_autosync.state"
_AUTOSYNC_LOG = _STATE_DIR / "kiro_autosync.log"

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


def resolve_retention_days(explicit: Optional[int] = None) -> int:
    """Resolve the retention window: explicit arg > env > default.

    Negative values clamp to 0 (disabled). The env var (set by ``kiro
    install`` into Kiro's mcp.json) lets the background auto-sync inherit
    the window the user chose at install time.
    """
    if explicit is not None:
        return max(0, explicit)
    raw = os.environ.get(RETENTION_ENV)
    if raw is not None and raw.strip():
        try:
            return max(0, int(raw))
        except ValueError:
            pass
    return DEFAULT_RETENTION_DAYS


def _mcp_entry(
    command: str,
    palace: Optional[str],
    autosync: bool = True,
    retention_days: int = DEFAULT_RETENTION_DAYS,
) -> dict:
    """Build the Kiro mcp.json entry for the MemPalace server.

    When ``autosync`` is on, the entry carries ``env`` so the server (spawned
    by Kiro every session) runs a debounced background sync on boot. The
    retention window rides along in the same ``env`` so the background sync
    prunes with the window chosen at install time.
    """
    args: list[str] = []
    if palace:
        args = ["--palace", str(Path(palace).expanduser())]
    entry = {
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
    env: dict[str, str] = {}
    if autosync:
        env[AUTOSYNC_ENV] = "1"
    env[RETENTION_ENV] = str(max(0, retention_days))
    entry["env"] = env
    return entry


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def register_mcp(
    base: Path,
    command: str,
    palace: Optional[str],
    autosync: bool = True,
    retention_days: int = DEFAULT_RETENTION_DAYS,
) -> str:
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
    servers[SERVER_KEY] = _mcp_entry(
        command, palace, autosync=autosync, retention_days=retention_days
    )
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
    autosync: bool = True,
    retention_days: int = DEFAULT_RETENTION_DAYS,
) -> list[str]:
    base = kiro_base(local)
    scope = f"local ({base})" if local else "global (~/.kiro)"
    retention_days = max(0, retention_days)
    lines = [f"MemPalace {__version__} → Kiro", f"scope: {scope}"]
    lines.append(
        register_mcp(
            base, command=command, palace=palace, autosync=autosync, retention_days=retention_days
        )
    )
    lines.append(write_steering(base))
    if autosync:
        lines.append(
            "auto-sync: ON — a background `kiro sync` runs when Kiro starts "
            f"(every ~{DEFAULT_AUTOSYNC_INTERVAL_MIN} min at most)."
        )
    else:
        lines.append("auto-sync: OFF — run `mempalace kiro sync` manually.")
    if retention_days > 0:
        lines.append(
            f"retention: {retention_days} days — older Kiro sessions are pruned on each sync."
        )
    else:
        lines.append("retention: disabled — Kiro history is kept forever.")
    lines.append("")
    lines.append("Next: reload Kiro (Command Palette → 'Developer: Reload Window').")
    lines.append("The mempalace MCP server starts automatically on the next session.")
    sessions = kiro_sessions_dir()
    if sessions is not None:
        lines.append("")
        lines.append(
            "Tip: backfill past Kiro chats now with `mempalace kiro sync` "
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


def prune_expired_sessions(
    palace_path: str,
    retention_days: int,
    dry_run: bool = False,
) -> dict:
    """Delete stored drawers from Kiro sessions older than the retention window.

    A Kiro drawer is identified by its ``source_file`` metadata pointing into a
    ``kiro.kiroagent/workspace-sessions`` path (``kiro_ingest.is_kiro_session_path``);
    drawers from other ingest sources are never touched. A session is "expired"
    when its transcript file's mtime is older than the window, OR the transcript
    file no longer exists (Kiro itself pruned it). Registry sentinels for expired
    sessions are removed too, and matching closets are purged.

    Returns a report dict: ``{pruned_drawers, expired_sessions, dry_run, skipped}``.
    ``skipped`` is True when retention is disabled or there's no palace yet.
    """
    report = {"pruned_drawers": 0, "expired_sessions": 0, "dry_run": dry_run, "skipped": False}
    if retention_days <= 0:
        report["skipped"] = True
        return report

    from .kiro_ingest import is_kiro_session_path
    from .palace import get_closets_collection, get_collection, mine_palace_lock

    cutoff = time.time() - retention_days * 86400
    batch = 1000

    def _expired(src: str) -> bool:
        try:
            return Path(src).stat().st_mtime < cutoff
        except OSError:
            return True  # transcript gone → definitely aged out

    remove_ids: list[str] = []
    remove_sources: set[str] = set()
    verdict: dict[str, bool] = {}

    with mine_palace_lock(palace_path):
        try:
            col = get_collection(palace_path, create=False)
        except Exception:
            report["skipped"] = True
            return report
        if col is None:
            report["skipped"] = True
            return report

        offset = 0
        while True:
            page = col.get(limit=batch, offset=offset, include=["metadatas"])
            ids = page.get("ids") or []
            metas = page.get("metadatas") or []
            if not ids:
                break
            for drawer_id, meta in zip(ids, metas):
                src = (meta or {}).get("source_file")
                if not src:
                    continue
                if src not in verdict:
                    verdict[src] = is_kiro_session_path(src) and _expired(src)
                if verdict[src]:
                    remove_ids.append(drawer_id)
                    remove_sources.add(src)
            if len(ids) < batch:
                break
            offset += len(ids)

        report["expired_sessions"] = len(remove_sources)
        if dry_run or not remove_ids:
            report["pruned_drawers"] = 0 if dry_run else 0
            report["would_remove"] = len(remove_ids)
            return report

        for i in range(0, len(remove_ids), batch):
            col.delete(ids=remove_ids[i : i + batch])
        report["pruned_drawers"] = len(remove_ids)

        # Best-effort: purge any closets tied to the pruned sources.
        try:
            ccol = get_closets_collection(palace_path, create=False)
            if ccol is not None and remove_sources:
                cids = (
                    ccol.get(where={"source_file": {"$in": list(remove_sources)}}, include=[]).get(
                        "ids"
                    )
                    or []
                )
                if cids:
                    ccol.delete(ids=cids)
        except Exception:
            pass

    return report


def sync(
    palace: Optional[str] = None,
    agent_dir: Optional[str] = None,
    dry_run: bool = False,
    retention_days: Optional[int] = None,
) -> list[str]:
    """Mine Kiro's session transcripts into the palace, then prune aged-out ones.

    ``retention_days`` resolves via ``resolve_retention_days`` (explicit arg >
    ``MEMPALACE_KIRO_RETENTION_DAYS`` env > default 30; 0 = keep everything).
    Sessions older than the window are skipped on ingest AND pruned from the
    palace, giving a rolling window.
    """
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
    days = resolve_retention_days(retention_days)
    newer_than = (time.time() - days * 86400) if days > 0 else None

    mine_convos(
        convo_dir=str(sessions),
        palace_path=palace_path,
        dry_run=dry_run,
        newer_than=newer_than,
    )

    lines = [f"Synced Kiro sessions from {sessions} into {palace_path}"]
    if days <= 0:
        lines.append("Retention: disabled — keeping all Kiro history.")
        return lines

    report = prune_expired_sessions(palace_path, days, dry_run=dry_run)
    if report.get("skipped"):
        lines.append(f"Retention: {days}-day window (nothing to prune yet).")
    elif dry_run:
        lines.append(
            f"Retention: {days}-day window — would prune "
            f"{report.get('would_remove', 0)} drawer(s) from "
            f"{report['expired_sessions']} expired session(s)."
        )
    else:
        lines.append(
            f"Retention: {days}-day window — pruned {report['pruned_drawers']} "
            f"drawer(s) from {report['expired_sessions']} expired session(s)."
        )
    return lines


def _detached_popen_kwargs() -> dict:
    """Kwargs that fully detach a background child so the parent can exit."""
    kwargs: dict = {"stdin": subprocess.DEVNULL, "close_fds": True}
    if os.name == "nt":
        flags = 0
        for name in ("DETACHED_PROCESS", "CREATE_NEW_PROCESS_GROUP", "CREATE_BREAKAWAY_FROM_JOB"):
            flags |= getattr(subprocess, name, 0)
        if flags:
            kwargs["creationflags"] = flags
    else:
        kwargs["start_new_session"] = True
    return kwargs


def _autosync_interval_secs() -> float:
    raw = os.environ.get(AUTOSYNC_INTERVAL_ENV)
    if raw and raw.strip():
        try:
            return max(0.0, float(raw)) * 60.0
        except ValueError:
            pass
    return DEFAULT_AUTOSYNC_INTERVAL_MIN * 60.0


def maybe_autosync() -> bool:
    """Spawn a debounced, detached background ``mempalace kiro sync``.

    Called from the MCP server's startup (guarded by ``MEMPALACE_KIRO_AUTOSYNC``)
    so opening Kiro keeps the palace current without a manual sync. Best-effort:
    never raises, never writes to stdout/stderr of the caller (the child's output
    goes to ``~/.mempalace/kiro_autosync.log``), and never blocks — it returns
    immediately after spawning. Returns True if a sync was launched, False if it
    was debounced or could not start.

    The child inherits the server's env, so the retention window
    (``MEMPALACE_KIRO_RETENTION_DAYS``) set at install time flows through.
    """
    try:
        interval = _autosync_interval_secs()
        now = time.time()
        try:
            last = float(_AUTOSYNC_STATE.read_text().strip())
        except (OSError, ValueError):
            last = 0.0
        if interval > 0 and (now - last) < interval:
            return False
        try:
            _STATE_DIR.mkdir(parents=True, exist_ok=True)
            _AUTOSYNC_STATE.write_text(str(now))
        except OSError:
            pass  # debounce marker is best-effort; still attempt the sync
        try:
            log_f = open(_AUTOSYNC_LOG, "a")
        except OSError:
            log_f = subprocess.DEVNULL
        try:
            subprocess.Popen(
                [sys.executable, "-m", "mempalace", "kiro", "sync"],
                stdout=log_f,
                stderr=log_f,
                **_detached_popen_kwargs(),
            )
        finally:
            if log_f not in (None, subprocess.DEVNULL):
                try:
                    log_f.close()
                except OSError:
                    pass
        return True
    except Exception:
        return False


def status(local: Optional[str] = None, agent_dir: Optional[str] = None) -> list[str]:
    base = kiro_base(local)
    cfg_path = base / "settings" / "mcp.json"
    steering = base / "steering" / STEERING_FILENAME
    config = _read_json(cfg_path)
    servers = config.get("mcpServers") if isinstance(config, dict) else None
    entry = servers.get(SERVER_KEY) if isinstance(servers, dict) else None
    registered = isinstance(entry, dict)
    env = entry.get("env", {}) if registered else {}
    autosync_on = str(env.get(AUTOSYNC_ENV, "")).strip().lower() in _TRUTHY
    retention = env.get(RETENTION_ENV)

    lines = [f"MemPalace {__version__} — Kiro integration status", f"  scope:     {base}"]
    lines.append(f"  mcp.json:  {'registered' if registered else 'not registered'} ({cfg_path})")
    lines.append(f"  steering:  {'present' if steering.exists() else 'absent'} ({steering})")
    if registered:
        lines.append(f"  auto-sync: {'on' if autosync_on else 'off'}")
        if retention is not None:
            lines.append(
                f"  retention: {retention} days"
                if str(retention) != "0"
                else "  retention: disabled (keep everything)"
            )
    sessions = kiro_sessions_dir(agent_dir)
    lines.append(f"  sessions:  {sessions if sessions else 'not found'}")
    return lines
