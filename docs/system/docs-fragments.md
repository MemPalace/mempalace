### Conflict-free documentation layout

- Module blocks moved from one `MODULES.md` into one file per module, `docs/modules/<module>.md`, and `SYSTEM.md` accepts extra one-section files in `docs/system/`. Parallel changes now edit different files and merge without a git conflict; before, every change appended to the same few large files and lost a merge round (cards #427 and #267).

- `docs-check` keeps every obligation (ownership, five fields, root and `commonplace/` listing, touched module, `SYSTEM.md` changed, waivers from the merge base). It reads `MODULES.md` and `docs/modules/` together, so a block still written in `MODULES.md` is accepted. A fragment file is exempt from the module-block rule, and a change under `docs/system/` is a change to `SYSTEM.md`.

- Residual: two changes that add a bullet at the same position of the same field of the same module file still conflict, as inside any file. The rule to add a new bullet next to its related topic stays.
