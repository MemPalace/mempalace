"""Propose and prune cross-wing tunnels from the hallway graph.

The miner drops an entity tunnel for every entity that has hallways in two
wings. Before the audit repair session that meant tunnels on ``content`` and
``thinking`` and four copies of one link under different spellings, and
nothing to link wings that share no symbol. This module gives the user a
reviewable list instead:

* **propose** — rank shared entities by the weaker side of the link (the
  same rule the miner now uses), drop generic tokens and weak links, and
  write ``<palace>/tunnels/proposal.json``; ``apply`` creates the approved
  rows through ``create_tunnel`` so they dedupe with everything else.
* **prune** — remove tunnels the audit counts as artifacts: generic tokens,
  endpoints whose wing no longer exists, and duplicate spellings of one
  link between the same two wings (the strongest survives).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Iterable, Optional

from .config import MempalaceConfig
from .hallways import entity_spelling_key, is_generic_entity

PROPOSAL_SCHEMA_VERSION = 1
DEFAULT_MAX_TUNNELS = 60


def propose_tunnels(
    hallways: list,
    existing_wings: Iterable[str],
    max_tunnels: int = DEFAULT_MAX_TUNNELS,
    min_count: Optional[int] = None,
) -> dict:
    """Rank candidate cross-wing links; strongest first, capped."""
    from .palace_graph import ENTITY_TUNNEL_MIN_COUNT, entity_tunnel_candidates

    wings = set(existing_wings)
    candidates = entity_tunnel_candidates(
        hallways, min_count=ENTITY_TUNNEL_MIN_COUNT if min_count is None else min_count
    )
    rows = []
    for entity, per_wing in candidates.items():
        present = [(w, disp, n) for w, (disp, n) in per_wing.items() if disp in wings]
        present.sort(key=lambda t: -t[2])
        for i in range(len(present)):
            for j in range(i + 1, len(present)):
                _, wing_a, n_a = present[i]
                _, wing_b, n_b = present[j]
                rows.append(
                    {
                        "entity": entity,
                        "wing_a": wing_a,
                        "wing_b": wing_b,
                        "strength": min(n_a, n_b),
                        "counts": {wing_a: n_a, wing_b: n_b},
                    }
                )
    rows.sort(key=lambda r: (-r["strength"], r["entity"], r["wing_a"], r["wing_b"]))
    return {
        "schema_version": PROPOSAL_SCHEMA_VERSION,
        "planned_at": datetime.now(timezone.utc).isoformat(),
        "candidates": len(rows),
        "tunnels": rows[:max_tunnels],
    }


def proposal_path(config: MempalaceConfig) -> str:
    return os.path.join(config.palace_path, "tunnels", "proposal.json")


def save_proposal(config: MempalaceConfig, plan: dict) -> str:
    path = proposal_path(config)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(plan, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)
    return path


def load_proposal(config: MempalaceConfig) -> dict:
    with open(proposal_path(config), encoding="utf-8") as f:
        plan = json.load(f)
    rows = plan.get("tunnels")
    if not isinstance(rows, list) or not rows:
        raise ValueError("tunnel proposal has no tunnels")
    for row in rows:
        for key in ("entity", "wing_a", "wing_b"):
            if not str(row.get(key) or "").strip():
                raise ValueError(f"proposal row is missing {key!r}: {row}")
    return plan


def apply_proposal(plan: dict, config: Optional[MempalaceConfig] = None) -> int:
    from .palace_graph import create_tunnel

    created = 0
    for row in plan["tunnels"]:
        room = f"entity:{row['entity']}"
        create_tunnel(
            source_wing=row["wing_a"],
            source_room=room,
            target_wing=row["wing_b"],
            target_room=room,
            label=f"shared entity: {row['entity']}",
            kind="entity",
            config=config,
        )
        created += 1
    return created


def prune_tunnels(tunnels: list, existing_wings: Iterable[str]) -> tuple[list, dict]:
    """``(kept, report)`` — drop generic, dangling and duplicate-spelling tunnels."""
    wings = set(existing_wings)
    generic = dangling = duplicates = 0
    kept: list = []
    seen: set = set()
    for t in sorted(
        (t for t in tunnels if isinstance(t, dict)),
        key=lambda t: -int(t.get("access_count") or 0),
    ):
        source, target = t.get("source") or {}, t.get("target") or {}
        bad = False
        for end in (source, target):
            room = str(end.get("room") or "")
            if room.startswith("entity:") and is_generic_entity(room[len("entity:") :]):
                generic += 1
                bad = True
                break
            if str(end.get("wing") or "") not in wings:
                dangling += 1
                bad = True
                break
        if not bad:
            pair = (
                tuple(sorted((str(source.get("wing") or ""), str(target.get("wing") or "")))),
                tuple(
                    sorted(
                        (
                            entity_spelling_key(str(source.get("room") or "")),
                            entity_spelling_key(str(target.get("room") or "")),
                        )
                    )
                ),
            )
            if pair in seen:
                duplicates += 1
                bad = True
            seen.add(pair)
        if not bad:
            kept.append(t)
    report = {
        "total": len(tunnels),
        "generic": generic,
        "dangling": dangling,
        "duplicates": duplicates,
        "removed": len(tunnels) - len(kept),
    }
    return kept, report
