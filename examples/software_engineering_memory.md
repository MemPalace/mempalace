# Software Engineering Memory

MemPalace works well for software engineering because a lot of important project knowledge now lives in conversations, scattered notes, and half-finished docs instead of one clean source of truth.

That includes:

- architecture debates
- implementation tradeoffs
- debugging history
- handoff notes
- release checks
- "we tried this and it failed because..." context

MemPalace gives that knowledge a structure that is still local-first and still keeps the original words available.

## Example Project Layout

Imagine a team working on a customer onboarding flow.

One practical memory shape might look like this:

- `wing_onboarding_app`
  The project itself

Halls inside that wing:

- `hall_facts`
  Locked decisions and accepted constraints
- `hall_events`
  Sessions, milestones, and debugging work
- `hall_discoveries`
  Breakthroughs, surprises, and things learned during implementation
- `hall_preferences`
  Tool choices, framework decisions, and working patterns
- `hall_advice`
  Recommended fixes, patterns, and next steps

Rooms inside those halls:

- `workflow-owner`
- `feature-onboarding-flow`
- `incident-form-timeout`
- `release-april`
- `handoff-implementation`

The important part is not the exact names. The important part is that memory stops being a flat pile of search results and starts becoming navigable.

## What Belongs In Memory

Each piece of content stored in a room is called a **drawer** - a chunk of text with metadata such as wing, room, and source file. When you mine a project, MemPalace splits your files into drawers automatically.

Useful engineering artifacts include:

- design notes
- decision records
- architecture discussions
- incident notes
- implementation handoffs
- validation and release notes

These are exactly the kinds of things teams repeat or lose over time.

## How Retrieval Helps By Stage

### Planning

When planning new work, the useful retrieval targets are usually:

- prior decisions
- comparable features
- known constraints
- earlier failures

That helps the model or the engineer start from the actual project history instead of rebuilding context from scratch.

### Implementation

During implementation, useful retrieval often looks like:

- the active feature room
- the latest handoff notes
- validated examples
- recent debugging history

That is especially useful when switching between humans, AI tools, or sessions with limited context windows.

### Validation

Before trusting an output, useful retrieval often includes:

- release notes
- expected behaviors
- known failure modes
- prior incident rooms

That makes memory useful not only for generating work, but for checking work.

## Software Engineering Handoffs

One especially practical use case is handoff between actors working on the same codebase.

The outgoing actor should:

1. save durable artifacts in indexed project paths
2. write a bounded handoff note
3. refresh MemPalace after those artifacts exist

The incoming actor should:

1. search for the handoff note
2. use its retrieval hints to find related project memory
3. open the authoritative files named in the handoff
4. validate the current state before continuing

The important operational detail is the refresh step. Writing the handoff is not enough if the next actor is going to start cold and rely on shared memory to find it.

This pattern is useful for:

- human-to-human handoffs
- human-to-agent handoffs
- agent-to-agent handoffs

## Why This Matters

The value of memory is not just "better search."

The value is that project knowledge becomes:

- durable
- structured
- easier to hand off
- easier to retrieve at the right time
- more useful to both humans and coding agents

That makes MemPalace a strong fit for long-running software projects, especially when decisions and reasoning happen in chat as often as they happen in code review or tickets.

## Getting Started

- [basic_mining.py](basic_mining.py) - mine a project directory into your palace
- [mcp_setup.md](mcp_setup.md) - connect MemPalace to your AI assistant via MCP
