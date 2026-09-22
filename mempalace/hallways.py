"""Hallways — within-wing entity-to-entity connectors.

A **hallway** is a connection between two entities (people, projects,
concepts, interests) inside one wing, materialized from their
co-occurrence across that wing's drawers. Conceptually:

    WING → has DRAWERS (each tagged with entities)
            entities → connected to other entities by HALLWAYS
                       (within-wing, built from drawer co-occurrence)
                       hallways → are the primitive
                                   tunnels → use hallways to spawn
                                             cross-wing connections

If Aya and Lumi are both mentioned in 47 drawers across the diary,
letters, and ideas rooms, there's a hallway between them. If Aya
and "consciousness" co-occur in 19 drawers, there's a hallway between
them too. The hallway *is* the structural fact of "these two entities
travel together inside this wing."

Mempalace's tunnel primitive in ``palace_graph.py`` connects rooms
across wings. This module fills the within-wing gap with an
entity-centric (not room-centric) model: hallways are about *who/what
relates to whom/what*, not *which rooms relate to which*. A planned
follow-up PR will refactor ``_compute_topic_tunnels_for_wing`` to
build cross-wing tunnels from hallway data (Wing → Drawer-entities →
Hallway → Tunnel).

Persistence mirrors ``palace_graph._TUNNEL_FILE``: a JSON file under
``~/.mempalace/`` so the records survive across mines and are
inspectable / editable by hand if needed.
"""

from __future__ import annotations

import hashlib
import json
import re
import logging
import os
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from itertools import combinations
from typing import Optional

from .dynamics import initialize_dynamics_fields

logger = logging.getLogger("mempalace_hallways")

# Persistence target is resolved through ``_get_hallway_file`` below, which
# mirrors ``palace_graph._get_tunnel_file`` (the 3.3.6 palace-scoped pattern)
# so the storage layout is uniform across the two related primitives. Tests
# should monkey-patch ``_get_hallway_file`` and ``_legacy_hallway_file`` rather
# than poking a module-level constant.

_SCHEMA_VERSION = 1


__all__ = [
    "compute_hallways_for_wing",
    "list_hallways",
    "delete_hallway",
]


# ─────────────────────────────────────────────────────────────────────────────
# Persistence — JSON file resolved from MempalaceConfig.hallway_file,
# restricted perms (0600) on POSIX. Pre-3.3.6 behavior (hardcoded
# ~/.mempalace/hallways.json) is kept only as a one-time orphan detection
# fallback, matching the palace_graph tunnel-file migration pattern.
# ─────────────────────────────────────────────────────────────────────────────


def _get_hallway_file(config=None) -> str:
    """Return the path to the hallways.json file, derived from MempalaceConfig.palace_path."""
    from .config import MempalaceConfig

    config = config or MempalaceConfig()
    return config.hallway_file


def _legacy_hallway_file() -> str:
    """The pre-palace-scoped hardcoded path. Kept only for one-time orphan detection."""
    return os.path.join(os.path.expanduser("~"), ".mempalace", "hallways.json")


def _load_hallways(config=None) -> list[dict]:
    """Read all hallway records. Returns ``[]`` if the file is missing or corrupt.

    Backwards-compatibility: prior to this migration the hallway file was
    hardcoded at ``~/.mempalace/hallways.json`` regardless of the configured
    palace_path. If the configured hallway file is missing but a legacy file
    exists at a different path, log a one-line warning naming both paths so
    users can move the file manually. We do NOT auto-migrate — auto-merging
    hallway state across two locations is too magical for a bugfix and risks
    clobbering newer data. Same posture as ``palace_graph._load_tunnels``.
    """
    current_hallway_file = _get_hallway_file(config)
    if os.path.exists(current_hallway_file):
        try:
            with open(current_hallway_file, encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, json.JSONDecodeError):
            logger.debug("hallways: load failed, treating as empty", exc_info=True)
            return []
        if isinstance(raw, dict) and "hallways" in raw:
            return raw.get("hallways") or []
        if isinstance(raw, list):
            return raw
        return []

    legacy = _legacy_hallway_file()
    if legacy != current_hallway_file and os.path.exists(legacy):
        logger.warning(
            "Legacy hallways file at '%s' is being ignored; configured location is '%s'. "
            "Move or copy the legacy file to the configured path to recover its hallways.",
            legacy,
            current_hallway_file,
        )
    return []


def _save_hallways(hallways: list[dict], config=None) -> None:
    """Atomically persist hallway records to the configured hallway file.

    Uses an os.replace temp-file dance so a crash mid-write doesn't
    corrupt the file. POSIX permission is restricted to 0600 because
    hallways reveal within-wing entity connections that the user may
    not want world-readable.
    """
    hallway_file = _get_hallway_file(config)
    directory = os.path.dirname(hallway_file)
    os.makedirs(directory, exist_ok=True)
    payload = {
        "schema_version": _SCHEMA_VERSION,
        "hallways": list(hallways),
    }
    fd, tmp_path = tempfile.mkstemp(prefix=".hallways-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        try:
            os.chmod(tmp_path, 0o600)
        except OSError:
            # Non-POSIX systems may not support chmod; not fatal.
            pass
        os.replace(tmp_path, hallway_file)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ─────────────────────────────────────────────────────────────────────────────
# Core algorithm — compute entity-pair hallways for one wing
# ─────────────────────────────────────────────────────────────────────────────


# File extensions stripped when deciding whether two entity spellings name
# the same file. A fixed set on purpose: ``ChatStore`` and ``ChatStore.send``
# are different entities and must not collapse.
_CODE_EXTENSIONS = frozenset(
    "py js ts tsx jsx mjs cjs zig swift rs go md json yaml yml toml sh c h cpp hpp "
    "java kt rb php html css sql txt cs vue svelte".split()
)


def entity_spelling_key(entity: str) -> str:
    """Basename without a known code extension, lower-cased.

    ``src/main.zig``, ``main.zig`` and ``/Users/x/proj/src/main.zig`` all key
    to ``main``; ``mcp_server`` and ``mcp_server.py`` both key to
    ``mcp_server``. Two entities sharing a key are one thing spelled two
    ways, so a hallway between them is the entity co-occurring with itself,
    not an association. Used by the miner to skip such pairs and by
    ``mempalace audit`` / ``mempalace hallways --prune-self-links`` to find
    the ones older mines already wrote.
    """
    base = str(entity).replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    stem, dot, ext = base.rpartition(".")
    if dot and stem and ext.lower() in _CODE_EXTENSIONS:
        base = stem
    return base.lower()


_GENERIC_ENTITY_RE = re.compile(r"[a-z]{2,8}")

# Names that appear in every coding transcript and identify no project: the
# harness's tool names, generic nouns, and files every repo has. Matched
# case-insensitively after stripping a trailing slash.
GENERIC_ENTITY_STOPLIST = frozenset(
    """
    bash read write edit grep glob task agent websearch webfetch toolsearch
    structuredoutput askuserquestion skill monitor notebookedit todowrite
    app server service client gateway api handler controller model view
    config settings utils util helpers helper index main core common base
    test tests spec fixture mock github github.com gitlab git npm pip uv
    docker dockerfile compose.yml docker-compose.yml package.json package-lock.json
    tsconfig.json pyproject.toml requirements.txt readme readme.md changelog.md
    license .env .gitignore makefile lib src dist build node_modules
    created_at updated_at id name type value data result results error errors
    """.split()
)


def is_generic_entity(name: str) -> bool:
    """A name that identifies no project: a short lower-case word (``content``,
    ``thinking``), a harness tool (``WebFetch``), a generic noun (``Server``)
    or a file every repo has (``compose.yml``).

    Symbols (``ChatStore``), qualified names (``store.baseURL``) and project
    names pass; the boundary with a short lower-case project name is fuzzy
    by construction. Cross-wing ubiquity is judged separately, where the
    wing counts are known.
    """
    text = str(name).strip()
    if _GENERIC_ENTITY_RE.fullmatch(text):
        return True
    # A bare single-segment path (``/app``, ``/model``) or a shouting constant
    # (``MESSAGES``, ``TEMPLATES``) is structure every project has.
    if re.fullmatch(r"/[A-Za-z0-9_-]+/?", text) or re.fullmatch(r"[A-Z][A-Z0-9_]{2,15}", text):
        return True
    # A lone lower-case English word of any length (``cancelled``,
    # ``operations``) is vocabulary; project names are the exception and are
    # usually short, which the first rule already accepts as generic too.
    if re.fullmatch(r"[a-z]{9,12}", text) and not any(c in text for c in "._-/"):
        return True
    return text.rstrip("/").lower() in GENERIC_ENTITY_STOPLIST


def is_self_link(record) -> bool:
    """True when a hallway record joins two spellings of one entity."""
    if not isinstance(record, dict):
        return False
    a, b = record.get("entity_a"), record.get("entity_b")
    if a is None or b is None:
        return False
    return entity_spelling_key(a) == entity_spelling_key(b)


def canonical_entities(entities: list[str]) -> list[str]:
    """One spelling per entity, in first-seen order.

    The structural extractor records a file as both its path and its
    basename, so a drawer's entity list holds ``src/main.zig`` and
    ``main.zig`` side by side. Pairing those raw spellings wrote a hallway
    from the entity to itself and four copies of every real association
    (``ChatStore`` × ``RootView`` under each spelling combination). The
    shortest spelling wins, so hallways read ``ChatStore ↔ RootView``.
    """
    chosen: dict[str, str] = {}
    for entity in entities:
        key = entity_spelling_key(entity)
        current = chosen.get(key)
        if current is None or len(entity) < len(current):
            chosen[key] = entity
    return list(chosen.values())


def _parse_entities(value) -> list[str]:
    """Drawer ``entities`` metadata is a semicolon-separated string. Parse it.

    Returns a deterministic *list* (not a set) because order matters for
    the deduplication semantics below: a drawer that mentions ``Aya;Aya``
    should only contribute one Aya to the entity set for that drawer.
    """
    if not value:
        return []
    if isinstance(value, (list, tuple, set)):
        items = [str(v).strip() for v in value if str(v).strip()]
    elif isinstance(value, str):
        items = [v.strip() for v in value.split(";") if v.strip()]
    else:
        return []
    # Dedupe while preserving first-seen order so id derivation is stable.
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _hallway_id(wing: str, entity_a: str, entity_b: str) -> str:
    """Deterministic id derived from wing + sorted entity pair.

    Sorting before hashing makes the id symmetric — (Aya, Lumi) and
    (Lumi, Aya) produce the same record. So an idempotent re-mine
    upserts the same hallway instead of creating two parallel records.
    """
    a, b = sorted([entity_a, entity_b])
    key = f"{wing}::{a}::{b}".encode("utf-8")
    suffix = hashlib.sha256(key).hexdigest()[:8]
    return f"hallway_{wing}_{a}_{b}_{suffix}"


def compute_hallways_for_wing(
    wing: str,
    col=None,
    min_count: int = 2,
    config=None,
) -> list[dict]:
    """Compute entity-pair hallways for one wing.

    Algorithm:
      1. Query drawers for ``wing`` from ``col``.
      2. For each drawer with entities, every pair of distinct entities in
         that drawer is one co-occurrence. Increment a counter for each
         pair; also record the room the drawer lives in.
      3. For each (entity_a, entity_b) pair whose co-occurrence count is
         ``>= min_count``, materialize a hallway record. The record
         carries the pair, the count, and the set of rooms where they
         co-occurred (useful context for navigation).
      4. Persist the full hallway list (records for other wings preserved,
         this wing's records replaced) and return the just-computed list.

    Args:
        wing: wing name to scan.
        col: ChromaDB collection — must support paginated
            ``.get(where={"wing": ...}, limit=..., offset=..., include=...)``.
            The fetch is scoped to ``wing`` server-side AND paginated: an
            unbounded ``.get(where=...)`` binds one SQL variable per matched
            id and overflows SQLite's ``SQLITE_MAX_VARIABLE_NUMBER`` on wings
            above ~32k drawers (#1619), while an unscoped page walk costs
            O(total palace drawers) on every mine, pegging the CPU for
            minutes on large palaces (#2466). A bounded page never binds more
            than ``batch_size`` ids. Fake collections and alternate backends
            must implement this shape. If ``None``, returns ``[]`` (caller
            didn't supply a backing store, so nothing to compute against).
            Tests pass a controlled MagicMock.
        min_count: minimum co-occurrence count required to materialize a
            hallway between two entities. Default 2 — single co-occurrences
            are noise (entities mentioned together once in one drawer);
            two or more is a real signal. Clamped to ``>=1``.
        config: Optional ``MempalaceConfig`` selecting the palace-scoped
            hallway sidecar. Callers using an explicit palace path must pass
            the matching config so derived graph state cannot leak into the
            default palace.

    Returns:
        List of hallway dicts created for this wing. Records for other
        wings already on disk are preserved.
    """
    if col is None:
        logger.debug("compute_hallways_for_wing: no collection provided for %s", wing)
        return []

    min_count = max(1, int(min_count))

    # 1. Query drawers for this wing: scoped to the wing server-side AND
    #    paginated. An unbounded get(where={"wing": wing}) binds one SQL
    #    variable per matched id and overflows SQLite's
    #    SQLITE_MAX_VARIABLE_NUMBER (32766) on wings > ~32k drawers (#1619);
    #    a bounded page binds at most batch_size ids. Walking the WHOLE
    #    collection instead and filtering client-side cost O(total palace
    #    drawers) on every mine, so filing one small session into an 800k-
    #    drawer palace pegged the CPU for minutes (#2466). The client-side
    #    wing check stays as a guard for stores that ignore ``where``. The
    #    loop ends on a short page: count() counts every wing, so it cannot
    #    bound a scoped walk.
    metadatas: list = []
    try:
        batch_size = 5000
        offset = 0
        while True:
            batch = col.get(
                where={"wing": wing},
                limit=batch_size,
                offset=offset,
                include=["metadatas"],
            )
            batch_metas = (batch or {}).get("metadatas") or []
            if not batch_metas:
                break
            metadatas.extend(
                m for m in batch_metas if isinstance(m, dict) and m.get("wing") == wing
            )
            offset += len(batch_metas)
            if len(batch_metas) < batch_size:
                break
    except Exception:
        logger.warning(
            "compute_hallways_for_wing: collection fetch failed for %s", wing, exc_info=True
        )
        return []

    if not metadatas:
        return []

    # 2. Walk drawers, counting entity-pair co-occurrence + tracking rooms.
    # pair_counts: {(entity_a, entity_b): count} — keys always sorted to
    # canonicalize the (a, b) vs (b, a) symmetry.
    # Pairs are keyed by spelling key, not raw spelling: the structural
    # extractor records a file as both path and basename, and one drawer may
    # say ``ChatStore.swift`` where the next says ``ChatStore``. ``display``
    # remembers the shortest spelling seen wing-wide so the materialized
    # record reads ``ChatStore ↔ RootView``.
    pair_counts: dict[tuple[str, str], int] = defaultdict(int)
    pair_rooms: dict[tuple[str, str], set[str]] = defaultdict(set)
    display: dict[str, str] = {}

    for meta in metadatas:
        if not isinstance(meta, dict):
            continue
        # Sentinel drawers carry no real content — skip them.
        if meta.get("is_sentinel"):
            continue
        entities = []
        for spelling in canonical_entities(_parse_entities(meta.get("entities"))):
            key = entity_spelling_key(spelling)
            if key not in display or len(spelling) < len(display[key]):
                display[key] = spelling
            entities.append(key)
        if len(entities) < 2:
            # Need at least 2 entities for a pair to exist.
            continue
        room = meta.get("room")
        room_str = room if isinstance(room, str) and room.strip() else None

        # Each unordered pair of distinct entities in this drawer is one
        # co-occurrence. itertools.combinations already gives unordered
        # pairs without repetition.
        for a, b in combinations(entities, 2):
            # Canonicalize order so (Aya, Lumi) and (Lumi, Aya) are the
            # same key. Skip self-pairs, including the same entity under two
            # spellings (``main.zig`` / ``src/main.zig``): the structural
            # extractor records both the path and the basename, and a
            # hallway between them is an entity paired with itself.
            if a == b:
                continue
            key = tuple(sorted([a, b]))
            pair_counts[key] += 1
            if room_str:
                pair_rooms[key].add(room_str)

    if not pair_counts:
        return []

    # 3. Materialize hallway records for pairs above the threshold.
    #    Before building, load existing records so we can PRESERVE L7
    #    dynamics fields (strength, stability, last_activated, access_count)
    #    across recomputes. Without this preservation, every mine wipes
    #    the connection weights accumulated through use — defeating the
    #    living-connection layer entirely.
    existing = _load_hallways(config)
    existing_dynamics_lookup: dict = {}
    for h in existing:
        if h.get("wing") != wing:
            continue
        # Canonicalize the lookup key by sorting the entity pair — must
        # match the symmetric ID generation in _hallway_id (which also
        # sorts). Without this, a persisted record with reversed entity
        # order would silently miss the lookup and lose its accumulated
        # dynamics on every recompute. Per PR #1578 review
        # (gemini-code-assist, HIGH priority).
        key = tuple(
            sorted(
                [
                    entity_spelling_key(str(h.get("entity_a"))),
                    entity_spelling_key(str(h.get("entity_b"))),
                ]
            )
        )
        # Only copy the fields the dynamics layer cares about; everything
        # else is recomputed deterministically from the drawer set.
        existing_dynamics_lookup[key] = {
            k: h[k] for k in ("strength", "stability", "last_activated", "access_count") if k in h
        }

    created: list[dict] = []
    created_at = datetime.now(timezone.utc).isoformat()
    for key in sorted(pair_counts.keys()):
        count = pair_counts[key]
        if count < min_count:
            continue
        entity_a, entity_b = sorted((display[key[0]], display[key[1]]))
        rooms = sorted(pair_rooms.get(key, set()))
        room_summary = ", ".join(rooms[:3]) if rooms else "(no room tags)"
        if len(rooms) > 3:
            room_summary += f", +{len(rooms) - 3} more"
        record = {
            "id": _hallway_id(wing, entity_a, entity_b),
            "wing": wing,
            "entity_a": entity_a,
            "entity_b": entity_b,
            "co_occurrence_count": count,
            "rooms": rooms,
            "label": f"{entity_a} ↔ {entity_b} (co-occur in {count} drawers across {len(rooms) or 'no'} room{'s' if len(rooms) != 1 else ''}: {room_summary})",
            "created_at": created_at,
            "created_by": "auto",
        }
        # Apply preserved dynamics if this entity pair existed in the
        # prior wing snapshot. Then initialize any still-missing fields
        # (the new-pair case + the legacy-record case both land cleanly).
        preserved = existing_dynamics_lookup.get(key, {})
        record.update(preserved)
        initialize_dynamics_fields(record)
        created.append(record)

    # 4. Persist — preserve other-wing records, replace this wing's records.
    preserved_other_wings = [h for h in existing if h.get("wing") != wing]
    _save_hallways(preserved_other_wings + created, config)

    return created


# ─────────────────────────────────────────────────────────────────────────────
# Query API — list_hallways, delete_hallway
# ─────────────────────────────────────────────────────────────────────────────


def list_hallways(wing: Optional[str] = None, config=None) -> list[dict]:
    """List hallway records. Filter by ``wing`` if specified."""
    all_hallways = _load_hallways(config)
    if wing is None:
        return list(all_hallways)
    return [h for h in all_hallways if h.get("wing") == wing]


def prune_spelling_hallways(config=None, apply: bool = False) -> dict:
    """Find (and with ``apply``) remove hallways that older mines wrote per spelling.

    Two defects, one cause: before :func:`canonical_entities` the miner paired
    raw spellings, so every code wing has ``main.zig ↔ src/main.zig``
    (an entity joined to itself) and four copies of ``ChatStore ↔ RootView``
    (one per spelling combination). Self-links are dropped; of each variant
    group the record with the highest co-occurrence count survives under
    its shortest spellings and the rest are dropped. Only the sidecar file
    is touched, never a drawer. ``removed`` is 0 on a dry run.
    """
    hallways = _load_hallways(config)
    self_links = [h for h in hallways if is_self_link(h)]
    groups: dict[tuple, list[dict]] = {}
    for h in hallways:
        if not isinstance(h, dict) or is_self_link(h):
            continue
        a, b = str(h.get("entity_a")), str(h.get("entity_b"))
        key = (str(h.get("wing") or ""), *sorted((entity_spelling_key(a), entity_spelling_key(b))))
        groups.setdefault(key, []).append(h)
    duplicates: list[dict] = []
    kept: list[dict] = []
    for members in groups.values():
        if len(members) == 1:
            kept.append(members[0])
            continue
        members.sort(key=lambda h: -int(h.get("co_occurrence_count") or 0))
        survivor = dict(members[0])
        spellings_a = canonical_entities([str(m.get("entity_a")) for m in members])
        spellings_b = canonical_entities([str(m.get("entity_b")) for m in members])
        if len(spellings_a) == 1 and len(spellings_b) == 1:
            survivor["entity_a"], survivor["entity_b"] = spellings_a[0], spellings_b[0]
            survivor["id"] = _hallway_id(
                survivor["wing"], survivor["entity_a"], survivor["entity_b"]
            )
        kept.append(survivor)
        duplicates.extend(members[1:])

    by_wing: dict[str, int] = {}
    for h in self_links + duplicates:
        wing = str(h.get("wing") or "?")
        by_wing[wing] = by_wing.get(wing, 0) + 1
    self_links.sort(key=lambda h: -int(h.get("co_occurrence_count") or 0))
    duplicates.sort(key=lambda h: -int(h.get("co_occurrence_count") or 0))
    sample = [f"{h.get('entity_a')} ↔ {h.get('entity_b')}" for h in (self_links + duplicates)[:10]]
    doomed = len(self_links) + len(duplicates)
    removed = 0
    if apply and doomed:
        _save_hallways(kept, config)
        removed = doomed
    return {
        "total": len(hallways),
        "self_links": len(self_links),
        "duplicates": len(duplicates),
        "by_wing": dict(sorted(by_wing.items(), key=lambda kv: -kv[1])),
        "sample": sample,
        "removed": removed,
    }


def delete_hallway(hallway_id: str, config=None) -> bool:
    """Remove one hallway record by id. Returns True if a record was removed."""
    hallways = _load_hallways(config)
    filtered = [h for h in hallways if h.get("id") != hallway_id]
    if len(filtered) == len(hallways):
        return False
    _save_hallways(filtered, config)
    return True
