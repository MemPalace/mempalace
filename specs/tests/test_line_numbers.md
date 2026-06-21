# Behavior Spec: Virtual Line Numbering

This spec is derived from the test suite `tests/test_line_numbers.py`, which
exercises two functions imported from the searcher module:
`render_with_line_numbers` and `extract_line_range`
(tests/test_line_numbers.py:L7-L10). The behaviors below are the externally
observable contracts the implementation must satisfy in any language.

## Shared concepts

- **Lines** are produced by splitting the input text on the newline character
  `\n`. A trailing newline therefore yields a final empty line position; e.g.
  `"a\nb\n"` is three positions `["a", "b", ""]`
  (tests/test_line_numbers.py:L64-L69).
- **Line prefix** is the string `[N] ` (open bracket, the line number, close
  bracket, a single space) prepended to a line's content
  (tests/test_line_numbers.py:L22-L29).
- **Already-numbered line**: a line whose text already begins with a `[N]`
  prefix is considered already numbered and is passed through verbatim
  (tests/test_line_numbers.py:L38-L41).
- Both functions are pure: they do not mutate their input text
  (tests/test_line_numbers.py:L77-L82, tests/test_line_numbers.py:L154-L158).

## `render_with_line_numbers(text, start_line=1) -> str`

Renders the supplied text with a `[N] ` prefix on each line, where `N` is a
running counter.

### Inputs
- `text`: the string to render. May be empty or absent/null.
- `start_line`: optional integer first line number; defaults to `1`
  (tests/test_line_numbers.py:L22-L23, tests/test_line_numbers.py:L32-L35).

### Output and behavior
- Empty string input returns the empty string
  (tests/test_line_numbers.py:L18-L19).
- A null/absent input returns the empty string (must not error)
  (tests/test_line_numbers.py:L72-L74).
- A single line `"hello"` returns `"[1] hello"`
  (tests/test_line_numbers.py:L22-L23).
- Multiple lines each get the prefix with an increasing counter starting at
  `start_line`; e.g. `"alpha\nbeta\ngamma"` → `"[1] alpha\n[2] beta\n[3] gamma"`
  (tests/test_line_numbers.py:L26-L29).
- A custom `start_line` shifts the counter base; e.g. `start_line=5` over
  `"first\nsecond"` → `"[5] first\n[6] second"`
  (tests/test_line_numbers.py:L32-L35).
- Lines already starting with `[N]` pass through unchanged regardless of the
  position or `start_line`; e.g. `"[1] already numbered\n[2] also numbered"`
  returns identical text, and `"[42] keep this number\n[43] and this"` with
  `start_line=100` returns identical text
  (tests/test_line_numbers.py:L38-L47).
- The counter advances on **every** line, including already-numbered lines:
  already-numbered lines pass through but still consume a counter position,
  while plain lines receive the current running counter value. For input
  `"[10] kept\nplain line\n[12] also kept"` the result is
  `"[10] kept\n[2] plain line\n[12] also kept"` — the plain line is the 2nd
  position so it gets `[2]` (tests/test_line_numbers.py:L50-L54).
- Blank lines are numbered like any other position; e.g. `"first\n\nthird"` →
  `"[1] first\n[2] \n[3] third"` (note the trailing space after `[2]`)
  (tests/test_line_numbers.py:L57-L61).
- Trailing-newline semantics are preserved by the split/rejoin: `"a\nb\n"` →
  `"[1] a\n[2] b\n[3] "` (the trailing empty position becomes `[3] `)
  (tests/test_line_numbers.py:L64-L69).
- The input text is not modified by the call
  (tests/test_line_numbers.py:L77-L82).

## `extract_line_range(text, line_start, line_end) -> str`

Extracts an inclusive range of lines from the text and renders them with line
numbers reflecting their **absolute position in the original text**, not a
relative 1-based numbering.

### Inputs
- `text`: source string (may be empty).
- `line_start`: 1-based inclusive start line number.
- `line_end`: 1-based inclusive end line number.

### Output and behavior
- A single-line range returns just that line with its absolute number; e.g. for
  `"a\nb\nc\nd\ne"`, range `(3, 3)` → `"[3] c"`
  (tests/test_line_numbers.py:L90-L92).
- An inclusive range returns all lines from `line_start` through `line_end`
  inclusive; e.g. `(2, 4)` over `"a\nb\nc\nd\ne"` →
  `"[2] b\n[3] c\n[4] d"` (tests/test_line_numbers.py:L95-L98).
- The full range `(1, 3)` over a 3-line text returns all lines numbered from 1
  (tests/test_line_numbers.py:L101-L104).
- **End-beyond-length clips**: if `line_end` exceeds the document length, the
  function returns what is available without error; e.g. over `"a\nb\nc"`,
  range `(2, 99)` → `"[2] b\n[3] c"` (tests/test_line_numbers.py:L107-L111).
- **Start-below-one clamps**: a `line_start < 1` clamps to 1; extraction begins
  at line 1 and numbering starts at 1; e.g. over `"a\nb\nc"`, range `(0, 2)` →
  `"[1] a\n[2] b"` (tests/test_line_numbers.py:L114-L119).
- **Start after end returns empty**: if `line_start > line_end`, the result is
  the empty string; e.g. `(5, 2)` over `"a\nb\nc"` → `""`
  (tests/test_line_numbers.py:L122-L124).
- Empty input text returns the empty string for any range; e.g. `("", 1, 5)` →
  `""` (tests/test_line_numbers.py:L127-L128).
- Already-numbered lines inside the slice pass through verbatim while plain
  lines in the slice get their absolute number; e.g. over
  `"plain\n[42] numbered\nplain again"`, range `(1, 3)` →
  `"[1] plain\n[42] numbered\n[3] plain again"`
  (tests/test_line_numbers.py:L131-L135).
- **Absolute (drawer) numbering contract**: extracting lines 5–7 must render
  `[5][6][7]`, not `[1][2][3]`. For text whose lines are `line1`..`line10`,
  range `(5, 7)` yields a string that starts with `"[5] line5"`, contains
  `"[6] line6"`, ends with `"[7] line7"`, and contains neither `"[1]"` nor
  `"[8]"`. This is the closet-pointer contract: a pointer
  `→2026-01-18:L55-L72` renders as `[55]`..`[72]` so the user sees which drawer
  positions they are reading (tests/test_line_numbers.py:L138-L151).
- The input text is not modified by the call
  (tests/test_line_numbers.py:L154-L158).

## Test execution contract

The suite is intended to be run as `pytest tests/test_line_numbers.py -v` and
documents virtual line numbering integrated in PR #1555 / mempalace 3.3.6
(tests/test_line_numbers.py:L1-L5).
