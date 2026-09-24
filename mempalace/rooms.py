"""Closed room sets for a wing: propose with an LLM, assign with a fast decider.

The transcript miner files nearly everything into five keyword rooms
(``technical``, ``architecture``, ``planning``, ``problems``, ``general``), so
on a real palace the room layer carries no information (``mempalace audit``
scores it near zero). This module fixes that in two decoupled steps:

1. **propose** — sample drawers from a wing, hand the sample to the
   configured LLM (local by default: Ollama, vLLM, NInfer, any
   OpenAI-compatible endpoint; an external provider only when the user
   configured one explicitly) and get back a *closed* room set: a slug, a
   one-line description and a few keywords per room. The user edits and
   approves the file before anything moves.

2. **apply** — assign every drawer in the wing to one room of that set with a
   :class:`RoomDecider`. The default decider embeds each room's description
   with the palace embedder and picks the nearest room by cosine against the
   drawer's *stored* embedding, so no drawer is re-embedded and no LLM call
   is made per drawer. A drawer whose best similarity is below the threshold
   keeps its current room. Only ``room`` metadata changes; content never.

``RoomDecider`` is deliberately tiny — ``decide(rows) -> [(room, confidence)]``
— so a calibrated structured-decision model (a System One / Jev-style
endpoint served locally) can replace the embedding decider without touching
the pipeline: it returns the same ``(room, probability)`` pairs.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import random
import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Optional, Protocol

from .config import MempalaceConfig, sanitize_name

ROOM_SET_SCHEMA_VERSION = 1
DEFAULT_SAMPLE_SIZE = 60
DEFAULT_MAX_ROOMS = 12
DEFAULT_THRESHOLD = 0.30
# Rooms the transcript miner falls back to. ``apply`` only reclassifies
# drawers sitting in these by default: a drawer someone filed into
# ``decisions`` or ``diary`` was placed on purpose and stays put.
GENERIC_ROOMS = frozenset({"general", "technical", "architecture", "planning", "problems"})
_PAGE_SIZE = 2000
_UPDATE_BATCH = 500
_SAMPLE_CHARS = 700
_EXAMPLES_PER_ROOM = 5


# ── room set ─────────────────────────────────────────────────────────────────


@dataclass
class RoomSpec:
    name: str
    description: str
    keywords: list[str] = field(default_factory=list)
    # Drawer ids the LLM assigned to this room in the sample. Their stored
    # embeddings average into the room's prototype, so classification runs in
    # the palace's own vector space instead of against a one-line description.
    exemplars: list[str] = field(default_factory=list)

    def prototype_text(self) -> str:
        """The text that is embedded to stand for this room."""
        parts = [self.name.replace("-", " "), self.description]
        if self.keywords:
            parts.append(", ".join(self.keywords))
        return ". ".join(p for p in parts if p)


@dataclass
class RoomSet:
    wing: str
    rooms: list[RoomSpec]
    proposed_by: str = ""
    proposed_at: str = ""
    sample_size: int = 0

    def names(self) -> list[str]:
        return [r.name for r in self.rooms]

    def to_dict(self) -> dict:
        return {
            "schema_version": ROOM_SET_SCHEMA_VERSION,
            "wing": self.wing,
            "proposed_by": self.proposed_by,
            "proposed_at": self.proposed_at,
            "sample_size": self.sample_size,
            "rooms": [asdict(r) for r in self.rooms],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "RoomSet":
        rooms = []
        for raw in data.get("rooms") or []:
            if not isinstance(raw, dict):
                continue
            name = sanitize_name(slugify_room(str(raw.get("name") or "")), "room")
            rooms.append(
                RoomSpec(
                    name=name,
                    description=str(raw.get("description") or "").strip(),
                    keywords=[
                        str(k).strip() for k in (raw.get("keywords") or []) if str(k).strip()
                    ],
                    exemplars=[str(e) for e in (raw.get("exemplars") or []) if str(e).strip()],
                )
            )
        if not rooms:
            raise ValueError("room set has no rooms")
        seen = set()
        for r in rooms:
            if r.name in seen:
                raise ValueError(f"room set lists {r.name!r} twice")
            seen.add(r.name)
        return cls(
            wing=str(data.get("wing") or ""),
            rooms=rooms,
            proposed_by=str(data.get("proposed_by") or ""),
            proposed_at=str(data.get("proposed_at") or ""),
            sample_size=int(data.get("sample_size") or 0),
        )


def slugify_room(name: str) -> str:
    """``Release Process`` → ``release-process``; the LLM does not always obey kebab-case.

    Dots inside a name survive (``release-3.6.0``): ``sanitize_name`` allows
    them, existing palaces use them for versions, and stripping them here
    would undo :func:`snap_to_existing` on the next load and recreate the
    very spelling drift it exists to prevent.
    """
    slug = re.sub(r"[^a-z0-9.]+", "-", str(name).strip().lower())
    slug = re.sub(r"\.{2,}", ".", slug).strip("-.")
    return slug[:60]


def room_set_path(config: MempalaceConfig, wing: str) -> str:
    """``<palace>/rooms/<wing>.json`` — one approved room set per wing."""
    return os.path.join(config.palace_path, "rooms", f"{sanitize_name(wing, 'wing')}.json")


def save_room_set(config: MempalaceConfig, room_set: RoomSet) -> str:
    path = room_set_path(config, room_set.wing)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(room_set.to_dict(), f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)
    return path


def load_room_set(config: MempalaceConfig, wing: str) -> RoomSet:
    path = room_set_path(config, wing)
    with open(path, encoding="utf-8") as f:
        return RoomSet.from_dict(json.load(f))


# ── sampling ─────────────────────────────────────────────────────────────────


def _iter_wing_rows(col, wing: str, include: list[str]) -> Iterable[dict]:
    """Page through one wing's rows; yields ``{"id", "metadata", "document"?, "embedding"?}``."""
    offset = 0
    while True:
        batch = col.get(where={"wing": wing}, limit=_PAGE_SIZE, offset=offset, include=include)
        ids = list(batch.get("ids") or [])
        if not ids:
            return
        metas = batch.get("metadatas") or [None] * len(ids)
        docs = batch.get("documents") or [None] * len(ids)
        embs = batch.get("embeddings")
        embs = list(embs) if embs is not None else [None] * len(ids)
        for i, row_id in enumerate(ids):
            meta = metas[i] or {}
            if meta.get("wing") != wing or meta.get("is_sentinel"):
                continue
            yield {"id": row_id, "metadata": meta, "document": docs[i], "embedding": embs[i]}
        offset += len(ids)
        if len(ids) < _PAGE_SIZE:
            return


def sample_drawers(col, wing: str, n: int = DEFAULT_SAMPLE_SIZE, seed: int = 0) -> list[dict]:
    """A reproducible random sample of ``n`` drawers, each with an excerpt.

    Two passes: ids and rooms first (cheap), then documents only for the
    sampled ids, so a 124k-drawer wing never loads its text to pick 60.
    """
    rows = list(_iter_wing_rows(col, wing, include=["metadatas"]))
    if not rows:
        return []
    rng = random.Random(seed)
    picked = rng.sample(rows, min(n, len(rows)))
    fetched = col.get(ids=[r["id"] for r in picked], include=["documents", "metadatas"])
    docs = dict(zip(fetched.get("ids") or [], fetched.get("documents") or []))
    out = []
    for r in picked:
        text = (docs.get(r["id"]) or "").strip()
        if not text:
            continue
        out.append(
            {
                "id": r["id"],
                "room": r["metadata"].get("room", ""),
                "excerpt": text[:_SAMPLE_CHARS],
            }
        )
    return out


# ── propose (LLM) ────────────────────────────────────────────────────────────

_PROPOSE_SYSTEM = (
    "You design the room layout of a memory palace wing. A wing is one project; "
    "rooms are the aspects a person or agent would browse to find a memory again. "
    "Given random excerpts from the wing, propose a CLOSED set of rooms that "
    "covers the material with as few rooms as possible. Rooms must be specific "
    "to this wing's content, not generic buckets like 'technical' or 'general'. "
    'Return ONLY JSON: {"rooms": [{"name": "kebab-case-slug", '
    '"description": "one sentence: what belongs here", '
    '"keywords": ["3-6 words or phrases that mark this room"]}], '
    '"assignments": [{"excerpt": 1, "room": "kebab-case-slug"}, ...]}. '
    "Assign EVERY excerpt to exactly one of your rooms."
)


def _propose_user_prompt(wing: str, samples: list[dict], max_rooms: int) -> str:
    lines = [
        f"Wing: {wing}",
        f"Propose between 4 and {max_rooms} rooms.",
        f"Excerpts ({len(samples)}):",
        "",
    ]
    for i, s in enumerate(samples, 1):
        lines.append(f"--- excerpt {i} (currently in room {s['room'] or '?'}) ---")
        lines.append(s["excerpt"])
    return "\n".join(lines)


def _json_object(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise ValueError("LLM response held no JSON object")
        data = json.loads(match.group(0))
    return data


def _parse_room_json(text: str) -> tuple[list[dict], list[dict]]:
    """``(rooms, assignments)`` from the LLM's answer; assignments may be empty."""
    data = _json_object(text)
    rooms = data.get("rooms") if isinstance(data, dict) else data
    if not isinstance(rooms, list):
        raise ValueError("LLM response JSON has no 'rooms' list")
    assignments = data.get("assignments") if isinstance(data, dict) else None
    return rooms, list(assignments) if isinstance(assignments, list) else []


def _parse_room_json_assignments(text: str) -> tuple[list[dict], list[dict]]:
    """``([], assignments)`` from the follow-up call; tolerates a bare list."""
    data = _json_object(text)
    assignments = data.get("assignments") if isinstance(data, dict) else data
    return [], list(assignments) if isinstance(assignments, list) else []


_ASSIGN_SYSTEM = (
    "You file excerpts into the rooms of a memory palace wing. You are given the "
    "closed list of rooms and numbered excerpts. Put EVERY excerpt into exactly one "
    "room from the list; never invent a room. Return ONLY JSON: "
    '{"assignments": [{"excerpt": 1, "room": "slug"}, ...]}.'
)


def _assign_user_prompt(wing: str, room_set: RoomSet, samples: list[dict]) -> str:
    lines = [f"Wing: {wing}", "Rooms:"]
    for r in room_set.rooms:
        lines.append(f"  {r.name}: {r.description}")
    lines.append(f"Excerpts ({len(samples)}):")
    for i, s in enumerate(samples, 1):
        lines.append(f"--- excerpt {i} ---")
        lines.append(s["excerpt"])
    return "\n".join(lines)


def _record_exemplars(room_set: RoomSet, samples: list[dict], assignments: list[dict]) -> int:
    """Attach sampled drawer ids to the rooms the LLM assigned them to."""
    by_name = {r.name: r for r in room_set.rooms}
    attached = 0
    for a in assignments:
        if not isinstance(a, dict):
            continue
        try:
            index = int(a.get("excerpt"))
        except (TypeError, ValueError):
            continue
        room = by_name.get(slugify_room(str(a.get("room") or "")))
        if room is None or not (1 <= index <= len(samples)):
            continue
        drawer_id = samples[index - 1]["id"]
        if drawer_id not in room.exemplars:
            room.exemplars.append(drawer_id)
            attached += 1
    return attached


def existing_rooms(col, wing: str) -> dict[str, int]:
    """``{room: drawers}`` for the wing, so proposals can reuse spellings."""
    counts: dict[str, int] = {}
    for row in _iter_wing_rows(col, wing, include=["metadatas"]):
        room = str(row["metadata"].get("room") or "")
        if room:
            counts[room] = counts.get(room, 0) + 1
    return counts


def snap_to_existing(room_set: RoomSet, existing: Iterable[str]) -> list[tuple[str, str]]:
    """Rename proposed rooms that collide with an existing room's spelling.

    ``release-3-6-0`` next to an existing ``release-3.6.0`` would recreate the
    naming drift ``mempalace audit`` flags, so the existing spelling wins.
    Returns the ``(proposed, existing)`` renames applied.
    """
    from .palace_audit import drift_key

    by_key = {drift_key(name): name for name in existing}
    renames = []
    taken = {r.name for r in room_set.rooms}
    for r in room_set.rooms:
        current = by_key.get(drift_key(r.name))
        if current and current != r.name:
            # Two proposals that both collapse onto one existing spelling
            # (``release-3-6-0`` and ``release_3_6_0``) must stay distinct,
            # or the set fails its own uniqueness check at apply time; the
            # first takes the existing name, the rest keep their own.
            if current in taken:
                continue
            taken.discard(r.name)
            taken.add(current)
            renames.append((r.name, current))
            r.name = current
    return renames


def propose_rooms(
    wing: str,
    samples: list[dict],
    provider,
    max_rooms: int = DEFAULT_MAX_ROOMS,
    existing: Optional[Iterable[str]] = None,
) -> RoomSet:
    """Ask the LLM for a closed room set over ``samples``, then for exemplars.

    Two calls: the room set first, then one narrower call that labels each
    excerpt with a room from that set (small local models drop the
    assignments when both are asked for at once; a model that returns them
    inline skips the second call). Raises ``ValueError`` on an unparsable or
    empty answer and lets the provider's ``LLMError`` propagate; never falls
    back to a generic set, because a generic set is the problem being fixed.
    """
    if not samples:
        raise ValueError(f"no drawers to sample in wing {wing!r}")
    response = provider.classify(
        _PROPOSE_SYSTEM, _propose_user_prompt(wing, samples, max_rooms), json_mode=True, think=False
    )
    raw_rooms, assignments = _parse_room_json(response.text)
    room_set = RoomSet.from_dict({"wing": wing, "rooms": raw_rooms[:max_rooms]})
    if existing:
        snap_to_existing(room_set, existing)
    # The follow-up call runs unless the first answer labelled most of the
    # sample: one stray assignment out of sixty must not skip it, or the
    # centroids are built from a single drawer.
    attached = _record_exemplars(room_set, samples, assignments)
    if attached < max(1, math.ceil(0.8 * len(samples))):
        follow_up = provider.classify(
            _ASSIGN_SYSTEM,
            _assign_user_prompt(wing, room_set, samples),
            json_mode=True,
            think=False,
        )
        _, assignments = _parse_room_json_assignments(follow_up.text)
        _record_exemplars(room_set, samples, assignments)
    room_set.proposed_by = f"{getattr(provider, 'name', 'llm')}/{getattr(provider, 'model', '')}"
    room_set.proposed_at = datetime.now(timezone.utc).isoformat()
    room_set.sample_size = len(samples)
    return room_set


# ── deciders ─────────────────────────────────────────────────────────────────


class RoomDecider(Protocol):
    """Assign a room to each row; ``confidence`` is in ``[0, 1]``.

    ``rows`` carry ``id``, ``metadata`` and, when the store has them,
    ``embedding`` and ``document``. The embedding decider uses the vector; a
    structured-decision model would use the document. Either returns one
    ``(room_name, confidence)`` per row, in order, with ``room_name`` drawn
    from the room set it was built with.
    """

    def decide(self, rows: list[dict]) -> list[tuple[str, float]]: ...


def _cosine(a, b) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _mean_vector(vectors: list) -> Optional[list[float]]:
    vectors = [list(v) for v in vectors if v is not None]
    if not vectors:
        return None
    dim = len(vectors[0])
    return [sum(v[i] for v in vectors) / len(vectors) for i in range(dim)]


class EmbeddingRoomDecider:
    """Nearest room prototype by cosine against the drawer's stored embedding.

    A room's prototype is the centroid of its exemplar drawers' stored
    embeddings when ``col`` is given and the room has exemplars (the drawers
    the LLM assigned to it at propose time), so the decision happens in the
    palace's own vector space. Rooms without exemplars fall back to the
    embedding of their description, via ``embed`` (ChromaDB EF protocol,
    ``embed_query`` preferred because a description is a query).
    Confidence is the best cosine similarity: the same scale search ranks on,
    not a calibrated probability.
    """

    def __init__(self, room_set: RoomSet, embed, col=None):
        self.room_set = room_set
        self.names = room_set.names()
        self.prototype_source: dict[str, str] = {}
        prototypes: dict[str, list[float]] = {}
        if col is not None:
            wanted = [d for r in room_set.rooms for d in r.exemplars]
            if wanted:
                fetched = col.get(ids=wanted, include=["embeddings"])
                by_id = dict(zip(fetched.get("ids") or [], fetched.get("embeddings") or []))
                for r in room_set.rooms:
                    centroid = _mean_vector([by_id.get(d) for d in r.exemplars])
                    if centroid is not None:
                        prototypes[r.name] = centroid
                        self.prototype_source[r.name] = f"centroid of {len(r.exemplars)}"
        missing = [r for r in room_set.rooms if r.name not in prototypes]
        if missing:
            texts = [r.prototype_text() for r in missing]
            query = getattr(embed, "embed_query", None)
            vectors = list(query(texts) if callable(query) else embed(texts))
            for r, vec in zip(missing, vectors):
                prototypes[r.name] = list(vec)
                self.prototype_source[r.name] = "description"
        self.prototypes = [prototypes[name] for name in self.names]

    def decide(self, rows: list[dict]) -> list[tuple[str, float]]:
        out = []
        for row in rows:
            vec = row.get("embedding")
            if vec is None:
                out.append(("", 0.0))
                continue
            best, best_sim = "", -1.0
            for name, proto in zip(self.names, self.prototypes):
                sim = _cosine(vec, proto)
                if sim > best_sim:
                    best, best_sim = name, sim
            out.append((best, max(0.0, best_sim)))
        return out


# ── plan + apply ─────────────────────────────────────────────────────────────


@dataclass
class RoomPlan:
    wing: str
    total: int
    changes: list[tuple[str, str, str]]  # (drawer_id, old_room, new_room)
    # {(source_file, room): {new_room or "": drawers}} — every drawer of the
    # wing counted under the room it started in, with "" for the ones that
    # did not move. The closet layer is keyed per source file and room, so
    # `rekey_closets` needs the stayers too: a closet may only follow a
    # source whose drawers *all* moved to one room.
    source_moves: dict
    kept: int
    below_threshold: int
    no_embedding: int
    per_room: dict[str, int]
    examples: dict[str, list[tuple[str, float]]] = field(default_factory=dict)
    # per new room: (drawer_id, confidence) of the first few moves, for eyeballing

    def example_excerpts(self, col, per_room: int = 3, chars: int = 160) -> dict[str, list[str]]:
        """Fetch short excerpts for :attr:`examples` so a dry run can be judged."""
        ids = [d for pairs in self.examples.values() for d, _ in pairs[:per_room]]
        if not ids:
            return {}
        fetched = col.get(ids=ids, include=["documents"])
        docs = dict(zip(fetched.get("ids") or [], fetched.get("documents") or []))
        out: dict[str, list[str]] = {}
        for room, pairs in self.examples.items():
            for drawer_id, confidence in pairs[:per_room]:
                text = " ".join((docs.get(drawer_id) or "").split())[:chars]
                out.setdefault(room, []).append(f"[{confidence:.2f}] {text}")
        return out

    def summary(self) -> dict:
        return {
            "wing": self.wing,
            "total": self.total,
            "changed": len(self.changes),
            "kept": self.kept,
            "below_threshold": self.below_threshold,
            "no_embedding": self.no_embedding,
            "per_room": self.per_room,
        }


def plan_rooms(
    col,
    wing: str,
    decider: RoomDecider,
    threshold: float = DEFAULT_THRESHOLD,
    progress=None,
    from_rooms: Optional[frozenset] = GENERIC_ROOMS,
) -> RoomPlan:
    """Decide a room for every drawer in ``wing``; change nothing.

    Only drawers whose current room is in ``from_rooms`` are candidates to
    move; the rest are counted under their current room as ``kept``. Pass
    ``from_rooms=None`` to reclassify every drawer in the wing.
    """
    changes: list[tuple[str, str, str]] = []
    source_moves: dict[tuple[str, str], Counter] = {}
    per_room: dict[str, int] = {}
    examples: dict[str, list[tuple[str, float]]] = {}
    total = kept = below = missing = 0
    batch: list[dict] = []

    def flush():
        nonlocal total, kept, below, missing
        if not batch:
            return
        for row, (room, confidence) in zip(batch, decider.decide(batch)):
            total += 1
            old = str(row["metadata"].get("room") or "")
            source = str(row["metadata"].get("source_file") or "")

            def stayed():
                """Record a drawer that keeps its room, for the closet layer."""
                if source:
                    source_moves.setdefault((source, old), Counter())[""] += 1

            if from_rooms is not None and old not in from_rooms:
                kept += 1
                per_room[old] = per_room.get(old, 0) + 1
                stayed()
                continue
            if not room:
                missing += 1
                per_room[old] = per_room.get(old, 0) + 1
                stayed()
                continue
            if confidence < threshold:
                below += 1
                per_room[old] = per_room.get(old, 0) + 1
                stayed()
                continue
            per_room[room] = per_room.get(room, 0) + 1
            if room == old:
                kept += 1
                stayed()
            else:
                changes.append((row["id"], old, room))
                if source:
                    source_moves.setdefault((source, old), Counter())[room] += 1
                bucket = examples.setdefault(room, [])
                if len(bucket) < _EXAMPLES_PER_ROOM:
                    bucket.append((row["id"], confidence))
        batch.clear()
        if progress:
            progress(total)

    for row in _iter_wing_rows(col, wing, include=["metadatas", "embeddings"]):
        batch.append(row)
        if len(batch) >= _UPDATE_BATCH:
            flush()
    flush()
    return RoomPlan(
        wing,
        total,
        changes,
        source_moves,
        kept,
        below,
        missing,
        dict(sorted(per_room.items())),
        examples,
    )


def closet_targets(plan: RoomPlan) -> tuple[dict, int]:
    """``({(source_file, old_room): new_room}, ambiguous_drawers)`` for ``plan``.

    A closet is one record per ``(wing, room, source_file)`` and search
    passes the *same* wing/room filter to the closet collection, so after a
    reclassification a closet left on the old room stops boosting the
    drawers it indexes.

    A closet follows its source only when *every* drawer of that source and
    room moved, and to one room: it indexes the whole source, so moving it
    while some of those drawers stayed put would take the boost away from
    the ones that stayed. Sources that split, or only partly moved, are
    counted in ``ambiguous`` and left alone; re-mining such a source
    rebuilds its closets exactly.
    """
    targets: dict[tuple[str, str], str] = {}
    ambiguous = 0
    for (source, old_room), counts in plan.source_moves.items():
        rooms = {r: n for r, n in counts.items() if r}
        if not rooms:
            continue  # nothing of this source moved
        if len(rooms) > 1 or counts.get(""):
            ambiguous += sum(rooms.values())
            continue
        targets[(source, old_room)] = next(iter(rooms))
    return targets, ambiguous


def rekey_closets_to(closets_col, wing: str, targets: dict) -> int:
    """Move each matching closet to its target room; returns closets moved.

    Idempotent: a closet already in its target room is left alone, so a
    retry after an interruption finishes exactly the remainder. Only
    ``room`` metadata is rewritten, never the record id, which is what
    ``wings split`` does for ``wing``.
    """
    if closets_col is None or not targets:
        return 0
    ids: list[str] = []
    metas: list[dict] = []
    stamp = datetime.now(timezone.utc).isoformat()
    for row in _iter_wing_rows(closets_col, wing, include=["metadatas"]):
        meta = row["metadata"]
        key = (str(meta.get("source_file") or ""), str(meta.get("room") or ""))
        target = targets.get(key)
        if not target or target == key[1]:
            continue
        ids.append(row["id"])
        metas.append({"room": target, "last_modified": stamp})
    for start in range(0, len(ids), _UPDATE_BATCH):
        closets_col.update(
            ids=ids[start : start + _UPDATE_BATCH],
            metadatas=metas[start : start + _UPDATE_BATCH],
        )
    return len(ids)


def rekey_closets(closets_col, plan: RoomPlan) -> dict:
    """Follow the drawers with the AAAK index layer; ``{"moved", "ambiguous"}``."""
    if closets_col is None:
        return {"moved": 0, "ambiguous": 0}
    targets, ambiguous = closet_targets(plan)
    return {"moved": rekey_closets_to(closets_col, plan.wing, targets), "ambiguous": ambiguous}


# ── resumable apply ─────────────────────────────────────────────────────────
#
# ``rooms apply`` writes drawers, then closets. The drawer phase resumes by
# itself (a retry plans over the drawers still to move), but once every
# drawer has moved a retry finds nothing to do and would never reach the
# closet phase. The first run therefore records its closet decisions here
# before touching anything, and a retry replays them. The decisions come
# from the first, complete plan: a retry only sees the drawers left over and
# cannot tell whether a source was split.


def pending_apply_path(config: MempalaceConfig, wing: str) -> str:
    return os.path.join(
        config.palace_path, "rooms", f"{sanitize_name(wing, 'wing')}.apply-pending.json"
    )


def apply_inputs(
    config: MempalaceConfig, wing: str, threshold: float, from_rooms: Optional[Iterable[str]]
) -> dict:
    """Everything besides the palace that decides a room apply's plan.

    Recorded with the pending apply, so a retry that would plan differently
    (another threshold, other source rooms, an edited room set) is refused
    instead of finishing the first run's closet phase against a different
    set of drawer moves.
    """
    try:
        with open(room_set_path(config, wing), "rb") as f:
            room_set_sha256 = hashlib.sha256(f.read()).hexdigest()
    except OSError:
        room_set_sha256 = None
    return {
        "threshold": float(threshold),
        "from_rooms": None if from_rooms is None else sorted(from_rooms),
        "room_set_sha256": room_set_sha256,
    }


def save_pending_apply(
    config: MempalaceConfig,
    wing: str,
    targets: dict,
    ambiguous: int,
    inputs: Optional[dict] = None,
) -> str:
    path = pending_apply_path(config, wing)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = {
        "wing": wing,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "ambiguous": int(ambiguous),
        "inputs": inputs,
        "closets": [[src, old, new] for (src, old), new in sorted(targets.items())],
    }
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)
    return path


def load_pending_apply(
    config: MempalaceConfig, wing: str
) -> Optional[tuple[dict, int, Optional[dict]]]:
    """``(targets, ambiguous, inputs)`` from an interrupted apply, or ``None``.

    ``inputs`` is ``None`` for a marker written before inputs were recorded.
    """
    path = pending_apply_path(config, wing)
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    targets = {}
    for row in data.get("closets") or []:
        if isinstance(row, list) and len(row) == 3 and all(isinstance(x, str) for x in row):
            targets[(row[0], row[1])] = row[2]
    inputs = data.get("inputs")
    return targets, int(data.get("ambiguous") or 0), inputs if isinstance(inputs, dict) else None


def clear_pending_apply(config: MempalaceConfig, wing: str) -> None:
    try:
        os.remove(pending_apply_path(config, wing))
    except FileNotFoundError:
        pass


def apply_plan(col, plan: RoomPlan, progress=None) -> int:
    """Write the planned ``room`` changes in batches; returns rows updated.

    Each batch is one backend write and a drawer is only ever wholly in its
    old room or wholly in its new one. An interrupted apply leaves the
    already-moved drawers classified and the rest where they were; running
    ``rooms apply`` again plans only over drawers still in ``from_rooms``,
    so it finishes the remainder and a completed apply is a no-op. Only
    ``room`` metadata changes; content is never rewritten.
    """
    stamp = datetime.now(timezone.utc).isoformat()
    done = 0
    for start in range(0, len(plan.changes), _UPDATE_BATCH):
        chunk = plan.changes[start : start + _UPDATE_BATCH]
        col.update(
            ids=[c[0] for c in chunk],
            metadatas=[{"room": c[2], "last_modified": stamp} for c in chunk],
        )
        done += len(chunk)
        if progress:
            progress(done)
    return done
