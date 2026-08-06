"""Dream: non-destructive memory consolidation.

A *dream* reorganizes an accumulated palace the way sleep consolidates memory.
It works entirely on a COPY of the palace: it merges near-identical drawers via
the existing deduplicator, flags knowledge-graph contradictions, and (opt-in)
retires the older side of each contradiction. The original palace is never
touched — the caller reviews the candidate, then adopts or discards it.

This mirrors the immutable-input / review-then-adopt contract of managed-agent
"dreams" while honoring MemPalace's own rule: incremental only, never destroy.

Run a dream when no session is actively writing the palace (e.g. on a schedule),
so the filesystem copy is consistent.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
from datetime import datetime

from .backends.chroma import ChromaBackend
from .dedup import DEFAULT_THRESHOLD, dedup_palace
from .knowledge_graph import KnowledgeGraph

COLLECTION_NAME = "mempalace_drawers"


def _kg_path(palace_path: str) -> str:
    """The knowledge graph lives inside the palace directory."""
    return os.path.join(palace_path, "knowledge_graph.sqlite3")


def _drawer_count(palace_path: str) -> int:
    return ChromaBackend().get_collection(palace_path, COLLECTION_NAME).count()


def _cow_copytree(src: str, dst: str) -> None:
    """Clone the palace copy-on-write when the filesystem supports it, else fall
    back to a deep copy.

    A dream only removes a handful of drawers from the candidate, so on a
    copy-on-write filesystem the clone shares blocks with the original and costs
    almost no extra time or disk regardless of palace size — only the blocks
    dedup rewrites diverge, and they diverge on the candidate, never the source.
    Uses APFS ``clonefile`` via ``cp -c`` on macOS and reflink via
    ``cp --reflink=auto`` on Linux (both silently degrade to a normal copy when
    the volume can't clone); ``shutil.copytree`` is the portable last resort.
    """
    import platform
    import subprocess

    if platform.system() == "Darwin":
        cmd = ["cp", "-cR", src, dst]
    else:
        cmd = ["cp", "-a", "--reflink=auto", src, dst]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        # cp missing, unsupported flag, or clone refused -> portable deep copy.
        if os.path.exists(dst):
            shutil.rmtree(dst, ignore_errors=True)
        shutil.copytree(src, dst)


def detect_kg_conflicts(kg_db_path: str) -> list[dict]:
    """Active (subject, predicate) pairs that point at more than one object.

    These are *candidate* contradictions. With no functional-vs-multivalued
    predicate model, some are genuine supersessions ("lives_in") and some are
    legitimately multi-valued ("knows"), so we surface them for review rather
    than guess. Each entry: {subject, predicate, objects: [(name, valid_from)]}.
    """
    if not os.path.exists(kg_db_path):
        return []
    conn = sqlite3.connect(kg_db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT es.name AS subject, t.predicate AS predicate,
                   eo.name AS object, t.valid_from AS valid_from
            FROM triples t
            JOIN entities es ON es.id = t.subject
            JOIN entities eo ON eo.id = t.object
            WHERE t.valid_to IS NULL
            ORDER BY es.name, t.predicate, t.valid_from
            """
        ).fetchall()
    finally:
        conn.close()

    grouped: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for r in rows:
        grouped.setdefault((r["subject"], r["predicate"]), []).append(
            (r["object"], r["valid_from"])
        )
    conflicts = []
    for (subject, predicate), objs in grouped.items():
        if len({o for o, _ in objs}) > 1:
            conflicts.append({"subject": subject, "predicate": predicate, "objects": objs})
    return conflicts


def _retire_older_conflicts(kg_db_path: str, conflicts: list[dict]) -> int:
    """Opt-in. Keep the newest-``valid_from`` object per conflict, invalidate the
    rest. Operates on the candidate KG only. Returns the count of facts retired.
    A ``None`` valid_from sorts oldest so a dated fact always supersedes it.
    """
    kg = KnowledgeGraph(db_path=kg_db_path)
    retired = 0
    for c in conflicts:
        objs = sorted(c["objects"], key=lambda o: (o[1] is not None, o[1] or ""))
        keeper_name, keeper_from = objs[-1]
        for obj_name, _ in objs[:-1]:
            if obj_name == keeper_name:
                continue
            kg.invalidate(c["subject"], c["predicate"], obj_name, ended=keeper_from)
            retired += 1
    return retired


def dream(
    palace_path: str,
    wing: str | None = None,
    threshold: float | None = None,
    candidate_path: str | None = None,
    retire_conflicts: bool = False,
) -> dict:
    """Consolidate a COPY of the palace. Returns a report dict; never mutates the
    input palace.
    """
    if threshold is None:
        threshold = DEFAULT_THRESHOLD
    palace_path = os.path.abspath(os.path.expanduser(palace_path))
    if not os.path.isdir(palace_path):
        raise FileNotFoundError(f"palace not found: {palace_path}")

    if candidate_path is None:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        candidate_path = f"{palace_path.rstrip('/')}.dream-{wing or 'palace'}-{stamp}"
    candidate_path = os.path.abspath(os.path.expanduser(candidate_path))
    if os.path.exists(candidate_path):
        raise FileExistsError(f"candidate path already exists: {candidate_path}")

    # Non-destructive: consolidate the copy, leave the original untouched.
    # Copy-on-write where the filesystem allows, so the candidate is cheap even
    # for a large palace (see _cow_copytree).
    _cow_copytree(palace_path, candidate_path)

    before = _drawer_count(candidate_path)
    dedup_palace(palace_path=candidate_path, threshold=threshold, dry_run=False, wing=wing)
    after = _drawer_count(candidate_path)

    conflicts = detect_kg_conflicts(_kg_path(candidate_path))
    retired = (
        _retire_older_conflicts(_kg_path(candidate_path), conflicts) if retire_conflicts else 0
    )

    return {
        "palace": palace_path,
        "candidate": candidate_path,
        "wing": wing,
        "drawers_before": before,
        "drawers_after": after,
        "drawers_merged": before - after,
        "kg_conflicts": conflicts,
        "kg_facts_retired": retired,
    }
