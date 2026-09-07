# Authored dates and search-result provenance

Conversation transcripts carry a per-line ISO-8601 `timestamp` (both Claude Code and
Codex JSONL). The miner records the most recent one per file as the drawer's
**`authored_at`**. Where transcript timestamps are available, this records a
source timestamp rather than the time the transcript was imported.

This is distinct from the ingest date:

| Field | Meaning |
|-------|---------|
| `filed_at` / result `created_at` | When the drawer was **mined** (written to the palace). If a drawer is re-imported, this reflects that import rather than its original authorship. |
| `authored_at` | The stored authorship timestamp, with the historical `filed_at` fallback when that metadata key is absent. Inspect `authored_at_source` before treating it as an authorship date. |
| `authored_at_source` | `authored_at` for a stored authorship timestamp; `filed_at` for the filing-time fallback; `unknown` for missing, empty, null, or unknown values. This labels the metadata field used, not independent verification of its accuracy. |
| `content_date` | A separate date inferred during file mining. It is not substituted for `authored_at` and is not necessarily the date the content was written. |
| `content_date_source` | `filename`, `frontmatter`, `body`, or `mtime` for newly recorded file-date provenance; `unknown` when no derivation was recorded. |

`authored_at` is surfaced in structured search results and is
used as a deterministic tie-break in hybrid ranking: candidates with identical scores
order by descending `authored_at` value, which may itself be a filing-time fallback.
Drawers without stored authorship metadata (e.g. project Markdown files) fall back
to `filed_at`.

The legacy `created_at` and `authored_at` result values are unchanged. Search now
also returns `filed_at` explicitly, equal to `created_at`. The additive provenance
fields are available on vector, union, and lexical-only search paths, including
results forwarded by the lightweight MCP server.

## Inferred content dates

File mining looks for a date in this order: filename, YAML frontmatter, content
body, then filesystem modification time. It records which source supplied the
date. A filename may name an event rather than an authoring date, and copying a
file may change its modification time; none of these heuristics proves authorship.

For example, a file without authorship metadata, imported on September 6 but
carrying an August 27 frontmatter date, can produce:

```json
{
  "created_at": "2026-09-06T12:00:00",
  "filed_at": "2026-09-06T12:00:00",
  "authored_at": "2026-09-06T12:00:00",
  "authored_at_source": "filed_at",
  "content_date": "2026-08-27",
  "content_date_source": "frontmatter"
}
```

The September date is visibly an ingestion fallback, not evidence that the note
was authored that day. The August date remains a separately labeled clue.

Existing stored `content_date` values are exposed without re-mining. Their
`content_date_source` remains `unknown` unless it was recorded at ingestion;
search never reconstructs historical provenance from today's file or timestamp.
An absent content date is returned as `null`. No migration or automatic re-mine
is required, and unchanged files skipped by incremental mining retain their
existing metadata.

**Filtering and ranking are unchanged:** `since` and `before` filter the filing
timestamp, not `authored_at` or `content_date`. Hybrid tie-breaking retains its
existing behavior. Content-date filtering would require a separate explicit API.

## Backfilling existing memory

New conversation mines populate `authored_at` automatically. Drawers mined before this feature only have
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
