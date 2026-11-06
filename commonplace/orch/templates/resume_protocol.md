Resume protocol. Your session can end at any moment; the orchestrator journal on the hub is the memory, not this conversation.

1. Start every session with `orch_resume` for your board and role. It claims the lease (never cards) and returns a checkpoint: open steps, steps `in_doubt`, steps that need a person, holds, the next step, jobs, misses and todo cards that have no start. Keep the lease token in memory only; never print it, post it or write it to a file.
2. Card classes in the checkpoint are read-only: `held` (yours, heartbeat it), `expired` and `not_held_by_caller`. Do not take over cards you do not hold.
3. Before each external effect, write the step to the journal (`intent`), then act, then record the result with its evidence: a runner job, the card state, or your own attestation.
4. Never repeat an external effect that may already have happened. For a step `in_doubt`, check the truth first:
   - merge: read the pull request state and head;
   - gate: read the review thread for the `Reviewed head:` and `Verdict:` lines; if they are missing, mark the step `needs_person`;
   - runner start: list threads by the job marker `[commonplace-job:<id>]`; an empty list is no proof, so mark the step `needs_person`;
   - card change: read the card.
   Only when the check shows the effect did not happen may you issue it again.
5. One gate per board, repository, pull request and head. A failed gate is final for that head; push a new head instead of asking again.
6. Wait in slices of at most 10 minutes and send `orch_heartbeat` between slices, so the lease does not expire while you wait. If the lease is lost or fenced, stop writing and call `orch_resume` again; a lost token means waiting for expiry or an owner force.
7. Start cards only through `orch_card_start` with a stable step key for the step; a retry with the same key returns the same job.
