"""Read-only inventory and reorganization planning for Chroma palaces.

This module deliberately reads Chroma's SQLite source of truth directly. It
does not construct a ``PersistentClient`` and therefore never opens or repairs
the HNSW index while producing a migration plan.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

from .config import sqlite_read_uri


DRAWERS_COLLECTION = "mempalace_drawers"
_PIN_KEYS = ("pinned", "memory_pinned", "is_pinned")


@dataclass(frozen=True)
class InventoryRecord:
    """One drawer reconstructed from SQLite plus its inferred source scope."""

    drawer_id: str
    content: str
    metadata: dict[str, Any]
    origin: str
    relative_identity: str | None
    source_path: str | None


@dataclass(frozen=True)
class MigrationAction:
    """A non-mutating recommendation for one drawer."""

    drawer_id: str
    action: str
    destination_wing: str
    reason: str
    content_sha256: str
    metadata: dict[str, Any]


def exact_hash(content: str) -> str:
    """Hash the exact drawer text, including whitespace and line endings."""

    return hashlib.sha256(content.encode("utf-8", errors="surrogatepass")).hexdigest()


def _expanded_path(value: os.PathLike[str] | str) -> Path:
    return Path(os.path.abspath(os.path.expanduser(os.fspath(value))))


def _normalized_roots(values: Iterable[os.PathLike[str] | str]) -> tuple[Path, ...]:
    return tuple(_expanded_path(value) for value in values)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _metadata_source_path(metadata: dict[str, Any]) -> Path | None:
    source = metadata.get("source_file") or metadata.get("source_path")
    source_root = metadata.get("source_root")
    if source:
        source_path = Path(os.path.expanduser(str(source)))
        if not source_path.is_absolute() and source_root:
            source_path = Path(os.path.expanduser(str(source_root))) / source_path
        if source_path.is_absolute():
            return _expanded_path(source_path)
    if source_root:
        root_path = Path(os.path.expanduser(str(source_root)))
        if root_path.is_absolute():
            return _expanded_path(root_path)
    return None


def _identity_from_metadata(metadata: dict[str, Any]) -> str | None:
    identity = metadata.get("source_identity")
    if not identity:
        return None
    normalized = str(identity).replace("\\", "/")
    prefix, separator, suffix = normalized.partition(":")
    if separator and prefix in {
        "code",
        "documentation",
        "session",
        "worktree-artifact",
        "curated",
    }:
        normalized = suffix
    normalized = normalized.lstrip("./")
    return normalized or None


def _classify_source(
    metadata: dict[str, Any],
    *,
    canonical_root: Path,
    worktree_roots: tuple[Path, ...],
    session_roots: tuple[Path, ...],
) -> tuple[str, Path | None, str | None]:
    source_path = _metadata_source_path(metadata)
    relative_identity = _identity_from_metadata(metadata)

    if source_path is not None:
        for root in worktree_roots:
            if _is_within(source_path, root):
                relative = source_path.relative_to(root).as_posix()
                return "worktree", source_path, relative_identity or relative or None
        for root in session_roots:
            if _is_within(source_path, root):
                relative = source_path.relative_to(root).as_posix()
                return "session", source_path, relative_identity or relative or None
        if _is_within(source_path, canonical_root):
            relative = source_path.relative_to(canonical_root).as_posix()
            return "canonical", source_path, relative_identity or relative or None

    source_kind = str(metadata.get("source_kind") or "")
    canonicality = str(metadata.get("source_canonicality") or "")
    if source_kind == "session":
        return "session", source_path, relative_identity
    if source_kind == "worktree-artifact" or canonicality == "linked-worktree":
        return "worktree", source_path, relative_identity
    if source_kind in {"code", "documentation"} and canonicality == "canonical":
        return "canonical", source_path, relative_identity
    if source_kind == "curated" or metadata.get("wing") == "se":
        return "curated", source_path, relative_identity
    return "unclassified", source_path, relative_identity


def _metadata_value(row: sqlite3.Row, value_columns: tuple[str, ...]) -> Any:
    for column in value_columns:
        value = row[column]
        if value is not None:
            if column == "bool_value":
                return bool(value)
            return value
    return None


def inventory_palace(
    palace_path: os.PathLike[str] | str,
    canonical_root: os.PathLike[str] | str,
    worktree_roots: Iterable[os.PathLike[str] | str],
    session_roots: Iterable[os.PathLike[str] | str],
    *,
    collection_name: str = DRAWERS_COLLECTION,
) -> list[InventoryRecord]:
    """Reconstruct drawers from Chroma SQLite in stable ID order.

    The database is opened with ``mode=ro`` and this function never imports or
    initializes ChromaDB.
    """

    palace = _expanded_path(palace_path)
    sqlite_path = palace / "chroma.sqlite3"
    if not sqlite_path.is_file():
        raise FileNotFoundError(f"Chroma SQLite database not found: {sqlite_path}")

    canonical = _expanded_path(canonical_root)
    worktrees = _normalized_roots(worktree_roots)
    sessions = _normalized_roots(session_roots)

    with sqlite3.connect(sqlite_read_uri(str(sqlite_path)), uri=True) as conn:
        conn.row_factory = sqlite3.Row
        columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(embedding_metadata)").fetchall()
        }
        value_columns = tuple(
            column
            for column in ("string_value", "int_value", "float_value", "bool_value")
            if column in columns
        )
        if not value_columns:
            raise RuntimeError("Chroma embedding_metadata has no scalar value columns")

        rows = conn.execute(
            f"""
            SELECT e.id, e.embedding_id, em.key, {", ".join(f"em.{c}" for c in value_columns)}
            FROM embeddings e
            JOIN segments s ON e.segment_id = s.id
            JOIN collections c ON s.collection = c.id
            LEFT JOIN embedding_metadata em ON em.id = e.id
            WHERE c.name = ?
            ORDER BY e.embedding_id, em.key
            """,
            (collection_name,),
        ).fetchall()

    reconstructed: dict[int, dict[str, Any]] = {}
    for row in rows:
        internal_id = int(row["id"])
        item = reconstructed.setdefault(
            internal_id,
            {
                "drawer_id": str(row["embedding_id"]),
                "content": "",
                "metadata": {},
            },
        )
        key = row["key"]
        if key is None:
            continue
        value = _metadata_value(row, value_columns)
        if key == "chroma:document":
            item["content"] = "" if value is None else str(value)
        else:
            item["metadata"][str(key)] = value

    records: list[InventoryRecord] = []
    for item in reconstructed.values():
        metadata = item["metadata"]
        origin, source_path, relative_identity = _classify_source(
            metadata,
            canonical_root=canonical,
            worktree_roots=worktrees,
            session_roots=sessions,
        )
        records.append(
            InventoryRecord(
                drawer_id=item["drawer_id"],
                content=item["content"],
                metadata=metadata,
                origin=origin,
                relative_identity=relative_identity,
                source_path=str(source_path) if source_path is not None else None,
            )
        )
    return sorted(records, key=lambda record: record.drawer_id)


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "pinned"}
    return bool(value)


def _session_tier(metadata: dict[str, Any], *, hot_days: int, now: date) -> str:
    if any(_truthy(metadata.get(key)) for key in _PIN_KEYS):
        return "hot"
    raw_date = metadata.get("authored_at") or metadata.get("filed_at")
    if not raw_date:
        return "hot"
    try:
        authored = date.fromisoformat(str(raw_date)[:10])
    except ValueError:
        return "hot"
    return "cold" if (now - authored).days > hot_days else "hot"


def _as_date(value: date | datetime | None) -> date:
    if value is None:
        return datetime.now().date()
    if isinstance(value, datetime):
        return value.date()
    return value


def plan_actions(
    records: Iterable[InventoryRecord],
    hot_days: int = 90,
    now: date | datetime | None = None,
) -> list[MigrationAction]:
    """Classify records without changing the palace."""

    if hot_days < 0:
        raise ValueError("hot_days must be non-negative")
    today = _as_date(now)
    ordered = sorted(records, key=lambda record: record.drawer_id)
    canonical_matches: dict[tuple[str, str], list[InventoryRecord]] = {}
    for record in ordered:
        if record.origin == "canonical" and record.relative_identity:
            key = (record.relative_identity, exact_hash(record.content))
            canonical_matches.setdefault(key, []).append(record)

    actions: list[MigrationAction] = []
    for record in ordered:
        metadata = dict(record.metadata)
        content_sha256 = exact_hash(record.content)
        if record.origin == "canonical":
            metadata.update(
                {
                    "memory_tier": "hot",
                    "source_canonicality": "canonical",
                    "source_kind": metadata.get("source_kind") or "code",
                }
            )
            action = "reclassify_canonical"
            destination = "se-code"
            reason = "source is inside the canonical repository"
        elif record.origin == "session":
            metadata.update(
                {
                    "memory_tier": _session_tier(metadata, hot_days=hot_days, now=today),
                    "source_kind": "session",
                }
            )
            action = "retain_session"
            destination = "se-sessions"
            reason = "session memory is retained and tiered by age"
        elif record.origin == "worktree":
            metadata.update(
                {
                    "memory_tier": "cold",
                    "source_kind": "worktree-artifact",
                    "source_canonicality": "linked-worktree",
                }
            )
            key = (record.relative_identity or "", content_sha256)
            matches = canonical_matches.get(key, []) if record.relative_identity else []
            if len(matches) == 1:
                action = "duplicate_candidate"
                reason = "one canonical drawer has the same relative identity and content"
            elif matches:
                action = "preserve_uncertain"
                reason = "multiple canonical drawers match; deletion identity is ambiguous"
            else:
                action = "preserve_unique"
                reason = "no canonical drawer has the same relative identity and content"
            destination = "se-sessions"
        elif record.origin == "curated":
            metadata.update(
                {
                    "memory_tier": metadata.get("memory_tier") or "hot",
                    "source_kind": metadata.get("source_kind") or "curated",
                }
            )
            action = "retain_curated"
            destination = "se"
            reason = "curated project memory stays in the decision wing"
        else:
            metadata["memory_tier"] = metadata.get("memory_tier") or "hot"
            action = "preserve_unclassified"
            destination = str(metadata.get("wing") or "se")
            reason = "source scope is unknown; preserve without guessing"

        actions.append(
            MigrationAction(
                drawer_id=record.drawer_id,
                action=action,
                destination_wing=destination,
                reason=reason,
                content_sha256=content_sha256,
                metadata=metadata,
            )
        )
    return actions
