# Loaded into mempalace.searcher via exec (see __init__.py). Not a standalone module.
if __name__ != "mempalace.searcher":
    raise ImportError(f"{__name__} is an implementation fragment; import mempalace.searcher")


def render_with_line_numbers(text: "str | None", start_line: int = 1) -> str:
    """Prefix each line of ``text`` with ``[N] `` for read-time grid display.

    Lines that already begin with ``[<digits>]`` pass through unchanged,
    but the counter still advances on them so callers can rely on positional
    alignment with the original line indices.

    ``None`` is treated as empty string. Pure function.
    """
    if not text:
        return ""
    out = []
    for i, line in enumerate(text.split("\n"), start=start_line):
        if _ALREADY_NUMBERED_RE.match(line):
            out.append(line)
        else:
            out.append(f"[{i}] {line}")
    return "\n".join(out)


def extract_line_range(text: str, line_start: int, line_end: int) -> str:
    """Return the 1-indexed inclusive slice ``[line_start, line_end]`` rendered with line numbers.

    This is the closet-pointer read path. A pointer like ``→2026-01-18:L55-L72``
    resolves by opening the day-drawer and calling ``extract_line_range(drawer_text, 55, 72)``.
    Out-of-bounds ranges are clamped. Invalid ranges return ``""``.
    """
    if not text:
        return ""
    if line_end < line_start:
        return ""

    lines = text.split("\n")
    effective_start = max(1, line_start)
    effective_end = min(len(lines), line_end)

    if effective_start > effective_end:
        return ""

    section = "\n".join(lines[effective_start - 1 : effective_end])
    return render_with_line_numbers(section, start_line=effective_start)
