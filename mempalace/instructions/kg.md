# MemPalace KG — commit facts to the knowledge graph

Turn what this session actually established into knowledge-graph triples, **reviewed by the
user before they land**.

## Why this is a command and not a hook

Drawers and the knowledge graph fail differently. A drawer that stores a claim later shown
wrong is still an honest record of what was said. A KG triple asserting that claim is read
back by future sessions as **fact**, and a wrong one is actively harmful — it does not just
fail to help, it steers the next decision wrong.

Sessions routinely reverse themselves. A metric can be treated as authoritative for weeks
and be retired in an afternoon once its floor is measured; an arm can look like the winner
until its control reproduces the score. Anything that auto-extracted triples mid-session
would have committed the pre-reversal version and then kept serving it. So the graph takes
facts only by explicit, reviewed commit.

## Step 1: Propose

Read back over the session and draft candidate triples: `subject → predicate → object`,
plus `valid_from` (YYYY-MM-DD) when the fact has a start date.

Propose only facts that are:
- **settled** — not a hypothesis, a partial measurement, or something a pending run may flip;
- **durable** — useful to a session weeks from now, not an artifact of today's cwd or queue;
- **attributable** — traceable to a measurement, a file, or an explicit user decision.

Skip: in-progress work, numbers that a control arm has not yet checked, anything phrased as
"seems" or "probably". Those belong in a drawer or a memory file, not the graph.

Prefer specific predicates (`achieves_real_robot_SR`, `was_retired_because`,
`is_worktree_of`) over vague ones (`relates_to`, `has`). The predicate carries the meaning
when the triple is read back with no surrounding context.

Note: subject/predicate/object reject some punctuation. Spell out `+` and `,` — write
`gather tb4`, not `gather+tb4`.

## Step 2: Show the user

Present the candidates as a compact table — subject, predicate, object, valid_from — and
say plainly which ones you are least sure about and why. Then ask which to commit.

Do not commit anything before the user answers. If they trim the list, commit only what
they kept.

## Step 3: Commit

For each approved candidate call `mempalace_kg_add`. Report the count committed.

## Step 4: Supersede, do not duplicate

If a new fact replaces an older one, call `mempalace_kg_invalidate` on the old triple in the
same pass. A graph holding both the retired and the current version of a fact is worse than
one holding neither, because a query returns both and the reader cannot tell which is live.

Check first with `mempalace_kg_query` on the subject.

## Step 5: Persist beyond the palace

The graph lives outside the palace directory and outside any git repo. If the project keeps
an export script (`export_kg.py` writing `kg/triples.jsonl`), run it so the new facts reach
the backup — drawers can be re-mined from source markdown, hand-authored triples cannot.
