"""Read-only inventory and reorganization planning for Chroma palaces.

This module deliberately reads Chroma's SQLite source of truth directly. It
does not construct a ``PersistentClient`` and therefore never opens or repairs
the HNSW index while producing a migration plan.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Iterable

from .config import sqlite_read_uri


DRAWERS_COLLECTION = "mempalace_drawers"
_PIN_KEYS = ("pinned", "memory_pinned", "is_pinned")
_SEMANTIC_JSON_COLUMNS = {
    ("collections", "config_json_str"),
    ("collections", "schema_str"),
    ("embeddings_queue_config", "config_json_str"),
}
_SEMANTIC_EPHEMERAL_ROW_TABLES = {"acquire_write"}


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


@dataclass(frozen=True)
class DuplicateEvidence:
    """Exact identity evidence linking a worktree drawer to a canonical one."""

    worktree_drawer_id: str
    canonical_drawer_id: str
    relative_identity: str
    chunk_index: int
    content_sha256: str
    source_sha256: str | None


def exact_hash(content: str) -> str:
    """Hash the exact drawer text, including whitespace and line endings."""

    return hashlib.sha256(content.encode("utf-8", errors="surrogatepass")).hexdigest()


def _expanded_path(value: os.PathLike[str] | str) -> Path:
    return Path(os.path.realpath(os.path.abspath(os.path.expanduser(os.fspath(value)))))


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
    return _safe_relative_identity(normalized)


def _safe_relative_identity(identity: str) -> str | None:
    normalized = str(identity).replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    path = PurePosixPath(normalized)
    windows_path = PureWindowsPath(normalized)
    if (
        not normalized
        or normalized == "."
        or path.is_absolute()
        or windows_path.drive
        or windows_path.is_absolute()
        or ".." in path.parts
    ):
        return None
    return path.as_posix()


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
    if source_kind == "curated":
        return "curated", source_path, relative_identity
    provenance_keys = {
        "source_file",
        "source_path",
        "source_root",
        "source_identity",
        "source_kind",
        "source_canonicality",
        "source_revision",
        "source_sha256",
    }
    has_provenance = any(metadata.get(key) not in (None, "") for key in provenance_keys)
    if metadata.get("wing") == "se" and not has_provenance:
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


def _chunk_index(record: InventoryRecord) -> int | None:
    value = record.metadata.get("chunk_index")
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def prove_worktree_duplicate(
    worktree: InventoryRecord,
    canonical: InventoryRecord,
) -> DuplicateEvidence | None:
    """Return strict duplicate evidence or ``None`` when proof is incomplete.

    Legacy pairs where neither drawer has a file-level source hash can still be
    proven by relative source identity, chunk coordinate, and an exact content
    hash. If only one side has a source hash, or both hashes disagree, the pair
    remains uncertain.
    """

    if worktree.origin != "worktree" or canonical.origin != "canonical":
        return None
    worktree_identity = (
        _safe_relative_identity(worktree.relative_identity) if worktree.relative_identity else None
    )
    canonical_identity = (
        _safe_relative_identity(canonical.relative_identity)
        if canonical.relative_identity
        else None
    )
    if not worktree_identity or not canonical_identity:
        return None
    if worktree_identity != canonical_identity:
        return None

    worktree_chunk = _chunk_index(worktree)
    canonical_chunk = _chunk_index(canonical)
    if worktree_chunk is None or canonical_chunk is None or worktree_chunk != canonical_chunk:
        return None

    worktree_content_hash = exact_hash(worktree.content)
    if worktree_content_hash != exact_hash(canonical.content):
        return None

    worktree_source_hash = worktree.metadata.get("source_sha256")
    canonical_source_hash = canonical.metadata.get("source_sha256")
    if bool(worktree_source_hash) != bool(canonical_source_hash):
        return None
    if worktree_source_hash and str(worktree_source_hash) != str(canonical_source_hash):
        return None

    return DuplicateEvidence(
        worktree_drawer_id=worktree.drawer_id,
        canonical_drawer_id=canonical.drawer_id,
        relative_identity=worktree_identity,
        chunk_index=worktree_chunk,
        content_sha256=worktree_content_hash,
        source_sha256=str(worktree_source_hash) if worktree_source_hash else None,
    )


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
                if prove_worktree_duplicate(record, matches[0]) is not None:
                    action = "duplicate_candidate"
                    reason = "one canonical drawer has strict exact-duplicate evidence"
                else:
                    action = "preserve_uncertain"
                    reason = "candidate identity is incomplete or source hashes are asymmetric"
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


def collect_duplicate_evidence(
    records: Iterable[InventoryRecord],
    actions: Iterable[MigrationAction] | None = None,
) -> list[DuplicateEvidence]:
    """Collect evidence only for unambiguous ``duplicate_candidate`` actions."""

    ordered = sorted(records, key=lambda record: record.drawer_id)
    candidate_ids = (
        {action.drawer_id for action in actions if action.action == "duplicate_candidate"}
        if actions is not None
        else {record.drawer_id for record in ordered if record.origin == "worktree"}
    )
    canonical_by_key: dict[tuple[str, str], list[InventoryRecord]] = {}
    for record in ordered:
        if record.origin != "canonical" or not record.relative_identity:
            continue
        key = (record.relative_identity, exact_hash(record.content))
        canonical_by_key.setdefault(key, []).append(record)

    evidence: list[DuplicateEvidence] = []
    for record in ordered:
        if record.drawer_id not in candidate_ids or record.origin != "worktree":
            continue
        key = (record.relative_identity or "", exact_hash(record.content))
        matches = canonical_by_key.get(key, []) if record.relative_identity else []
        if len(matches) != 1:
            continue
        proof = prove_worktree_duplicate(record, matches[0])
        if proof is not None:
            evidence.append(proof)
    return sorted(evidence, key=lambda item: item.worktree_drawer_id)


def sha256_file(path: os.PathLike[str] | str) -> str:
    """Hash a file without loading it into memory."""

    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _stable_file_snapshot(path: Path) -> dict[str, int | str]:
    before = path.stat()
    digest = sha256_file(path)
    after = path.stat()
    before_identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    after_identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if before_identity != after_identity:
        raise RuntimeError(f"file changed while hashing: {path}")
    return {
        "size": after.st_size,
        "mtime_ns": after.st_mtime_ns,
        "sha256": digest,
    }


def palace_snapshot(palace_path: os.PathLike[str] | str) -> dict[str, Any]:
    """Capture the read-only SQLite facts needed to audit a plan."""

    sqlite_path = _expanded_path(palace_path) / "chroma.sqlite3"
    snapshot: dict[str, Any] = _stable_file_snapshot(sqlite_path)
    wal_path = Path(f"{sqlite_path}-wal")
    try:
        wal_snapshot = _stable_file_snapshot(wal_path)
    except FileNotFoundError:
        snapshot.update(
            {
                "wal_present": False,
                "wal_size": None,
                "wal_mtime_ns": None,
                "wal_sha256": None,
            }
        )
    else:
        snapshot.update(
            {
                "wal_present": True,
                "wal_size": wal_snapshot["size"],
                "wal_mtime_ns": wal_snapshot["mtime_ns"],
                "wal_sha256": wal_snapshot["sha256"],
            }
        )
    return snapshot


def _semantic_sqlite_value(table: str, column: str, value: Any) -> Any:
    """Return a type-stable, JSON-serializable SQLite value."""
    if isinstance(value, bytes):
        return ["blob", value.hex()]
    if isinstance(value, float):
        return ["float", value.hex()]
    if isinstance(value, str) and (table, column) in _SEMANTIC_JSON_COLUMNS:
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            pass
        else:
            canonical = json.dumps(
                parsed,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            return ["json", canonical]
    if value is None:
        return ["null", None]
    return [type(value).__name__, value]


def palace_semantic_snapshot(palace_path: os.PathLike[str] | str) -> dict[str, Any]:
    """Hash SQLite schema and durable rows while normalizing Chroma bookkeeping.

    Chroma may rewrite collection JSON with different whitespace or key order
    when a collection is opened. That changes the physical database checksum
    without changing its meaning. This snapshot remains stable across that
    bookkeeping, but any schema, durable row, or non-equivalent JSON change
    changes the digest. Chroma's append-only ``acquire_write`` lock history is
    excluded because merely reopening a collection adds rows to it; its table
    schema remains covered by the schema-object hash.
    """
    sqlite_path = _expanded_path(palace_path) / "chroma.sqlite3"
    if not sqlite_path.is_file():
        raise FileNotFoundError(f"Chroma SQLite database not found: {sqlite_path}")

    digest = hashlib.sha256()
    table_count = 0
    schema_object_count = 0
    row_count = 0
    with sqlite3.connect(sqlite_read_uri(str(sqlite_path)), uri=True) as connection:
        schema_objects = connection.execute(
            """
            SELECT type, name, tbl_name, sql
            FROM sqlite_master
            WHERE type IN ('table', 'index', 'trigger', 'view')
              AND name NOT LIKE 'sqlite_%'
            ORDER BY type, name
            """
        ).fetchall()
        for object_type, object_name, table_name, schema_sql in schema_objects:
            descriptor = json.dumps(
                [
                    "schema",
                    str(object_type),
                    str(object_name),
                    str(table_name),
                    str(schema_sql or ""),
                ],
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8", errors="surrogatepass")
            digest.update(len(descriptor).to_bytes(8, "big"))
            digest.update(descriptor)
            schema_object_count += 1

        tables = [item for item in schema_objects if item[0] == "table"]
        for _object_type, table_name, _owning_table, _schema_sql in tables:
            table = str(table_name)
            quoted = table.replace('"', '""')
            cursor = connection.execute(f'SELECT * FROM "{quoted}"')
            columns = [str(item[0]) for item in (cursor.description or [])]
            descriptor = json.dumps(
                ["rows", table, columns],
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8", errors="surrogatepass")
            digest.update(len(descriptor).to_bytes(8, "big"))
            digest.update(descriptor)

            row_digests: list[bytes] = []
            if table not in _SEMANTIC_EPHEMERAL_ROW_TABLES:
                for row in cursor:
                    encoded = json.dumps(
                        [
                            _semantic_sqlite_value(table, column, value)
                            for column, value in zip(columns, row)
                        ],
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode("utf-8", errors="surrogatepass")
                    row_digests.append(hashlib.sha256(encoded).digest())
            row_digests.sort()
            digest.update(len(row_digests).to_bytes(8, "big"))
            for row_digest in row_digests:
                digest.update(row_digest)
            table_count += 1
            row_count += len(row_digests)

    return {
        "schema_object_count": schema_object_count,
        "table_count": table_count,
        "row_count": row_count,
        "sha256": digest.hexdigest(),
    }


def _manifest_counts(
    inventory: list[InventoryRecord],
    actions: list[MigrationAction],
    evidence: list[DuplicateEvidence],
) -> dict[str, Any]:
    origins = Counter(record.origin for record in inventory)
    action_types = Counter(action.action for action in actions)
    tiers = Counter(str(action.metadata.get("memory_tier") or "unspecified") for action in actions)
    return {
        "inventory_total": len(inventory),
        "inventory_by_origin": dict(sorted(origins.items())),
        "actions_by_type": dict(sorted(action_types.items())),
        "memory_tiers": dict(sorted(tiers.items())),
        "verified_duplicate_candidates": len(evidence),
    }


def _manifest_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "chunk_index",
        "content_sha256",
        "memory_tier",
        "source_canonicality",
        "source_identity",
        "source_kind",
        "source_revision",
        "source_sha256",
    }
    return {key: metadata[key] for key in sorted(allowed & metadata.keys())}


def build_manifest_payload(
    inventory: Iterable[InventoryRecord],
    actions: Iterable[MigrationAction],
    evidence: Iterable[DuplicateEvidence],
    *,
    palace_path: os.PathLike[str] | str | None = None,
    sqlite_snapshot: dict[str, Any] | None = None,
    source_semantic_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the deterministic reviewed-manifest payload without writing it."""
    inventory_list = sorted(inventory, key=lambda record: record.drawer_id)
    action_list = sorted(actions, key=lambda action: action.drawer_id)
    evidence_list = sorted(evidence, key=lambda item: item.worktree_drawer_id)
    palace_string = str(_expanded_path(palace_path)) if palace_path is not None else "unspecified"
    snapshot = (
        dict(sqlite_snapshot)
        if sqlite_snapshot is not None
        else palace_snapshot(palace_string)
        if palace_path is not None
        else {}
    )
    semantic_snapshot = (
        dict(source_semantic_snapshot)
        if source_semantic_snapshot is not None
        else palace_semantic_snapshot(palace_string)
        if palace_path is not None
        else {}
    )
    return {
        "version": 2,
        "palace_path_sha256": exact_hash(palace_string),
        "sqlite_snapshot": snapshot,
        "source_semantic_snapshot": semantic_snapshot,
        "counts": _manifest_counts(inventory_list, action_list, evidence_list),
        "inventory": [
            {
                "drawer_id": record.drawer_id,
                "origin": record.origin,
                "relative_identity": record.relative_identity,
                "content_sha256": exact_hash(record.content),
                "source_path_sha256": (
                    exact_hash(record.source_path) if record.source_path is not None else None
                ),
            }
            for record in inventory_list
        ],
        "actions": [
            {
                "drawer_id": action.drawer_id,
                "action": action.action,
                "destination_wing": action.destination_wing,
                "reason": action.reason,
                "content_sha256": action.content_sha256,
                "metadata": _manifest_metadata(action.metadata),
            }
            for action in action_list
        ],
        "duplicate_evidence": [
            {
                "worktree_drawer_id": item.worktree_drawer_id,
                "canonical_drawer_id": item.canonical_drawer_id,
                "relative_identity": item.relative_identity,
                "chunk_index": item.chunk_index,
                "content_sha256": item.content_sha256,
                "source_sha256": item.source_sha256,
            }
            for item in evidence_list
        ],
    }


def read_manifest(path: os.PathLike[str] | str) -> dict[str, Any]:
    """Read a reviewed manifest and reject non-object or unsupported payloads."""
    manifest_path = Path(path).expanduser()
    if manifest_path.is_symlink():
        raise ValueError(f"manifest must not be a symlink: {manifest_path}")
    with manifest_path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict) or payload.get("version") not in {1, 2}:
        raise ValueError("unsupported or malformed reorganization manifest")
    return payload


def validate_reviewed_manifest(
    reviewed: dict[str, Any],
    inventory: Iterable[InventoryRecord],
    actions: Iterable[MigrationAction],
    evidence: Iterable[DuplicateEvidence],
    *,
    palace_path: os.PathLike[str] | str,
    sqlite_snapshot: dict[str, Any],
    source_semantic_snapshot: dict[str, Any] | None = None,
) -> None:
    """Require exact equality with the plan that would be generated now."""
    expected = build_manifest_payload(
        inventory,
        actions,
        evidence,
        palace_path=palace_path,
        sqlite_snapshot=sqlite_snapshot,
        source_semantic_snapshot=source_semantic_snapshot,
    )
    if reviewed.get("version") == 1:
        expected["version"] = 1
        expected.pop("source_semantic_snapshot", None)
    if reviewed != expected:
        raise ValueError("reviewed manifest does not match the current palace migration plan")


def write_manifest(
    path: os.PathLike[str] | str,
    inventory: Iterable[InventoryRecord],
    actions: Iterable[MigrationAction],
    evidence: Iterable[DuplicateEvidence],
    *,
    palace_path: os.PathLike[str] | str | None = None,
    sqlite_snapshot: dict[str, Any] | None = None,
    source_semantic_snapshot: dict[str, Any] | None = None,
) -> None:
    """Write a deterministic, owner-only migration manifest.

    Drawer text and source paths are intentionally excluded. The manifest
    records stable IDs, relative identities, hashes, proposed metadata, and
    duplicate evidence so it can be reviewed without duplicating the corpus.
    """

    payload = build_manifest_payload(
        inventory,
        actions,
        evidence,
        palace_path=palace_path,
        sqlite_snapshot=sqlite_snapshot,
        source_semantic_snapshot=source_semantic_snapshot,
    )
    encoded = (json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode(
        "utf-8", errors="surrogatepass"
    )

    manifest_path = Path(path)
    manifest_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    file_descriptor = os.open(manifest_path, flags, 0o600)
    try:
        os.fchmod(file_descriptor, 0o600)
        with os.fdopen(file_descriptor, "wb") as handle:
            file_descriptor = -1
            handle.write(encoded)
    finally:
        if file_descriptor >= 0:
            os.close(file_descriptor)
