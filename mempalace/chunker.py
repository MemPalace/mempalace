"""
chunker.py — Meaning-aware text chunking for the palace.

The previous chunker hard-cut at exactly CHUNK_SIZE chars, producing
mid-word fragments. This module replaces the cut with a backward
boundary search (paragraph → sentence → newline → word) and adds
content-type exclusion for tool-output noise (logs, ps, diffs,
truncation messages).

Two public entry points:

- ``smart_split(text, target, ceiling)`` — split a single string into
  boundary-aware chunks. Used by both the conversation chunker and the
  general miner.
- ``is_excluded_content(text)`` — return True if the chunk is dominated
  by tool-output noise and should be skipped from indexing.

These functions are pure: no I/O, no chromadb, no LLM. Unit-testable.
"""

import re

# Code fences used by Markdown and most chat exports. We treat content
# between matching fences as atomic so a code listing never gets split
# across drawers mid-line.
_CODE_FENCE = "```"

# Boundary hierarchy used by smart_split, in preference order. Each
# entry is (separator, keep_chars, skip_chars):
#   keep_chars — how many chars of the separator stay with the *previous*
#                chunk (e.g. the "." in ". " sticks with the sentence
#                that just ended).
#   skip_chars — total length of the separator (so the next chunk starts
#                at sep_pos + skip_chars).
# The chunker scans backward from `ceiling` looking for the first
# separator whose match falls inside the [target, ceiling] window;
# ties are broken by list order (paragraph > sentence > newline > word).
_BOUNDARIES: list[tuple[str, int, int]] = [
    ("\n\n", 0, 2),    # paragraph: drop both newlines
    (". ", 1, 2),      # sentence: keep the period, drop the space
    ("! ", 1, 2),
    ("? ", 1, 2),
    (".\n", 1, 2),     # sentence at line end: keep period, drop newline
    ("!\n", 1, 2),
    ("?\n", 1, 2),
    ("\n", 0, 1),      # any newline: drop it
    (" ", 0, 1),       # word boundary: drop the space
]


# ---------------------------------------------------------------------------
# Content-type exclusion
# ---------------------------------------------------------------------------

# Log line prefix: ISO timestamp or python logger format. Catches
# "2026-04-17 12:42:22,856 [vestige.crystallization] INFO: ..." and
# "INFO:vestige.recall:..." style.
_LOG_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}"
    r"|(?:INFO|WARNING|ERROR|DEBUG|CRITICAL):"
    r"|\[(?:vestige|chromadb|httpx|urllib3|asyncio|mempalace)\.)"
)

# `ps aux` row: USER PID %CPU %MEM ... where USER is a username.
_PS_RE = re.compile(r"^\s*[a-z][\w_-]{2,}\s+\d+\s+\d+\.\d+\s+\d+\.\d+\s")

# Line-numbered diff / file dump: "920- ... → 921- ..." or
# "245+    ..." or "922:    code". Common output of grep -n / diff
# tooling pasted into transcripts. The line begins with optional
# indent, digits, then exactly one of the line-marker chars (-, +, :)
# followed by a space. We reject the simple "digit space text" form
# because that shows up in normal prose ("step 5 of the ...").
_DIFF_RE = re.compile(r"^\s*\d+[+\-:]\s")

# Tool-output redirect arrow: lines that are entirely the rendered
# output of a previous tool call (file listings, shell output) inlined
# into the transcript. Normalize.py inserts these as structural markers
# but they carry no semantic value — a line of the form
# "→ -rw-r--r--  1 user  staff  4912 Apr  2  19:11 foo.json" is
# filesystem metadata, not memory.
_ARROW_LINE_RE = re.compile(r"^\s*→\s")

# JSON object/JSONL line: starts with `{"key":` (the canonical Claude Code
# session log shape). Matched anchored to line start so prose mentioning
# `{"foo": 1}` inline is not flagged.
_JSON_OBJECT_LINE_RE = re.compile(r'^\s*\{"\w+":')

# JSON key/value separator. Counts the canonical `,"key":` shape that
# appears between adjacent pairs in a JSON object. We use the looser
# `,"key":` form (comma + open-quote + word + close-quote + colon) rather
# than `","key":` so that array-close transitions like `"],"sessionId":`
# also count — the real session-log shape interleaves arrays of strings
# with object keys, and the strict form misses those boundaries. Real
# prose almost never has `,"word":` without surrounding whitespace, so
# the false-positive risk is small.
_JSON_KV_PAIR_RE = re.compile(r',"\w+":')

# Strong markers that the chunk is a Claude Code or chat-platform session
# log fragment. Any one of these in combination with high JSON-KV density
# is decisive.
_TRANSCRIPT_MARKER_RE = re.compile(
    r'"(?:uuid|sessionId|requestId|messageId|parentUuid)"\s*:'
    r'|"timestamp"\s*:\s*"\d{4}-\d{2}-\d{2}'
)


def is_excluded_content(text: str) -> bool:
    """True if `text` is dominated by tool-output noise.

    The chunker calls this on each candidate chunk. A True result means
    the chunk is skipped entirely — it never enters the palace. The
    intent is not to drop chunks that mention logs in passing (a prose
    description with one timestamp embedded is fine), but to drop
    chunks that ARE log dumps, ps listings, or line-numbered diffs.

    Heuristics, all line-ratio-based so a small log fragment in prose
    survives:
    - >=70% of non-empty lines start with a log/timestamp prefix
    - >=50% of non-empty lines look like a ps row
    - >=70% of non-empty lines look like line-numbered diff entries
    - The chunk is essentially just a truncation message
    - The chunk is a dense run of arrow-redirected tool output (>5
      arrows on a single visual line)
    - >=50% of non-empty lines start with a JSON object pattern
      ({"key":), or the chunk has a high density of JSON key/value
      separators (`","key":`) combined with a transcript marker
      (uuid/sessionId/etc.) — catches Claude Code session logs spilled
      into tool-results files or pasted into transcripts as raw JSONL.
    """
    if not text or not text.strip():
        return True

    lines = [ln for ln in text.split("\n") if ln.strip()]
    if not lines:
        return True

    n = len(lines)
    log_n = sum(1 for ln in lines if _LOG_RE.search(ln))
    ps_n = sum(1 for ln in lines if _PS_RE.match(ln))
    diff_n = sum(1 for ln in lines if _DIFF_RE.match(ln))
    arrow_n = sum(1 for ln in lines if _ARROW_LINE_RE.match(ln))

    if log_n >= 0.7 * n:
        return True
    if ps_n >= 0.5 * n:
        return True
    if diff_n >= 0.7 * n:
        return True
    # >=60% of lines are arrow-redirected tool output (file listings,
    # ls dumps, command output piped into the transcript by normalize).
    if arrow_n >= 0.6 * n:
        return True

    # Standalone truncation message: "Output too large (52.8KB)" or
    # "[truncated, 4905 chars]" with negligible surrounding prose.
    if "Output too large" in text or "[truncated," in text:
        stripped = re.sub(
            r"\[truncated[^\]]*\]|Output too large[^\n]*", "", text
        ).strip()
        if len(stripped) < 100:
            return True

    # Arrow-redirected tool output dumps: "→ a → b → c → d → e → f"
    # collapsed into a single visual line by the per-line strip-and-join
    # path the previous chunker used.
    if n <= 3 and text.count(" → ") > 5:
        return True

    # JSONL session-log shape: each non-empty line is its own JSON object
    # starting with `{"key":...`. Catches raw Claude Code session files
    # parsed as plain text (e.g., when normalize() falls through because
    # the JSON parsers couldn't extract messages).
    json_object_lines = sum(1 for ln in lines if _JSON_OBJECT_LINE_RE.match(ln))
    if json_object_lines >= 0.5 * n:
        return True

    # Inline JSON-blob density: tool-results files or transcript blobs
    # frequently arrive as one giant line/paragraph rather than line-per-
    # object. Density of `","key":` patterns plus a transcript marker
    # (uuid/sessionId/timestamp) is decisive. Threshold: >=1 KV pair per
    # 200 chars (real prose maxes out around 1 per 800-1200 chars even
    # when describing JSON).
    kv_pairs = len(_JSON_KV_PAIR_RE.findall(text))
    if (
        kv_pairs > 0
        and _TRANSCRIPT_MARKER_RE.search(text)
        and kv_pairs * 200 >= len(text)
    ):
        return True

    return False


# ---------------------------------------------------------------------------
# Boundary-aware splitting
# ---------------------------------------------------------------------------


def _find_code_block_extension(text: str, pos: int, ceiling_end: int) -> int:
    """If `ceiling_end` falls inside a code fence, return the position
    just past the closing fence. Otherwise return -1.

    A "code fence" is a triple backtick. We count fences in
    text[pos:ceiling_end]; if odd, we are inside an open block at
    ceiling_end and need to extend.
    """
    window = text[pos:ceiling_end]
    if window.count(_CODE_FENCE) % 2 == 0:
        return -1

    # Inside an open fence — find the closer
    close = text.find(_CODE_FENCE, ceiling_end)
    if close < 0:
        return -1

    return close + len(_CODE_FENCE)


def smart_split(text: str, target: int, ceiling: int) -> list[str]:
    """Split `text` into chunks, preferring semantic boundaries.

    Each chunk targets `target` chars and is allowed up to `ceiling`
    chars while searching backward for a clean boundary. The boundary
    hierarchy is paragraph break → sentence end → newline → word
    boundary; if none falls inside the [target, ceiling] window, the
    chunk hard-cuts at ceiling.

    Code blocks (triple-backtick fenced) are atomic: a chunk boundary
    will never land inside an open code fence. If a code block alone
    exceeds ceiling, it is emitted as one oversized chunk rather than
    split mid-line — better one large chunk than many fragmented ones.

    Args:
        text: Content to split.
        target: Soft target chunk length (chars).
        ceiling: Hard maximum chunk length (chars), except for atomic
            code blocks which may exceed this.

    Returns:
        List of chunk strings. Empty if `text` is empty after
        stripping. Each chunk has surrounding whitespace stripped.
    """
    if target <= 0 or ceiling < target:
        raise ValueError(
            f"smart_split needs 0 < target <= ceiling; got target={target} ceiling={ceiling}"
        )

    text = text.strip()
    if not text:
        return []

    chunks: list[str] = []
    pos = 0
    n = len(text)

    while pos < n:
        remaining = n - pos
        if remaining <= ceiling:
            tail = text[pos:].strip()
            if tail:
                chunks.append(tail)
            break

        # Code-block protection: if our ceiling falls inside an open
        # code fence, extend to past the closing fence so we don't
        # split mid-listing.
        ceiling_end = pos + ceiling
        ext = _find_code_block_extension(text, pos, ceiling_end)
        if ext > 0:
            piece = text[pos:ext].strip()
            if piece:
                chunks.append(piece)
            pos = ext
            continue

        # Boundary search: scan backward from ceiling looking for the
        # first separator whose match falls in [target, ceiling].
        window_start = pos + target
        window_end = pos + ceiling
        chosen = -1
        chosen_keep = 0
        chosen_skip = 0
        for sep, keep, skip in _BOUNDARIES:
            idx = text.rfind(sep, window_start, window_end)
            if idx >= window_start:
                chosen = idx
                chosen_keep = keep
                chosen_skip = skip
                break

        if chosen >= 0:
            # Keep punctuation (e.g. "." in ". ") with the closing
            # chunk; the separator chars after `keep` are dropped.
            piece = text[pos : chosen + chosen_keep].strip()
            if piece:
                chunks.append(piece)
            pos = chosen + chosen_skip
        else:
            # No boundary in the window — hard cut. Rare in practice
            # because real prose has at least a space every <=ceiling
            # chars, but keep the fallback for pathological input.
            piece = text[pos:window_end].strip()
            if piece:
                chunks.append(piece)
            pos = window_end

    return chunks
