"""
oplog_verify.py — Dual-write shadow divergence check (RFC 004 step 2a)
==========================================================================

During the shadow period every semantic palace write emits an op beside the
authoritative store write. This module replays the op-log into an expected
final state and compares it against the live store, one-way: every op must
be reflected in the store. A clean report over a real usage period is the
cutover gate.

Deliberately NOT checked here: store rows with no ops. The 150k+ drawers
mined before the op-log existed are unstamped by design; the one-time
local-capture promotion pass (an RFC 004 step-2a commitment) is the
mechanism that brings them into the log, and its own accounting is the
coverage check. Verifying "no op-less store rows" before promotion would
only re-count the backlog.
"""

import hashlib
import os
from typing import Optional

from .oplog import OPLOG_DB_FILENAME, OpLog

# How many offending ids to include per divergence bucket — the counts are
# always exact; the samples keep the report readable.
SAMPLE_CAP = 20

_DRAWER_KINDS = ("drawer.add", "drawer.revise", "drawer.tombstone")
_KG_KINDS = ("kg.assert", "kg.close", "kg.entity.upsert")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_all_ops(palace_path: str, kinds=None) -> list:
    """Every op in the palace's log, paged out in arrival order."""
    with OpLog(os.path.join(os.path.expanduser(palace_path), OPLOG_DB_FILENAME)) as oplog:
        ops = []
        cursor = 0
        while True:
            page = oplog.list_all(after_seq=cursor, limit=1000, kinds=kinds)
            if not page:
                break
            ops.extend(page)
            cursor = page[-1]["seq"]
        return ops


def fold_expected(ops: list) -> dict:
    """Fold ops (in HLC order — the cross-replica merge order) into the
    expected final state of drawers, triples, and entities."""
    drawers: dict = {}
    triples: dict = {}
    entities: dict = {}
    for op in sorted(ops, key=lambda o: o["hlc"]):
        kind, payload = op["kind"], op["payload"]
        if kind in ("drawer.add", "drawer.revise"):
            drawers[payload["drawer_id"]] = {
                "content_sha256": payload.get("content_sha256"),
                "tombstoned": False,
            }
        elif kind == "drawer.tombstone":
            drawers[payload["drawer_id"]] = {
                "content_sha256": payload.get("content_sha256"),
                "tombstoned": True,
            }
        elif kind == "kg.assert":
            triples[payload["triple_id"]] = {"open": payload.get("valid_to") is None}
        elif kind == "kg.close":
            for triple_id in payload.get("closed_triple_ids") or []:
                triples.setdefault(triple_id, {})["open"] = False
        elif kind == "kg.entity.upsert":
            entities[payload["entity_id"]] = {
                "name": payload.get("name"),
                "type": payload.get("type"),
            }
    return {"drawers": drawers, "triples": triples, "entities": entities}


def _result_ids(result) -> list:
    return list(result.get("ids") or []) if isinstance(result, dict) else []


def read_logical_drawer(col, drawer_id: str) -> Optional[dict]:
    """Read a logical drawer: the direct row, or its rejoined chunk group."""
    result = col.get(ids=[drawer_id], include=["documents", "metadatas"])
    ids = _result_ids(result)
    if ids:
        docs = result.get("documents") or [""]
        return {"content": docs[0] or ""}
    result = col.get(where={"parent_drawer_id": drawer_id}, include=["documents", "metadatas"])
    ids = _result_ids(result)
    if not ids:
        return None
    rows = sorted(
        zip(ids, result.get("documents") or [], result.get("metadatas") or []),
        key=lambda row: (row[2] or {}).get("chunk_index", 0),
    )
    return {"content": "".join(doc or "" for _, doc, _ in rows)}


def _verify_drawers(col, expected: dict) -> dict:
    ok = 0
    missing: list = []
    diverged: list = []
    not_tombstoned: list = []
    for drawer_id, exp in expected.items():
        record = read_logical_drawer(col, drawer_id)
        if exp["tombstoned"]:
            if record is None:
                ok += 1
            else:
                not_tombstoned.append(drawer_id)
        elif record is None:
            missing.append(drawer_id)
        elif exp["content_sha256"] and _sha256(record["content"]) != exp["content_sha256"]:
            diverged.append(drawer_id)
        else:
            ok += 1
    return {
        "checked": len(expected),
        "ok": ok,
        "missing_count": len(missing),
        "missing": missing[:SAMPLE_CAP],
        "diverged_count": len(diverged),
        "diverged": diverged[:SAMPLE_CAP],
        "not_tombstoned_count": len(not_tombstoned),
        "not_tombstoned": not_tombstoned[:SAMPLE_CAP],
    }


def _verify_kg(kg, expected_triples: dict, expected_entities: dict) -> dict:
    ok = 0
    missing: list = []
    diverged: list = []
    for triple_id, exp in expected_triples.items():
        row = kg.get_triple(triple_id)
        if row is None:
            missing.append(triple_id)
        elif "open" in exp and (row["valid_to"] is None) != exp["open"]:
            diverged.append(triple_id)
        else:
            ok += 1
    entity_missing: list = []
    for entity_id, exp in expected_entities.items():
        row = kg.get_entity(entity_id)
        if row is None or row.get("name") != exp["name"]:
            entity_missing.append(entity_id)
        else:
            ok += 1
    return {
        "checked": len(expected_triples) + len(expected_entities),
        "ok": ok,
        "missing_count": len(missing),
        "missing": missing[:SAMPLE_CAP],
        "diverged_count": len(diverged),
        "diverged": diverged[:SAMPLE_CAP],
        "entity_missing_count": len(entity_missing),
        "entity_missing": entity_missing[:SAMPLE_CAP],
    }


def verify_shadow(palace_path: str, collection=None, kg=None) -> dict:
    """Replay the op-log and compare against the live store.

    ``collection``/``kg`` accept pre-opened handles (tests, callers that
    already hold them); by default the palace collection and its
    palace-local knowledge graph are opened read-only-intent. Returns a
    report dict; ``clean`` is True when every op is reflected in the store.
    """
    palace_path = os.path.expanduser(palace_path)
    ops = load_all_ops(palace_path, kinds=list(_DRAWER_KINDS + _KG_KINDS))
    expected = fold_expected(ops)

    if collection is None and expected["drawers"]:
        from .palace import get_collection

        collection = get_collection(palace_path, create=False)
    drawer_report = (
        _verify_drawers(collection, expected["drawers"])
        if expected["drawers"]
        else {"checked": 0, "ok": 0}
    )

    if kg is None and (expected["triples"] or expected["entities"]):
        from .knowledge_graph import KnowledgeGraph

        # Same palace-local resolution as the MCP server.
        kg = KnowledgeGraph(db_path=os.path.join(palace_path, "knowledge_graph.sqlite3"))
    kg_report = (
        _verify_kg(kg, expected["triples"], expected["entities"])
        if (expected["triples"] or expected["entities"])
        else {"checked": 0, "ok": 0}
    )

    clean = drawer_report.get("checked", 0) == drawer_report.get("ok", 0) and kg_report.get(
        "checked", 0
    ) == kg_report.get("ok", 0)
    return {
        "palace": palace_path,
        "ops": len(ops),
        "drawers": drawer_report,
        "kg": kg_report,
        "clean": clean,
    }
