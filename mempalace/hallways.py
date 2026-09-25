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


def _hallway_file_lock(config=None):
    """The per-file lock every hallway writer holds from load to save.

    Same mechanism as ``palace_graph.create_tunnel``: without it two
    concurrent writers (a mine's recompute and ``hallways --prune-spellings``,
    or two mines of different wings) each load, edit and save the whole
    file, and the later save drops the earlier one's records.
    """
    from .palace import mine_lock

    return mine_lock(_get_hallway_file(config))


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
    mod types routes router store page layout schema schemas models constants
    logger middleware services component components hooks context styles style
    theme globals setup init __init__ repository entity entities interfaces
    interface enums enum validators validator
    cargo.toml cargo.lock go.mod go.sum gemfile gemfile.lock setup.py setup.cfg
    uv.lock poetry.lock yarn.lock pnpm-lock.yaml package-lock.json roadmap.md
    contributing.md security.md license.md code_of_conduct.md todo.md notes.md
    .eslintrc .prettierrc vite.config.ts next.config.js tailwind.config.js
    webpack.config.js jest.config.js vitest.config.ts vercel.json fly.toml procfile
    created_by updated_by deleted_at deleted_by user_id primary_key foreign_key
    is_active is_deleted read_text write_text
    changedetectionstrategy changedetectionstrategy.onpush changedetectionstrategy.eager
    changedetectorref oninit ondestroy onchanges injectable ngmodule httpclient
    formsmodule commonmodule usestate useeffect usememo usecallback useref
    usecontext usereducer afterviewinit routerlink
    docker-compose python.exe xcode.app assert_eq to_string serde_json
    transformers.js tool_call tool_calls authored_at memory.used total_bytes
    tsconfig.app tsconfig.spec failure_scenario
    db ctx.allocator powershell.exe cmd.exe valueerror typeerror keyerror
    docker-compose.yaml docker-compose.yml source_file memory.total buffer.from
    hashmap hashset vec box arc rc
    """.split()
)

# Source files whose stem is a generic word: ``app.js``, ``model.ts``,
# ``mod.rs``, ``main.py`` exist in most repos of that language and link
# nothing when two wings share one.
_SOURCE_EXTENSIONS = frozenset(
    """
    js mjs cjs ts tsx jsx py rs go rb php java kt swift cs c cc cpp h hpp zig
    css scss html vue svelte sql sh
    """.split()
)

# ``pathlib.Path``, ``page.evaluate``, ``console.log``: the first segment is a
# runtime, standard library or test-framework namespace, so the qualified
# name is vocabulary every project in that language shares.
_LIBRARY_NAMESPACES = frozenset(
    """
    os sys json re pathlib subprocess asyncio typing datetime time logging math
    random collections itertools functools shutil io tempfile unittest pytest
    np numpy pd pandas plt torch tf document window console process page
    browser navigator locator expect react vue angular fs path http https crypto
    util express next localstorage sessionstorage self this cls super
    """.split()
)


def is_generic_entity(name: str) -> bool:
    """A name that identifies no project: a short lower-case word (``content``,
    ``thinking``), a harness tool (``WebFetch``), a generic noun (``Server``),
    a file every repo has (``compose.yml``, ``app.js``) or a library
    reference (``pathlib.Path``).

    Symbols (``ChatStore``), qualified project names (``store.baseURL``) and
    project names pass; the boundary with a short lower-case project name is
    fuzzy by construction. Cross-wing ubiquity is judged separately, where
    the wing counts are known.
    """
    text = str(name).strip()
    if _GENERIC_ENTITY_RE.fullmatch(text):
        return True
    if "/" not in text and "." in text:
        head, _, tail = text.rpartition(".")
        if tail.lower() in _SOURCE_EXTENSIONS and head.lower() in GENERIC_ENTITY_STOPLIST:
            return True
        first = text.split(".", 1)[0].lower()
        if first in _LIBRARY_NAMESPACES and tail.lower() not in _SOURCE_EXTENSIONS:
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


# ``git diff`` prints both sides of a change as ``a/<path>`` and ``b/<path>``,
# so a transcript that shows a diff names every touched file twice under two
# fake top-level directories. The evidence is the pair: ``a/x`` and ``b/x``
# with the same ``x`` are one file, at any depth, while a lone ``a/x`` may be
# a real directory named ``a`` and is left alone.
_GIT_DIFF_PREFIXES = frozenset({"a", "b"})


def _path_parts(entity: str) -> list[str]:
    return [p for p in str(entity).replace("\\", "/").split("/") if p]


def _diff_alias(entity: str) -> Optional[tuple[str, str]]:
    """``("a", "src/x.py")`` for ``a/src/x.py``; ``None`` without a diff prefix."""
    parts = _path_parts(entity)
    if len(parts) >= 2 and parts[0] in _GIT_DIFF_PREFIXES:
        return parts[0], "/".join(parts[1:])
    return None


def _dir_segments(entity: str) -> list[str]:
    """Directory part of a spelling, innermost last: ``src/models/user.py`` → ``["src", "models"]``."""
    return _path_parts(entity)[:-1]


def _is_diff_pair(a: str, b: str) -> bool:
    """``a/<path>`` and ``b/<path>`` naming the same ``<path>``."""
    x, y = _diff_alias(a), _diff_alias(b)
    return bool(x and y and x[0] != y[0] and x[1] == y[1])


def _diff_resolved(entities: list[str]) -> dict[str, str]:
    """Map each ``a/`` or ``b/`` spelling whose counterpart is present to its path."""
    aliases = {e: _diff_alias(e) for e in entities}
    present = {(al[0], al[1]) for al in aliases.values() if al}
    out: dict[str, str] = {}
    for e, al in aliases.items():
        if al and ("b" if al[0] == "a" else "a", al[1]) in present:
            out[e] = al[1]
    return out


def same_file_spelling(a: str, b: str) -> bool:
    """True when two spellings can only be the same file.

    They must share a basename key *and* one path must be a suffix of the
    other: ``main.zig`` and ``src/main.zig`` are one file, while
    ``src/models/user.py`` and ``tests/models/user.py`` are two files that
    happen to share a name. Keying on the basename alone merged their
    associations and let ``--prune-spellings`` delete one of them.
    """
    if entity_spelling_key(a) != entity_spelling_key(b):
        return False
    if _is_diff_pair(a, b):
        return True
    da, db = _dir_segments(a), _dir_segments(b)
    short, long_ = (da, db) if len(da) <= len(db) else (db, da)
    return not short or long_[len(long_) - len(short) :] == short


def canonical_spelling(cluster: list[str]) -> str:
    """The spelling records carry for one entity: its most qualified path first.

    A path is what tells ``src/models/user.py`` from ``tests/fixtures/user.py``
    once a record leaves its wing, so the spelling with the most directory
    segments wins; keeping the shortest threw that away and let two unrelated
    files meet as bare ``user.py`` in the cross-wing tunnel builder. Among
    spellings with no directory the shortest reads best (``ChatStore`` over
    ``ChatStore.swift``). Ties break on the text, so the choice is stable.
    """
    resolved = _diff_resolved(cluster)
    return min(
        cluster,
        key=lambda e: (
            e in resolved,  # a diff alias only when a real spelling exists
            -len(_dir_segments(resolved.get(e, e))),
            len(e),
            e,
        ),
    )


def _spelling_clusters(entities: list[str]) -> list[list[str]]:
    """Group spellings of one basename into the distinct files they name.

    Spellings are bucketed by their directory path. A path that is a suffix
    of exactly one longer path is the same file and joins it (``main.zig``
    under ``src/main.zig``). One that could belong to two or more names no
    file at all (``user.py`` beside ``src/models/user.py`` and
    ``tests/models/user.py``) and is left out of every cluster: attributing
    it to either file would be a guess, and keeping it as an entity of its
    own would pair it with the very files it might be, which
    :func:`is_self_link` and :func:`association_groups` then rightly call
    artifacts. Callers skip spellings that appear in no cluster.
    """
    # ``a/x`` beside ``b/x`` is one file seen through a diff: bucket both
    # under ``x``'s directory so they cluster with each other and with ``x``.
    resolved = _diff_resolved(entities)
    groups: dict[tuple, list[str]] = {}
    for e in entities:
        groups.setdefault(tuple(_dir_segments(resolved.get(e, e))), []).append(e)
    by_length = sorted(groups, key=len, reverse=True)
    maximal: list[tuple] = []
    for dirs in by_length:
        if not any(len(m) > len(dirs) and m[len(m) - len(dirs) :] == dirs for m in maximal):
            maximal.append(dirs)
    clusters: dict[tuple, list[str]] = {m: list(groups[m]) for m in maximal}
    for dirs in by_length:
        if dirs in clusters:
            continue
        hosts = [m for m in maximal if len(m) > len(dirs) and m[len(m) - len(dirs) :] == dirs]
        if len(hosts) == 1:
            clusters[hosts[0]].extend(groups[dirs])
        # Two or more hosts: ambiguous, deliberately left out.
    return list(clusters.values())


def is_self_link(record) -> bool:
    """True when a hallway record joins two spellings of one entity."""
    if not isinstance(record, dict):
        return False
    a, b = record.get("entity_a"), record.get("entity_b")
    if a is None or b is None:
        return False
    return same_file_spelling(str(a), str(b))


def canonical_entities(entities: list[str]) -> list[str]:
    """One spelling per entity, in first-seen order.

    The structural extractor records a file as both its path and its
    basename, so a drawer's entity list holds ``src/main.zig`` and
    ``main.zig`` side by side. Pairing those raw spellings wrote a hallway
    from the entity to itself and four copies of every real association
    (``ChatStore`` × ``RootView`` under each spelling combination). Each
    entity keeps its :func:`canonical_spelling`, so hallways read
    ``ChatStore ↔ RootView``.
    """
    by_key: dict[str, list[str]] = {}
    order: list[str] = []
    for entity in entities:
        key = entity_spelling_key(entity)
        if key not in by_key:
            by_key[key] = []
            order.append(key)
        by_key[key].append(entity)
    out: list[str] = []
    for key in order:
        # Spellings of one basename may name several files (``src/user.py``
        # and ``tests/user.py``); each cluster keeps its own canonical
        # spelling instead of collapsing into one entity.
        for cluster in _spelling_clusters(by_key[key]):
            out.append(canonical_spelling(cluster))
    return out


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


def _wing_file_keys(metadatas) -> dict[str, str]:
    """Map every spelling seen in the wing to the file it names.

    The value is the file's :func:`canonical_spelling`, which is both the key
    pairs are counted under and the spelling the record is materialized
    with. Clustering is wing-wide, not per drawer: one drawer saying
    ``ChatStore.swift`` and the next saying ``ChatStore`` are one file, while
    ``src/models/user.py`` and ``tests/models/user.py`` stay two files even
    though they share a basename (:func:`_spelling_clusters`).
    """
    by_base: dict[str, set[str]] = defaultdict(set)
    for meta in metadatas:
        if not isinstance(meta, dict) or meta.get("is_sentinel"):
            continue
        for spelling in _parse_entities(meta.get("entities")):
            by_base[entity_spelling_key(spelling)].add(spelling)
    mapping: dict[str, str] = {}
    for spellings in by_base.values():
        for cluster in _spelling_clusters(sorted(spellings)):
            canonical = canonical_spelling(cluster)
            for spelling in cluster:
                mapping[spelling] = canonical
    return mapping


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
    # Pairs are keyed by the file an entity names, resolved wing-wide by
    # ``_wing_file_keys``: the structural extractor records a file as both
    # path and basename and one drawer may say ``ChatStore.swift`` where the
    # next says ``ChatStore``, so the key is the canonical spelling of that
    # file and the record reads ``ChatStore ↔ RootView``.
    pair_counts: dict[tuple[str, str], int] = defaultdict(int)
    pair_rooms: dict[tuple[str, str], set[str]] = defaultdict(set)
    file_keys = _wing_file_keys(metadatas)

    for meta in metadatas:
        if not isinstance(meta, dict):
            continue
        # Sentinel drawers carry no real content — skip them.
        if meta.get("is_sentinel"):
            continue
        entities = []
        for spelling in canonical_entities(_parse_entities(meta.get("entities"))):
            # The file this spelling names, not its basename: two files
            # sharing a name must not merge into one entity here either,
            # or one drawer naming both counts the same pair twice.
            canonical = file_keys.get(spelling)
            if canonical is None:
                continue  # an ambiguous name: it identifies no single file
            if canonical not in entities:
                entities.append(canonical)
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

    # No early return on an empty ``pair_counts``: the drawers were read, so
    # "no pairs" is this wing's real answer and must replace its old records.
    # Returning here left every stale hallway of the wing in place after a
    # rebuild that correctly produced none.

    # 3. Materialize hallway records for pairs above the threshold.
    #    Before building, load existing records so we can PRESERVE L7
    #    dynamics fields (strength, stability, last_activated, access_count)
    #    across recomputes. Without this preservation, every mine wipes
    #    the connection weights accumulated through use — defeating the
    #    living-connection layer entirely.
    # Load → materialize → save under the hallway-file lock, or a mine of
    # another wing in between rewrites the file without this wing's records.
    with _hallway_file_lock(config):
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
                        file_keys.get(str(h.get("entity_a")), str(h.get("entity_a"))),
                        file_keys.get(str(h.get("entity_b")), str(h.get("entity_b"))),
                    ]
                )
            )
            # Only copy the fields the dynamics layer cares about; everything
            # else is recomputed deterministically from the drawer set.
            existing_dynamics_lookup[key] = {
                k: h[k]
                for k in ("strength", "stability", "last_activated", "access_count")
                if k in h
            }

        created: list[dict] = []
        created_at = datetime.now(timezone.utc).isoformat()
        for key in sorted(pair_counts.keys()):
            count = pair_counts[key]
            if count < min_count:
                continue
            entity_a, entity_b = key
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


def _hallway_sort_key(hallway: dict) -> int | float:
    """Sort key for strongest-first ordering: the co-occurrence count.

    Records missing a count (or carrying a non-numeric one) sort last, so a
    bounded/truncated answer is always the most-connected slice first (#2327).
    """
    count = hallway.get("co_occurrence_count", 0)
    if isinstance(count, bool) or not isinstance(count, (int, float)):
        try:
            count = int(count)
        except (TypeError, ValueError):
            return 0
    return count


def list_hallways(
    wing: Optional[str] = None,
    config=None,
    limit: Optional[int] = None,
    sort: bool = False,
) -> list[dict]:
    """List hallway records. Filter by ``wing`` if specified.

    By default the records are returned in store (file) order — the order
    in which they were written by the miner. This preserves the
    first-seen-wins contract that downstream consumers such as
    ``entity_tunnels_for_wing`` rely on: the record order there decides
    which raw-wing display form wins a tie for a normalized wing, and
    sorting by strength would silently change that on wing renames.

    Pass ``sort=True`` for strongest-first ordering (highest
    ``co_occurrence_count`` first) so a capped answer is always the most
    useful slice — this is what ``tool_list_hallways`` needs before it
    applies its limit (#2327).

    When ``limit`` is given (a non-negative integer), only that many
    records are returned; when it is ``None`` (the default) the full
    matching set is returned.

    Returns a plain list; callers that need the full-match count and an
    explicit truncation signal (e.g. ``tool_list_hallways``) wrap the result
    in a ``{rows, total, truncated}`` envelope.
    """
    all_hallways = _load_hallways(config)
    if wing is not None:
        all_hallways = [h for h in all_hallways if h.get("wing") == wing]
    if sort:
        all_hallways = sorted(all_hallways, key=_hallway_sort_key, reverse=True)
    if limit is not None:
        # ``bool`` is a subclass of ``int`` and ``int(True)`` would silently
        # coerce it to ``1`` — reject it *before* the ``int()`` coercion below
        # can hide it. Coerce once (accepting numeric strings), then range-check
        # the coerced value so negatives and non-numbers raise a clean error.
        if isinstance(limit, bool):
            raise ValueError("limit must be a non-negative integer")
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            raise ValueError("limit must be a non-negative integer") from None
        if limit < 0:
            raise ValueError("limit must be a non-negative integer")
        all_hallways = all_hallways[:limit]
    return all_hallways


def prune_spelling_hallways(config=None, apply: bool = False) -> dict:
    """Find (and with ``apply``) remove hallways that older mines wrote per spelling.

    Two defects, one cause: before :func:`canonical_entities` the miner paired
    raw spellings, so every code wing has ``main.zig ↔ src/main.zig``
    (an entity joined to itself) and four copies of ``ChatStore ↔ RootView``
    (one per spelling combination). Self-links are dropped; of each variant
    group the record with the highest co-occurrence count survives under
    its canonical spellings and the rest are dropped. Only the sidecar file
    is touched, never a drawer. ``removed`` is 0 on a dry run.
    """
    with _hallway_file_lock(config):
        return _prune_spelling_hallways_locked(config, apply)


def association_groups(records: list[dict]) -> list[list[dict]]:
    """Group hallway records that are one association, per wing.

    Each endpoint is mapped to the file it names using the wing's own
    spellings (:func:`_spelling_clusters`), so two files that share a
    basename are two entities, and a bare name that could be either of them
    belongs to no file. A record with such an ambiguous endpoint is a group
    of its own: comparing it pairwise let it match both files and bridge
    their records into one group, and the prune then deleted a real
    association. Self-links are expected to be filtered out first.

    Shared by ``--prune-spellings`` and ``mempalace audit`` so the audit never
    reports a duplicate the prune would keep.
    """
    by_wing: dict[str, list[dict]] = defaultdict(list)
    for h in records:
        if isinstance(h, dict):
            by_wing[str(h.get("wing") or "")].append(h)
    groups: list[list[dict]] = []
    for members in by_wing.values():
        spellings: dict[str, set[str]] = defaultdict(set)
        for h in members:
            for e in (str(h.get("entity_a")), str(h.get("entity_b"))):
                spellings[entity_spelling_key(e)].add(e)
        cluster_of: dict[str, tuple] = {}
        for key, names in spellings.items():
            for i, cluster in enumerate(_spelling_clusters(sorted(names))):
                for e in cluster:
                    cluster_of[e] = (key, i)
        buckets: dict[tuple, list[dict]] = {}
        for h in members:
            ca = cluster_of.get(str(h.get("entity_a")))
            cb = cluster_of.get(str(h.get("entity_b")))
            if ca is None or cb is None:
                groups.append([h])
                continue
            buckets.setdefault(tuple(sorted((ca, cb))), []).append(h)
        groups.extend(buckets.values())
    return groups


def _merge_variant_group(members: list[dict], kept: list[dict], duplicates: list[dict]) -> None:
    """Keep the strongest record of one association under its canonical spellings."""
    if len(members) == 1:
        kept.append(members[0])
        return
    members.sort(key=lambda h: -int(h.get("co_occurrence_count") or 0))
    survivor = dict(members[0])
    # Variants may arrive with reversed endpoints (``a ↔ b`` and ``b.py ↔ a``),
    # so canonicalize per entity across both columns rather than per column,
    # then keep the survivor's own orientation.
    # Canonicalize each side on its own, never through the basename: an
    # association between two files that share one (``src/user.py ↔
    # tests/user.py``) would otherwise get the same spelling on both sides
    # and turn into a self-link. Members may be written in either
    # orientation, so each is aligned to the survivor first.
    sa, sb = str(survivor["entity_a"]), str(survivor["entity_b"])
    side_a, side_b = [sa], [sb]
    for m in members[1:]:
        ma, mb = str(m.get("entity_a")), str(m.get("entity_b"))
        if same_file_spelling(ma, sa) and same_file_spelling(mb, sb):
            side_a.append(ma)
            side_b.append(mb)
        elif same_file_spelling(mb, sa) and same_file_spelling(ma, sb):
            side_a.append(mb)
            side_b.append(ma)
    survivor["entity_a"] = canonical_spelling(side_a)
    survivor["entity_b"] = canonical_spelling(side_b)
    survivor["id"] = _hallway_id(survivor["wing"], survivor["entity_a"], survivor["entity_b"])
    kept.append(survivor)
    duplicates.extend(members[1:])


def _prune_spelling_hallways_locked(config, apply: bool) -> dict:
    hallways = _load_hallways(config)
    self_links = [h for h in hallways if is_self_link(h)]
    candidates = [h for h in hallways if isinstance(h, dict) and not is_self_link(h)]
    duplicates: list[dict] = []
    kept: list[dict] = []
    for members in association_groups(candidates):
        _merge_variant_group(members, kept, duplicates)

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
    with _hallway_file_lock(config):
        hallways = _load_hallways(config)
        filtered = [h for h in hallways if h.get("id") != hallway_id]
        if len(filtered) == len(hallways):
            return False
        _save_hallways(filtered, config)
    return True
