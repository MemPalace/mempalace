"""Palace organization audit — how navigable is the palace, not how big.

``mempalace status`` answers "what is filed". This module answers "is it
filed somewhere an agent can find by walking the palace", scoring five
layers that each have a known failure mode:

* **rooms** — mined transcripts collapse into a handful of generic rooms
  (``technical``, ``architecture``, ``planning``, ``general``, ``problems``),
  so the room layer stops carrying information.
* **naming** — wings and rooms drift into near-duplicate spellings
  (``acme-app`` / ``acme_app``, ``release-3.6.0`` / ``release_3_6_0``).
* **tunnels** — auto-generated entity tunnels on generic tokens that nobody
  ever traverses.
* **hallways** — entity co-occurrence links between an entity and its own
  path or file spelling (``main.zig`` ↔ ``src/main.zig``).
* **knowledge graph** — one-off predicates that make the graph a log rather
  than something queryable by relation.

Every reader here is read-only and never opens the vector index: drawer
counts come from the backend's grouped sqlite query, tunnels and hallways
from their JSON sidecars, and the knowledge graph from a read-only sqlite
connection. The audit therefore runs while an MCP server holds the palace
lock, which is exactly when you want to look at a live palace.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import textwrap
import time
from collections import Counter, defaultdict
from typing import Optional

from .backends._inproc_sqlite import open_reader as open_palace_reader
from .config import MempalaceConfig, normalize_wing_name
from .hallways import (
    entity_spelling_key,
    is_generic_entity,
    is_self_link,
    list_hallways,
    association_groups,
)
from .palace_graph import _load_tunnels
from .tunnels_tool import LinkIndex, tunnel_endpoints

# Rooms the transcript classifier falls back to. A drawer in one of these
# rooms is findable by search, not by walking the palace.
GENERIC_ROOMS = frozenset({"general", "technical", "architecture", "planning", "problems"})

# A wing with fewer drawers than this is a stub — usually an agent identity
# or a one-off mine that belongs inside another wing.
TINY_WING_MAX = 5

# Above this share, a wing's largest room is "flat": the wing has rooms in
# name only. Only wings with at least FLAT_WING_MIN_DRAWERS are judged.
FLAT_WING_SHARE = 0.90
FLAT_WING_MIN_DRAWERS = 100

# A wing holding transcripts from this many distinct source projects is a
# machine-level export, not a project: rooms cannot mean anything inside it.
MIXED_WING_MIN_PROJECTS = 5
MIXED_WING_MIN_DRAWERS = 500

_SAMPLE_LIMIT = 10


# ── name normalization ───────────────────────────────────────────────────────


def drift_key(name: str) -> str:
    """Collapse a wing/room name to the key two drifted spellings share.

    Lower-cases, drops every non-alphanumeric character, and strips a
    trailing ``s`` so ``concierge-automation`` and ``concierge-automations``
    meet. ``release-3.6.0`` and ``release_3_6_0`` both become ``release360``.
    """
    key = re.sub(r"[^a-z0-9]", "", str(name).lower())
    if len(key) > 3 and key.endswith("s"):
        key = key[:-1]
    return key


def _wing_prefix_pairs(wings: list[str]) -> list[tuple[str, str]]:
    """Pairs where one wing is the other plus a suffix at a separator.

    ``arcade`` / ``arcade_game`` and ``weatherstation`` /
    ``weatherstation_global`` are the same project filed twice; ``wing_a`` /
    ``wing_ab`` is not, hence the separator requirement.
    """
    pairs = []
    normalized = {w: normalize_wing_name(w) for w in wings}
    for a in wings:
        na = normalized[a]
        if not na:
            continue
        for b in wings:
            if a == b:
                continue
            nb = normalized[b]
            if len(nb) > len(na) + 1 and nb.startswith(na) and nb[len(na)] == "_":
                pairs.append((a, b))
    return pairs


def hallway_entity_key(entity: str) -> str:
    """Kept as the audit's name for :func:`mempalace.hallways.entity_spelling_key`."""
    return entity_spelling_key(entity)


# ── readers ──────────────────────────────────────────────────────────────────


def _read_wing_room_counts(config: MempalaceConfig) -> Optional[dict[str, dict[str, int]]]:
    """``{wing: {room: n}}`` from the backend's grouped sqlite query.

    Returns ``None`` when the palace is not sqlite-readable so the caller can
    fall back to the collection, and never opens the vector index.
    """
    from .palace_graph import sqlite_grouped_counts_reader

    reader = sqlite_grouped_counts_reader(config)
    if reader is None:
        return None
    rows = reader(config.palace_path, config.collection_name)
    if rows is None:
        return None
    counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for row in rows:
        room, wing, n = row[0], row[1], row[3]
        counts[str(wing or "?")][str(room or "?")] += int(n)
    return {w: dict(r) for w, r in counts.items()}


def _read_wing_room_counts_from_collection(
    config: MempalaceConfig,
) -> Optional[dict[str, dict[str, int]]]:
    from .palace_graph import _get_collection, _iter_all_metadata

    col = _get_collection(config)
    if col is None:
        return None
    counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for meta in _iter_all_metadata(col):
        meta = meta or {}
        counts[str(meta.get("wing") or "?")][str(meta.get("room") or "?")] += 1
    return {w: dict(r) for w, r in counts.items()}


def _wing_source_counts_reader(config: MempalaceConfig):
    """The backend's ``(wing, source_file, n)`` reader, or ``None``.

    Resolved from configuration like ``sqlite_grouped_counts_reader``, never
    by sniffing the palace directory; both in-tree sqlite layouts are served
    (the ChromaDB metadata database and the sqlite_exact / rust_exact file),
    each scoped to the drawer collection so closets are not counted twice.
    """
    try:
        from .palace import resolve_backend_name

        backend = resolve_backend_name(
            config.palace_path, explicit=os.environ.get("MEMPALACE_BACKEND_EXPLICIT")
        )
    except Exception:
        return None
    if backend == "chroma":
        from .backends.chroma import sqlite_wing_source_counts

        return sqlite_wing_source_counts
    if backend in {"sqlite_exact", "rust_exact"}:
        from .backends.sqlite_exact import sqlite_wing_source_counts

        return sqlite_wing_source_counts
    return None


def _read_wing_project_mix(config: MempalaceConfig) -> Optional[dict[str, dict]]:
    """``{wing: {"projects": n, "drawers": n, "top": [(key, n), ...]}}`` from sqlite.

    Groups drawers by the project key their transcript path carries, read
    from the backend's sqlite file scoped to the drawer collection; ``None``
    on a backend the audit cannot read directly.
    """
    from .wing_split import project_key, resolve_target

    reader = _wing_source_counts_reader(config)
    if reader is None:
        return None
    try:
        rows = reader(config.palace_path, config.collection_name)
    except Exception:
        return None
    if rows is None:
        return None
    per_wing: dict[str, Counter] = defaultdict(Counter)
    drawers: Counter = Counter()
    wings = {str(w) for w, _, _ in rows if w}
    for wing, source, n in rows:
        if not wing:
            continue
        # Path-derived keys only: a Codex cwd needs the file, which an audit
        # never opens. Keys are resolved to the wing they would split into,
        # so two machines' paths (or a worktree) of one project count once.
        key = project_key(str(source)) if ".claude" in str(source) else None
        drawers[str(wing)] += int(n)
        if key:
            target, _ = resolve_target(key, wings)
            per_wing[str(wing)][target] += int(n)
    return {
        wing: {
            "projects": len(targets),
            "drawers": drawers[wing],
            "top": targets.most_common(5),
        }
        for wing, targets in per_wing.items()
    }


def resolve_kg_path(palace_path: str, explicit: bool = False) -> str:
    """The knowledge graph that belongs to ``palace_path``.

    A palace-local ``knowledge_graph.sqlite3`` always wins. The legacy
    ``~/.mempalace/knowledge_graph.sqlite3`` belongs to the legacy default
    palace (``~/.mempalace/palace``) alone, so it is used only for that
    palace — whether it was reached by default, ``--palace``,
    ``MEMPALACE_PALACE_PATH`` or ``config.json``. Any other palace keeps its
    graph inside itself, so ``audit`` cannot report and ``kg normalize
    --yes`` cannot rewrite an unrelated graph. ``explicit`` (``--palace``
    given) forces the palace-local path, as the MCP server does for its
    flag. Neither file is created here.
    """
    from .config import DEFAULT_PALACE_PATH
    from .knowledge_graph import DEFAULT_KG_PATH

    local = os.path.join(palace_path, "knowledge_graph.sqlite3")
    if explicit or os.path.isfile(local):
        return local
    if os.path.realpath(palace_path) == os.path.realpath(DEFAULT_PALACE_PATH):
        return DEFAULT_KG_PATH
    return local


def _read_kg(kg_path: str) -> Optional[dict]:
    """Entity/triple/predicate counts over a read-only connection."""
    if not os.path.isfile(kg_path):
        return None
    try:
        conn = open_palace_reader(kg_path, timeout=2.0)
    except (sqlite3.Error, ValueError):
        return None
    try:
        entities = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
        triples = conn.execute("SELECT COUNT(*) FROM triples").fetchone()[0]
        current = conn.execute("SELECT COUNT(*) FROM triples WHERE valid_to IS NULL").fetchone()[0]
        # Reuse is judged over open facts only: a normalized (closed) fact
        # keeps its old predicate for history and must not count against it.
        predicate_rows = conn.execute(
            "SELECT predicate, COUNT(*) FROM triples WHERE valid_to IS NULL "
            "GROUP BY predicate ORDER BY 2 DESC, 1"
        ).fetchall()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    predicates = {str(p): int(n) for p, n in predicate_rows}
    one_off = sorted(p for p, n in predicates.items() if n == 1)
    reuse = 0.0 if current == 0 else 1.0 - (len(predicates) / current)
    return {
        "path": kg_path,
        "entities": int(entities),
        "triples": int(triples),
        "current_facts": int(current),
        "expired_facts": int(triples - current),
        "distinct_predicates": len(predicates),
        "one_off_predicates": len(one_off),
        "one_off_sample": one_off[:_SAMPLE_LIMIT],
        "predicate_reuse": round(reuse, 4),
    }


# ── analyses ─────────────────────────────────────────────────────────────────


def _analyze_rooms(wing_rooms: dict[str, dict[str, int]]) -> dict:
    total = sum(sum(r.values()) for r in wing_rooms.values())
    generic = sum(
        n for rooms in wing_rooms.values() for room, n in rooms.items() if room in GENERIC_ROOMS
    )
    room_totals: Counter = Counter()
    room_wings: dict[str, set] = defaultdict(set)
    for wing, rooms in wing_rooms.items():
        for room, n in rooms.items():
            room_totals[room] += n
            room_wings[room].add(wing)

    flat_wings = []
    for wing, rooms in sorted(wing_rooms.items()):
        wing_total = sum(rooms.values())
        if wing_total < FLAT_WING_MIN_DRAWERS or not rooms:
            continue
        room, n = max(rooms.items(), key=lambda item: item[1])
        share = n / wing_total
        if share >= FLAT_WING_SHARE:
            flat_wings.append(
                {"wing": wing, "room": room, "drawers": wing_total, "share": round(share, 4)}
            )
    flat_wings.sort(key=lambda f: -f["drawers"])

    generic_share = 0.0 if total == 0 else generic / total
    return {
        "total_drawers": total,
        "wings": len(wing_rooms),
        "distinct_rooms": len(room_totals),
        "room_instances": sum(len(r) for r in wing_rooms.values()),
        "generic_drawers": generic,
        "generic_share": round(generic_share, 4),
        "flat_wings": flat_wings,
        "cross_wing_rooms": [
            {"room": room, "wings": len(wings), "drawers": room_totals[room]}
            for room, wings in sorted(room_wings.items(), key=lambda item: (-len(item[1]), item[0]))
            if len(wings) >= 2
        ][:_SAMPLE_LIMIT],
    }


def _analyze_naming(
    wing_rooms: dict[str, dict[str, int]], project_mix: Optional[dict[str, dict]] = None
) -> dict:
    wings = sorted(wing_rooms)
    wing_groups: dict[str, list[str]] = defaultdict(list)
    for wing in wings:
        wing_groups[drift_key(wing)].append(wing)
    wing_drift = [sorted(g) for g in wing_groups.values() if len(g) > 1]

    seen = {tuple(g) for g in wing_drift}
    prefix_pairs = []
    for a, b in _wing_prefix_pairs(wings):
        pair = tuple(sorted((a, b)))
        if pair not in seen:
            seen.add(pair)
            prefix_pairs.append(list(pair))

    room_groups: dict[str, dict[str, set]] = defaultdict(lambda: defaultdict(set))
    for wing, rooms in wing_rooms.items():
        for room in rooms:
            room_groups[drift_key(room)][room].add(wing)
    room_drift = []
    for spellings in room_groups.values():
        if len(spellings) > 1:
            room_drift.append(
                {
                    "rooms": sorted(spellings),
                    "wings": sorted(set().union(*spellings.values())),
                }
            )
    room_drift.sort(key=lambda d: d["rooms"])

    tiny = [
        {"wing": w, "drawers": sum(r.values())}
        for w, r in sorted(wing_rooms.items())
        if sum(r.values()) <= TINY_WING_MAX
    ]
    mixed = []
    for wing, info in sorted((project_mix or {}).items(), key=lambda kv: -kv[1]["drawers"]):
        if (
            info["projects"] >= MIXED_WING_MIN_PROJECTS
            and info["drawers"] >= MIXED_WING_MIN_DRAWERS
            and wing in wing_rooms
        ):
            mixed.append(
                {
                    "wing": wing,
                    "projects": info["projects"],
                    "drawers": info["drawers"],
                    "top": [list(t) for t in info["top"]],
                }
            )
    return {
        "wing_drift": wing_drift,
        "wing_prefix_pairs": prefix_pairs,
        "room_drift": room_drift,
        "tiny_wings": tiny,
        "mixed_wings": mixed,
    }


def _analyze_tunnels(
    tunnels: list[dict], wings: Optional[set] = None, hallways: Optional[list] = None
) -> dict:
    """Quality and coverage; traversal is reported, not scored.

    An artifact tunnel links a generic token, points at a wing that no
    longer exists, or duplicates another tunnel between the same two wings
    under a different spelling of the same entity. Coverage is the share of
    *linkable* wings — wings that share a strong entity with another wing
    according to the hallways — that at least one sound tunnel touches;
    ``mempalace tunnels propose`` raises it. Traversal (``access_count``)
    only rises when an agent follows a tunnel, so it is informational.
    """
    total = len(tunnels)
    wings_norm = None if wings is None else {normalize_wing_name(str(w)) or str(w) for w in wings}
    kinds: Counter = Counter()
    never_used = 0
    generic = []
    dangling = 0
    duplicates = 0
    seen_pairs = LinkIndex()
    artifacts = 0
    tunneled_wings: set = set()
    records = sorted(
        (t for t in tunnels if isinstance(t, dict)),
        key=lambda t: -int(t.get("access_count") or 0),
    )
    for t in records:
        kinds[str(t.get("kind") or "unknown")] += 1
        if not t.get("access_count"):
            never_used += 1
        source, target = t.get("source") or {}, t.get("target") or {}
        bad = False
        for end in (source, target):
            room = str(end.get("room") or "")
            if room.startswith("entity:"):
                name = room[len("entity:") :]
                if is_generic_entity(name):
                    bad = True
                    if name not in generic:
                        generic.append(name)
            end_wing = str(end.get("wing") or "").strip()
            if (
                wings_norm is not None
                and (normalize_wing_name(end_wing) or end_wing) not in wings_norm
            ):
                bad = True
                dangling += 1
        # Same rule `tunnels prune` uses: endpoints stay paired, wings
        # normalized, spellings of one file match, two files that only share
        # a basename do not.
        ends = tunnel_endpoints(t)
        if ends is not None:
            if seen_pairs.contains(ends):
                bad = True
                duplicates += 1
            else:
                seen_pairs.add(ends)
        if bad:
            artifacts += 1
        else:
            for end in (source, target):
                w = str(end.get("wing") or "").strip()
                if w:
                    tunneled_wings.add(normalize_wing_name(w) or w)
    linkable: dict[str, str] = {}
    if hallways:
        from .palace_graph import entity_tunnel_candidates

        for per_wing in entity_tunnel_candidates(hallways).values():
            present = {
                w: disp
                for w, (disp, _n) in per_wing.items()
                if wings is None or disp in wings or w in wings
            }
            if len(present) >= 2:
                linkable.update(present)
    unlinked = sorted(disp for w, disp in linkable.items() if w not in tunneled_wings)
    coverage = 1.0 if not linkable else 1.0 - len(unlinked) / len(linkable)
    return {
        "total": total,
        "by_kind": dict(kinds),
        "never_traversed": never_used,
        "never_traversed_share": round(never_used / total, 4) if total else 0.0,
        "generic_entities": sorted(generic),
        "dangling_endpoints": dangling,
        "duplicates": duplicates,
        "artifacts": artifacts,
        "artifact_share": round(artifacts / total, 4) if total else 0.0,
        "linkable_wings": len(linkable),
        "unlinked_wings": unlinked,
        "coverage": round(coverage, 4),
    }


# Self-links are judged where they matter: among the strongest hallways, which
# is the slice a wake-up or a traversal actually reads.
HALLWAY_TOP_N = 100


def _analyze_hallways(hallways: list[dict]) -> dict:
    """Self-links and spelling-variant duplicates, overall and among the strongest.

    Both come from pairing raw entity spellings at mine time. A duplicate is
    any record beyond the strongest in one :func:`association_groups` group,
    so a real association counted under four spellings scores three
    artifacts, and two files that merely share a basename score none.
    """
    records = [h for h in hallways if isinstance(h, dict)]
    total = len(hallways)

    def strength(h: dict) -> int:
        return -int(h.get("co_occurrence_count") or 0)

    artifacts: set = set()
    self_link_records = [h for h in records if is_self_link(h)]
    artifacts.update(id(h) for h in self_link_records)
    # The prune's own grouping: every record past the strongest in a group
    # is a duplicate, and nothing else is.
    duplicates = 0
    for group in association_groups([h for h in records if not is_self_link(h)]):
        group.sort(key=strength)
        duplicates += len(group) - 1
        artifacts.update(id(h) for h in group[1:])
    self_links = len(self_link_records)
    sample = [
        f"{h.get('entity_a')} ↔ {h.get('entity_b')}"
        for h in sorted(records, key=strength)
        if id(h) in artifacts
    ][:_SAMPLE_LIMIT]
    top = sorted(records, key=lambda h: -int(h.get("co_occurrence_count") or 0))[:HALLWAY_TOP_N]
    top_artifacts = sum(1 for h in top if id(h) in artifacts)
    return {
        "total": total,
        "self_links": self_links,
        "duplicates": duplicates,
        "artifact_share": round((self_links + duplicates) / total, 4) if total else 0.0,
        "top_n": len(top),
        "top_artifacts": top_artifacts,
        "top_artifact_share": round(top_artifacts / len(top), 4) if top else 0.0,
        "artifact_sample": sample,
    }


# ── scoring ──────────────────────────────────────────────────────────────────


def _clamp(value: float) -> int:
    return int(round(max(0.0, min(100.0, value))))


def _score(rooms: dict, naming: dict, tunnels: dict, hallways: dict, kg: Optional[dict]) -> dict:
    """0–100 per layer, ``None`` where the layer is empty, and the mean.

    Each score is the share of that layer that carries navigational
    information. They are deliberately simple so a number can be tracked
    across releases rather than argued about.
    """
    scores: dict[str, Optional[int]] = {}
    scores["rooms"] = _clamp(100 * (1 - rooms["generic_share"])) if rooms["total_drawers"] else None
    # Prefix pairs are informational: ``mempalace`` / ``mempalace-ts`` are
    # legitimately two wings, so they are reported but never scored.
    drift_groups = (
        len(naming["wing_drift"]) + len(naming["room_drift"]) + len(naming["mixed_wings"])
    )
    scores["naming"] = _clamp(100 - 10 * drift_groups) if rooms["wings"] else None
    # Quality × coverage: `tunnels prune` raises the first factor, `tunnels
    # propose` the second. Traversal is not scored — it only rises with use.
    # A palace whose wings share nothing (or has a single wing) has no
    # tunnel layer to score.
    scores["tunnels"] = (
        _clamp(100 * (1 - tunnels["artifact_share"]) * tunnels["coverage"])
        if tunnels["total"] or tunnels["linkable_wings"]
        else None
    )
    scores["hallways"] = (
        _clamp(100 * (1 - hallways["top_artifact_share"])) if hallways["total"] else None
    )
    scores["knowledge_graph"] = (
        _clamp(100 * kg["predicate_reuse"]) if kg and kg["triples"] else None
    )
    present = [s for s in scores.values() if s is not None]
    scores["overall"] = _clamp(sum(present) / len(present)) if present else None
    return scores


# ── findings ─────────────────────────────────────────────────────────────────


def _name_list(names: list[str], keep: int = 6) -> str:
    """``a, b, c, +N more`` — long lists belong in --json, not in a sentence."""
    if len(names) <= keep:
        return ", ".join(names)
    return ", ".join(names[:keep]) + f", +{len(names) - keep} more"


def _findings(rooms, naming, tunnels, hallways, kg) -> list[dict]:
    """``{"layer", "text"}`` sentences an operator can act on, worst first."""
    out: list[dict] = []

    def add(layer, text):
        out.append({"layer": layer, "text": text})

    if rooms["total_drawers"] and rooms["generic_share"] >= 0.5:
        add(
            "rooms",
            f"{rooms['generic_drawers']} of {rooms['total_drawers']} drawers "
            f"({rooms['generic_share']:.0%}) sit in generic rooms; "
            "the room layer carries little information.",
        )
    for f in rooms["flat_wings"][:3]:
        add(
            "rooms",
            f"{f['wing']} keeps {f['share']:.0%} of its {f['drawers']} drawers in {f['room']}.",
        )
    if len(rooms["flat_wings"]) > 3:
        extra = len(rooms["flat_wings"]) - 3
        add("rooms", f"{extra} more flat wing{'s' if extra != 1 else ''}; see --json.")

    for group in naming["wing_drift"]:
        add("naming", f"{' / '.join(group)}: one wing spelled two ways.")
    for d in naming["room_drift"][:5]:
        add(
            "naming",
            f"{' / '.join(d['rooms'])}: one room spelled two ways "
            f"(in {_name_list(d['wings'], 4)}).",
        )
    if len(naming["room_drift"]) > 5:
        extra = len(naming["room_drift"]) - 5
        add("naming", f"{extra} more room spelling group{'s' if extra != 1 else ''}; see --json.")
    if naming["wing_prefix_pairs"]:
        pairs = [f"{a} / {b}" for a, b in naming["wing_prefix_pairs"]]
        add("naming", f"May be one project filed twice (not scored): {_name_list(pairs, 5)}.")
    for m in naming["mixed_wings"][:4]:
        top = ", ".join(f"{k} ({n})" for k, n in m["top"][:3])
        add(
            "naming",
            f"{m['wing']} mixes {m['projects']} projects in {m['drawers']} drawers "
            f"(largest: {top}); run `mempalace wings split --wing {m['wing']}`.",
        )
    if len(naming["mixed_wings"]) > 4:
        add("naming", f"{len(naming['mixed_wings']) - 4} more mixed wings; see --json.")
    if len(naming["tiny_wings"]) >= 3:
        names = [t["wing"] for t in naming["tiny_wings"]]
        add(
            "naming",
            f"{len(names)} wings hold {TINY_WING_MAX} drawers or fewer: {_name_list(names)}.",
        )

    if tunnels["total"] and tunnels["artifact_share"] >= 0.1:
        add(
            "tunnels",
            f"{tunnels['artifacts']} of {tunnels['total']} tunnels are artifacts "
            f"({tunnels['dangling_endpoints']} dangling endpoints, {tunnels['duplicates']} "
            f"duplicate spellings, generic tokens below); run `mempalace tunnels prune`.",
        )
    if tunnels["unlinked_wings"]:
        add(
            "tunnels",
            f"{len(tunnels['unlinked_wings'])} of {tunnels['linkable_wings']} wings share "
            f"entities with another wing but no tunnel reaches them: "
            f"{_name_list(tunnels['unlinked_wings'])}; run `mempalace tunnels propose`.",
        )
    elif not tunnels["total"]:
        add("tunnels", "No tunnels; run `mempalace tunnels propose` to link related wings.")
    if tunnels["total"] and tunnels["never_traversed_share"] >= 0.9:
        add(
            "tunnels",
            f"{tunnels['never_traversed']} of {tunnels['total']} tunnels have never been "
            "followed (not scored; mempalace_follow_tunnels records each crossing).",
        )
    if tunnels["generic_entities"]:
        add(
            "tunnels",
            f"Tunnels on generic entity names: {_name_list(tunnels['generic_entities'])}.",
        )

    if hallways["total"] and hallways["top_artifact_share"] >= 0.1:
        add(
            "hallways",
            f"{hallways['top_artifacts']} of the {hallways['top_n']} strongest hallways "
            f"({hallways['top_artifact_share']:.0%}) are spelling artifacts: an entity paired "
            f"with its own path or file spelling, or a duplicate of another spelling. "
            f"{hallways['self_links']} self-links and {hallways['duplicates']} duplicates "
            f"of {hallways['total']} overall; run `mempalace hallways --prune-spellings`.",
        )

    if kg and kg["triples"]:
        if kg["predicate_reuse"] < 0.5:
            add(
                "knowledge graph",
                f"{kg['distinct_predicates']} predicates for {kg['current_facts']} open facts "
                f"({kg['one_off_predicates']} used once); not queryable by relation.",
            )
    elif kg is None:
        add("knowledge graph", "No knowledge graph file found.")
    else:
        add("knowledge graph", "Knowledge graph is empty.")
    return out


# ── entry point ──────────────────────────────────────────────────────────────


def audit_palace(
    palace_path: Optional[str] = None,
    config: Optional[MempalaceConfig] = None,
    progress=None,
    explicit_palace: Optional[bool] = None,
) -> dict:
    """Run every read-only check and return the report as a dict.

    ``explicit_palace`` says the caller passed ``--palace``; see
    :func:`resolve_kg_path` for which knowledge graph belongs to a palace.

    ``progress(step, detail)`` is called before and after each reader — the
    hallway sidecar alone can be tens of megabytes, and a caller on a terminal
    wants to see that something is happening. ``detail`` is ``None`` at the
    start of a step and a short result string at its end.

    Raises ``FileNotFoundError`` when the palace directory does not exist and
    ``RuntimeError`` when drawer counts cannot be read from either the sqlite
    fast path or the collection.
    """

    def step(name, fn, describe):
        if progress:
            progress(name, None)
        value = fn()
        if progress:
            progress(name, describe(value))
        return value

    if config is None:
        config = MempalaceConfig(palace_path=palace_path) if palace_path else MempalaceConfig()
    palace_path = config.palace_path
    if not os.path.isdir(palace_path):
        raise FileNotFoundError(f"palace not found at {palace_path}")
    explicit = bool(explicit_palace)

    source = "sqlite"

    def read_counts():
        nonlocal source
        counts = _read_wing_room_counts(config)
        if counts is None:
            counts = _read_wing_room_counts_from_collection(config)
            source = "collection"
        if counts is None:
            raise RuntimeError(f"could not read drawer counts from palace at {palace_path}")
        return counts

    wing_rooms = step(
        "drawers",
        read_counts,
        lambda c: f"{sum(sum(r.values()) for r in c.values())} in {len(c)} wings",
    )
    tunnel_records = step("tunnels", lambda: _load_tunnels(config), lambda t: str(len(t)))
    hallway_records = step("hallways", lambda: list_hallways(config=config), lambda h: str(len(h)))
    kg = step(
        "knowledge graph",
        lambda: _read_kg(resolve_kg_path(palace_path, explicit=explicit)),
        lambda k: f"{k['triples']} facts" if k else "no file",
    )

    rooms = _analyze_rooms(wing_rooms)
    project_mix = step(
        "source projects",
        lambda: _read_wing_project_mix(config),
        lambda m: f"{len(m)} transcript wings" if m is not None else "n/a",
    )
    naming = _analyze_naming(wing_rooms, project_mix)
    tunnels = _analyze_tunnels(tunnel_records, wings=set(wing_rooms), hallways=hallway_records)
    hallways = _analyze_hallways(hallway_records)

    return {
        "palace": palace_path,
        "counts_source": source,
        "scores": _score(rooms, naming, tunnels, hallways, kg),
        "rooms": rooms,
        "naming": naming,
        "tunnels": tunnels,
        "hallways": hallways,
        "knowledge_graph": kg,
        "findings": _findings(rooms, naming, tunnels, hallways, kg),
    }


# ── rendering ────────────────────────────────────────────────────────────────

_LAYERS = (
    ("overall", "overall", ""),
    ("rooms", "rooms", "drawers outside generic rooms"),
    ("naming", "naming", "wing/room spellings that do not collide"),
    ("tunnels", "tunnels", "sound tunnels x linkable wings they reach"),
    ("hallways", "hallways", "strongest hallways that are not spelling artifacts"),
    ("knowledge_graph", "knowledge graph", "predicate reuse across facts"),
)


def _bar(score: Optional[int], width: int = 10) -> str:
    if score is None:
        return " " * width
    filled = int(round(score / 100 * width))
    return "#" * filled + "." * (width - filled)


def render_audit(report: dict, width: Optional[int] = None) -> str:
    """Terminal rendering of :func:`audit_palace`'s dict.

    Wraps to ``width`` (default: the terminal's, capped at 100 columns) with a
    hanging indent, so a finding stays one visual unit on a narrow terminal.
    """
    if width is None:
        width = min(shutil.get_terminal_size((80, 24)).columns, 100)
    width = max(width, 40)
    s = report["scores"]
    r = report["rooms"]

    lines = ["", f"MemPalace audit: {report['palace']}", ""]
    for key, label, meaning in _LAYERS:
        score = s.get(key)
        num = "n/a" if score is None else f"{score:>3d}"
        lines.append(f"  {label:<16}{num:>4}  {_bar(score)}  {meaning}".rstrip())
    lines.append("")
    lines.append(
        f"  {r['total_drawers']} drawers, {r['wings']} wings, {r['distinct_rooms']} rooms, "
        f"{report['tunnels']['total']} tunnels, {report['hallways']['total']} hallways, "
        f"{(report['knowledge_graph'] or {}).get('triples', 0)} facts"
    )
    lines.append("")

    findings = report["findings"]
    if not findings:
        lines.append("  No findings. The palace is well organized.")
    current = None
    for f in findings:
        if f["layer"] != current:
            current = f["layer"]
            lines.append(f"  {current.upper()}")
        lines.append(
            textwrap.fill(
                f["text"],
                width=width,
                initial_indent="    - ",
                subsequent_indent="      ",
                break_long_words=False,
                break_on_hyphens=False,
            )
        )
    lines.append("")
    return "\n".join(lines)


def audit_to_json(report: dict) -> str:
    return json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True)


def terminal_progress(stream):
    """A ``progress`` callback that prints one timed line per reader.

    Meant for stderr on an interactive terminal: it never lands in ``--json``
    output or a redirected report.
    """
    started = {}

    def cb(step, detail):
        if detail is None:
            started[step] = time.monotonic()
            stream.write(f"  reading {step}...")
            stream.flush()
        else:
            elapsed = time.monotonic() - started.get(step, time.monotonic())
            stream.write(f" {detail} ({elapsed:.1f}s)\n")
            stream.flush()

    return cb
