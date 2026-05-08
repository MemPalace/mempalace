# MemPalace Search

When the user wants to search their MemPalace memories, follow these steps:

## 1. Parse the Search Query

Extract the core search intent from the user's message. Identify any explicit
or implicit filters:
- Wing -- a top-level category (e.g., "work", "personal", "research")
- Room -- a sub-category within a wing
- Keywords / semantic query -- the actual search terms

## 2. Determine Wing/Room Filters

If the user mentions a specific domain, topic area, or context, map it to the
appropriate wing and/or room. If unsure, omit filters to search globally. You
can discover the taxonomy first if needed.

## 3. Use MCP Tools (Preferred)

If MCP tools are available, use them in this priority order:

- mempalace_search(query, wing, room) -- Primary search tool. Pass the semantic
  query and any wing/room filters.
- mempalace_list_wings -- Discover all available wings. Use when the user asks
  what categories exist or you need to resolve a wing name.
- mempalace_list_rooms(wing) -- List rooms within a specific wing. Use to help
  the user navigate or to resolve a room name.
- mempalace_get_taxonomy -- Retrieve the full wing/room/drawer tree. Use when
  the user wants an overview of their entire memory structure.
- mempalace_traverse(room) -- Walk the knowledge graph starting from a room.
  Use when the user wants to explore connections and related memories.
- mempalace_find_tunnels(wing1, wing2) -- Find cross-wing connections (tunnels)
  between two wings. Use when the user asks about relationships between
  different knowledge domains.

## 4. CLI Fallback

If MCP tools are not available, fall back to the CLI:

    mempalace search "query" [--wing X] [--room Y]

## 5. Present Results

When presenting search results:
- Always include source attribution: wing, room, and drawer for each result
- Show relevance or similarity scores if available
- Group results by wing/room when returning multiple hits
- Quote or summarize the memory content clearly

## 6. Offer Next Steps

After presenting results, offer the user options to go deeper:
- Drill deeper -- search within a specific room or narrow the query
- Traverse -- explore the knowledge graph from a related room
- Check tunnels -- look for cross-wing connections if the topic spans domains
- Browse taxonomy -- show the full structure for manual exploration
