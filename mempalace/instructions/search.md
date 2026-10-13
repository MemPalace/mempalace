# MemPalace Search

When the user wants to search their MemPalace memories, follow these steps:

## 1. Parse the Search Query

Extract the core search intent from the user's message. Identify any explicit
or implicit filters:
- Wing -- a top-level category (e.g., "work", "personal", "research")
- Room -- a sub-category within a wing
- Keywords / semantic query -- the actual search terms

## 2. Determine Wing/Room Filters

Use a wing and/or room filter when its name is known and relevant to the
question. If unsure, omit filters to search globally, or discover the taxonomy
first. An empty scoped search is a reason to check or widen the scope, not
proof that the memory does not exist.

## 3. Use MCP Tools (Preferred)

Choose the tool that fits the question, then stop when it answers the question.
Follow the shared [recall protocol's query guide](https://github.com/MemPalace/mempalace/blob/develop/integrations/shared/recall-protocol.md#retrieve-only-the-context-you-need):

- mempalace_kg_query(entity, as_of, direction) -- Start here for a known
  entity's relationships or time-bound facts. Set as_of for a historical date;
  without it, facts include active, historical, and future relationships, so
  use active_facts for current-state questions. If the KG is empty or does not
  answer the question, search the stored text.
- mempalace_search(query, wing, room, limit) -- Start here for decisions,
  explanations, or exact source words. Use a short query, relevant known
  filters, and a small limit such as 3. Search returns stored text; retrieve a
  specific drawer if you need its complete content, including a logical
  drawer split into chunks.
- mempalace_diary_read(agent_name, last_n, wing) -- Start here for recent
  agent continuity or a handover. Use a small last_n such as 3 and a relevant
  wing if known; omitting wing reads across this agent's wings. Results are
  newest first and may be chunks of longer entries, not complete sessions.
  For older or topic-specific context, use search.

These are starting points, not a mandatory KG-to-search-to-diary sequence.
Request more context only when the answer needs it. Response sizes vary with
the number of facts and text length; result limits do not set a token budget.

Use discovery tools when scope or navigation needs clarification:

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
- Quote the stored memory content verbatim

## 6. Offer Next Steps

If the results answer the question, stop. When more context is needed, offer
relevant options to go deeper:
- Drill deeper -- search within a specific room or narrow the query
- Traverse -- explore the knowledge graph from a related room
- Check tunnels -- look for cross-wing connections if the topic spans domains
- Browse taxonomy -- show the full structure for manual exploration
