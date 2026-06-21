# Behavior Spec: Lone-Surrogate Sanitisation

This file is a test suite that pins the observable contract for stripping lone
UTF-16 surrogates (code points U+D800–U+DFFF) from user-supplied text before it
reaches the storage backend. It exists because MCP clients sometimes relay lone
surrogates, which cannot be UTF-8 encoded and otherwise crash backend
add/upsert operations with a `-32000` Internal Error
(tests/test_clean_lone_surrogates.py:L1-L18).

## Core sanitiser: `strip_lone_surrogates(text) -> text`

A function that accepts a string and returns a string of the same logical text
with every lone surrogate code point replaced by the Unicode replacement
character U+FFFD ("�"). The returned string MUST always be UTF-8 encodable
(tests/test_clean_lone_surrogates.py:L62-L67).

### Pass-through (no surrogates)
Plain ASCII text is returned unchanged (tests/test_clean_lone_surrogates.py:L29-L30).
Non-ASCII text such as CJK characters is returned unchanged, both alone and
mixed with ASCII (tests/test_clean_lone_surrogates.py:L31-L32).
The empty string returns the empty string (tests/test_clean_lone_surrogates.py:L34-L35).

### Replacement of lone surrogates
A single lone high-range surrogate embedded between normal text is replaced with
one U+FFFD, leaving surrounding text intact: `"hello\udc95world"` →
`"hello�world"` (tests/test_clean_lone_surrogates.py:L37-L38). A run of three lone
surrogates becomes three U+FFFD characters
(tests/test_clean_lone_surrogates.py:L39).

A lone low-range surrogate (e.g. U+D800) embedded in text is likewise replaced
by a single U+FFFD: `"test\ud800more"` → `"test�more"`
(tests/test_clean_lone_surrogates.py:L41-L43).

Multiple lone surrogates at different positions are each replaced independently,
preserving the interleaved normal characters: `"a\udca1b\udcffc"` → `"a�b�c"`
(tests/test_clean_lone_surrogates.py:L45-L46). A string consisting solely of lone
surrogates becomes the same count of U+FFFD characters, one per surrogate
(tests/test_clean_lone_surrogates.py:L58-L60).

### Replacement is per-code-point, not pairing
The sanitiser does NOT attempt to combine adjacent surrogates into an astral
code point. Two adjacent lone surrogates yield two U+FFFD characters
(tests/test_clean_lone_surrogates.py:L43, L59). Four adjacent lone surrogates yield
four U+FFFD characters (tests/test_clean_lone_surrogates.py:L60).

### Real (astral) characters preserved
Valid astral-plane code points (e.g. U+1F600, U+1F680) — which contain no lone
surrogates when represented as single code points — pass through unchanged,
alone or embedded in surrounding text
(tests/test_clean_lone_surrogates.py:L48-L52). When a real astral code point is
adjacent to a lone surrogate, the astral code point is preserved and only the
lone surrogate is replaced, regardless of order: `U+1F600 + \udc95` →
`U+1F600 + �`, and `\udc95 + U+1F600` → `� + U+1F600`
(tests/test_clean_lone_surrogates.py:L54-L56).

### Observable contract: result is encodable
The cleaned string MUST be UTF-8 encodable without error; encoding it and
hashing (SHA-256 hex) yields a 64-character hex digest
(tests/test_clean_lone_surrogates.py:L62-L67). A specific real-world payload
`"2026-04-27\udcadworkBuddy relay"` MUST sanitise to
`"2026-04-27�workBuddy relay"` and then encode without raising
(tests/test_clean_lone_surrogates.py:L69-L74).

## Sanitiser wrappers route through the strip

Three higher-level sanitisers perform lone-surrogate stripping inline so all
callers inherit the fix:

- `sanitize_content(text)`: `"hello\udc95world"` → `"hello�world"`
  (tests/test_clean_lone_surrogates.py:L84-L87).
- `sanitize_kg_value(text)`: `"Alice\udc95"` → `"Alice�"`
  (tests/test_clean_lone_surrogates.py:L89-L92).
- `sanitize_query(text)`: returns a result object whose `clean_query` field
  contains no lone surrogate and contains U+FFFD in its place
  (tests/test_clean_lone_surrogates.py:L94-L99).

## End-to-end MCP tool contract

Every MCP write/read tool that accepts user text MUST not crash on lone
surrogates in any of its string arguments. The tools are exercised against a
patched server where `_config` and the knowledge-graph accessor are replaced
(tests/test_clean_lone_surrogates.py:L105-L114).

- `tool_add_drawer(wing, room, content, ...)`: with a lone surrogate in
  `content`, returns `success == True` and a `drawer_id` that begins with
  `"drawer_<wing>_<room>_"` (here `"drawer_test_surrogate_"`)
  (tests/test_clean_lone_surrogates.py:L116-L126). Lone surrogates in the optional
  `source_file` and `added_by` metadata arguments are also tolerated, still
  returning `success == True` (tests/test_clean_lone_surrogates.py:L128-L139).
- `tool_check_duplicate(content)`: with a lone surrogate in `content`, returns a
  dict containing the key `is_duplicate`
  (tests/test_clean_lone_surrogates.py:L141-L147).
- `tool_search(query)`: with a lone surrogate in `query`, returns a dict that
  either lacks an `error` key or contains a `results` key (i.e. does not crash)
  (tests/test_clean_lone_surrogates.py:L149-L155).
- `tool_update_drawer(drawer_id, content)`: updating an existing drawer with a
  lone surrogate in `content` returns `success == True`
  (tests/test_clean_lone_surrogates.py:L157-L168).
- `tool_diary_write(agent_name, entry, topic)`: with CJK `agent_name`, a lone
  surrogate embedded in `entry`, returns `success == True`
  (tests/test_clean_lone_surrogates.py:L170-L179).

## Backend chokepoint contract (`ChromaCollection`)

The storage backend collection wrapper MUST strip lone surrogates from the
`documents` payload of its `add`, `upsert`, and `update` operations before
forwarding to the underlying client. This guards bulk-ingest paths that build
documents without routing through the content sanitiser; a single poisoned row
must not abort the entire batch (tests/test_clean_lone_surrogates.py:L202-L208).

The contract is verified by wrapping a capturing collection that records the
forwarded keyword arguments (tests/test_clean_lone_surrogates.py:L185-L215):

- `add(documents=["clean\udc95doc"], ids=["1"])` forwards
  `documents == ["clean�doc"]`, which is UTF-8 encodable
  (tests/test_clean_lone_surrogates.py:L217-L222).
- `upsert(documents=["a\ud800b"], ids=["1"], metadatas=[{...}])` forwards
  `documents == ["a�b"]` (tests/test_clean_lone_surrogates.py:L224-L228).
- `update(ids=["1"], documents=["x\udcffy"])` forwards
  `documents == ["x�y"]` (tests/test_clean_lone_surrogates.py:L230-L234).
- A batch of mixed documents where only one row carries a surrogate forwards all
  rows, with only the poisoned row cleaned and the `ids` list length preserved:
  `["ok one", "poison\udc95row", "ok three"]` →
  `["ok one", "poison�row", "ok three"]` with 3 ids; each forwarded document is
  UTF-8 encodable (tests/test_clean_lone_surrogates.py:L236-L248).
- Real astral code points in documents are preserved:
  `add(documents=["ship \U0001f680"], ...)` forwards
  `["ship \U0001f680"]` unchanged (tests/test_clean_lone_surrogates.py:L250-L254).

### Ordering / type invariant: bare-string document is not split
A `documents` argument supplied as a single bare string (rather than a list) MUST
be sanitised as one whole document and forwarded as a string of the same type,
NOT split into per-character documents:
`upsert(documents="one\udc95document", ...)` forwards
`documents == "one�document"` and that value is still a string
(tests/test_clean_lone_surrogates.py:L256-L264).
