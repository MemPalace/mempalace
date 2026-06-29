# Python API

High-level overview of the key Python interfaces you'd use to integrate MemPalace into your application.

## Search

The primary way to query the palace programmatically.

```python
from mempalace.searcher import search_memories

results = search_memories(
    query="why did we switch to GraphQL",
    wing="myapp",          # optional filter
    room="architecture",   # optional filter
    n_results=5,
)

# Results structure:
# {
#     "query": "...",
#     "filters": {"wing": "myapp", "room": "architecture"},
#     "results": [
#         {"text": "...", "wing": "...", "room": "...", "source_file": "...", "similarity": 0.89}
#     ]
# }
```

## Memory Stack

The 4-layer memory system with a unified interface.

```python
from mempalace.layers import MemoryStack

stack = MemoryStack()  # uses default paths from MempalaceConfig

# Wake-up: L0 (identity) + L1 (essential story)
context = stack.wake_up(wing="myapp")  # ~600-900 tokens

# On-demand: L2 retrieval
recall = stack.recall(wing="myapp", room="auth", n_results=10)

# Deep search: L3 semantic search
results = stack.search("pricing change", wing="myapp")

# Status
status = stack.status()
```

## Knowledge Graph

Temporal entity-relationship graph built on SQLite.

```python
from mempalace.knowledge_graph import KnowledgeGraph

kg = KnowledgeGraph()  # uses default path: ~/.mempalace/knowledge_graph.sqlite3

# Write
kg.add_entity("Kai", entity_type="person")
kg.add_triple("Kai", "works_on", "Orion", valid_from="2025-06-01")
kg.invalidate("Kai", "works_on", "Orion", ended="2026-03-01")

# Read
facts = kg.query_entity("Kai", as_of="2026-01-15", direction="both")
relationships = kg.query_relationship("works_on")
timeline = kg.timeline("Orion")
stats = kg.stats()
```

## Palace Graph

Room-based navigation graph built from ChromaDB metadata.

```python
from mempalace.palace_graph import build_graph, traverse, find_tunnels, graph_stats

# Build the graph
nodes, edges = build_graph()

# Navigate
path = traverse("auth-migration", max_hops=2)
tunnels = find_tunnels(wing_a="wing_code", wing_b="wing_team")
stats = graph_stats()
```

## AAAK Dialect

Lossy compression for token density at scale.

```python
from mempalace.dialect import Dialect

# Basic
dialect = Dialect()
text = "We decided to use GraphQL because REST was too chatty for our dashboard."
compressed = dialect.compress(text)

# With entity mappings
dialect = Dialect(entities={"Alice": "ALC", "Bob": "BOB"})
compressed = dialect.compress(text, metadata={"wing": "myapp"})

# From config
dialect = Dialect.from_config("entities.json")

# Stats
stats = dialect.compression_stats(text, compressed)
```

## Configuration

```python
from mempalace.config import MempalaceConfig

config = MempalaceConfig()
print(config.palace_path)       # ~/.mempalace/palace
print(config.collection_name)   # mempalace_drawers
```

For detailed parameter documentation, see [API Reference](/reference/api-reference).
