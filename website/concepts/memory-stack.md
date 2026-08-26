# Memory Stack

MemPalace uses a 4-layer memory stack. Each layer loads progressively more data only when needed.

## The Layers

| Layer | What | Size | When |
|-------|------|------|------|
| **L0** | Identity — who is this AI? | ~50-100 tokens | Always loaded |
| **L1** | Essential Story — top moments | ~500-800 tokens | Always loaded |
| **L2** | Room Recall — filtered retrieval | ~200–500 each | When topic comes up |
| **L3** | Deep Search — full semantic query | Variable | When explicitly asked |

In the current implementation, a typical wake-up is roughly **~600-900 tokens** for L0 + L1. Searches only fire when needed.

## Layer 0: Identity

A plain text file at `~/.mempalace/identity.txt`. Always loaded as the AI's self-concept.

```text
I am Atlas, a personal AI assistant for Alice.
Traits: warm, direct, remembers everything.
People: Alice (creator), Bob (Alice's partner).
Project: A journaling app that helps people process emotions.
```

~50 tokens. Tells the AI who it is and who it works with.

## Layer 1: Essential Story

Auto-generated from the highest-importance drawers in the palace. Groups by room, picks the top moments, and keeps the output bounded.

The generation process:
1. Reads all drawers from ChromaDB
2. Scores each by importance/emotional weight
3. Takes the top 15 moments
4. Groups by room for readability
5. Truncates to fit within 3,200 characters

```
## L1 — ESSENTIAL STORY

[auth-migration]
  - Team decided to migrate from Auth0 to Clerk — pricing + DX  (session_2026-01-15.md)
  - Kai debugged the OAuth token refresh issue  (session_2026-01-20.md)

[deploy-process]
  - Switched to blue-green deploys after the January outage  (session_2026-02-01.md)
```

### What Layer 1 leaves out

Wake-up has room for about 15 lines, so the selection filters before it
renders. It is deterministic and lexical — no LLM call, nothing that would
blow the hook's latency budget:

- Drawers that are structurally not prose are skipped: a harness wrapper such
  as `<system-reminder>` *opening* the text, a bare timestamp, table soup, or
  too few letters to be writing at all.
- Mentioning tooling is not disqualifying. A drawer that quotes a fenced
  command, an exit code or a tool-call name is still someone's account of what
  happened, so it ranks lower but is never dropped. Only the shape of the text
  decides, which is why raw JSON, log lines and command output fail while a
  sentence quoting a path passes.
- Drawers whose wording reports an outcome (shipped, fixed, decided, reverted,
  root cause) rank above ordinary prose. The keyword list is the module
  constant `L1_OUTCOME_KEYWORDS` in `mempalace/layers.py` and is English. It is
  the one language-specific part of Layer 1: drawers in another language are
  ranked by the remaining signals instead. To change it, replace the tuple and
  recompile the derived regex with `_l1_compile_outcome_re`.
- No single source file contributes more than two lines, so one long
  transcript cannot own the whole story.
- Near-duplicate drawers collapse to one line.
- Snippets start at a sentence boundary and cut on a word boundary, so a line
  never opens or ends mid-word.

Nothing is deleted or rewritten by this: skipped drawers stay in the palace
verbatim and are still returned by Layer 2 and Layer 3. If every candidate is
filtered out, Layer 1 renders the unfiltered top drawers rather than nothing.

## Layer 2: On-Demand Recall

Loaded when a specific topic or wing comes up in conversation. Retrieves drawers filtered by wing and/or room — typically ~200–500 tokens.

```python
stack = MemoryStack()
stack.recall(wing="driftwood", room="auth")
# → returns recent drawers about auth in the driftwood project
```

## Layer 3: Deep Search

Full semantic search against the entire palace. This is what fires when you or the AI explicitly asks a question.

```python
stack.search("why did we switch to GraphQL")
# → returns top-5 matching drawers with similarity scores
```

## Wake-Up Budget

The point of the stack is bounded startup context, not a fixed universal token count. The exact size depends on your identity file and what Layer 1 selects, but the implementation keeps wake-up meaningfully smaller than loading the full corpus into the prompt.

## Using the Stack

### CLI

```bash
# Wake-up context (L0 + L1)
mempalace wake-up

# Project-specific wake-up
mempalace wake-up --wing driftwood
```

### Python API

```python
from mempalace.layers import MemoryStack

stack = MemoryStack()

# L0 + L1: wake-up (~600-900 tokens in typical use)
print(stack.wake_up())

# L2: on-demand recall
print(stack.recall(wing="myapp"))

# L3: deep search
print(stack.search("pricing change"))

# Status
print(stack.status())
```
