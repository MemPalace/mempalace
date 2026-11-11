# Modules

Who owns each path, what it promises, how it fails and which tests hold it to
that. `SYSTEM.md` holds the cross-module decisions and residuals.

The blocks live one per file in `docs/modules/<module>.md`, so two changes to
different modules (or a new module) never touch the same file and merge without
a git conflict. This file keeps only the format. `commonplace orch docs-check
--print modules` prints the whole map in one piece. A new module is a new file;
do not list modules here.

Format (read by `commonplace orch docs-check`):

- One `## <module>` block per module, alone in `docs/modules/<module>.md` (the
  file name is the module name). Each block has exactly the five fields
  **Paths**, **Owns**, **Promises**, **How it fails** and **Tests**; a missing
  or empty field fails the check.
- **Owns**, **Promises**, **How it fails** and **Tests** use indented
  sub-bullets, separated by blank lines. Keep one topic or test area per bullet.
  Add a NEW sub-bullet next to its related topic instead of extending an existing
  line or appending every change at the end of the field. Separate insertion
  positions let independent topics merge; concurrent edits at the same position
  can still conflict. Inline fields remain accepted for older maps.
- **Paths** lists repository paths in backticks, separated by commas. A
  directory ends in `/`. There are no wildcards.
- Every entry of the repository root and of `commonplace/` is listed exactly
  once. A path belongs to the block that lists its longest matching entry, so a
  more specific path (for example `docs/runner.md`) may be listed in another
  block than its directory (`docs/`). `commonplace/` itself is split into its
  entries.
- A change that touches a module's paths updates that module's block (its file
  in `docs/modules/`), unless a `Docs-waiver` from `SYSTEM.md` applies. Editing
  the files in `docs/modules/` or `docs/system/` does not itself touch a module.
