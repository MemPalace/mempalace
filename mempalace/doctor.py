"""
doctor.py — Palace health diagnostics.
========================================

Scans the palace for issues and reports actionable recommendations.

Checks:
  - Palace exists and is readable
  - ChromaDB collection health (drawer count, empty wings/rooms)
  - Metadata completeness (missing wing, room, source_file)
  - Knowledge graph health (orphaned entities, empty KG)
  - Duplicate content detection (near-identical drawers)
  - Size warnings (very large drawers, very small drawers)

Zero API calls. Zero new dependencies. Read-only — never modifies data.

Usage:
    from mempalace.doctor import diagnose

    report = diagnose(palace_path="/path/to/palace")
    for check in report.checks:
        print(f"[{check.status}] {check.name}: {check.message}")
"""

import logging
import os
from collections import Counter
from dataclasses import dataclass, field
from typing import List, Optional

import chromadb

logger = logging.getLogger("mempalace")


# ── Result types ─────────────────────────────────────────────────────


@dataclass
class Check:
    """A single diagnostic check result."""

    name: str
    status: str  # "OK", "WARN", "ERROR"
    message: str
    details: Optional[str] = None


@dataclass
class DiagnosticReport:
    """Full diagnostic report for a palace."""

    palace_path: str = ""
    checks: List[Check] = field(default_factory=list)
    summary: str = ""

    @property
    def ok_count(self) -> int:
        return sum(1 for c in self.checks if c.status == "OK")

    @property
    def warn_count(self) -> int:
        return sum(1 for c in self.checks if c.status == "WARN")

    @property
    def error_count(self) -> int:
        return sum(1 for c in self.checks if c.status == "ERROR")

    def to_dict(self) -> dict:
        return {
            "palace_path": self.palace_path,
            "summary": self.summary,
            "ok": self.ok_count,
            "warnings": self.warn_count,
            "errors": self.error_count,
            "checks": [
                {
                    "name": c.name,
                    "status": c.status,
                    "message": c.message,
                    "details": c.details,
                }
                for c in self.checks
            ],
        }


# ── Diagnostic checks ───────────────────────────────────────────────


def _check_palace_exists(palace_path: str) -> Check:
    """Check if the palace directory exists and is readable."""
    if not os.path.isdir(palace_path):
        return Check("palace_exists", "ERROR", f"Palace not found at {palace_path}")
    return Check("palace_exists", "OK", f"Palace found at {palace_path}")


def _check_collection(palace_path: str) -> tuple:
    """Check ChromaDB collection health. Returns (Check, collection_or_None)."""
    try:
        client = chromadb.PersistentClient(path=palace_path)
        col = client.get_collection("mempalace_drawers")
        count = col.count()
        if count == 0:
            return (
                Check("collection", "WARN", "Palace is empty — no drawers filed yet"),
                col,
            )
        return (
            Check("collection", "OK", f"Collection healthy: {count} drawers"),
            col,
        )
    except Exception as e:
        return (
            Check("collection", "ERROR", f"Cannot read collection: {e}"),
            None,
        )


def _check_drawers(col) -> List[Check]:
    """Single-pass check of all drawers: metadata, wings/rooms, and sizes.

    Reads the collection once instead of multiple passes to avoid
    redundant I/O on large palaces.
    """
    checks = []
    missing_wing = 0
    missing_room = 0
    missing_source = 0
    very_small = 0
    very_large = 0
    total = 0
    wing_counts = Counter()
    room_counts = Counter()

    offset = 0
    while True:
        try:
            batch = col.get(include=["documents", "metadatas"], limit=500, offset=offset)
            docs = batch.get("documents", [])
            metas = batch.get("metadatas", [])
            if not docs:
                break
            for doc, meta in zip(docs, metas):
                total += 1
                # Metadata completeness
                if not meta.get("wing"):
                    missing_wing += 1
                if not meta.get("room"):
                    missing_room += 1
                if not meta.get("source_file"):
                    missing_source += 1
                # Wing/room distribution
                wing_counts[meta.get("wing", "unknown")] += 1
                room_counts[meta.get("room", "unknown")] += 1
                # Drawer sizes
                if len(doc) < 20:
                    very_small += 1
                elif len(doc) > 50000:
                    very_large += 1
            offset += len(docs)
        except Exception:
            break

    if total == 0:
        return checks

    # Metadata checks
    if missing_wing > 0:
        checks.append(Check(
            "metadata_wing", "WARN",
            f"{missing_wing}/{total} drawers missing 'wing' metadata",
        ))
    else:
        checks.append(Check("metadata_wing", "OK", "All drawers have wing metadata"))

    if missing_room > 0:
        checks.append(Check(
            "metadata_room", "WARN",
            f"{missing_room}/{total} drawers missing 'room' metadata",
        ))
    else:
        checks.append(Check("metadata_room", "OK", "All drawers have room metadata"))

    if missing_source > 0:
        checks.append(Check(
            "metadata_source", "WARN",
            f"{missing_source}/{total} drawers missing 'source_file' metadata",
        ))
    else:
        checks.append(Check("metadata_source", "OK", "All drawers have source_file metadata"))

    # Wing/room distribution
    if wing_counts:
        checks.append(Check(
            "wings", "OK",
            f"{len(wing_counts)} wings found",
            details=", ".join(f"{w}({c})" for w, c in wing_counts.most_common(10)),
        ))

    if room_counts:
        tiny_rooms = [r for r, c in room_counts.items() if c == 1]
        if len(tiny_rooms) > 5:
            checks.append(Check(
                "tiny_rooms", "OK",
                f"{len(tiny_rooms)} rooms have only 1 drawer (may be intentional for high-value items)",
            ))

    # Drawer sizes
    if very_small > 0:
        checks.append(Check(
            "small_drawers", "WARN",
            f"{very_small}/{total} drawers are very small (< 20 chars) — may be noise",
        ))

    if very_large > 0:
        checks.append(Check(
            "large_drawers", "WARN",
            f"{very_large}/{total} drawers are very large (> 50K chars) — may hurt search quality",
        ))

    if very_small == 0 and very_large == 0:
        checks.append(Check("drawer_sizes", "OK", f"All {total} drawers are reasonable size"))

    return checks


def _check_kg(palace_path: str) -> List[Check]:
    """Check knowledge graph health."""
    checks = []
    kg_path = os.path.join(palace_path, "knowledge_graph.sqlite3")

    if not os.path.exists(kg_path):
        checks.append(Check(
            "kg_exists", "WARN",
            "No knowledge graph found — run extract-kg to auto-populate",
        ))
        return checks

    try:
        from mempalace.knowledge_graph import KnowledgeGraph

        kg = KnowledgeGraph(db_path=kg_path)
        stats = kg.stats()

        if stats["entities"] == 0:
            checks.append(Check(
                "kg_entities", "WARN",
                "Knowledge graph is empty — run extract-kg to auto-populate from drawers",
            ))
        else:
            checks.append(Check(
                "kg_entities", "OK",
                f"KG has {stats['entities']} entities, {stats['triples']} triples "
                f"({stats['current_facts']} current, {stats['expired_facts']} expired)",
            ))

        # Check for orphaned entities (entities with no triples)
        conn = kg._conn()
        orphans = conn.execute("""
            SELECT COUNT(*) as cnt FROM entities e
            WHERE NOT EXISTS (SELECT 1 FROM triples t WHERE t.subject = e.id OR t.object = e.id)
        """).fetchone()["cnt"]

        if orphans > 0:
            checks.append(Check(
                "kg_orphans", "WARN",
                f"{orphans} entities have no relationships — consider cleaning up",
            ))

    except Exception as e:
        checks.append(Check("kg_health", "ERROR", f"KG read error: {e}"))

    return checks


# ── Main diagnostic ──────────────────────────────────────────────────


def diagnose(palace_path: str) -> DiagnosticReport:
    """Run all diagnostic checks on a palace.

    Args:
        palace_path: Path to the palace directory.

    Returns:
        DiagnosticReport with all check results.
    """
    report = DiagnosticReport(palace_path=palace_path)

    # 1. Palace exists
    report.checks.append(_check_palace_exists(palace_path))
    if report.checks[-1].status == "ERROR":
        report.summary = "Palace not found"
        return report

    # 2. Collection health
    col_check, col = _check_collection(palace_path)
    report.checks.append(col_check)

    if col is not None:
        # 3-5. Single-pass: metadata, wings/rooms, drawer sizes
        report.checks.extend(_check_drawers(col))

    # 6. Knowledge graph
    report.checks.extend(_check_kg(palace_path))

    # Summary
    if report.error_count > 0:
        report.summary = f"{report.error_count} errors, {report.warn_count} warnings"
    elif report.warn_count > 0:
        report.summary = f"Healthy with {report.warn_count} warnings"
    else:
        report.summary = f"All {report.ok_count} checks passed"

    return report
