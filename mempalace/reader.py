"""Reader — consume closet pointers and return surgical drawer slices.

Backs the ``mempalace read`` CLI verb (Task #87). Resurrects the original
``read.py`` concept Aya designed with Lumi: opening only the slice the
closet pointer references, never the whole drawer.

Three pure functions + one CLI-orchestrating function:

  - ``parse_pointer(s)`` — string → ``ParsedPointer`` (no I/O)
  - ``resolve_drawers(col, parsed)`` — find ``DrawerCandidate`` records
    from a chromadb collection
  - ``format_drawer_menu(candidates)`` — render the interactive picker
  - ``read_slice(candidate, requested_line_start, requested_line_end)``
    — call ``extract_line_range`` on the candidate's document

The CLI handler (``cmd_read`` in ``cli.py``) wires these together with
argparse + the interactive prompt.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import NamedTuple, Optional


# ─────────────────────────────────────────────────────────────────────────────
# Data types
# ─────────────────────────────────────────────────────────────────────────────


class ParsedPointer(NamedTuple):
    """Parsed components of a closet pointer or shorthand."""

    date: Optional[str]
    line_start: Optional[int]
    line_end: Optional[int]
    source_file: Optional[str]
    drawer_ids: list[str]


class DrawerCandidate(NamedTuple):
    """A drawer record from the palace, ready to read."""

    drawer_id: str
    source_file: str
    chunk_index: int
    line_start: Optional[int]
    line_end: Optional[int]
    document: str


# ─────────────────────────────────────────────────────────────────────────────
# parse_pointer — string → ParsedPointer
# ─────────────────────────────────────────────────────────────────────────────


# Closet pointer date+line locator: "YYYY-MM-DD:Lstart-Lend"
_DATE_LINE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}):L(\d+)-L(\d+)$")

# Shorthand: "YYYY-MM-DD:Lstart-Lend <source_file>" (source may contain
# spaces if quoted at shell level, so use \S+ for the typical case and let
# the user shell-quote any path with whitespace).
_SHORTHAND_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}):L(\d+)-L(\d+)\s+(\S.*)$")


def parse_pointer(s: str) -> ParsedPointer:
    """Parse a closet pointer or shorthand into a ``ParsedPointer``.

    Accepts (first match wins):
      1. Full Tier 6a closet pointer (4 pipe-separated segments):
         ``topic|entities|YYYY-MM-DD:Lstart-Lend|→drawer_a,drawer_b``
      2. Legacy 3-segment closet pointer (no date+line):
         ``topic|entities|→drawer_a,drawer_b``
      3. Shorthand (date + line range + source file):
         ``YYYY-MM-DD:Lstart-Lend source_file.md``

    Raises ``ValueError`` for empty input, malformed line ranges (end <
    start, start ≤ 0), or strings that don't match any accepted form.
    Per /negative-tests discipline (Task #87): the failure contract is
    pinned BEFORE the happy-path positives — see tests/test_reader.py.
    """
    if not s or not s.strip():
        raise ValueError("pointer is empty")

    stripped = s.strip()

    # Mode 1 & 2: full closet pointer (has ``|`` segments).
    if "|" in stripped:
        return _parse_closet_pointer(stripped)

    # Mode 3: shorthand.
    return _parse_shorthand(stripped)


def _parse_closet_pointer(stripped: str) -> ParsedPointer:
    """Parse the ``topic|entities|...|→ids`` closet-pointer form."""
    parts = stripped.split("|")
    if len(parts) < 3:
        raise ValueError(f"could not parse pointer (need at least 3 segments): {stripped!r}")

    # Last segment must be the drawer-ids arrow.
    arrow_segment = parts[-1].strip()
    if not arrow_segment.startswith("→"):
        raise ValueError(
            f"could not parse pointer (last segment must start with '→'): {arrow_segment!r}"
        )
    drawer_ids_str = arrow_segment[1:].strip()  # strip the arrow
    drawer_ids = [d.strip() for d in drawer_ids_str.split(",") if d.strip()]
    if not drawer_ids:
        raise ValueError(f"pointer has no drawer ids after '→': {arrow_segment!r}")

    # Middle segment (if 4 total): date+line locator.
    date: Optional[str] = None
    line_start: Optional[int] = None
    line_end: Optional[int] = None
    if len(parts) >= 4:
        date_line_seg = parts[-2].strip()
        m = _DATE_LINE_RE.match(date_line_seg)
        if m:
            date = m.group(1)
            line_start = int(m.group(2))
            line_end = int(m.group(3))
            _validate_line_range(line_start, line_end)

    return ParsedPointer(
        date=date,
        line_start=line_start,
        line_end=line_end,
        source_file=None,
        drawer_ids=drawer_ids,
    )


def _parse_shorthand(stripped: str) -> ParsedPointer:
    """Parse the ``YYYY-MM-DD:Lstart-Lend source_file`` shorthand form."""
    m = _SHORTHAND_RE.match(stripped)
    if not m:
        raise ValueError(
            f"could not parse pointer: expected '<topic>|<entities>|<date>:L<n>-L<n>|→<ids>' "
            f"or '<date>:L<n>-L<n> <source_file>', got: {stripped!r}"
        )
    date = m.group(1)
    line_start = int(m.group(2))
    line_end = int(m.group(3))
    source_file = m.group(4).strip()
    _validate_line_range(line_start, line_end)
    return ParsedPointer(
        date=date,
        line_start=line_start,
        line_end=line_end,
        source_file=source_file,
        drawer_ids=[],
    )


def _validate_line_range(line_start: int, line_end: int) -> None:
    """Reject non-positive line numbers and end-before-start ranges."""
    if line_start <= 0 or line_end <= 0:
        raise ValueError(f"line numbers must be positive (got start={line_start}, end={line_end})")
    if line_end < line_start:
        raise ValueError(f"line range end ({line_end}) must be >= start ({line_start})")


# ─────────────────────────────────────────────────────────────────────────────
# resolve_drawers — find DrawerCandidate records
# ─────────────────────────────────────────────────────────────────────────────


def resolve_drawers(col, parsed: ParsedPointer) -> list[DrawerCandidate]:
    """Find drawer records matching the parsed pointer.

    Two resolution modes:
      1. ``parsed.drawer_ids`` non-empty → query by id directly (fast,
         exact, the canonical path from a full closet pointer)
      2. ``parsed.source_file`` non-empty → scan drawers in the palace
         and filter by source-file basename match (the shorthand path)

    Returns a list of ``DrawerCandidate`` objects, possibly empty if
    nothing matched. Order: same as the closet pointer's drawer-id list
    when in mode 1; chunk_index ascending in mode 2.
    """
    if parsed.drawer_ids:
        return _resolve_by_ids(col, parsed.drawer_ids)
    if parsed.source_file:
        return _resolve_by_source(col, parsed.source_file)
    return []


def _resolve_by_ids(col, drawer_ids: list[str]) -> list[DrawerCandidate]:
    """Fetch drawers by explicit id list."""
    try:
        result = col.get(ids=list(drawer_ids), include=["documents", "metadatas"])
    except Exception:
        return []
    candidates: list[DrawerCandidate] = []
    for did, doc, meta in zip(
        result.get("ids") or [], result.get("documents") or [], result.get("metadatas") or []
    ):
        meta = meta or {}
        candidates.append(
            DrawerCandidate(
                drawer_id=did,
                source_file=meta.get("source_file", ""),
                chunk_index=int(meta.get("chunk_index", 0) or 0),
                line_start=_optional_int(meta.get("line_start")),
                line_end=_optional_int(meta.get("line_end")),
                document=doc or "",
            )
        )
    return candidates


def _resolve_by_source(col, source_file: str) -> list[DrawerCandidate]:
    """Scan all drawers and match by source-file basename or full path.

    Tries exact-match first (cheap chromadb ``where`` filter). If empty,
    falls back to a metadatas-only scan filtered by basename match, then
    fetches documents only for the matched IDs — avoids loading every
    drawer's full text on large palaces.
    """
    candidates: list[DrawerCandidate] = []

    # Try exact path first.
    try:
        result = col.get(where={"source_file": source_file}, include=["documents", "metadatas"])
        ids_out = result.get("ids") or []
        if ids_out:
            for did, doc, meta in zip(
                ids_out,
                result.get("documents") or [],
                result.get("metadatas") or [],
            ):
                meta = meta or {}
                candidates.append(
                    DrawerCandidate(
                        drawer_id=did,
                        source_file=meta.get("source_file", ""),
                        chunk_index=int(meta.get("chunk_index", 0) or 0),
                        line_start=_optional_int(meta.get("line_start")),
                        line_end=_optional_int(meta.get("line_end")),
                        document=doc or "",
                    )
                )
            candidates.sort(key=lambda c: c.chunk_index)
            return candidates
    except Exception:
        pass

    # Fallback: scan metadatas only, identify matches, THEN fetch documents
    # for just the matched IDs. On a 100K-drawer palace, this avoids
    # pulling ~100 MB of document text we'd immediately discard.
    target_basename = Path(source_file).name
    try:
        meta_result = col.get(include=["metadatas"])
    except Exception:
        return []
    matched: list[tuple[str, dict]] = []
    for did, meta in zip(meta_result.get("ids") or [], meta_result.get("metadatas") or []):
        meta = meta or {}
        full_path = meta.get("source_file", "")
        if Path(full_path).name == target_basename or full_path == source_file:
            matched.append((did, meta))

    if not matched:
        return []

    matched_ids = [did for did, _ in matched]
    try:
        doc_result = col.get(ids=matched_ids, include=["documents", "metadatas"])
    except Exception:
        return []
    for did, doc, meta in zip(
        doc_result.get("ids") or [],
        doc_result.get("documents") or [],
        doc_result.get("metadatas") or [],
    ):
        meta = meta or {}
        candidates.append(
            DrawerCandidate(
                drawer_id=did,
                source_file=meta.get("source_file", ""),
                chunk_index=int(meta.get("chunk_index", 0) or 0),
                line_start=_optional_int(meta.get("line_start")),
                line_end=_optional_int(meta.get("line_end")),
                document=doc or "",
            )
        )
    candidates.sort(key=lambda c: c.chunk_index)
    return candidates


def _optional_int(value: object) -> Optional[int]:
    """Best-effort int coerce; None for None / non-numeric."""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ─────────────────────────────────────────────────────────────────────────────
# format_drawer_menu — interactive picker rendering
# ─────────────────────────────────────────────────────────────────────────────


_SNIPPET_MAX_CHARS = 120


def format_drawer_menu(candidates: list) -> str:
    """Render the multi-drawer picker menu.

    Output shape (for a 3-candidate pointer):

      Source: 2024-11-08-lumi.md
      Found 3 drawers in this pointer:

        [1] chunk 2, lines 42-78
            "Aya: what brands have you heard good things about?..."

        [2] chunk 4, lines 110-145
            "Aya: but what about wet food vs dry?..."

      Which one? [1-3, or 'all']:

    Caller is responsible for the actual ``input()`` prompt and the
    blank-line / newline placement; this function returns only the menu
    block (header + candidate entries). Single-candidate input still
    produces a one-entry menu so the caller's display path is uniform.
    """
    if not candidates:
        return "No drawers to show.\n"

    lines: list = []

    # Single source file across the closet pointer (true by construction —
    # build_closet_lines is per-source). Show it once at the top.
    source_basename = Path(candidates[0].source_file).name if candidates[0].source_file else "?"
    lines.append(f"Source: {source_basename}")

    if len(candidates) == 1:
        lines.append("1 drawer in this pointer:")
    else:
        lines.append(f"Found {len(candidates)} drawers in this pointer:")

    for idx, cand in enumerate(candidates, start=1):
        lines.append("")
        if cand.line_start is not None and cand.line_end is not None:
            line_label = f"lines {cand.line_start}-{cand.line_end}"
        else:
            line_label = "lines unknown"
        lines.append(f"  [{idx}] chunk {cand.chunk_index}, {line_label}")
        snippet = _make_snippet(cand.document)
        if snippet:
            lines.append(f'      "{snippet}"')

    return "\n".join(lines)


def _make_snippet(document: str) -> str:
    """Produce a single-line truncated preview of the drawer's document."""
    if not document:
        return ""
    # Normalize whitespace + take first chunk of content
    flat = " ".join(document.split())
    if len(flat) <= _SNIPPET_MAX_CHARS:
        return flat
    return flat[: _SNIPPET_MAX_CHARS - 1].rstrip() + "…"


# ─────────────────────────────────────────────────────────────────────────────
# read_slice — call extract_line_range on a candidate's document
# ─────────────────────────────────────────────────────────────────────────────


def read_slice(
    candidate: DrawerCandidate,
    requested_line_start: Optional[int] = None,
    requested_line_end: Optional[int] = None,
) -> str:
    """Return the slice of the drawer's document rendered with SOURCE-file
    line numbers.

    Key semantic: ``candidate.line_start`` / ``candidate.line_end`` are
    positions in the ORIGINAL SOURCE FILE — they describe where this
    chunk sits in the source, NOT positions inside ``candidate.document``.
    ``candidate.document`` is the chunk's content starting at within-doc
    line 1. The translation between the two coordinate systems happens
    here so callers can pass source-file line numbers (which is what
    closet pointers use) and see source-file line numbers back.

    Two modes:
      - **No explicit request:** render the entire drawer document with
        line numbers starting at ``candidate.line_start`` — i.e., show
        the whole chunk in its natural source-file numbering.
      - **Explicit request:** translate ``requested_line_start`` /
        ``requested_line_end`` from source-file coordinates into within-
        chunk positions, slice, then render with source-file numbering.

    Wraps ``searcher.render_with_line_numbers`` — Igor's helper that
    handles the ``[N]`` prefix rendering. The within-chunk slicing is
    done inline so the source-file/within-chunk coordinate translation
    stays in one place.
    """
    from .searcher import render_with_line_numbers

    if not candidate.document:
        return ""

    chunk_start = candidate.line_start if candidate.line_start is not None else 1

    # No explicit request → render whole document with source-file numbering.
    if requested_line_start is None or requested_line_end is None:
        return render_with_line_numbers(candidate.document, start_line=chunk_start)

    # Explicit request — translate source-file coords to within-chunk positions.
    offset = chunk_start - 1  # chunk_start=10 → source line 10 == within-chunk position 1
    within_start = requested_line_start - offset
    within_end = requested_line_end - offset

    lines = candidate.document.split("\n")
    effective_within_start = max(1, within_start)
    effective_within_end = min(len(lines), within_end)

    if effective_within_start > effective_within_end:
        return ""

    section = "\n".join(lines[effective_within_start - 1 : effective_within_end])
    # Render with source-file line numbers (translate back).
    source_line_start = effective_within_start + offset
    return render_with_line_numbers(section, start_line=source_line_start)
