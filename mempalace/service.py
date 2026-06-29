"""Shared service operations used by daemon-backed entry points.

The MCP server remains the owner of MCP transport details. This module owns the
small, transport-neutral execution surface the daemon needs: classify known
tools and execute durable background jobs without printing directly to the
caller's terminal.
"""

from __future__ import annotations

import contextlib
import io
import os
import sys
from typing import Any

from .config import MempalaceConfig

_EXPLICIT_BACKEND_ENV = "MEMPALACE_BACKEND_EXPLICIT"
_PALACE_PATH_ENV = "MEMPALACE_PALACE_PATH"
_BACKEND_ENV = "MEMPALACE_BACKEND"
# Env vars a job may mutate via _apply_backend / palace_path injection. They are
# snapshotted per job and restored afterward so a job that switches the backend
# (e.g. qdrant) cannot poison every later job in the same daemon process —
# including mcp_tool jobs, which read MempalaceConfig (and thus the leaked env).
_PER_JOB_ENV = (_PALACE_PATH_ENV, _BACKEND_ENV, _EXPLICIT_BACKEND_ENV)


READ_TOOLS = frozenset(
    {
        "mempalace_status",
        "mempalace_list_wings",
        "mempalace_list_rooms",
        "mempalace_get_taxonomy",
        "mempalace_get_aaak_spec",
        "mempalace_traverse",
        "mempalace_find_tunnels",
        "mempalace_graph_stats",
        "mempalace_list_tunnels",
        "mempalace_list_hallways",
        "mempalace_follow_tunnels",
        "mempalace_search",
        "mempalace_check_duplicate",
        "mempalace_get_drawer",
        "mempalace_list_drawers",
        "mempalace_diary_read",
        "mempalace_memories_filed_away",
        "mempalace_kg_query",
        "mempalace_kg_stats",
        "mempalace_kg_timeline",
    }
)

WRITE_TOOLS = frozenset(
    {
        "mempalace_add_drawer",
        "mempalace_checkpoint",
        "mempalace_delete_drawer",
        "mempalace_update_drawer",
        "mempalace_diary_write",
        "mempalace_kg_add",
        "mempalace_kg_invalidate",
        "mempalace_create_tunnel",
        "mempalace_delete_tunnel",
        "mempalace_delete_hallway",
        "mempalace_hook_settings",
    }
)

MAINTENANCE_TOOLS = frozenset({"mempalace_mine", "mempalace_sync", "mempalace_reconnect"})


def classify_tool(name: str) -> str:
    """Return ``read``, ``write``, ``maintenance``, or ``unknown`` for an MCP tool."""
    if name in READ_TOOLS:
        return "read"
    if name in WRITE_TOOLS:
        return "write"
    if name in MAINTENANCE_TOOLS:
        return "maintenance"
    return "unknown"


def _apply_backend(backend: str | None) -> None:
    if not backend:
        return
    backend_name = str(backend).strip().lower()
    from .backends import get_backend_class

    get_backend_class(backend_name)
    os.environ[_EXPLICIT_BACKEND_ENV] = backend_name
    os.environ[_BACKEND_ENV] = backend_name


def _capture(fn):
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        result = fn()
    return result, stdout.getvalue(), stderr.getvalue()


def execute_job(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Execute one daemon job and return a JSON-serializable result."""

    def _run():
        if kind == "mine":
            return run_mine(payload)
        if kind == "sync":
            return run_sync(payload)
        if kind == "diary_write":
            return run_diary_write(payload)
        if kind == "mcp_tool":
            return run_mcp_tool(payload)
        return {"success": False, "error": f"unknown daemon job kind: {kind}", "exit_code": 2}

    # Per-job env isolation: snapshot the backend/palace env vars and restore
    # them after the job so one job's _apply_backend / palace_path injection
    # can't leak into the next job in the same long-lived process.
    saved_env = {key: os.environ.get(key) for key in _PER_JOB_ENV}
    try:
        result, stdout, stderr = _capture(_run)
    finally:
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    if result is None:
        result = {}
    if not isinstance(result, dict):
        result = {"success": True, "value": result}
    result.setdefault("success", True)
    result.setdefault("exit_code", 0 if result.get("success") else 1)
    if stdout:
        result["stdout"] = stdout
    if stderr:
        result["stderr"] = stderr
    return result


def run_mine(payload: dict[str, Any]) -> dict[str, Any]:
    """Run the same mine operation as the CLI, without daemon transport concerns."""
    palace_path = os.path.abspath(
        os.path.expanduser(payload.get("palace_path") or MempalaceConfig().palace_path)
    )
    os.environ["MEMPALACE_PALACE_PATH"] = palace_path
    _apply_backend(payload.get("backend"))

    source = payload.get("source") or payload.get("dir")
    mode = payload.get("mode") or "projects"
    wing = payload.get("wing")
    agent = payload.get("agent") or "mempalace"
    limit = int(payload.get("limit") or 0)
    dry_run = bool(payload.get("dry_run"))

    if payload.get("redetect_origin"):
        from .cli import _run_pass_zero

        _run_pass_zero(project_dir=source, palace_dir=palace_path, llm_provider=None)

    from .palace import MineAlreadyRunning, MineValidationError

    try:
        if mode == "convos":
            from .convo_miner import mine_convos

            mine_convos(
                convo_dir=source,
                palace_path=palace_path,
                wing=wing,
                agent=agent,
                limit=limit,
                dry_run=dry_run,
                extract_mode=payload.get("extract") or "exchange",
            )
        elif mode == "extract":
            from .format_miner import mine_formats

            mine_formats(
                format_dir=source,
                palace_path=palace_path,
                wing=wing,
                agent=agent,
                limit=limit,
                dry_run=dry_run,
            )
        elif mode == "projects":
            include_ignored = payload.get("include_ignored") or []
            from .miner import mine

            mine(
                project_dir=source,
                palace_path=palace_path,
                wing_override=wing,
                agent=agent,
                limit=limit,
                dry_run=dry_run,
                respect_gitignore=not bool(payload.get("no_gitignore")),
                include_ignored=include_ignored,
                max_chunks_per_file=payload.get("max_chunks_per_file"),
            )
        else:
            return {"success": False, "error": f"invalid mine mode: {mode}", "exit_code": 2}
    except MineAlreadyRunning as exc:
        return {
            "success": False,
            "error": str(exc),
            "error_class": "LockHeldByOtherProcess",
            "exit_code": 1,
        }
    except MineValidationError as exc:
        return {
            "success": False,
            "error": str(exc),
            "error_class": "MineValidationError",
            "exit_code": 1,
        }
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
        return {
            "success": code == 0,
            "error": str(exc),
            "error_class": "SystemExit",
            "exit_code": code,
        }
    except Exception as exc:
        return {"success": False, "error": f"mine failed: {exc}", "exit_code": 1}

    return {"success": True, "kind": "mine", "mode": mode, "dry_run": dry_run, "exit_code": 0}


def run_sync(payload: dict[str, Any]) -> dict[str, Any]:
    """Run sync and render the same operator-facing summary shape as the CLI."""
    palace_path = os.path.abspath(
        os.path.expanduser(payload.get("palace_path") or MempalaceConfig().palace_path)
    )
    os.environ["MEMPALACE_PALACE_PATH"] = palace_path
    _apply_backend(payload.get("backend"))

    from .backends import detect_backend_for_path
    from .palace import MineAlreadyRunning, _backend_artifact_label, resolve_backend_name

    if not os.path.isdir(palace_path):
        print(f"\n  No palace found at {palace_path}")
        return {"success": True, "exit_code": 0}

    try:
        backend_name = resolve_backend_name(palace_path)
    except Exception as exc:
        return {
            "success": False,
            "error": f"Could not resolve palace backend: {exc}",
            "exit_code": 1,
        }

    if detect_backend_for_path(palace_path) is None:
        print(
            f"\n  Palace dir at {palace_path} exists but has no "
            f"{_backend_artifact_label(backend_name)} yet."
        )
        print("  Run: mempalace mine <dir>")
        return {"success": True, "exit_code": 0}

    project_dirs = []
    if payload.get("dir"):
        project_dirs.append(os.path.expanduser(str(payload["dir"])))
    project_dirs.extend(os.path.expanduser(str(root)) for root in payload.get("root") or [])
    project_dirs = project_dirs or None
    dry_run = bool(payload.get("dry_run", True))

    print(f"\n{'=' * 55}")
    print("  MemPalace Sync — Gitignore-aware drawer prune")
    print(f"{'=' * 55}")
    print(f"  Palace:   {palace_path}")
    if payload.get("wing"):
        print(f"  Wing:     {payload['wing']}")
    if project_dirs:
        for project_dir in project_dirs:
            print(f"  Project:  {project_dir}")
    print(
        "  Mode:     DRY RUN (no deletions)" if dry_run else "  Mode:     APPLY (deleting drawers)"
    )
    print(f"{'-' * 55}\n")

    try:
        from .sync import sync_palace
        from .wal import _wal_log

        report = sync_palace(
            palace_path=palace_path,
            project_dirs=project_dirs,
            wing=payload.get("wing"),
            dry_run=dry_run,
            wal_log=_wal_log,
        )
    except MineAlreadyRunning as exc:
        return {
            "success": False,
            "error": str(exc),
            "error_class": "LockHeldByOtherProcess",
            "exit_code": 1,
        }
    except ValueError as exc:
        return {"success": False, "error": str(exc), "exit_code": 2}
    except Exception as exc:
        return {"success": False, "error": f"sync failed: {exc}", "exit_code": 1}

    removed_suffix = "(would remove)" if dry_run else "(removed)"
    print(f"  Scanned:        {report['scanned']}")
    print(f"  Kept:           {report['kept']}")
    print(f"  Gitignored:     {report['gitignored']}  {removed_suffix}")
    print(f"  Missing:        {report['missing']}  {removed_suffix}")
    print(f"  No source:      {report['no_source']}  (kept)")
    print(f"  Out of scope:   {report['out_of_scope']}  (kept)")

    by_source = report.get("by_source") or {}
    if by_source:
        top = sorted(by_source.items(), key=lambda kv: -kv[1])[:5]
        label = "Top sources to remove" if dry_run else "Top sources removed"
        print(f"\n  {label}:")
        for src, n in top:
            print(f"    {src}  ({n})")

    if dry_run:
        if report["gitignored"] + report["missing"] > 0:
            print("\n  Re-run with --apply to commit these deletions.")
    else:
        print(
            f"\n  Removed {report['removed_drawers']} drawers, {report['removed_closets']} closets."
        )

    print(f"\n{'=' * 55}\n")
    return {"success": True, "report": report, "exit_code": 0}


def run_diary_write(payload: dict[str, Any]) -> dict[str, Any]:
    palace_path = payload.get("palace_path")
    if palace_path:
        os.environ["MEMPALACE_PALACE_PATH"] = os.path.abspath(os.path.expanduser(palace_path))
    _apply_backend(payload.get("backend"))

    from .mcp_server import tool_diary_write

    result = tool_diary_write(
        agent_name=payload.get("agent_name") or "mempalace",
        entry=payload.get("entry") or "",
        topic=payload.get("topic") or "general",
        wing=payload.get("wing") or "",
    )
    result.setdefault("exit_code", 0 if result.get("success") else 1)
    return result


def run_mcp_tool(payload: dict[str, Any]) -> dict[str, Any]:
    """Execute an MCP tool by name over the daemon queue.

    The daemon is a durable, retried write surface — not a general MCP transport.
    Restrict ``mcp_tool`` to write-classified tools only: read tools would
    exfiltrate verbatim palace content into the queue DB and the job result
    (stored world-readable-by-default without the perms fix, and returned over
    /jobs), and maintenance tools already have their own dedicated kinds
    (mine/sync). No internal caller currently uses ``mcp_tool``; this allowlist
    bounds the blast radius of the generic escape hatch.
    """
    name = payload.get("name")
    arguments = payload.get("arguments") or {}
    if not isinstance(arguments, dict):
        return {"success": False, "error": "arguments must be an object", "exit_code": 2}
    classification = classify_tool(name) if name else "unknown"
    if classification != "write":
        return {
            "success": False,
            "error": f"daemon mcp_tool only accepts write tools; {name!r} is {classification}",
            "exit_code": 2,
        }
    from .mcp_server import TOOLS

    if name not in TOOLS:
        return {"success": False, "error": f"unknown MCP tool: {name}", "exit_code": 2}
    result = TOOLS[name]["handler"](**arguments)
    if isinstance(result, dict):
        # Several write tools signal failure with a bare {"error": ...} and no
        # explicit success flag (e.g. tool_create_tunnel / tool_delete_tunnel
        # validation paths). Infer failure from the "error" key so the daemon
        # does not persist a failed write as succeeded with exit_code 0.
        if "success" not in result:
            result["success"] = "error" not in result
        result.setdefault("exit_code", 0 if result.get("success") else 1)
        return result
    return {"success": True, "value": result, "exit_code": 0}


def print_job_result(result: dict[str, Any]) -> int:
    """Replay captured daemon job output and return the intended process exit code."""
    stdout = result.get("stdout")
    stderr = result.get("stderr")
    if stdout:
        print(stdout, end="")
    if stderr:
        print(stderr, end="", file=sys.stderr)
    if not result.get("success", True) and result.get("error") and not stderr:
        print(f"mempalace: {result['error']}", file=sys.stderr)
    return int(result.get("exit_code", 0 if result.get("success", True) else 1) or 0)
