"""Normalize knowledge-graph predicates onto a closed vocabulary.

Agents filing facts one at a time invent a predicate per fact
(``brazil_presidential_mae_after_ranker_fix``), so ``mempalace_kg_query`` can
never find facts by relation: on the maintainers' palace 51 of 57 facts had a
predicate no other fact used. This module maps every open fact whose
predicate is outside the vocabulary onto one of the allowed predicates, with
the detail the old predicate carried folded into the object.

Two steps, like ``rooms``:

* **plan** — the LLM proposes, per off-vocabulary fact, the target predicate
  and the rewritten object (``(weatherstation, deployed_commit, fc81c6f6)`` →
  ``(weatherstation, status, "deployed commit fc81c6f6 on staging")``). The
  plan is written to ``<palace>/kg/normalize.json`` for review; any row can
  be edited or dropped.
* **apply** — for each row, the old fact is closed and the new one opened at
  one shared instant (``invalidate`` + ``add_triple`` with the same
  timestamp), so history survives and an as-of query before the boundary
  still returns the original wording. Nothing is deleted.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from typing import Iterable, Optional

from .config import MempalaceConfig

NORMALIZE_SCHEMA_VERSION = 1
DEFAULT_VOCABULARY = (
    "works_on",
    "owns",
    "depends_on",
    "uses",
    "decided",
    "status",
    "located_in",
    "measured",
)

_PLAN_SYSTEM = (
    "You normalize a knowledge graph. Every fact is (subject, predicate, object). "
    "Rewrite each fact so its predicate is one of the ALLOWED predicates and the "
    "meaning the old predicate carried moves into the object as plain words. Keep "
    "the subject unchanged. Keep every number, date, commit id and name from the "
    'original. Return ONLY JSON: {"facts": [{"index": 1, "predicate": '
    '"allowed_predicate", "object": "rewritten object"}, ...]}.'
)


def off_vocabulary_facts(kg, vocabulary: Iterable[str]) -> list[dict]:
    """Open facts whose predicate is not in ``vocabulary``, oldest first."""
    allowed = {v.lower() for v in vocabulary}
    conn = kg._conn()
    rows = conn.execute(
        """
        SELECT t.id, t.predicate, s.name AS subject, o.name AS object, t.valid_from
        FROM triples t
        JOIN entities s ON s.id = t.subject
        JOIN entities o ON o.id = t.object
        WHERE t.valid_to IS NULL
        ORDER BY t.valid_from, t.predicate
        """
    ).fetchall()
    return [
        {
            "id": r["id"],
            "subject": r["subject"],
            "predicate": r["predicate"],
            "object": r["object"],
            "valid_from": r["valid_from"],
        }
        for r in rows
        if str(r["predicate"]).lower() not in allowed
    ]


def _user_prompt(facts: list[dict], vocabulary: Iterable[str]) -> str:
    lines = ["ALLOWED predicates: " + ", ".join(vocabulary), "", f"Facts ({len(facts)}):"]
    for i, f in enumerate(facts, 1):
        lines.append(
            f"{i}. subject={f['subject']!r} predicate={f['predicate']!r} object={f['object']!r}"
        )
    return "\n".join(lines)


def _json_object(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise ValueError("LLM response held no JSON object")
        return json.loads(match.group(0))


def _humanize_prefix(obj: str, old_predicate: str) -> str:
    """``remediated_h1 ...`` → ``remediated h1 ...``: models tend to paste the old
    predicate verbatim at the front of the rewritten object."""
    if obj.lower().startswith(old_predicate.lower()):
        return old_predicate.replace("_", " ") + obj[len(old_predicate) :]
    return obj


def plan_normalize(
    facts: list[dict], provider, vocabulary: Iterable[str] = DEFAULT_VOCABULARY
) -> dict:
    """Ask the LLM for a rewrite of every off-vocabulary fact.

    Rows whose proposed predicate is not in the vocabulary are dropped from
    the plan and counted in ``rejected``; the user can add them by hand.
    """
    vocabulary = tuple(vocabulary)
    if not facts:
        return {
            "schema_version": NORMALIZE_SCHEMA_VERSION,
            "vocabulary": list(vocabulary),
            "planned_at": datetime.now(timezone.utc).isoformat(),
            "facts": [],
            "rejected": 0,
        }
    response = provider.classify(
        _PLAN_SYSTEM, _user_prompt(facts, vocabulary), json_mode=True, think=False
    )
    data = _json_object(response.text)
    proposals = data.get("facts") if isinstance(data, dict) else data
    if not isinstance(proposals, list):
        raise ValueError("LLM response JSON has no 'facts' list")
    allowed = {v.lower() for v in vocabulary}
    by_index: dict[int, dict] = {}
    for p in proposals:
        if not isinstance(p, dict):
            continue
        try:
            index = int(p.get("index"))
        except (TypeError, ValueError):
            continue
        by_index[index] = p
    rows = []
    rejected = 0
    for i, f in enumerate(facts, 1):
        p = by_index.get(i)
        predicate = str((p or {}).get("predicate") or "").strip().lower().replace(" ", "_")
        obj = _humanize_prefix(str((p or {}).get("object") or "").strip(), f["predicate"])
        if not p or predicate not in allowed or not obj:
            rejected += 1
            continue
        rows.append(
            {
                "id": f["id"],
                "subject": f["subject"],
                "old_predicate": f["predicate"],
                "old_object": f["object"],
                "predicate": predicate,
                "object": obj,
            }
        )
    return {
        "schema_version": NORMALIZE_SCHEMA_VERSION,
        "vocabulary": list(vocabulary),
        "planned_at": datetime.now(timezone.utc).isoformat(),
        "proposed_by": f"{getattr(provider, 'name', 'llm')}/{getattr(provider, 'model', '')}",
        "facts": rows,
        "rejected": rejected,
    }


def normalize_plan_path(config: MempalaceConfig) -> str:
    return os.path.join(config.palace_path, "kg", "normalize.json")


def save_normalize_plan(config: MempalaceConfig, plan: dict) -> str:
    path = normalize_plan_path(config)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(plan, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)
    return path


def load_normalize_plan(config: MempalaceConfig) -> dict:
    with open(normalize_plan_path(config), encoding="utf-8") as f:
        plan = json.load(f)
    allowed = {str(v).lower() for v in plan.get("vocabulary") or DEFAULT_VOCABULARY}
    rows = plan.get("facts")
    if not isinstance(rows, list):
        raise ValueError("normalize plan has no facts list")
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError(f"plan row is not an object: {row!r}")
        if not str(row.get("id") or "").strip():
            raise ValueError(f"plan row is missing 'id': {row}")
        for key in ("subject", "old_predicate", "old_object", "predicate", "object"):
            if not str(row.get(key) or "").strip():
                raise ValueError(f"plan row is missing {key!r}: {row}")
        if str(row["predicate"]).lower() not in allowed:
            raise ValueError(f"predicate {row['predicate']!r} is not in the vocabulary")
    return plan


def apply_normalize(kg, plan: dict, at: Optional[str] = None) -> dict:
    """Close each old fact and open its rewrite at one shared instant.

    Each row is one ``KnowledgeGraph.rewrite`` call, addressed by the
    triple id the plan recorded: one transaction per fact, and a fact that
    was closed or replaced while the plan sat under review is left alone
    and counted in ``stale`` instead of being resurrected.
    """
    boundary = at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    applied = skipped = stale = 0
    for row in plan["facts"]:
        if row["old_predicate"] == row["predicate"] and row["old_object"] == row["object"]:
            skipped += 1
            continue
        new_id = kg.rewrite(
            str(row["id"]),
            row["predicate"],
            row["object"],
            at=boundary,
            source_file="mempalace kg normalize",
        )
        if new_id is None:
            stale += 1
        else:
            applied += 1
    return {"applied": applied, "skipped": skipped, "stale": stale, "boundary": boundary}
