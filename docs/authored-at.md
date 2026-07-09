# Authored date (`authored_at`)

Conversation transcripts carry a per-line ISO-8601 `timestamp` (both Claude Code and
Codex JSONL). The miner records the most recent one per file as the drawer's
**`authored_at`** — when the content was actually written.

This is distinct from the ingest date:

| Field | Meaning |
|-------|---------|
| `filed_at` / result `created_at` | When the drawer was **mined** (written to the palace). A bulk re-mine collapses these to a single instant. |
| `authored_at` | When the underlying content was **written**, recovered from the transcript timestamps. Survives re-mining. |

`authored_at` is surfaced in search results (and shown in the CLI `search` output), and is
used as a deterministic tie-break in hybrid ranking: candidates with identical scores order
with the more recently authored drawer first. Drawers without per-line timestamps (e.g.
markdown) fall back to `filed_at`.

## Backfilling existing memory

New mines populate `authored_at` automatically. Drawers mined before this feature only have
`filed_at`. Re-mining does **not** fix them — the scanner skips files already mined at the
current `NORMALIZE_VERSION`. Two options:

1. **In-place backfill (recommended — no re-embedding).** `scripts/backfill_authored_at.py`
   reads each convos drawer's source transcript and updates only the `authored_at` metadata.
   Idempotent and safe to re-run; embeddings are untouched.

   ```bash
   python scripts/backfill_authored_at.py \
       --palace ~/.mempalace/palace \
       --sessions ~/.claude --sessions ~/.codex          # dry run
   python scripts/backfill_authored_at.py \
       --palace ~/.mempalace/palace \
       --sessions ~/.claude --sessions ~/.codex --apply  # write
   ```

   For the Docker MCP image, mount the volume and session dirs read-only — see the header of
   `scripts/backfill_authored_at.py` for the exact `docker run` invocation.

   > Back up first: `tar czf palace-backup.tgz -C <palace-dir> .` (or snapshot the
   > `mempalace-data` volume).

2. **Drop and recreate.** Delete the affected drawers and re-mine the transcripts; the fresh
   mine stamps `authored_at`. Simpler, but re-embeds everything.
