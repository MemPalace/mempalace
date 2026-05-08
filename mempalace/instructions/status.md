# MemPalace Status

Display the current state of the user's memory palace.

## Step 1: Gather Palace Status

Check if MCP tools are available (look for mempalace_status in available tools).

- If MCP is available: Call the mempalace_status tool to retrieve palace state.
- If MCP is not available: Run the CLI command: mempalace status

## Step 2: Display Wing/Room/Drawer Counts

Present the palace structure counts clearly:
- Number of wings
- Number of rooms
- Number of drawers
- Total memories stored

Keep the output concise -- use a brief summary format, not verbose tables.

## Step 3: Knowledge Graph Stats (MCP only)

If MCP tools are available, also call:
- mempalace_kg_stats -- for a knowledge graph overview (triple count, entity
  count, relationship types)
- mempalace_graph_stats -- for connectivity information (connected components,
  average connections per entity)

Present these alongside the palace counts in a unified summary.

## Step 4: Suggest Next Actions

Based on the current state, suggest one relevant action:

- Empty palace (zero memories): Suggest "Try /mempalace:mine to add data from
  files, URLs, or text."
- Has data but no knowledge graph (memories exist but KG stats show zero
  triples): Suggest "Consider adding knowledge graph triples for richer
  queries."
- Healthy palace (has memories and KG data): Suggest "Use /mempalace:search to
  query your memories."

## Output Style

- Be concise and informative -- aim for a quick glance, not a report.
- Use short labels and numbers, not prose paragraphs.
- If any step fails or a tool is unavailable, note it briefly and continue
  with what is available.
