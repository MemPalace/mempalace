"""
Kiro exec-store enrichment for MemPalace ingestion.

Kiro stores a chat in two places:

  1. The workspace-session transcript (``workspace-sessions/<hash>/<id>.json``,
     a ``history[]`` array) — but the *assistant* turn's content is frequently
     just the stub ``"On it."``. That turn carries an ``executionId``.

  2. The per-execution "exec store": JSON files two directory levels deep under
     the agent dir (``<agentDir>/<workspaceHash>/<chatHash>/<leaf>``, leaf files
     have no extension), each ``{ executionId, actions: [...] }`` where the REAL
     generated output lives in ``actions[]`` as ``actionType`` ``"reasoning"``
     (thinking) and ``"say"`` (prose) with the text under ``output.message``.

The ``executionId`` on the transcript's assistant turn equals the exec file's
top-level ``executionId``, so we can splice the real text back onto the stub.

This mirrors kiro-recall's ``execlog.ts`` + ``parser.ts`` so MemPalace ingests
the same content kiro-recall does. Dependency-free (stdlib only) and defensive:
unknown shapes are skipped, never raised.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

# The transcript stub Kiro writes when the real output lives in the exec store.
# We splice ONLY turns whose text is exactly this, to avoid clobbering genuine
# short assistant replies that happen to resemble it.
ASSISTANT_STUB = "On it."

_AGENT_DIR = "kiro.kiroagent"

# Skip exec-store leaf files larger than this when building the map. The real
# say/reasoning text is prose; a multi-hundred-MB file is almost certainly an
# input/context blob we don't want to load into memory. Generous ceiling.
_MAX_EXEC_FILE_BYTES = 25 * 1024 * 1024

# Process-level cache: agent-dir path -> { executionId: spliced text }. The exec
# store can hold thousands of files, so we build the map once per agent dir per
# process rather than per session file during a mine sweep.
_EXEC_MAP_CACHE: dict[str, dict[str, str]] = {}


def clear_cache() -> None:
    """Drop the cached exec maps (used by tests that mutate the exec store)."""
    _EXEC_MAP_CACHE.clear()


def _exec_log_files(agent_dir: Path) -> list[Path]:
    """Enumerate exec-store leaf files: ``<agentDir>/*/*/*`` (depth-2 dirs).

    Skips ``workspace-sessions`` (handled by the transcript parser). Mirrors
    kiro-recall's ``execLogFiles`` directory shape.
    """
    out: list[Path] = []
    try:
        level1 = list(agent_dir.iterdir())
    except OSError:
        return out
    for l1 in level1:
        if l1.name == "workspace-sessions":
            continue
        if not l1.is_dir():
            continue
        try:
            level2 = list(l1.iterdir())
        except OSError:
            continue
        for l2 in level2:
            if not l2.is_dir():
                continue
            try:
                leaves = list(l2.iterdir())
            except OSError:
                continue
            for leaf in leaves:
                if leaf.is_file():
                    out.append(leaf)
    return out


def _extract_exec_text(obj: object) -> Optional[tuple[str, str]]:
    """Return ``(executionId, spliced_text)`` from one parsed exec-log object.

    ``spliced_text`` concatenates reasoning blocks then say blocks (matching
    kiro-recall's ordering). Returns ``None`` if the object isn't a usable exec
    log or carries no searchable prose (e.g. a pure tool-call execution).
    """
    if not isinstance(obj, dict):
        return None
    execution_id = obj.get("executionId")
    if not isinstance(execution_id, str) or not execution_id:
        return None
    actions = obj.get("actions")
    if not isinstance(actions, list):
        return None
    says: list[str] = []
    reasons: list[str] = []
    for action in actions:
        if not isinstance(action, dict):
            continue
        output = action.get("output")
        if not isinstance(output, dict):
            continue
        message = output.get("message")
        if not isinstance(message, str) or not message.strip():
            continue
        action_type = action.get("actionType")
        if action_type == "say":
            says.append(message)
        elif action_type == "reasoning":
            reasons.append(message)
    parts: list[str] = []
    if reasons:
        parts.append("\n\n".join(reasons))
    if says:
        parts.append("\n\n".join(says))
    joined = "\n\n".join(parts).strip()
    if not joined:
        return None
    return execution_id, joined


def build_exec_map(agent_dir: Path) -> dict[str, str]:
    """Build ``{ executionId: assistant_text }`` from the exec store.

    Best-effort: unreadable / non-JSON / oversized files are skipped silently.
    Unlike kiro-recall, this does not persist an incremental manifest — it is
    rebuilt per process (cached in-memory). On very large exec stores the first
    build can be slow; that cost is paid once per ``mempalace`` invocation.
    """
    exec_map: dict[str, str] = {}
    for file in _exec_log_files(agent_dir):
        try:
            if file.stat().st_size > _MAX_EXEC_FILE_BYTES:
                continue
            raw = file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        extracted = _extract_exec_text(obj)
        if extracted is not None:
            exec_map[extracted[0]] = extracted[1]
    return exec_map


def _agent_dir_for(filepath: str) -> Optional[Path]:
    """Return the ``kiro.kiroagent`` dir for a session file path, or None.

    Only returns a directory when the path is a Kiro workspace-session
    transcript (i.e. it sits under ``kiro.kiroagent/workspace-sessions/...``),
    so arbitrary JSON files are never treated as Kiro sessions.
    """
    try:
        parts = Path(filepath).resolve().parts
    except (OSError, RuntimeError):
        return None
    if "workspace-sessions" not in parts:
        return None
    for i, segment in enumerate(parts):
        if segment == _AGENT_DIR:
            return Path(*parts[: i + 1])
    return None


def exec_map_for_session(filepath: str) -> Optional[dict[str, str]]:
    """Return the cached exec map for the Kiro session at ``filepath``, or None.

    ``None`` means "not a Kiro session path" — the caller should fall back to
    plain transcript parsing. An empty dict means "Kiro session, but no exec
    store found" — splicing is a no-op and stubs remain as-is.
    """
    agent_dir = _agent_dir_for(filepath)
    if agent_dir is None:
        return None
    key = str(agent_dir)
    if key not in _EXEC_MAP_CACHE:
        _EXEC_MAP_CACHE[key] = build_exec_map(agent_dir)
    return _EXEC_MAP_CACHE[key]
