# Knowledge Graph

MemPalace includes a temporal entity-relationship graph — like Zep's Graphiti, but SQLite instead of Neo4j. Local and free.

## What It Stores

Entity-relationship triples with temporal validity:

```
Subject → Predicate → Object [valid_from → valid_to]
```

Facts have time windows. When something stops being true, you invalidate it — and historical queries still find it.

## Usage

### Python API

```python
from mempalace.knowledge_graph import KnowledgeGraph

kg = KnowledgeGraph()

# Add facts
kg.add_triple("Kai", "works_on", "Orion", valid_from="2025-06-01")
kg.add_triple("Maya", "assigned_to", "auth-migration", valid_from="2026-01-15")
kg.add_triple("Maya", "completed", "auth-migration", valid_from="2026-02-01")

# Query: everything about Kai
kg.query_entity("Kai")
# → [Kai → works_on → Orion (current), Kai → recommended → Clerk (2026-01)]

# Query: what was true in January?
kg.query_entity("Maya", as_of="2026-01-20")
# → [Maya → assigned_to → auth-migration (active)]

# Timeline
kg.timeline("Orion")
# → chronological story of the project
```

### Invalidating Facts

When something stops being true:

```python
kg.invalidate("Kai", "works_on", "Orion", ended="2026-03-01")
```

Now queries for Kai's current work won't return Orion. Historical queries still will.

### MCP Tools

Through the MCP server, the knowledge graph is available as tools:

| Tool | Description |
|------|-------------|
| `mempalace_kg_query` | Query entity relationships with time filtering |
| `mempalace_kg_add` | Add facts |
| `mempalace_kg_invalidate` | Mark facts as ended |
| `mempalace_kg_timeline` | Chronological entity story |
| `mempalace_kg_stats` | Graph overview |

## Storage

The knowledge graph uses SQLite with two tables:

**`entities`** — people, projects, tools, concepts:
- `id` — lowercase normalized name
- `name` — display name
- `type` — person, project, tool, concept, etc.
- `properties` — JSON blob for extra metadata

**`triples`** — relationships between entities:
- `subject` → `predicate` → `object`
- `valid_from` — when this became true
- `valid_to` — when it stopped being true (NULL = still current)
- `confidence` — 0.0 to 1.0
- `source_closet` — link back to the verbatim memory

Database location: `~/.mempalace/knowledge_graph.sqlite3`

## Related Work

Temporal entity-relationship graphs are a familiar pattern — Zep's
Graphiti, for example, also exposes a bi-temporal model. MemPalace's
knowledge graph is local-first (SQLite, everything on disk) and free;
Zep is a managed service backed by Neo4j with its own pricing, SLAs,
and compliance surface. See Zep's own [documentation](https://www.getzep.com/)
for authoritative details on their deployment model.
