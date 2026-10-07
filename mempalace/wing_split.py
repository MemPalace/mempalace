"""Split a machine-level transcript wing into one wing per source project.

``mempalace mine <convo-dir> --mode convos`` without ``--wing`` files every
session under one wing named after the export (``claude_conversations``,
``codex_conversations_windows``). On a real palace that wing is hundreds of
projects: the maintainers' own held 846 Claude Code project directories in
one wing, so no room set could mean anything and a wing-scoped search
returned everything. This module re-keys those drawers by the project the
transcript came from.

Two steps, like ``rooms``:

* **plan** — group the wing's drawers by *project key* (the encoded project
  directory in a Claude Code transcript path, or the ``cwd`` recorded in a
  Codex rollout when the file is readable), resolve each key to a target
  wing (an existing wing whose name the key ends with, else a name derived
  from the key) and write ``<palace>/wings/split-<wing>.json`` for review.
* **apply** — rewrite ``wing`` metadata for every drawer in the plan, in
  batches, under the palace lock. Content and room never change. Hallway
  records of the split wing are dropped because they are keyed by wing and
  the next mine rebuilds them per target wing.
"""

from __future__ import annotations

import json
import os
import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import Iterable, Optional

from .config import MempalaceConfig, normalize_wing_name, sanitize_name

SPLIT_SCHEMA_VERSION = 1
_PAGE_SIZE = 2000
_UPDATE_BATCH = 500

_CLAUDE_PROJECTS_RE = re.compile(r"[/\\]\.claude[/\\]projects[/\\]([^/\\]+)[/\\]")
_CODEX_SESSIONS_RE = re.compile(r"[/\\]\.codex[/\\]sessions[/\\]")
# Path segments after which the project name starts when no existing wing
# matches: ``-Users-me-dev-acme-app`` → ``acme-app``.
_ROOT_MARKERS = ("dev", "projects", "claude-projects", "src", "repos", "code", "work")


# ── project keys ─────────────────────────────────────────────────────────────


def _codex_cwd(path: str) -> Optional[str]:
    """``cwd`` from a Codex rollout's first lines, or ``None`` when unreadable."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for _ in range(3):
                line = f.readline()
                if not line:
                    break
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                payload = obj.get("payload") if isinstance(obj, dict) else None
                for holder in (obj, payload):
                    if isinstance(holder, dict) and isinstance(holder.get("cwd"), str):
                        return holder["cwd"]
    except OSError:
        return None
    return None


def project_key(source_file: Optional[str]) -> Optional[str]:
    """The project identifier a transcript path carries, or ``None``.

    Claude Code encodes the working directory in the path
    (``.claude/projects/-Users-me-dev-mempalace/<session>.jsonl``,
    subagent transcripts nest below it); the encoded segment is the key.
    Codex rollouts carry no project in the path; the key is the basename of
    the ``cwd`` in the file when the file is on this machine.
    """
    if not source_file:
        return None
    match = _CLAUDE_PROJECTS_RE.search(source_file)
    if match:
        return match.group(1)
    if _CODEX_SESSIONS_RE.search(source_file):
        cwd = _codex_cwd(source_file)
        if cwd:
            base = cwd.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
            return base or None
    return None


def _wing_variants(wing: str) -> set[str]:
    norm = normalize_wing_name(wing)
    return {norm, norm.replace("_", "-")}


def resolve_target(key: str, existing_wings: Iterable[str]) -> tuple[str, str]:
    """``(target_wing, how)`` for a project key.

    ``how`` is ``"existing"`` when the key ends with an existing wing's name
    at a segment boundary (longest match wins: ``p--acme-portal`` →
    ``portal``, ``P--org-invoices`` → ``invoices``), otherwise ``"derived"``
    with a name taken from the key after the last root marker
    (``-Users-me-dev-acme-app`` → ``acme_app``) or, without one,
    after the leading drive and user segments.
    """
    lowered = key.lower().strip("-")
    # A Claude Code worktree (``<project>--claude-worktrees-<agent>``) belongs
    # to the project it was branched from.
    marker = lowered.find("-claude-worktrees-")
    if marker > 0:
        return resolve_target(key[:marker].rstrip("-"), existing_wings)
    # A Codex worktree (``-Users-x--codex-worktrees-<id>-<project>``) names
    # the project after the worktree id.
    codex = re.search(r"-codex-worktrees-[0-9a-f]+-(.+)$", lowered)
    if codex:
        return resolve_target(codex.group(1), existing_wings)
    best: Optional[str] = None
    for wing in existing_wings:
        for variant in _wing_variants(wing):
            if not variant:
                continue
            if lowered == variant or lowered.endswith("-" + variant):
                if best is None or len(variant) > len(normalize_wing_name(best)):
                    best = wing
    if best is not None:
        return best, "existing"

    # Drop a Windows drive prefix (``p--``, ``C--``) and split on the encoder's
    # separator; a real hyphen inside a directory name is indistinguishable
    # from a separator, so the tail is kept whole after the last root marker.
    body = re.sub(r"^[a-zA-Z]--", "", key.strip("-"))
    segments = [s for s in body.split("-") if s]
    tail = segments
    for i in range(len(segments) - 1, -1, -1):
        if segments[i].lower() in _ROOT_MARKERS:
            tail = segments[i + 1 :]
            break
    else:
        if len(segments) > 2 and segments[0].lower() in {"users", "home"}:
            tail = segments[2:]
    name = normalize_wing_name("_".join(tail)) or normalize_wing_name(body)
    return name, "derived"


# ── plan ─────────────────────────────────────────────────────────────────────


def _iter_wing_rows(col, wing: str):
    offset = 0
    while True:
        batch = col.get(
            where={"wing": wing}, limit=_PAGE_SIZE, offset=offset, include=["metadatas"]
        )
        ids = list(batch.get("ids") or [])
        if not ids:
            return
        metas = batch.get("metadatas") or [None] * len(ids)
        for row_id, meta in zip(ids, metas):
            meta = meta or {}
            if meta.get("wing") == wing:
                yield row_id, meta
        offset += len(ids)
        if len(ids) < _PAGE_SIZE:
            return


def plan_split(col, wing: str, existing_wings: Iterable[str]) -> dict:
    """Group the wing's drawers by project key and resolve targets.

    Returns ``{"wing", "planned_at", "projects": {key: {"target", "how",
    "drawers"}}, "unresolved": n}``; drawers whose path carries no project
    stay where they are and are only counted.
    """
    existing = [w for w in existing_wings if w != wing]
    counts: dict[str, int] = defaultdict(int)
    unresolved = 0
    for _, meta in _iter_wing_rows(col, wing):
        key = project_key(meta.get("source_file"))
        if key is None:
            unresolved += 1
            continue
        counts[key] += 1
    projects = {}
    for key in sorted(counts, key=lambda k: -counts[k]):
        target, how = resolve_target(key, existing)
        projects[key] = {"target": target, "how": how, "drawers": counts[key]}
    return {
        "schema_version": SPLIT_SCHEMA_VERSION,
        "wing": wing,
        "planned_at": datetime.now(timezone.utc).isoformat(),
        "projects": projects,
        "unresolved": unresolved,
    }


def split_plan_path(config: MempalaceConfig, wing: str) -> str:
    return os.path.join(config.palace_path, "wings", f"split-{sanitize_name(wing, 'wing')}.json")


def save_split_plan(config: MempalaceConfig, plan: dict) -> str:
    path = split_plan_path(config, plan["wing"])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(plan, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)
    return path


def load_split_plan(config: MempalaceConfig, wing: str) -> dict:
    with open(split_plan_path(config, wing), encoding="utf-8") as f:
        plan = json.load(f)
    if not isinstance(plan, dict):
        raise ValueError("split plan is not an object")
    # ``apply_split`` re-keys the wing the plan names, so a copied or edited
    # file naming another wing would move the wrong drawers.
    if str(plan.get("wing") or "") != wing:
        raise ValueError(
            f"plan is for wing {plan.get('wing')!r}, not {wing!r}; re-run without --yes"
        )
    projects = plan.get("projects")
    if not isinstance(projects, dict) or not projects:
        raise ValueError("split plan has no projects")
    for key, entry in projects.items():
        target = entry.get("target") if isinstance(entry, dict) else None
        if not target:
            raise ValueError(f"project {key!r} has no target wing")
        entry["target"] = sanitize_name(str(target), "wing")
    return plan


def plan_targets(plan: dict) -> dict[str, int]:
    """``{target_wing: drawers}`` the plan would produce."""
    out: dict[str, int] = defaultdict(int)
    for entry in plan["projects"].values():
        out[entry["target"]] += int(entry.get("drawers") or 0)
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


# ── apply ────────────────────────────────────────────────────────────────────


def _rekey_collection(col, wing: str, targets: dict[str, str], stamp: str, progress=None):
    """Move every record of ``wing`` whose source file maps to a target.

    Returns ``(moved, per_target, skipped)``. Ids are materialized first:
    rewriting ``wing`` while paging a wing-scoped query would shift the pages
    under the cursor.
    """
    per_target: dict[str, int] = defaultdict(int)
    moved = skipped = 0
    batch_ids: list[str] = []
    batch_metas: list[dict] = []

    def flush():
        nonlocal moved
        if not batch_ids:
            return
        col.update(ids=list(batch_ids), metadatas=list(batch_metas))
        moved += len(batch_ids)
        batch_ids.clear()
        batch_metas.clear()
        if progress:
            progress(moved)

    rows = [(row_id, meta.get("source_file")) for row_id, meta in _iter_wing_rows(col, wing)]
    for row_id, source_file in rows:
        target = targets.get(project_key(source_file) or "")
        if not target or target == wing:
            skipped += 1
            continue
        batch_ids.append(row_id)
        batch_metas.append({"wing": target, "last_modified": stamp})
        per_target[target] += 1
        if len(batch_ids) >= _UPDATE_BATCH:
            flush()
    flush()
    return moved, per_target, skipped


def split_pending_path(config: MempalaceConfig, wing: str) -> str:
    """Marker that a split of ``wing`` started and has not finished every phase."""
    return os.path.join(config.palace_path, "wings", f"split-{sanitize_name(wing, 'wing')}.pending")


def apply_split(
    col,
    plan: dict,
    config: Optional[MempalaceConfig] = None,
    progress=None,
    closets_col=None,
    resuming: bool = False,
) -> dict:
    """Rewrite ``wing`` for every drawer whose project key is in the plan.

    Returns ``{"moved", "per_target", "skipped", "closets_moved",
    "hallways_dropped"}``. Drawers whose key is missing from the plan (or
    resolves back to the source wing) are skipped. ``closets_col`` — the AAAK
    index layer, keyed by the same ``source_file`` and filtered by the same
    ``wing`` at search time — is re-keyed by the same rule, so a wing-scoped
    search keeps its index boosts after the split. Hallway records for the
    source wing are dropped since they are keyed by wing; rebuild them with
    ``mempalace hallways --rebuild`` or the next mine.

    Interruption: rows move in batches, each batch one backend write, and a
    row is only ever wholly in its old wing or wholly in its new one. The
    loop reads the rows *currently* in the source wing, so running the same
    plan again after a crash or Ctrl-C moves exactly the remainder (drawers,
    then closets, then the hallway drop). ``resuming`` says a previous run
    started and did not finish: the hallway drop then runs even if no drawer
    is left to move, because the crash may have come after the drawer phase.
    A completed split re-run without ``resuming`` changes nothing. Nothing
    is deleted and no content is rewritten at any point.
    """
    wing = plan["wing"]
    targets = {key: entry["target"] for key, entry in plan["projects"].items()}
    stamp = datetime.now(timezone.utc).isoformat()
    moved, per_target, skipped = _rekey_collection(col, wing, targets, stamp, progress)
    closets_moved = 0
    if closets_col is not None:
        closets_moved, _, _ = _rekey_collection(closets_col, wing, targets, stamp)

    hallways_dropped = 0
    if moved or resuming:
        from .hallways import _hallway_file_lock, _load_hallways, _save_hallways

        # Under the hallway-file lock: the palace lock this command holds does
        # not serialize with a concurrent `hallways --rebuild` or
        # `--prune-spellings`, which take only the per-sidecar lock.
        with _hallway_file_lock(config):
            records = _load_hallways(config)
            kept = [h for h in records if not (isinstance(h, dict) and h.get("wing") == wing)]
            hallways_dropped = len(records) - len(kept)
            if hallways_dropped:
                _save_hallways(kept, config)

    return {
        "moved": moved,
        "per_target": dict(sorted(per_target.items(), key=lambda kv: -kv[1])),
        "skipped": skipped,
        "closets_moved": closets_moved,
        "hallways_dropped": hallways_dropped,
    }
