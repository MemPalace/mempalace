# Behavior Spec: `mempalace/sources/transforms.py`

Reference implementations of the reserved content transformations used by source
adapters. Each source adapter declares the set of transformations it applies to
source bytes; a conformance suite reproduces adapter output from source bytes by
applying *only* the declared transformations in declaration order, using these
reference implementations (mempalace/sources/transforms.py:L1-L22). The enforced
invariant: no transformation is applied that is not declared in the adapter's set;
adapters with an empty set are byte-preserving end-to-end except for the initial
UTF-8 decode (mempalace/sources/transforms.py:L12-L15).

## Transformation contract

Every reserved transformation is a callable taking one positional argument and
returning a string. The argument is `bytes` for the byte-stage transformation
(`utf8_replace_invalid`) or `str` for all post-decode transformations; the return
is always `str` (mempalace/sources/transforms.py:L30-L39). Adapters compose
transformations in declaration order: the first step operates on the original
source bytes, and every subsequent step operates on the prior step's output
(mempalace/sources/transforms.py:L33-L37).

## Reserved transformations (concrete behavior)

`utf8_replace_invalid(raw: bytes) -> str`: Decodes the input bytes as UTF-8,
replacing each invalid byte sequence with the Unicode replacement character
U+FFFD. This is the only reserved transformation operating on bytes
(mempalace/sources/transforms.py:L47-L53).

`newline_normalize(text: str) -> str`: Converts CRLF (`\r\n`) and bare-CR (`\r`)
line endings to LF (`\n`). CRLF must be converted before bare-CR so that CRLF does
not produce a doubled LF (mempalace/sources/transforms.py:L56-L58).

`whitespace_trim(text: str) -> str`: Strips leading and trailing whitespace at the
record boundary only (i.e. trims the whole string's ends, not per line)
(mempalace/sources/transforms.py:L61-L63).

`whitespace_collapse_internal(text: str) -> str`: Collapses runs of three or more
blank lines to exactly two blank lines (producing `\n\n\n` between content). A
"blank line" is a line containing only spaces or tabs; such lines are first
normalized to empty before collapsing so the match is consistent regardless of
their whitespace content. Single and double blank-line runs are preserved
(mempalace/sources/transforms.py:L66-L79). The collapse target is the literal
three-newline string `"\n\n\n"`, which renders as two visually blank lines between
content blocks (mempalace/sources/transforms.py:L79).

`line_trim(text: str) -> str`: Strips leading and trailing whitespace from each
individual line, splitting on and rejoining with LF
(mempalace/sources/transforms.py:L82-L84).

`line_join_spaces(text: str) -> str`: Joins adjacent non-blank lines with a single
space, preserving paragraph breaks. Paragraphs are delimited by at least one blank
line (a line that may contain spaces or tabs between LFs). Within each paragraph,
non-blank lines are trimmed and joined with a single space; empty lines within a
paragraph are dropped. Paragraphs are rejoined with a blank line (`\n\n`)
(mempalace/sources/transforms.py:L87-L95).

`blank_line_drop(text: str) -> str`: Removes blank lines, keeping only lines that
contain non-whitespace content, rejoined with LF
(mempalace/sources/transforms.py:L98-L100).

## Adapter-specific identity shims

The following reserved transformations are declared in the spec but are deeply
adapter-specific. Their reference implementations here are identity functions that
return the input unchanged. Adapters that declare these MUST override with a
concrete implementation or provide a namespaced reference under the name
`<adapter_name>_<transform_name>` on this module; the conformance suite looks up
the adapter-specific implementation first and falls back to these identity shims
only when none exists (mempalace/sources/transforms.py:L103-L112):

- `strip_tool_chrome(text) -> text`: would remove system tags, hook output, and
  tool UI chrome; identity here because noise patterns differ per transcript
  format (mempalace/sources/transforms.py:L115-L124).
- `tool_result_truncate(text) -> text`: head/tail window on tool output with a
  middle marker (mempalace/sources/transforms.py:L127-L129).
- `tool_result_omitted(text) -> text`: fully omits some tool outputs
  (mempalace/sources/transforms.py:L132-L134).
- `spellcheck_user(text) -> text`: rewrite user turns via autocorrect
  (mempalace/sources/transforms.py:L137-L143).
- `synthesized_marker(text) -> text`: adapter inserts its own strings such as
  `[N lines omitted]` (mempalace/sources/transforms.py:L146-L148).
- `speaker_role_assignment(text) -> text`: multi-party speakers alternately
  assigned user/assistant (mempalace/sources/transforms.py:L151-L153).

## Registry and lookup

A registry maps each reserved transformation name to its reference implementation.
The registered names, in order, are: `utf8_replace_invalid`, `newline_normalize`,
`whitespace_trim`, `whitespace_collapse_internal`, `line_trim`, `line_join_spaces`,
`blank_line_drop`, `strip_tool_chrome`, `tool_result_truncate`,
`tool_result_omitted`, `spellcheck_user`, `synthesized_marker`,
`speaker_role_assignment` (mempalace/sources/transforms.py:L166-L180).

`get_transformation(name: str) -> Transformation`: Returns the reference
implementation registered under the given reserved name. If the name is not a
reserved name, raises a `KeyError` whose message is
`unknown transformation <name>; reserved names: <sorted list of reserved names>`
(the name shown with its quotes, the reserved names sorted). This helper resolves
reserved names only; callers seeking adapter-namespaced references should look up
those attributes on the module directly first
(mempalace/sources/transforms.py:L183-L196).

## Side effects

None. This module performs no filesystem, network, process, or environment access;
all transformations are pure string/byte functions
(mempalace/sources/transforms.py:L9-L10).
