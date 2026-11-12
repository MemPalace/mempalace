### Per-change field fragments and union merge

- Many changes touch `tests/`, so each one had to edit the same `docs/modules/tests.md` block and, often, `SYSTEM.md`; a batch of two such PRs always conflicted (observed in the first lean batch: #382, #406 and #409 each merged cleanly alone and conflicted together). `docs-check` now accepts field fragments in `docs/modules/<module>.d/<name>.md` (Owns, Promises, How it fails or Tests bullets) appended to the module's fields; a fragment is an edit of the block. `SYSTEM.md` notes go in `docs/system/<section>.md` (already supported).

- Stopgap: `.gitattributes` sets `merge=union` for `docs/modules/tests.md` and `SYSTEM.md`, so a local batch merge keeps both sides of a concurrent append instead of conflicting. Union can also keep two edits of the same line; `docs-check` still validates the merged result (a duplicated field fails it).

- Residual: fragments are not folded into the main block automatically; a periodic fold-in (batch time) is left for later, and `docs-check --print modules` already shows the combined text.
