# Virtual line numbering for drawers

**Proposed for mempalace 3.3.6.**
**Target file:** `mempalace/searcher.py` (new module-level functions, alongside `_tokenize`, `_bm25_scores`, etc.)
**New tests:** `tests/test_line_numbers.py` (21 cases, all green; see `PROOF.md`).

---

## What it does

Adds two pure functions that apply line numbers to drawer text **at read time**, without modifying anything stored on disk:

```python
render_with_line_numbers(text: str, start_line: int = 1) -> str
extract_line_range(text: str, line_start: int, line_end: int) -> str
```

A closet pointer like `→2026-01-18:L55-L72` now resolves by opening the drawer and calling `extract_line_range(drawer_text, 55, 72)`. The returned string carries line numbers `[55]` through `[72]` so the reader sees which drawer positions they are looking at, even though the drawer on disk has no line numbers in it.

---

## Why this design

### Read-time, not backfill

The alternative is to rewrite every drawer with `[N]` prefixes embedded in the stored text. That choice would:

1. **Invalidate every existing closet pointer** the moment a drawer is re-emitted with different numbering. Today there are 1,377+ drawers across users' palaces; rewriting them all to add `[N]` is a corpus-wide migration with no fallback.
2. **Couple storage to display.** A drawer's purpose is to be the verbatim of a day. The instant we mix presentation (line numbers) into storage, the "verbatim" claim becomes conditional on the renderer.
3. **Lose idempotence.** Re-running mine on the same source would either skip (and miss new line-number conventions) or re-rewrite (and shift numbering on edits).

Read-time numbering sidesteps all three. The drawer on disk is exactly what was written. The grid exists only in the act of reading.

### Already-numbered passthrough

Some drawers arrive at the function already prefixed with `[N]` — e.g. transcripts from pre-numbering tools, or output of an earlier render that was saved. Detection is a `^\[\d+\]` regex on each line; matched lines pass through unchanged. The counter **still advances** on those lines so positional alignment with the underlying drawer is preserved.

This is defensive, not aspirational: most drawers are plain text, but mixed inputs must not double-prefix.

### Pure, no I/O

Neither function reads or writes the palace. They take strings, return strings. The caller (the search pipeline higher up in `searcher.py`) is responsible for opening drawers and persisting results. This keeps the primitive testable in isolation and reusable from anywhere — MCP server, CLI, future Flutter UI — without dragging in chromadb.

---

## API reference

### `render_with_line_numbers(text, start_line=1)`

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `text` | `str \| None` | (required) | The drawer body. `None` is treated as empty. |
| `start_line` | `int` | `1` | Number assigned to the first line of the output. |

**Returns:** `str` — each line prefixed with `[N] `, where `N` is the running counter beginning at `start_line`. Lines that already match `^\[\d+\]` pass through unchanged; the counter still advances.

**Pure:** input is not mutated.

```python
>>> render_with_line_numbers("alpha\nbeta")
'[1] alpha\n[2] beta'

>>> render_with_line_numbers("first\nsecond", start_line=5)
'[5] first\n[6] second'

>>> render_with_line_numbers("[42] kept\nplain line")
'[42] kept\n[2] plain line'
```

### `extract_line_range(text, line_start, line_end)`

| Parameter | Type | Meaning |
|---|---|---|
| `text` | `str` | The full drawer body. |
| `line_start` | `int` | First line of the slice, 1-indexed, inclusive. Values below `1` are clamped to `1`. |
| `line_end` | `int` | Last line of the slice, 1-indexed, inclusive. Values past the end of the drawer are clipped to the drawer length. |

**Returns:** `str` — the extracted slice with virtual line numbers starting at `line_start`. Empty / invalid ranges return `""` (no exception).

**Pure:** input is not mutated.

```python
>>> extract_line_range("a\nb\nc\nd\ne", 2, 4)
'[2] b\n[3] c\n[4] d'

>>> extract_line_range("a\nb\nc", 2, 99)   # end clipped to drawer length
'[2] b\n[3] c'

>>> extract_line_range("a\nb\nc", 5, 2)    # invalid range
''
```

---

## Edge cases covered by the test suite

The 21 tests in `tests/test_line_numbers.py` exercise:

**`render_with_line_numbers`**
- Empty string → `""`
- `None` → `""`
- Single line
- Multi-line
- Custom `start_line`
- Already-numbered passthrough (no double-prefix)
- Already-numbered passthrough with a custom `start_line` (line numbers in source win, not the counter)
- Mixed numbered + plain input (counter advances on every line)
- Blank lines (still numbered — they are real positions)
- Trailing newline semantics (`"a\nb\n".split("\n")` → 3 positions, all numbered)
- Input not mutated

**`extract_line_range`**
- Single-line extraction
- Inclusive range
- Full-document extraction
- End beyond length → clipped
- Start below 1 → clamped to 1
- Start > end → empty
- Empty text → empty
- Slice containing already-numbered lines (source numbers preserved, counter still advances elsewhere)
- Closet-pointer contract: extracting lines 5-7 returns `[5][6][7]`, not `[1][2][3]`
- Input not mutated

All 21 pass on Python 3.9 / pytest 8.4.2 in 0.01s. See `PROOF.md` for verbatim run output.

---

## Where to integrate in `mempalace/searcher.py`

Drop the two functions at module level, alongside the existing helpers (`_tokenize`, `_bm25_scores`, `_hybrid_rank`). They have no dependencies beyond `re` (already imported).

In the existing `search()` and `search_memories()` flows, when a result row carries explicit `line_start` / `line_end` metadata (e.g. from a closet pointer), pass the drawer text through `extract_line_range` before returning. When a result has no explicit range (BM25 / vector hit on the full drawer body), pass it through `render_with_line_numbers` instead. Both behaviors are additive — existing callers that ignore line numbers get the same string back, just with a `[N] ` prefix per line.

No new dependencies. No changes to drawer storage. No migration step.

---

## Out of scope for 3.3.6 (deferred)

- **Conversation-boundary detection** (the original `_extract_section` in private tooling expanded line ranges to natural conversation boundaries using timestamp heuristics). That logic is corpus-shape-specific and belongs in its own PR after we agree on the heuristic.
- **Day-aggregated drawers** (v4 spec — one drawer per day). 3.3.6 keeps the existing chunk-shaped drawer model. Line numbering works for both, but day-aggregation is a separate architectural change.
- **Halls / tunnels.** Unrelated to read-time rendering.
- **CLI surface.** No new flags. The behavior is internal to the search pipeline.

---

## Backwards compatibility

- **Drawer storage unchanged.** Existing palaces continue to work without migration.
- **Closet pointers unchanged.** The `→date:Lstart-Lend` syntax is unchanged; this PR adds the resolver, not the syntax.
- **Public API additive.** Two new module-level functions; nothing renamed or removed.
- **No new required arguments** on existing functions. If a caller never passes `line_start` / `line_end`, behavior is identical to today.
