# WorkBuddy transcript support

**Module:** `mempalace/normalize.py` (reader), `mempalace/convo_miner.py` (path
detection and authorship time)
**Tests:** `tests/test_normalize.py`, `tests/test_convo_miner_unit.py`
**Related:** [`authored-at.md`](authored-at.md) — this format carries a numeric
`timestamp`, not the ISO-8601 string the other JSONL formats do.

---

## What it does

WorkBuddy (and its CodeBuddy engine) writes one JSONL file per session under
`~/.workbuddy/projects/<encoded-cwd>/<session-id>.jsonl`. `normalize.py` now
reads that shape, and `convo_miner.py` recognises the directory as a
transcript root.

The file is JSONL like Claude Code's and Codex's, but the row shape differs in
three ways:

| | Claude Code / Codex | WorkBuddy |
|---|---|---|
| author fields | nested: `{"message": {"role": …}}` | **top level**: `{"role": …}` |
| block type for prose | `{"type": "text"}` | **`input_text` / `output_text`** |
| row discriminator | `{"type": "user"}` / `{"type": "assistant"}` | **`{"type": "message"}`** |
| `timestamp` | ISO-8601 string | **epoch milliseconds (int)** |

A WorkBuddy transcript also **mixes both shapes**: some rows carry a nested
`message`, others put `role` at the top level. The reader accepts both, so a
file with either or both is read in full.

---

## The three injected blocks — same tag, opposite handling

WorkBuddy injects scaffolding into the stored transcript. Three of these blocks
carry a *verbatim copy of the user's own turn*, and the right response differs
per block. Handling them with one rule either loses the user's words or stores
seven thousand copies of them.

| Block | What it is | What we do |
|---|---|---|
| `<system-reminder>` | platform-injected system preamble | **strip the whole block** |
| `<user_query>` | **the user's actual words**, wrapped in a shell | **strip the shell, keep the words** |
| `<previous_*>` | the entire prior history, re-injected each turn | **strip the whole block** |
| `<cb_summary>` / `<conversation_history_summary>` | the platform's rolling compressed summary | **strip the whole block** |

The `<user_query>` case is the one that matters. The tag is swallowed by
`strip_noise()` — that is correct for a system reminder — but the text inside
it is the user speaking. `_peel_user_query()` unwraps the tag and keeps the
payload, so the transcript still stores the user's exact words.

**Ordering is load-bearing.** `_strip_summary_echo()` must run *before*
`_peel_user_query()`. The injected scaffolding re-emits user turns: across the
corpus the parsed text carries **14,186 `<user_query>` openers against 7,600
user turns**, so nearly half of those shells are copies sitting inside the
scaffolding rather than the turn itself. Unwrapping first would leave every copy
in the transcript as if the user had said it twice.

### Why a second pattern set, and not a change to the default

`_tag_pattern(name, cross_blank_lines=False)` picks one of two body patterns:

- **default (`False`)** — `body = r"(?:(?!\n\s*\n)[\s\S])*?"`: the body may not
  cross a blank line. That boundary is deliberate (see the comment on
  `_NOISE_TAG_PATTERNS`) — it stops a dangling open tag from swallowing several
  messages.
- **`True`** — the blank-line ban is *swapped* for a different boundary, not
  simply lifted: the body stops at the next line-initial opener for any of
  `_NOISE_TAGS`, i.e.
  `body = rf"(?:(?!\n(?:> )?<(?:{guard})(?:\s[^>]*)?>)[\s\S])*?"`. It still
  refuses to swallow a neighbouring injected block. The `KNOWN BOUNDARY`
  comment on `_tag_pattern` states the one shape this guard does not cover, and
  why that shape does not occur in real transcripts.

WorkBuddy's injected blocks routinely contain blank lines, so under the default
boundary the pattern matches *nothing* — the block is never stripped. Rather
than loosen the default, `_tag_pattern()` gained a second parameter:

```python
def _tag_pattern(name, cross_blank_lines=False): ...
_NOISE_TAG_PATTERNS = [_tag_pattern(t) for t in _NOISE_TAGS]                      # unchanged
_WORKBUDDY_TAG_PATTERNS = [_tag_pattern(t, cross_blank_lines=True) for t in ...] # new
```

`strip_noise(text)` keeps its old signature and its old default pattern list;
`strip_noise(text, tag_patterns=_WORKBUDDY_TAG_PATTERNS)` is the new call site.
Every existing caller is unaffected.

---

## Authorship time

`_extract_authored_at()` read only `isinstance(ts, str)`. A WorkBuddy
`timestamp` is an integer, so on this format the function returned `None` and
every drawer fell back to `filed_at` — the import time. A session from August
was stored with a September date.

The numeric branch converts epoch milliseconds to the same ISO-8601 shape the
string branch produces. It is **bounded on purpose**:

```python
_EPOCH_MS_MIN = 1_000_000_000_000   # 2001-09-09
_EPOCH_MS_MAX = 4_102_444_800_000   # 2100-01-01
```

A bare `1`, or a seconds-precision epoch such as `1234567890`, falls outside
the window and is **skipped rather than guessed**. Reading `1234567890` as
milliseconds would date the session to 1970 — an error of fifty years, which is
worse than admitting the time is unknown. Outside the window the function keeps
its existing `None` behaviour.

Measured: `1789268666044` → `2026-09-13T03:04:26.044Z`, against a session file
named `2026-09-13-11-04-25` (local time, UTC+8) — a one-second difference.

---

## Wing detection

`_resolve_wing()` defaults to `wing_api` when it cannot find a source path;
`_is_ai_tool_path()` is what it consults. It matched `.codex`, `.gemini`, and
the pair `.claude/projects`. `.workbuddy/projects` is added as the same kind of
**consecutive-segment pair** — `.workbuddy` alone is *not* a match, because the
engine's home holds non-conversation state (session heartbeats, config) beside
the transcript tree. The new pair is added as a **separate** loop rather than
folded into the existing one, so the `.claude` test keeps its exact prior
behaviour and the diff stays purely additive.

---

## Empirical check

[`tools/bench-normalize-regression.py`](../tools/bench-normalize-regression.py)
compares two source trees and prints everything below. Snapshot
`2026-09-27T17:14:13Z` on one local install. **The corpora are live** — new
sessions are written while they are sampled — so counts drift between runs; the
script records every file's input hash and excludes any file whose bytes changed
between the two passes. Re-run it against your own transcripts.

| Metric | Result |
|---|---|
| WorkBuddy files in the tree | 376 |
| Files compared (1 changed mid-run, excluded) | 375 |
| Read as conversations | **372 / 375** |
| Read by the unpatched tree | **0 / 375** |
| Rows matched by the WorkBuddy branch | **7,600** of 161,240 |
| Rows matched by the Claude-shaped branch on those same files | **0** |
| Rows matching **both** branches | **0** |

The 3 unread files are `subagents/agent-*.jsonl`, each holding a single
orphaned user message with no reply. `_try_workbuddy_jsonl` requires at least a
user/assistant pair, so it skips them — the same outcome as a subagent
transcript under any other format.

**The two branches cannot double-count.** Across those 161,240 rows the
nested-`message` branch matched zero and the top-level-`role` branch matched
7,600, with **no row satisfying both** — each row carries exactly one of the two
shapes.

### What the strips remove

Same tree, same parsing pass: the parsed text *before* the strip functions run,
against the text finally stored. Counts are line-anchored — the rule the
strippers themselves use.

| Injected block | Before | After |
|---|---|---|
| `<previous_assistant_message>` | 28,015 | 0 |
| `<previous_tool_call>` | 39,189 | 0 |
| `<previous_user_message>` | 7,406 | 1 |
| `<user_query>` (shell only — payload kept) | 14,186 | 0 |
| `<system-reminder>` | 6,943 | 2 |
| `<cb_summary>` | 203 | 3 |
| `<conversation_history_summary>` | 147 | 2 |

In total **80.7%** of the parsed conversation text is re-injected scaffolding —
**49.0M characters before the strips, 9.46M after**. The `<user_query>` payload,
the user's own words, is kept; only its shell goes.

The small residue in the "After" column is unpaired openers, which the
paired-only rule leaves verbatim rather than guessing at. For every block type
the opening and closing counts agree to within a fraction of a percent, with no
nesting observed.

Cross-format regression, byte-for-byte against the same tree without this patch:

| Format | Files | Result |
|---|---|---|
| WorkBuddy | 376 | 372 / 375 read (3 subagent fragments, by design) |
| Claude Code | 38 | 38 / 38, **byte-identical to before** |
| Pi agent | 15 | 14 / 14 compared, **byte-identical to before** (1 changed mid-run, excluded) |

Detection additionally requires `"type": "message"`, a value Claude Code's
`"user"` / `"assistant"` rows never carry, so the new reader cannot claim
another format's rows — which is what the byte-comparison above confirms.

---

## Cases NOT handled

- **`cwd` as a project root.** WorkBuddy's `cwd` is a per-session workspace
  directory (`.../WorkBuddy/2026-09-13-11-04-25`), recreated for every session.
  It carries no project name, so it is not used for wing naming here — see the
  hook-side handling of the same fact.
- **Sessions recovered from cloud sync.** Same limits as every other format.
- **Other CodeBuddy-derived products.** Only the transcript shape above was
  tested against real files.

---

## Backwards compatibility

- **No existing format changes behaviour.** The `<user_query>` peel and the
  echo strips run only inside `_try_workbuddy_jsonl()`; a Claude Code or Codex
  file does not reach them.
- **`strip_noise()` and `_tag_pattern()` keep their old signatures and their
  old defaults.** The new pattern list is opt-in at the call site.
- **No new dependencies.**
- **No on-disk format changes.** The drawers this produces have the same shape
  as every other convo-mined drawer.
