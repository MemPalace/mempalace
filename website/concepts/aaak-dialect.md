# AAAK Dialect

AAAK is an experimental lossy abbreviation system designed to pack repeated entities and relationships into fewer tokens at scale. It is readable by any LLM — Claude, GPT, Gemini, Llama, Mistral — without a decoder.

::: warning Experimental
AAAK is a separate compression layer, **not the storage default**. The 96.6% benchmark score comes from raw verbatim mode. AAAK mode currently scores 84.2% R@5 — a 12.4 point regression. We're iterating.
:::

## What AAAK Is

- **Lossy, not lossless.** Uses regex-based abbreviation, not reversible compression.
- **A structured summary format.** Extracts entities, topics, key sentences, emotions, and flags from plain text.
- **Readable by any LLM.** No decoder needed — models read it naturally.
- **Designed for scale.** Saves tokens when the same entities appear hundreds of times.

## What AAAK Is Not

- **Not lossless compression.** The original text cannot be reconstructed.
- **Not efficient at small scale.** Short text already tokenizes efficiently — AAAK overhead costs more than it saves.
- **Not the default storage format.** MemPalace stores raw verbatim text in ChromaDB.

## Format

```
Header:   FILE_NUM|PRIMARY_ENTITY|DATE|TITLE
Zettel:   ZID:ENTITIES|topic_keywords|"key_quote"|WEIGHT|EMOTIONS|FLAGS
Tunnel:   T:ZID<->ZID|label
Arc:      ARC:emotion->emotion->emotion
```

### Entity Codes

Three-letter uppercase codes: `ALC=Alice`, `KAI=Kai`, `MAX=Max`.

### Emotion Codes

| Code | Meaning | Code | Meaning |
|------|---------|------|---------|
| `vul` | vulnerability | `joy` | joy |
| `fear` | fear | `trust` | trust |
| `grief` | grief | `wonder` | wonder |
| `rage` | rage | `love` | love |
| `hope` | hope | `despair` | despair |
| `peace` | peace | `humor` | humor |
| `tender` | tenderness | `raw` | raw honesty |
| `doubt` | self-doubt | `relief` | relief |
| `anx` | anxiety | `exhaust` | exhaustion |

### Flags

| Flag | Meaning |
|------|---------|
| `ORIGIN` | Origin moment (birth of something) |
| `CORE` | Core belief or identity pillar |
| `SENSITIVE` | Handle with absolute care |
| `PIVOT` | Emotional turning point |
| `GENESIS` | Led directly to something existing |
| `DECISION` | Explicit decision or choice |
| `TECHNICAL` | Technical architecture detail |

## Example

**Input:**
```
We decided to use GraphQL instead of REST because the frontend team needs
flexible queries. Kai recommended it after researching both options. The team
was excited about the schema-first approach.
```

**AAAK output:**
```
0:KAI|graphql_rest_decided|"decided to use GraphQL instead of REST"|determ+excite|DECISION+TECHNICAL
```

## Usage

### Compress drawers

```bash
# Preview compression
mempalace compress --wing myapp --dry-run

# Compress and store
mempalace compress --wing myapp
```

### With entity config

```bash
mempalace compress --wing myapp --config entities.json
```

Entity config format:
```json
{
  "entities": {"Alice": "ALC", "Bob": "BOB"},
  "skip_names": ["Gandalf", "Sherlock"]
}
```

### Python API

```python
from mempalace.dialect import Dialect

# Basic compression
dialect = Dialect()
compressed = dialect.compress("We decided to use GraphQL...")

# With entity mappings
dialect = Dialect(entities={"Alice": "ALC", "Kai": "KAI"})
compressed = dialect.compress(text, metadata={"wing": "myapp", "room": "arch"})

# From config file
dialect = Dialect.from_config("entities.json")
```

## When to Use AAAK

AAAK is most useful when:
- You have **many repeated entities** across thousands of sessions
- You need to **compress context** for local models with small windows
- You want **structured summaries** pointing back to verbatim drawers

For most users, raw verbatim mode is the better default.
