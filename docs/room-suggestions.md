# Optional room suggestions

Room suggestions classify existing drawers against your reviewed room definitions
using an installed local Ollama decision model. The feature is off until you run
it. Classification runs in a detached background process, checkpoints each result,
and never changes a drawer. Nothing is added to hooks or startup.

## Start and review

First create a room set with the existing `rooms propose --wing my-project`
command and review its saved JSON file at `<palace>/rooms/my-project.json`.
You can also author that file yourself. The decision
classifier accepts 1–25 rooms with explicit descriptions, plus its reserved
`__none__` answer for content that does not fit. Room names are never invented by
the classifier.

Install a decision model yourself, for example `ollama pull d3:4b`. This download
contacts Ollama's model registry. The feature never downloads a model or switches
to a cloud provider. Your Ollama runtime must support `/v1/systemone` and advertise
the installed model's `decision` capability. Ordinary chat models use a different
API and cannot be substituted here.

```sh
mempalace rooms suggestions start --wing my-project --model d3:4b --limit 100
# Copy the job ID printed by start:
mempalace rooms suggestions status JOB_ID
mempalace rooms suggestions preview JOB_ID
mempalace rooms suggestions preview JOB_ID --offset 20
```

Use the global `--palace PATH` and `--backend NAME` flags before `rooms` when
working with a particular palace. Every suggestions command supports `--json`.
`start --foreground` is available for scripts that need to wait.

The default scope covers the miner's fallback rooms: `general`, `technical`,
`architecture`, `planning`, and `problems`. Manually filed rooms stay outside the
job unless you specify `--from room-one,room-two` or `--from all`. Bookkeeping
records are excluded. A job considers at most 100 drawers by default; `--limit`
accepts 1–10000. Run one job at a time per palace.

Preview shows the drawer ID, original/current room, proposed room, model score,
outcome and a verbatim excerpt read from the current drawer. It pages through all
outcomes, with suggested changes first (`--limit 1..200`, `--offset N`). Full
original content remains available through normal drawer retrieval.

The default score threshold is 0.8, adjustable with `start --threshold`. This is
a model probability, **not a calibrated accuracy guarantee**. Rejected, uncertain,
unchanged, stale and skipped drawers receive no proposed move. Inputs that exceed
the 64 KiB request limit or the runtime's context capacity are skipped without
truncating the original. Network failures stop the job with unfinished rows still
pending. The [pilot](ollama-room-decision-pilot.md) explains the initial evaluation
and its limits; users should review predictions on their own data.

## Apply reviewed changes and undo

```sh
# These commands only report status; neither writes without --yes:
mempalace rooms suggestions apply JOB_ID
mempalace rooms suggestions undo JOB_ID

# Apply selected reviewed suggestions, or omit --ids to apply every suggestion:
mempalace rooms suggestions apply JOB_ID --ids DRAWER_ID ANOTHER_ID --yes
mempalace rooms suggestions undo JOB_ID --yes
```

Explicit apply and undo currently support local Chroma, SQLite exact and Rust
exact palaces. Remote stores may be previewed; they cannot be changed by this
feature. Apply and undo take the existing palace writer lease. If a hub owns that
lease, stop its writer first and retry the same operation.

Only room metadata and an already-present `last_modified` timestamp change.
Drawer text, stored vectors and other metadata are preserved. A closet follows
only when every drawer from its source in that room moves to the same destination;
partial selections leave its room alone. The durable journal covers both drawers
and affected closets. New or removed source drawers, or changes to unselected
drawers' rooms, invalidate a pending closet move or its undo. Once apply starts its selection is frozen: retry it with
the same IDs, or without `--ids`. Start another job for additional selections.

Before writing, apply checks that the approved taxonomy, configured collection,
drawer text and all recorded metadata still match the reviewed job. Undo checks
the recorded post-apply state. Later edits block the operation instead of being
overwritten. Undo still works if you edit or remove the taxonomy afterward.

A crash can leave an operation partially applied. Retry `apply JOB_ID --yes` or
`undo JOB_ID --yes` to replay the journal idempotently. You can also undo a partial
apply. This is a recoverable per-record journal, not a transaction spanning every
drawer and closet. Keep the local suggestion state until you no longer need undo.
If later edits prevent recovery, the job becomes `apply_conflict` or
`undo_conflict`. Its journal stays available for review and the same operation can
be retried after the conflict is resolved. Other suggestion jobs can still run.

## Background controls and privacy

```sh
mempalace rooms suggestions list
mempalace rooms suggestions cancel JOB_ID
mempalace rooms suggestions resume JOB_ID
```

Cancel waits for the current request, then pauses while retaining completed
predictions. The per-request timeout defaults to 120 seconds (`--timeout 1..600`).
Resume retries pending rows; it refuses a changed model digest, classifier rubric
or taxonomy. A process killed abruptly may still show `running`; resume is safe
once its worker lease has been released. Resource use is bounded by one sequential
inference at a time; the runtime may use CPU/GPU while a job runs.

Inference uses an HTTP loopback Ollama endpoint only. Proxy forwarding, HTTP
redirects, cloud model tags and remote model aliases are refused. Model identity
is checked before each inference. There is no external fallback.

The owner-only sidecar at
`~/.mempalace/room-suggestions/<palace-key>/suggestions.sqlite3` stores
job settings, hashes, original metadata, predictions and undo journals. It does
not copy drawer bodies. Cached predictions are keyed by the full input, taxonomy,
model identity and rubric. Inference holds a separate job lease and never occupies
the palace writer lease while waiting for the model. It does not alter the
ordinary embedding-based `rooms apply` command.
