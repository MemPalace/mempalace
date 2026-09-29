# Loaded into mempalace.palace via exec (see __init__.py). Not a standalone module.
if __name__ != "mempalace.palace":
    raise ImportError(f"{__name__} is an implementation fragment; import mempalace.palace")


CLOSET_CHAR_LIMIT = 1500  # fill closet until ~1500 chars, then start a new one
CLOSET_EXTRACT_WINDOW = 5000  # how many chars of source content to scan for entities/topics

# Common capitalized words that look like proper nouns but are usually
# sentence-starters or filler. Filtered out of entity extraction.
_ENTITY_STOPLIST = frozenset(
    {
        "The",
        "This",
        "That",
        "These",
        "Those",
        "When",
        "Where",
        "What",
        "Why",
        "Who",
        "Which",
        "How",
        "After",
        "Before",
        "Then",
        "Now",
        "Here",
        "There",
        "And",
        "But",
        "Or",
        "Yet",
        "So",
        "If",
        "Else",
        "Yes",
        "No",
        "Maybe",
        "Okay",
        "User",
        "Assistant",
        "System",
        "Tool",
        "Monday",
        "Tuesday",
        "Wednesday",
        "Thursday",
        "Friday",
        "Saturday",
        "Sunday",
        "January",
        "February",
        "March",
        "April",
        "May",
        "June",
        "July",
        "August",
        "September",
        "October",
        "November",
        "December",
    }
)


_CANDIDATE_RX_CACHE = None


def _candidate_entity_words(text: str) -> list:
    """Find entity candidate words using i18n-aware patterns.

    Uses the same candidate_patterns as entity_detector (loaded from locale
    JSON files via get_entity_patterns), so non-Latin names (Cyrillic,
    accented Latin, etc.) are detected alongside ASCII names.
    """
    global _CANDIDATE_RX_CACHE
    if _CANDIDATE_RX_CACHE is None:
        from ..config import MempalaceConfig
        from ..i18n import get_entity_patterns

        patterns = get_entity_patterns(MempalaceConfig().entity_languages)
        rxs = []
        for pat in patterns["candidate_patterns"]:
            try:
                rxs.append(re.compile(pat))
            except re.error:
                continue
        _CANDIDATE_RX_CACHE = rxs
    # Defuse ReDoS on long ASCII blobs before matching (#2063); see
    # entity_detector._collapse_long_ascii_runs.
    stripped = _collapse_long_ascii_runs(text)
    words = []
    for rx in _CANDIDATE_RX_CACHE:
        words.extend(rx.findall(stripped))
    return words


def build_closet_lines(source_file, drawer_ids, content, wing, room, drawer_metas=None):
    """Build compact closet pointer lines from drawer content.

    Returns a LIST of lines (not joined). Each line is one complete topic
    pointer — never split across closets.

    Legacy format (3 segments): ``topic|entities|→drawer_ids``
    Tier 6a format (4 segments): ``topic|entities|YYYY-MM-DD:Lstart-Lend|→drawer_ids``

    When ``drawer_metas`` is provided and the first meta carries both
    ``line_start``/``line_end`` plus a parseable ``filed_at``, the 4-segment
    form is emitted so retrieval can jump to the right span. Otherwise the
    legacy 3-segment form is used — backward compat for drawers filed before
    Tier 6a and for direct callers that don't have metadata handy.
    """
    import re
    from pathlib import Path

    drawer_ref = ",".join(drawer_ids[:3])
    window = content[:CLOSET_EXTRACT_WINDOW]

    # Tier 6a — date+line locator segment. Built once per call; ``None``
    # signals "fall back to legacy 3-segment format" for every emitted line.
    date_line_seg = _build_date_line_segment(drawer_metas)

    # Extract proper nouns (2+ occurrences). Uses i18n-aware patterns so
    # non-Latin names (Cyrillic, accented Latin, etc.) are also detected.
    # Tier 3 linguistics cleanup — known-systems compound pre-pass. Detects
    # multi-word product names ("Claude Code", "GitHub Copilot", …) atomically
    # and masks them out of the working window so the single-word extraction
    # below doesn't decompose them.
    working_window, compound_counts = _apply_known_systems_prepass(window)

    coca_filter = _get_coca_filter()
    words = _candidate_entity_words(working_window)
    word_freq: dict = dict(compound_counts)
    for w in words:
        if w in _ENTITY_STOPLIST:
            continue
        # Tier 2 linguistics cleanup — drop common English content words
        # ("Code", "Line", "Note", "Phase", …) so they don't appear in
        # closet pointers as fake entities.
        if w.lower() in coca_filter:
            continue
        word_freq[w] = word_freq.get(w, 0) + 1
    entities = sorted(
        [w for w, c in word_freq.items() if c >= 2],
        key=lambda w: -word_freq[w],
    )[:5]
    entity_str = ";".join(entities) if entities else ""

    # Extract key phrases — action verbs + context
    topics = []
    for pattern in [
        r"(?:built|fixed|wrote|added|pushed|tested|created|decided|migrated|reviewed|deployed|configured|removed|updated)\s+[\w\s]{3,40}",
    ]:
        topics.extend(re.findall(pattern, window, re.IGNORECASE))
    # Also grab section headers if present
    for header in re.findall(r"^#{1,3}\s+(.{5,60})$", window, re.MULTILINE):
        topics.append(header.strip())
    # Dedupe preserving order
    topics = list(dict.fromkeys(t.strip().lower() for t in topics))[:12]

    # Extract quotes
    quotes = re.findall(r'"([^"]{15,150})"', window)

    # Build pointer lines — each one is atomic, never split. When the
    # Tier 6a date+line segment is available, splice it in as the 3rd
    # pipe-separated field; otherwise emit the legacy 3-segment form.
    def _pointer(prefix: str) -> str:
        if date_line_seg is not None:
            return f"{prefix}|{entity_str}|{date_line_seg}|→{drawer_ref}"
        return f"{prefix}|{entity_str}|→{drawer_ref}"

    lines = []
    for topic in topics:
        lines.append(_pointer(topic))
    for quote in quotes[:3]:
        lines.append(_pointer(f'"{quote}"'))

    # Always have at least one line
    if not lines:
        name = Path(source_file).stem[:40]
        lines.append(_pointer(f"{wing}/{room}/{name}"))

    return lines


def _build_date_line_segment(drawer_metas):
    """Tier 6a — produce ``YYYY-MM-DD:Lstart-Lend`` from a drawer-meta list.

    Reads the first meta's ``filed_at`` (date prefix only — never the raw
    ISO timestamp; closet pointers stay compact and grep-friendly) plus its
    ``line_start`` / ``line_end``. Returns ``None`` when any of the three
    fields is missing or unparseable — caller then falls back to the legacy
    3-segment closet pointer format. The choice to read only the first meta
    matches ``drawer_ids[:3]`` truncation in ``build_closet_lines``: pointers
    are approximate locators, not exhaustive indexes.
    """
    if not drawer_metas:
        return None
    meta = drawer_metas[0]
    if not isinstance(meta, dict):
        return None
    line_start = meta.get("line_start")
    line_end = meta.get("line_end")
    if line_start is None or line_end is None:
        return None

    # Tier 6a date hierarchy: prefer ``content_date`` (extracted from file
    # content, frontmatter, filename, or mtime — see
    # mempalace.miner._extract_content_date) when present. Fall back to
    # ``filed_at`` (ingestion timestamp) only when no content-aware date
    # was extractable. ``content_date`` is already an ISO ``YYYY-MM-DD``;
    # ``filed_at`` may be a full ISO timestamp like
    # ``2026-05-21T22:30:00.123456+00:00`` and gets truncated at ``T``.
    content_date = meta.get("content_date")
    if content_date:
        date_part = str(content_date)
    else:
        filed_at = meta.get("filed_at")
        if not filed_at:
            return None
        date_part = str(filed_at).split("T", 1)[0]
    if not date_part:
        return None
    return f"{date_part}:L{line_start}-L{line_end}"


def purge_file_closets(closets_col, source_file: str) -> None:
    """Delete every closet associated with ``source_file``.

    Call this before ``upsert_closet_lines`` on a re-mine so stale topics
    from a prior schema/version don't survive in the closet collection.
    Mirrors the drawer-purge step in process_file().
    """
    try:
        closets_col.delete(where={"source_file": source_file})
    except Exception:
        logger.debug("Closet purge failed for %s", source_file, exc_info=True)


def upsert_closet_lines(closets_col, closet_id_base, lines, metadata):
    """Write topic lines to closets, packed greedily without splitting a line.

    Closets are deterministically numbered (``..._01``, ``..._02``, …) and
    each ``upsert`` fully overwrites the prior content at that ID. Callers
    are expected to ``purge_file_closets`` first when re-mining a source
    file so stale-numbered closets from larger prior runs don't leak.

    Returns the number of closets written.
    """
    closet_num = 1
    current_lines: list = []
    current_chars = 0
    closets_written = 0

    def _flush():
        nonlocal closets_written
        if not current_lines:
            return
        closet_id = f"{closet_id_base}_{closet_num:02d}"
        text = "\n".join(current_lines)
        closets_col.upsert(documents=[text], ids=[closet_id], metadatas=[metadata])
        closets_written += 1

    for line in lines:
        line_len = len(line)
        # Would this line fit whole in the current closet?
        if current_chars > 0 and current_chars + line_len + 1 > CLOSET_CHAR_LIMIT:
            _flush()
            closet_num += 1
            current_lines = []
            current_chars = 0

        current_lines.append(line)
        current_chars += line_len + 1  # +1 for newline

    _flush()
    return closets_written
