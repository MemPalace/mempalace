"""Centralized drawer/triple ID construction with collision-safe delimiter.

Drawer IDs and content-addressed identifiers built by concatenating strings
without a delimiter before hashing form a defect class that allows
``hash(s1 + str(i1)) == hash(s2 + str(i2))`` whenever
``s1 + str(i1) == s2 + str(i2)``. Under ChromaDB's primary-key constraint
the second upsert silently overwrites the first, losing content with no
error raised. The styleguide's partial-scope-key-migration rule names this
shape — every concat-into-hash site is a candidate that must be triaged.

This module is the single source of truth for ID construction in mempalace.
All call sites use the named helpers below; no module should inline
``hashlib.sha256(a + b)`` patterns.
"""

from __future__ import annotations

import hashlib

# Recipe tag written to every drawer's metadata under this module's helpers.
# Audits compare like-for-like: drawers without ``id_recipe`` are treated
# as legacy ``v1`` (pre-delimiter recipe), drawers with ``id_recipe="v2"``
# are guaranteed collision-safe within the v2 generation. The constant is
# exported so call sites use ``ids.ID_RECIPE`` rather than a magic string.
ID_RECIPE: str = "v2"

# '|' is reserved in Windows filenames and cannot appear in source paths
# on any supported platform, making it strictly safer than ':' (which
# appears in Windows drive letters and URL ports). Matches the existing
# diary_ingest precedent at diary_ingest.py:52,76,91,98.
_DELIM: str = "|"

# SHA-256 hex truncation lengths. Drawer IDs historically truncate at 24
# chars; knowledge-graph triple IDs at 12. Preserved per-recipe so existing
# fixture comparisons that hard-code truncation length still parse.
_HASH_TRUNC_DRAWER: int = 24
_HASH_TRUNC_TRIPLE: int = 12


def _delimited_sha256(parts: tuple[object, ...], truncate: int) -> str:
    """Hash parts joined by the unambiguous delimiter, truncate to N hex chars.

    Internal helper. Call sites should use the named ``make_*`` wrappers
    below so the per-site contract is documented in code, not derived
    from caller arguments.

    Each part is coerced to ``str`` before joining so the helper mirrors
    the pre-v2 behavior of ``f"{a}{b}"`` for ``None`` and numeric inputs —
    e.g. ``valid_from=None`` joins as the literal string ``"None"`` rather
    than crashing.
    """
    key = _DELIM.join(str(p) for p in parts).encode()
    return hashlib.sha256(key).hexdigest()[:truncate]


def make_drawer_id_from_chunk(wing: str, room: str, source_file: str, chunk_index: int) -> str:
    """Drawer ID for the project / format miner paths.

    Hash input is ``f"{source_file}|{chunk_index}"`` — the '|' separator
    prevents the classic ``"/a1" + "23" == "/a" + "123"`` collision.

    Returns ``drawer_{wing}_{room}_{hash24}`` where hash24 is the first
    24 hex chars of SHA-256 over the delimited input.
    """
    return (
        f"drawer_{wing}_{room}_"
        f"{_delimited_sha256((source_file, str(chunk_index)), _HASH_TRUNC_DRAWER)}"
    )


def make_drawer_id_from_content(wing: str, room: str, content: str) -> str:
    """Drawer ID for the MCP ``add_drawer`` tool path.

    Hash input is ``f"{wing}|{room}|{content}"`` — the delimiters prevent
    ``wing="foo" + room="bar"`` colliding with ``wing="fooba" + room="r"``
    (architecturally identical defect class to the chunk-index sites,
    even though astronomically rare in practice since content is large
    freeform text).
    """
    return f"drawer_{wing}_{room}_{_delimited_sha256((wing, room, content), _HASH_TRUNC_DRAWER)}"


def make_convo_drawer_id(
    wing: str, room: str, source_file: str, extract_mode: str, chunk_index: int
) -> str:
    """Drawer ID for the conversation miner path.

    Pre-v2 the convo miner used ':' as delimiter; this helper migrates
    to '|' for codebase-wide consistency and to remove the Windows-path
    / URL-source edge case that ':' carried.

    Hash input is ``f"{source_file}|{extract_mode}|{chunk_index}"``.
    """
    return (
        f"drawer_{wing}_{room}_"
        f"{_delimited_sha256((source_file, extract_mode, str(chunk_index)), _HASH_TRUNC_DRAWER)}"
    )


def make_convo_sentinel_id(source_file: str, extract_mode: str) -> str:
    """Sentinel registry ID for the conversation miner zero-chunk-file path.

    Pre-v2 the sentinel used ':' as delimiter; this helper migrates to
    '|' for the same reasons as ``make_convo_drawer_id``.

    Hash input is ``f"{source_file}|{extract_mode}"``.
    """
    return f"_reg_{_delimited_sha256((source_file, extract_mode), _HASH_TRUNC_DRAWER)}"


def make_triple_id(
    sub_id: str, predicate: str, obj_id: str, valid_from: str, recorded_at: str
) -> str:
    """Triple ID for knowledge-graph insertion.

    Pre-v2 the recorded_at hash input was
    ``f"{valid_from}{datetime.now().isoformat()}"`` with no delimiter —
    two ISO datetimes concatenated could collide in principle (e.g.
    ``valid_from="2026-01-01" + isoformat "T12:..."`` vs
    ``valid_from="2026-01-01T12" + isoformat ":..."``).

    Returns ``t_{sub_id}_{predicate}_{obj_id}_{hash12}`` where hash12 is
    the first 12 hex chars of SHA-256 over ``f"{valid_from}|{recorded_at}"``.
    """
    return (
        f"t_{sub_id}_{predicate}_{obj_id}_"
        f"{_delimited_sha256((valid_from, recorded_at), _HASH_TRUNC_TRIPLE)}"
    )
