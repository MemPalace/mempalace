# The Palace

Ancient Greek orators memorized entire speeches by placing ideas in rooms of an imaginary building. Walk through the building, find the idea. MemPalace applies the same principle to AI memory.

## Structure

Your conversations are organized into a navigable hierarchy:

```mermaid
graph LR
    classDef wingPerson fill:#1e1b4b,stroke:#4f46e5,color:#e0e7ff,stroke-width:2px,rx:8px,ry:8px;
    classDef wingProject fill:#164e63,stroke:#06b6d4,color:#cffafe,stroke-width:2px,rx:8px,ry:8px;
    classDef room fill:#312e81,stroke:#6366f1,color:#e0e7ff,stroke-width:1px,rx:4px,ry:4px;
    classDef closet fill:#3b0764,stroke:#8b5cf6,color:#f3e8ff,stroke-width:1px,rx:4px,ry:4px;
    classDef drawer fill:#0f766e,stroke:#14b8a6,color:#ccfbf1,stroke-width:1px,rx:4px,ry:4px;
    classDef tunnel_link stroke:#8b5cf6,stroke-width:2px,stroke-dasharray: 5 5;

    subgraph W1 [WING: Person]
        direction TB
        RA["Room A"]
        RB["Room B"]
        CA["Closet"]
        DA["Drawer (verbatim)"]
        
        RA -- "hall" --> RB
        RA --> CA --> DA
    end

    subgraph W2 [WING: Project]
        direction TB
        RA2["Room A"]
        RC["Room C"]
        CA2["Closet"]
        DA2["Drawer (verbatim)"]
        
        RA2 -- "hall" --> RC
        RA2 --> CA2 --> DA2
    end

    RA <==> |tunnel bridge| RA2

    class W1 wingPerson;
    class W2 wingProject;
    class RA,RB,RA2,RC room;
    class CA,CA2 closet;
    class DA,DA2 drawer;
```

## Components

### Wings

A person or project. As many as you need.

Every project, person, or topic gets its own wing in the palace. Wings are the top-level organizational unit.

### Rooms

Specific topics within a wing. Examples: `auth-migration`, `graphql-switch`, `ci-pipeline`.

Rooms are named ideas. They're auto-detected from your folder structure during `mempalace init`, and you can create additional rooms manually.

### Halls

Halls are the conceptual categories that describe how related memories connect *within* a wing:

- `hall_facts` — decisions made, choices locked in
- `hall_events` — sessions, milestones, debugging
- `hall_discoveries` — breakthroughs, new insights
- `hall_preferences` — habits, likes, opinions
- `hall_advice` — recommendations and solutions

### Tunnels

Connections *between* wings. When the same room appears in different wings, the graph layer can treat that as a cross-wing connection.

```
wing_kai       / hall_events / auth-migration  → "Kai debugged the OAuth token refresh"
wing_driftwood / hall_facts  / auth-migration  → "team decided to migrate auth to Clerk"
wing_priya     / hall_advice / auth-migration  → "Priya approved Clerk over Auth0"
```

Same room. Three wings. The graph can use that shared room name as a bridge.

### Closets

Closets are the summary layer in the broader MemPalace vocabulary: compact notes that point back to the original content. In the current implementation, the main persisted storage path is still the underlying drawer text plus metadata.

### Drawers

The original stored text chunks. This is the primary retrieval layer used by the current search and benchmark flows.

## Why Structure Matters

Wing and room identifiers become metadata filters at query time. Narrowing a search to a specific wing (or wing + room) means the vector store only scores candidates inside that scope, which is useful when you have many unrelated projects or people filed in the same palace.

This is standard metadata filtering in the underlying vector store, not a novel retrieval mechanism. The useful property here is operational — clear scoping rules that a human or an agent can apply predictably — not a magic retrieval boost.

## Navigation

The palace supports graph traversal across wings:

```text
MCP tool: mempalace_traverse
  arguments: { "start_room": "auth-migration" }
  → discovers rooms in wing_kai, wing_driftwood, wing_priya

MCP tool: mempalace_find_tunnels
  arguments: { "wing_a": "wing_code", "wing_b": "wing_team" }
  → auth-migration, deploy-process, ci-pipeline
```

This is the navigation story: shared room structure gives the model more than one way to reach relevant context.
