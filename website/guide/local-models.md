# Local Models

MemPalace works with any local LLM — Llama, Mistral, or any offline model. Since local models generally don't speak MCP yet, there are two approaches.

## Wake-Up Command

Load your world into the model's context:

```bash
mempalace wake-up > context.txt
# Paste context.txt into your local model's system prompt
```

This gives your local model a bounded wake-up context, typically around **~600-900 tokens** in the current implementation. It includes:
- **Layer 0**: Your identity — who you are, what you work on
- **Layer 1**: Top moments from the palace — key decisions, recent work

For project-specific context:
```bash
mempalace wake-up --wing driftwood > context.txt
```

## CLI Search

Query on demand, feed results into your prompt:

```bash
mempalace search "auth decisions" > results.txt
# Include results.txt in your prompt
```

## Python API

For programmatic integration with your local model pipeline:

```python
from mempalace.searcher import search_memories

results = search_memories(
    "auth decisions",
    palace_path="~/.mempalace/palace",
)

# Format results for your model's context
context = "\n".join(
    f"[{r['wing']}/{r['room']}] {r['text']}"
    for r in results["results"]
)

# Inject into your local model's prompt
prompt = f"Context from memory:\n{context}\n\nUser: What did we decide about auth?"
```

## AAAK for Compression

Use [AAAK dialect](/concepts/aaak-dialect) to compress wake-up context further:

```bash
mempalace compress --wing myapp --dry-run
```

AAAK is readable by any LLM that reads text — Claude, GPT, Gemini, Llama, Mistral — without a decoder.

## Full Offline Stack

The core memory stack can run offline:
- **ChromaDB** on your machine — vector storage and search
- **Local model** on your machine — reasoning and responses
- **AAAK** for compression — optional, no cloud dependency
- **Optional reranking or external model integrations** may introduce cloud calls, depending on how you configure the system
