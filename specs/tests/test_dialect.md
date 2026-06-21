# Behavior Spec: `tests/test_dialect.py`

This is a test module that pins down the observable behavior of the AAAK Dialect
compression system, exercised through the public `Dialect` type
(`tests/test_dialect.py:L1-L8`). The behaviors below are the contracts the
implementation under test must satisfy; each is the externally observable
guarantee asserted by the tests.

## Construction

A `Dialect` can be constructed with no arguments (`tests/test_dialect.py:L13-L13`).
It optionally accepts an `entities` mapping from a human name to a 3-character
code (e.g. `{"Alice": "ALC", "Bob": "BOB"}`) (`tests/test_dialect.py:L30-L30`,
`tests/test_dialect.py:L42-L42`). It optionally accepts a `skip_names` list of
names to suppress from entity encoding (`tests/test_dialect.py:L52-L52`).

## `compress(text, metadata=None) -> str`

`compress` takes a text string (and optional `metadata`) and returns a string
(`tests/test_dialect.py:L14-L15`). For non-empty input the returned string is
non-empty (`tests/test_dialect.py:L16-L16`) and contains at least one pipe `|`
character, since the AAAK output is pipe-separated fields
(`tests/test_dialect.py:L17-L18`).

When `metadata` is provided as a mapping with keys `wing`, `room`, and
`source_file`, the values for `wing` and `room` appear verbatim in the output
(e.g. `"project"` and `"backend"`) (`tests/test_dialect.py:L20-L27`).

When the `Dialect` was constructed with an `entities` map and the input text
mentions one or more of those entities, at least one of the corresponding codes
appears in the output (e.g. `"ALC"` or `"BOB"`) (`tests/test_dialect.py:L29-L32`).

Empty input text is accepted: `compress("")` returns a string (no error)
(`tests/test_dialect.py:L34-L37`).

## Entity detection

`_detect_entities_in_text(text) -> collection of codes`. For a text mentioning a
known entity, the configured code is present in the result (e.g. `"ALC"` for a
sentence mentioning "Alice") (`tests/test_dialect.py:L41-L44`).

For an unknown entity (no configured code) detected in text, the system
auto-assigns a code; at least one returned code has exactly length 3
(`tests/test_dialect.py:L46-L49`). The 3-character code length is the observable
contract for entity codes.

`encode_entity(name) -> code | None`. When a name is in `skip_names`, encoding it
returns `None`, even if that name also appears in the `entities` map
(`tests/test_dialect.py:L51-L54`). `skip_names` takes precedence over `entities`.

## Emotion detection

`_detect_emotions(text) -> list of emotions`. For text expressing emotion, the
result is non-empty (`tests/test_dialect.py:L58-L60`). The number of emotions
returned is capped at a maximum of 3, even when the text names more than three
distinct emotions (`tests/test_dialect.py:L63-L67`).

## Topic extraction

`_extract_topics(text) -> list of topics`. For text with substantive content the
result is non-empty and contains at most 3 topics
(`tests/test_dialect.py:L71-L78`). Technical and repeated/capitalized terms are
favored: a term mentioned multiple times and capitalized (e.g. "GraphQL") appears
in the topic list (compared case-insensitively) (`tests/test_dialect.py:L80-L85`).

## Key sentence extraction

`_extract_key_sentence(text) -> str`. From multi-sentence text, the returned
sentence is the most salient one; sentences signaling a decision (containing words
like "decided" or "instead") are selected over neutral sentences
(`tests/test_dialect.py:L89-L97`). The returned key sentence is length-bounded:
its length is at most 55 characters, with long sentences truncated
(`tests/test_dialect.py:L99-L103`).

## Compression statistics

`compression_stats(original, compressed) -> mapping`. The returned mapping has
exactly this key set: `original_chars`, `summary_chars`, `original_tokens_est`,
`summary_tokens_est`, `size_ratio`, and `note`
(`tests/test_dialect.py:L118-L130`). For a real compression where the compressed
form is shorter, `size_ratio` is greater than 1 and `original_chars` exceeds
`summary_chars` (`tests/test_dialect.py:L106-L113`).

`Dialect.count_tokens(text) -> int` is a static/class-level operation that
estimates token count; `count_tokens("hello world")` returns `2`
(i.e. whitespace-separated word count) (`tests/test_dialect.py:L115-L116`).

## Zettel encoding

`encode_zettel(zettel) -> str`. The input is a mapping describing a zettel with
fields including `id`, `people` (list of names), `topics` (list), `content`,
`emotional_weight`, `emotional_tone`, `origin_moment`, `sensitivity`, `notes`,
`origin_label`, and `title` (`tests/test_dialect.py:L134-L149`). The encoded
output contains the entity code for people listed (e.g. `"ALC"` for "Alice" when
that mapping is configured) and contains topic terms verbatim (e.g. `"memory"`)
(`tests/test_dialect.py:L150-L151`).

`encode_tunnel(tunnel) -> str`. The input is a mapping with keys `from`, `to`, and
`label` (`tests/test_dialect.py:L155-L155`). The encoded output begins with /
contains the marker `"T:"` and embeds the numeric portions of both endpoint ids
(e.g. `"001"` and `"002"` from `"zettel-001"`/`"zettel-002"`)
(`tests/test_dialect.py:L153-L159`).

## Decoding round-trip

`decode(encoded) -> mapping`. Given an AAAK-encoded string of the form
`HEADER\nARC:<arc>\n<zettel-line>` where the header is
`<file>|<entity-codes>|<date>|<title>` and a zettel line is
`<id>:<codes>|<topics>|<quote>|<weight>|<tone>`
(`tests/test_dialect.py:L165-L167`), the decoded mapping contains:
`decoded["header"]["file"]` equal to the header file field (e.g. `"001"`),
`decoded["arc"]` equal to the arc value (e.g. `"journey"`), and
`decoded["zettels"]` a list whose length matches the number of zettel lines
(e.g. length 1) (`tests/test_dialect.py:L162-L171`).

<promise>SPEC_WRITTEN path=specs/tests/test_dialect.md citations=30</promise>
