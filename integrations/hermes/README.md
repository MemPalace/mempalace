# MemPalace × Hermes Integration

Plug MemPalace into [Hermes](https://github.com/NousResearch/hermes-agent) as a
first-class memory provider. Every conversation you have with your AI gets filed
into the palace automatically — verbatim, searchable, permanent.

## One-command install

pip install mempalace
mempalace hermes install

That's it. Hermes will use MemPalace for memory on the next session start.

## What it does

- **Session start:** Injects your identity + AAAK critical facts (~170 tokens) into
  the system prompt automatically.
- **Every turn:** Files the exchange to the palace immediately — wing-classified,
  semantic-search ready, available in the next session.
- **Session end:** Mines the full session + regenerates the AAAK critical facts layer.
- **Before compression:** Extracts key exchanges before Hermes compresses context.

## 8 tools available to your AI

| Tool | What |
|------|------|
| `mempalace_search` | Semantic search across all sessions |
| `mempalace_status` | Palace overview + AAAK spec |
| `mempalace_list_wings` | Wings with counts |
| `mempalace_list_rooms` | Rooms within a wing |
| `mempalace_kg_query` | Entity relationships with time filtering |
| `mempalace_kg_add` | Add facts to knowledge graph |
| `mempalace_diary_write` | Write AAAK diary entry |
| `mempalace_diary_read` | Read recent diary entries |

## Manual install

If you prefer to install manually:

1. Copy `integrations/hermes/__init__.py` to
   `~/.hermes/hermes-agent/plugins/memory/mempalace/__init__.py`
2. Copy `integrations/hermes/backfill.py` to
   `~/.hermes/hermes-agent/plugins/memory/mempalace/backfill.py`
3. In `~/.hermes/config.yaml`, set `memory.provider: mempalace`
4. Restart Hermes: `hermes gateway start`

## Backfill existing sessions

Mine your existing Hermes session history into the palace:

cd ~/.hermes/hermes-agent
venv/bin/python3 -m plugins.memory.mempalace.backfill

## Configuration

Configure via `hermes memory setup` or by editing `~/.mempalace/`:

- `identity.txt` — Layer 0: who you are (loaded every session, ~50 tokens)
- `wing_config.json` — Wing routing: maps keywords to wings for auto-classification
  Generate with: `mempalace init <your-project-dir>`

## Benchmark

MemPalace scores 100% on LongMemEval R@5 (with Haiku rerank) and 96.6% raw —
the highest published score, free, no cloud API required.
