# MemPalace Audit and Repair Session

Measure how well organized the user's palace is, then walk them through
fixing it. The audit is the measurement; the repair session is a structured
conversation where the user decides and you execute. Never restructure a
palace on your own initiative: every rename, move, or deletion below is the
user's call, made one question at a time.

## Step 1: Run the audit

Run the CLI (there is no MCP equivalent; the audit reads the palace files
directly and is safe while the MCP server is running):

```bash
mempalace audit --json
```

Parse the JSON. It contains `scores` (0-100 per layer plus `overall`), one
section per layer (`rooms`, `naming`, `tunnels`, `hallways`,
`knowledge_graph`), and `findings`, a list of `{layer, text}` sentences.

If the command is missing, the installed MemPalace predates the audit; tell
the user to upgrade and stop. If it exits 1, the palace path is wrong; ask
which palace to audit and pass `--palace <path>`.

## Step 2: Present the scorecard

Show the five layer scores and the overall score in a short table, then the
findings grouped by layer, worst layer first. Keep this to a glance: the
detail lives in the JSON and you will surface it per question in Step 3.

Explain in one sentence what each low score means for recall:

- **rooms** low: drawers are findable by search but not by walking the
  palace; wake-up and traversal see one undifferentiated blob.
- **naming** low: the same wing or room exists under two spellings, so a
  scoped search or wake-up misses half of it.
- **tunnels** low: cross-wing links exist but nothing follows them, or they
  link generic tokens that connect nothing.
- **hallways** low: the strongest associations are an entity linked to its
  own file or path spelling, which crowds out real associations.
- **knowledge graph** low: nearly every fact has its own predicate, so
  `mempalace_kg_query` cannot find facts by relation.

Ask whether the user wants to run a repair session now. If not, stop here.

## Step 3: Repair session, one layer at a time

Work through the layers in the order below, skipping any with no findings.
For each, ask **one structured question with a small set of options**, wait
for the answer, then act. Put the recommended option first and say why.
After each action, say exactly what changed, in numbers.

### 3a. Naming: wings spelled two ways (`naming.wing_drift`)

For each group, show the spellings and their drawer counts from `rooms`
data, then ask:

> Wings `acme-app` (31 drawers) and `acme_app` (158 drawers) are one
> project. Which should survive?
> 1. Merge into `acme_app` (Recommended: the larger one, and the
>    underscore form matches how `init` normalizes names)
> 2. Merge into `acme-app`
> 3. Keep both (they are different things)

To merge: list the drawers in the losing wing with `mempalace_list_drawers`
(wing filter) and move each with `mempalace_update_drawer`, setting `wing`
to the survivor and leaving content untouched. Also ask whether any explicit
tunnels reference the losing wing (`mempalace_list_tunnels` with that wing)
and recreate them on the survivor before deleting the old ones. State the
count before you start; for more than a few hundred drawers, say so and
confirm again, because moves are one call per drawer.

Do not use `mempalace migrate-wings` for this: it only strips leading and
trailing separators and does not merge two distinct spellings.

### 3b. Naming: rooms spelled two ways (`naming.room_drift`)

Same question shape, per group, scoped to the wing(s) named in the finding:

> In wing `mempalace`, rooms `release-3.6.0` and `release_3_6_0` are one
> room. Merge into which?
> 1. `release-3.6.0` (Recommended: hyphenated slugs are the documented
>    room convention)
> 2. `release_3_6_0`
> 3. Keep both

Move drawers with `mempalace_update_drawer` setting `room`. Groups that span
many wings (for example `release` / `releases`) are a convention decision:
ask once which spelling is canonical, then apply it wing by wing, confirming
each wing's count.

### 3c. Naming: wings that may be one project (`naming.wing_prefix_pairs`)

These are **not scored** because siblings like `mempalace` / `mempalace-ts`
are often legitimately separate. Ask per pair:

> `arcade` (2 drawers) and `arcade_game` (36 drawers): same project?
> 1. Yes, merge into `arcade_game` (Recommended: keeps the fuller wing)
> 2. Yes, merge into `arcade`
> 3. No, separate projects

Merge as in 3a. Accept "no" without argument.

### 3d. Naming: stub wings (`naming.tiny_wings`)

Group them before asking. Agent-identity wings (`wing_mac-codex`,
`wing_windows-claude`, and similar) usually belong together; one-drawer
project wings are usually a test mine. Ask per group:

> Six wings hold one to three drawers each and are named after agent
> identities. What should happen to them?
> 1. Move their drawers into a shared wing such as `shared_agent_brain`
>    (Recommended)
> 2. Leave them; they are intentional
> 3. Show me each drawer first

Never delete a drawer to tidy a wing. Moving is reversible; deletion is not.
If the user asks to delete, read the drawer back to them first and require
an explicit yes per drawer, then use `mempalace_delete_drawer`.

### 3e. Tunnels (`tunnels`)

The score is quality × coverage. Quality: no generic tokens, no dangling
wings, no duplicate spellings. Coverage: the share of *linkable* wings
(wings that share a strong entity with another wing) that a sound tunnel
reaches; `tunnels.unlinked_wings` lists the rest. If `tunnels.artifacts` is
non-zero, ask:

> 5 of 28 tunnels link generic tokens or a wing that no longer exists.
> 1. Run `mempalace tunnels prune` then `--yes` (Recommended)
> 2. Keep them

If `tunnels.unlinked_wings` is non-empty, offer `mempalace tunnels propose`:
it skips links that already exist, gives every unlinked wing its strongest
link first, then fills by strength, and writes a plan; show the user the
top rows, let them delete any, then `--yes`. Traversal
(`tunnels.never_traversed`) is reported but not scored: every call to
`mempalace_follow_tunnels` records the crossing, so it rises with use.

### 3f. Hallways (`hallways`)

Self-links (`main.zig` ↔ `src/main.zig`) and spelling-variant duplicates
(`ChatStore ↔ RootView` filed again as `ChatStore.swift ↔ RootView`) are an
entity-normalization artifact of older mines, not user data. Show
`artifact_sample` and the counts, then ask:

> 36 of the 100 strongest hallways are spelling artifacts (2437 self-links
> and 30805 duplicates overall). Options:
> 1. Run `mempalace hallways --prune-spellings` now, then `--yes` to apply
>    (Recommended; edits only the hallways sidecar, drawers untouched)
> 2. Leave them; the current miner no longer writes them, so they fade on
>    the next full re-mine
> 3. Skip

The prune keeps the highest-count record of each association under its
shortest spellings. Do not fetch the whole hallway list through MCP on a
large palace; the CLI reads the sidecar directly.

### 3g. Knowledge graph (`knowledge_graph`)

Show `one_off_sample` and the reuse ratio. Ask:

> 51 of 57 facts use a predicate no other fact uses. Options:
> 1. Normalize them onto the fixed vocabulary (`works_on`, `owns`,
>    `depends_on`, `uses`, `decided`, `status`, `located_in`, `measured`)
>    with `mempalace kg normalize`, keeping history (Recommended)
> 2. Adopt the vocabulary for new facts only
> 3. Skip

For option 1 run `mempalace kg normalize` (the LLM proposes a rewrite per
fact into `<palace>/kg/normalize.json`), show the user the rows, let them
edit or drop any, then `--yes`: each old fact is closed and its rewrite
opened at one instant, so nothing is lost. Either way record the vocabulary
as a drawer in wing `global`, room `conventions`, with
`mempalace_add_drawer`, so every agent that wakes up finds it.

### 3h. Rooms: generic-room concentration (`rooms`)

This is the largest problem. The transcript miner files most drawers into
`technical`, `architecture`, `planning`, `general`, and `problems`. Do not
move drawers by hand at this scale; there are tools.

First, if `naming.mixed_wings` lists a wing, it is a machine-level export
holding many projects, and no room set can be meaningful for it. Ask:

> `claude_conversations_windows` holds 27 projects in 124k drawers.
> Options:
> 1. Split it into one wing per project with `mempalace wings split`
>    (Recommended; existing project wings receive their drawers)
> 2. Leave it mixed

Run `mempalace wings split --wing <wing>`, show the plan's targets (edit
derived names in the JSON with the user: strip organisation prefixes, send
home-directory sessions to a machine wing), then `--yes`. Applying needs the
palace lock, so the MCP server must be stopped for the seconds it takes.
Afterwards, while the server is still stopped, run
`mempalace hallways --rebuild`: it needs the palace lock too, because it
scans drawers.

Then ask per flat wing (`rooms.flat_wings`, largest first):

> 98% of `claude_conversations` (12443 drawers) sits in `technical`.
> Options:
> 1. Propose a closed room set for it with the local LLM, review the file,
>    then apply (Recommended)
> 2. Accept it for this wing; rely on search rather than the room layer
> 3. Skip

For option 1 run, in order:

```bash
mempalace rooms propose --wing <wing>            # samples 60 drawers → <palace>/rooms/<wing>.json
mempalace rooms apply --wing <wing>              # dry run: how many drawers would move where
mempalace rooms apply --wing <wing> --yes        # writes room metadata only
```

Show the user the proposed rooms before applying and let them edit the
JSON (rename, merge, delete rooms). `propose` sends the sampled excerpts to
the configured LLM: local endpoints (Ollama, vLLM, NInfer, LM Studio) need
nothing; a non-local endpoint refuses unless `--accept-external-llm` is
passed, and you must say so before passing it. `apply --yes` needs the
palace lock, so it fails while an MCP server or a mine holds the palace;
tell the user to stop the server first and restart it after.

Whatever they pick, record the decision as a drawer in wing `global`, room
`decisions`, so the next audit does not re-ask.

## Step 4: Re-run and report

Run `mempalace audit` again (text mode) and show the before and after
scores side by side. List every change made with counts: drawers moved,
tunnels deleted, hallways deleted, conventions recorded. List anything
deferred and why.

Write a diary entry with `mempalace_diary_write` summarizing the session:
the before and after scores, each decision the user made, and what was
deferred. That is the record the next session reads.

## Rules for the whole session

- One question at a time. Never batch five decisions into one prompt.
- Numbers before actions. Say how many drawers a move touches before doing
  it; confirm again above a few hundred.
- Moves over deletions. A wrong move is one more move; a deletion is gone.
- Verbatim always. Moving a drawer changes its `wing` or `room`, never its
  content.
- Read before delete. When the user asks to delete a drawer, read it back
  first and require an explicit yes.
- Do not run `mempalace repair`. That rebuilds the vector index and is for
  corruption, not organization.
- If the MCP server is not connected, everything above except the audit
  itself is unavailable; say so and point to the `mempalace` setup skill.
