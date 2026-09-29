"""Tests for virtual line numbering (mempalace 3.3.6, integrated in PR #1555).

Run with:
    pytest tests/test_line_numbers.py -v
"""

from mempalace.searcher import (  # noqa: E402
    extract_line_range,
    render_with_line_numbers,
)


# ─────────────────────────────────────────────────────────────────────────────
# render_with_line_numbers
# ─────────────────────────────────────────────────────────────────────────────


def test_render_empty_string():
    assert render_with_line_numbers("") == ""


def test_render_single_line():
    assert render_with_line_numbers("hello") == "[1] hello"


def test_render_multi_line():
    text = "alpha\nbeta\ngamma"
    expected = "[1] alpha\n[2] beta\n[3] gamma"
    assert render_with_line_numbers(text) == expected


def test_render_custom_start_line():
    text = "first\nsecond"
    expected = "[5] first\n[6] second"
    assert render_with_line_numbers(text, start_line=5) == expected


def test_render_preserves_already_numbered_lines():
    """Lines that already start with [N] must pass through unchanged."""
    text = "[1] already numbered\n[2] also numbered"
    assert render_with_line_numbers(text) == text


def test_render_preserves_already_numbered_with_offset():
    """Already-numbered lines pass through verbatim regardless of start_line."""
    text = "[42] keep this number\n[43] and this"
    assert render_with_line_numbers(text, start_line=100) == text


def test_render_mixed_numbered_and_plain():
    """Counter advances on every line; numbered lines pass through, plain lines get the running counter."""
    text = "[10] kept\nplain line\n[12] also kept"
    expected = "[10] kept\n[2] plain line\n[12] also kept"
    assert render_with_line_numbers(text) == expected


def test_render_preserves_blank_lines():
    """Blank lines must still get a number — they're real positions in the drawer."""
    text = "first\n\nthird"
    expected = "[1] first\n[2] \n[3] third"
    assert render_with_line_numbers(text) == expected


def test_render_preserves_trailing_newline_semantics():
    """Splitting on \\n and rejoining preserves the original boundary count."""
    text = "a\nb\n"
    result = render_with_line_numbers(text)
    # "a\nb\n".split("\n") → ["a", "b", ""] — three positions
    assert result == "[1] a\n[2] b\n[3] "


def test_render_none_input_returns_empty():
    """Defensive: None must not crash."""
    assert render_with_line_numbers(None) == ""


def test_render_does_not_modify_original_text():
    """Function is pure — no mutation of input."""
    original = "line one\nline two"
    snapshot = original
    render_with_line_numbers(original)
    assert original == snapshot


# ─────────────────────────────────────────────────────────────────────────────
# extract_line_range
# ─────────────────────────────────────────────────────────────────────────────


def test_extract_single_line():
    text = "a\nb\nc\nd\ne"
    assert extract_line_range(text, 3, 3) == "[3] c"


def test_extract_inclusive_range():
    text = "a\nb\nc\nd\ne"
    expected = "[2] b\n[3] c\n[4] d"
    assert extract_line_range(text, 2, 4) == expected


def test_extract_full_range():
    text = "first\nsecond\nthird"
    expected = "[1] first\n[2] second\n[3] third"
    assert extract_line_range(text, 1, 3) == expected


def test_extract_end_beyond_length_clips():
    """If line_end exceeds the document, return what's available — don't error."""
    text = "a\nb\nc"
    expected = "[2] b\n[3] c"
    assert extract_line_range(text, 2, 99) == expected


def test_extract_start_below_one_clamps():
    """start_line < 1 clamps to 1; numbering starts from where extraction starts."""
    text = "a\nb\nc"
    # Clamping to 1 means we extract from line 1; numbering starts at 1.
    expected = "[1] a\n[2] b"
    assert extract_line_range(text, 0, 2) == expected


def test_extract_start_after_end_returns_empty():
    text = "a\nb\nc"
    assert extract_line_range(text, 5, 2) == ""


def test_extract_empty_text():
    assert extract_line_range("", 1, 5) == ""


def test_extract_honors_already_numbered_lines():
    """If the slice contains already-numbered lines, they pass through."""
    text = "plain\n[42] numbered\nplain again"
    expected = "[1] plain\n[42] numbered\n[3] plain again"
    assert extract_line_range(text, 1, 3) == expected


def test_extract_uses_drawer_line_numbers_not_relative():
    """When extracting lines 5-7, the rendered numbers must be [5][6][7], not [1][2][3].

    This is the closet-pointer contract: a pointer →2026-01-18:L55-L72 must show
    [55] through [72] in the rendered output, so the user sees which drawer
    positions they're reading.
    """
    text = "\n".join(f"line{i}" for i in range(1, 11))  # line1..line10
    result = extract_line_range(text, 5, 7)
    assert result.startswith("[5] line5")
    assert "[6] line6" in result
    assert result.endswith("[7] line7")
    assert "[1]" not in result
    assert "[8]" not in result


def test_extract_does_not_modify_original_text():
    original = "x\ny\nz"
    snapshot = original
    extract_line_range(original, 1, 2)
    assert original == snapshot
