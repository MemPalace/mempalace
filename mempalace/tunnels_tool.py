"""Propose and prune cross-wing tunnels from the hallway graph.

The miner drops an entity tunnel for every entity that has hallways in two
wings. Before the audit repair session that meant tunnels on ``content`` and
``thinking`` and four copies of one link under different spellings, and
nothing to link wings that share no symbol. This module gives the user a
reviewable list instead:

* **propose** — rank shared entities by the weaker side of the link (the
  same rule the miner now uses), drop generic tokens, weak links and links
  that already exist, give every wing without a tunnel its strongest link
  before filling the rest by strength, and write
  ``<palace>/tunnels/proposal.json``; ``apply`` creates the approved rows
  through ``create_tunnel`` so they dedupe with everything else.
* **prune** — remove tunnels the audit counts as artifacts: generic tokens,
  endpoints whose wing no longer exists, and duplicate spellings of one
  link between the same two wings (the strongest survives).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Iterable, Optional

from .config import MempalaceConfig, normalize_wing_name
from .hallways import entity_spelling_key, is_generic_entity, same_file_spelling

PROPOSAL_SCHEMA_VERSION = 1
DEFAULT_MAX_TUNNELS = 60


def _norm_wing(wing: str) -> str:
    return normalize_wing_name(wing) or wing


def room_spelling_key(room: str) -> str:
    """Bucket key for a tunnel endpoint room: ``entity:src/main.zig`` and
    ``entity:main.zig`` share one.

    ``entity_spelling_key`` takes a basename, so the ``entity:`` prefix must
    come off first or a path spelling (``entity:src/main.zig`` → ``main``)
    and a bare one (``entity:main.zig`` → ``entity:main``) never match. The
    key is deliberately lossy — ``src/models/user.py`` and
    ``tests/fixtures/user.py`` share it too — so it only narrows the search;
    :class:`LinkIndex` decides with :func:`same_file_spelling`.
    """
    text = str(room or "")
    if text.startswith("entity:"):
        return "entity:" + entity_spelling_key(text[len("entity:") :])
    return entity_spelling_key(text)


def _link_key(wing_a: str, wing_b: str, room_a: str, room_b: str) -> tuple:
    """One key per link regardless of spelling or direction.

    Each endpoint stays a ``(wing, room)`` pair and the two pairs are sorted
    together, so ``A/x -> B/y`` and ``A/y -> B/x`` are two links, not one.
    """
    return tuple(
        sorted(
            (
                (_norm_wing(wing_a), room_spelling_key(room_a)),
                (_norm_wing(wing_b), room_spelling_key(room_b)),
            )
        )
    )


def tunnel_endpoints(tunnel: dict) -> Optional[tuple]:
    """``((wing, room), (wing, room))`` with wings normalized, or ``None``."""
    source, target = tunnel.get("source") or {}, tunnel.get("target") or {}
    wing_a, wing_b = str(source.get("wing") or ""), str(target.get("wing") or "")
    if not wing_a or not wing_b:
        return None
    return (
        (_norm_wing(wing_a), str(source.get("room") or "")),
        (_norm_wing(wing_b), str(target.get("room") or "")),
    )


def _rooms_match(a: str, b: str) -> bool:
    if a.startswith("entity:") and b.startswith("entity:"):
        return same_file_spelling(a[len("entity:") :], b[len("entity:") :])
    return room_spelling_key(a) == room_spelling_key(b)


class LinkIndex:
    """The links seen so far, matched the way a reader would.

    Two tunnels are one link when they join the same two wings through the
    same endpoints, whichever direction they were written and whichever
    spelling they use (``entity:main.zig`` / ``entity:src/main.zig``). Two
    files that only share a basename (``src/models/user.py`` and
    ``tests/fixtures/user.py``) are two links: deduping them as one would
    delete a real connection.
    """

    def __init__(self) -> None:
        self._buckets: dict[tuple, list[tuple]] = {}

    @staticmethod
    def _bucket(ends: tuple) -> tuple:
        (wing_a, room_a), (wing_b, room_b) = ends
        return _link_key(wing_a, wing_b, room_a, room_b)

    def contains(self, ends: tuple) -> bool:
        (wa, ra), (wb, rb) = ends
        for (wa2, ra2), (wb2, rb2) in self._buckets.get(self._bucket(ends), []):
            if wa == wa2 and wb == wb2 and _rooms_match(ra, ra2) and _rooms_match(rb, rb2):
                return True
            if wa == wb2 and wb == wa2 and _rooms_match(ra, rb2) and _rooms_match(rb, ra2):
                return True
        return False

    def add(self, ends: tuple) -> None:
        self._buckets.setdefault(self._bucket(ends), []).append(ends)


def propose_tunnels(
    hallways: list,
    existing_wings: Iterable[str],
    max_tunnels: int = DEFAULT_MAX_TUNNELS,
    min_count: Optional[int] = None,
    existing_tunnels: Optional[list] = None,
) -> dict:
    """Rank candidate cross-wing links, coverage first, then strength, capped.

    Links that already exist as tunnels are left out, so re-running after
    ``--yes`` proposes only what is still missing. Every wing that has no
    proposed link yet gets its strongest candidate before the remaining
    slots go to the strongest links overall; the audit's coverage measure
    is the share of linkable wings a tunnel reaches, and this ordering
    raises it fastest.
    """
    from .palace_graph import ENTITY_TUNNEL_MIN_COUNT, entity_tunnel_candidates

    wings = {_norm_wing(str(w)) for w in existing_wings}
    known = LinkIndex()
    for t in existing_tunnels or []:
        if isinstance(t, dict):
            ends = tunnel_endpoints(t)
            if ends:
                known.add(ends)
    candidates = entity_tunnel_candidates(
        hallways, min_count=ENTITY_TUNNEL_MIN_COUNT if min_count is None else min_count
    )
    rows = []
    for entity, per_wing in candidates.items():
        # ``per_wing`` is keyed by normalized wing; membership must be too, or
        # a hallway wing spelled ``foo-bar`` next to a drawer wing ``foo_bar``
        # silently loses its links.
        present = [(w, disp, n) for w, (disp, n) in per_wing.items() if w in wings]
        present.sort(key=lambda t: -t[2])
        room = f"entity:{entity}"
        for i in range(len(present)):
            for j in range(i + 1, len(present)):
                _, wing_a, n_a = present[i]
                _, wing_b, n_b = present[j]
                if known.contains(((_norm_wing(wing_a), room), (_norm_wing(wing_b), room))):
                    continue
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

    # Pass 1: the strongest link for every wing that no chosen row reaches
    # yet, walking rows strongest-first so a wing's first link is its best.
    covered: set = set()
    for t in existing_tunnels or []:
        if isinstance(t, dict) and tunnel_endpoints(t):
            for end in (t.get("source") or {}, t.get("target") or {}):
                w = str(end.get("wing") or "")
                if w:
                    covered.add(normalize_wing_name(w) or w)
    chosen: list = []
    chosen_ids: set = set()
    for idx, row in enumerate(rows):
        if len(chosen) >= max_tunnels:
            break
        ends = {
            normalize_wing_name(row["wing_a"]) or row["wing_a"],
            normalize_wing_name(row["wing_b"]) or row["wing_b"],
        }
        if ends - covered:
            chosen.append(row)
            chosen_ids.add(idx)
            covered |= ends
    # Pass 2: fill the remaining slots by strength.
    for idx, row in enumerate(rows):
        if len(chosen) >= max_tunnels:
            break
        if idx not in chosen_ids:
            chosen.append(row)
            chosen_ids.add(idx)
    chosen.sort(key=lambda r: (-r["strength"], r["entity"], r["wing_a"], r["wing_b"]))
    return {
        "schema_version": PROPOSAL_SCHEMA_VERSION,
        "planned_at": datetime.now(timezone.utc).isoformat(),
        "candidates": len(rows),
        "tunnels": chosen,
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
        if not isinstance(row, dict):
            raise ValueError(f"proposal row is not an object: {row!r}")
        for key in ("entity", "wing_a", "wing_b"):
            if not str(row.get(key) or "").strip():
                raise ValueError(f"proposal row is missing {key!r}: {row}")
    return plan


def apply_proposal(plan: dict, config: Optional[MempalaceConfig] = None) -> int:
    """Create the plan's tunnels; returns how many were created.

    The plan sat under review, so the tunnel file is read again here: a row
    whose link now exists under another spelling (``entity:main.py`` added
    while the plan says ``entity:src/main.py``) is skipped, as is a row that
    repeats an earlier row. ``create_tunnel`` dedupes only identical endpoint
    ids and would write both spellings.
    """
    from .palace_graph import _load_tunnels, create_tunnel

    known = LinkIndex()
    for t in _load_tunnels(config):
        if isinstance(t, dict):
            ends = tunnel_endpoints(t)
            if ends:
                known.add(ends)
    created = 0
    for row in plan["tunnels"]:
        room = f"entity:{row['entity']}"
        ends = ((_norm_wing(row["wing_a"]), room), (_norm_wing(row["wing_b"]), room))
        if known.contains(ends):
            continue
        known.add(ends)
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
    """``(kept, report)`` — drop generic, dangling and duplicate-spelling tunnels.

    Wings compare through ``normalize_wing_name`` on both sides, the same
    way ``follow_tunnels`` resolves them, so a tunnel that still resolves
    is never counted as dangling.
    """
    wings = {_norm_wing(str(w)) for w in existing_wings}
    generic = dangling = duplicates = 0
    kept: list = []
    seen = LinkIndex()
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
            if _norm_wing(str(end.get("wing") or "")) not in wings:
                dangling += 1
                bad = True
                break
        if not bad:
            ends = tunnel_endpoints(t)
            if ends is not None:
                if seen.contains(ends):
                    duplicates += 1
                    bad = True
                else:
                    seen.add(ends)
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
