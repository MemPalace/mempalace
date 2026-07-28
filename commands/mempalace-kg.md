---
description: Commit this session's established facts to the knowledge graph, after showing them to you for review.
---

Invoke the `mempalace` skill from this plugin and run the `kg` instructions, then follow them.

Concretely: run `mempalace instructions kg` in a terminal, then carry out the steps it prints.

Facts enter the graph only after you approve them. A wrong triple is read back by later
sessions as fact, so this is deliberately a reviewed commit rather than an automatic one.
