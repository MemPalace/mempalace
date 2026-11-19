# Unfamiliar syntax and edge cases

Ordinary recall uses the entry point and exposed schemas; this is not a checklist.
Use host-exposed names/permissions. Missing/read-only tools do not permit policy
bypass. Schemas own signatures; never invent fields or use Python parameter names.

## Light payloads

`palace_query` takes `query`; `palace_exec` takes `command` or structured `action`
fields. These are tool arguments, not shell commands. For example:

- Read: `palace_query` with `{"query":"FIND database decision LIMIT 3"}`.
- File: `palace_exec` with `{"action":"add_drawer","wing":"myapp","room":"decisions","content":"Exact source text."}`.

Use real scope/content, never probe writes. Prefer structured fields for arbitrary
verbatim text to avoid DSL flag/quoting ambiguity.
Not every full-tool field is a light top-level field. Consult the relevant
[PQL subsection][pql] only for syntax not covered by exposed schemas.

## Interpretation and temporal writes

- Exact-source search uses the full stored `source_path` as `source_file`, not
  a basename/glob. Search/list date filters measure filing time, not event time.
  Recent diary rows may be chunks, not complete sessions.
- Confirm entity, predicate, evidence, and effective time before changing facts.
  Backfill already-ended facts with known `valid_from`/`valid_to`, not a fictional
  present-day change. Keep uncertain conflicts unchanged pending investigation.
- Atomic supersede's date-only `at` means midnight UTC; standalone invalidate's
  date-only `ended` retains whole-day validity. Use a known precise instant when
  needed; separate invalidate/add calls can overlap on the boundary day.
- Supersede can succeed without an open old fact and does not enforce global
  single-valued cardinality or repair conflicting history. Success is NOT proof
  the old fact existed. Inspect relevant facts before and the result/time-scoped
  query after a change.

## Filing and maintenance boundaries

Use real provenance, never an invented path. Drawer filing deduplicates; checkpoint
items remain verbatim and accept only schema-supported fields. If per-item source
provenance is needed but unsupported, use individual drawers. Diary observations
in AAAK are not user/source text; retain the entry point's hook/save-evidence check.

For requested imports/cleanup, confirm exact source/IDs/project/wing. Preview
supported operations, show count/sample, and obtain approval before applying.
For irreversible ID deletes without dry-run, inspect and present targets first.
Read hook settings before changing only approved fields; verify persistence.
Reconnect refreshes stale state after external writes, not corruption. Follow
[offline recovery][recovery], never blind re-mining or an in-process rebuild.

[pql]: https://mempalaceofficial.com/guide/lightweight-mcp.html#palace-query-language-pql-reference
[recovery]: https://github.com/MemPalace/mempalace/blob/develop/integrations/shared/recall-protocol.md#recovering-a-corrupt-index
