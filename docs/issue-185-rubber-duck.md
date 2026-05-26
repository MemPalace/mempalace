# Issue 185 Rubber Duck Log

Date: 2026-05-26
Issue: https://github.com/MemPalace/mempalace/issues/185

## One-Sentence Bug Statement

Expected: `mempalace init` should let users control where project config artifacts are written.  
Actual: `mempalace.yaml` and `entities.json` are written to the project root by default with no init-time location choice.

## Expected vs Actual

- Expected behavior:
  - Users can keep default project-root behavior, or choose a specific config directory during init.
  - The resulting config location remains usable by downstream mining/config loading.
- Actual behavior (current develop):
  - `mempalace.yaml` is written in project root.
  - `entities.json` is written in project root when entities are detected.
  - Git repos get `.gitignore` protection (merged fix for commit-risk), but file location is still fixed to root.

## Assumptions Called Out

1. "Issue #185 is solved because `.gitignore` entries are auto-added."
   - False: this only mitigates accidental commits; it does not provide location control.
2. "Moving files would break mining because `load_config()` only reads root."
   - True in current state unless loader fallback/pointer logic is added.
3. "Non-interactive init can tolerate new prompts."
   - False: scriptability must remain stable (`--yes` / explicit flags should avoid new blocking prompts).

## Code-Path Walk (Relevant)

1. `cmd_init()` discovers and confirms entities.
2. `cmd_init()` writes `entities.json` to `<project>/entities.json`.
3. `detect_rooms_local()` writes `<project>/mempalace.yaml`.
4. `load_config()` in `miner.py` currently reads `<project>/mempalace.yaml` (legacy fallback: `mempal.yaml`).
5. `_ensure_mempalace_files_gitignored()` protects root filenames in git repos.

## Contradiction Found

The system currently assumes project-root config paths in both write and read flows, while the product expectation in #185 is configurable placement. The commit-risk mitigation fixed one symptom, not the root location constraint.

## Root Cause (One Sentence)

Config artifact paths are hardcoded to project-root conventions in init and loader paths, and init has no location-selection interface.

## Enhancement Direction

Add an init-time config-location choice (default vs custom path), persist/resolve that location safely, and ensure loader compatibility via explicit resolution logic.

## Adjacent Startup/Config Issues Reviewed

- #1313 (`--palace` ignored by `init`)  
  Status in this branch: addressed; `cmd_init` now honors top-level `--palace`.
- #557 (`init --empty`)  
  Added in this branch: `mempalace init --empty` for startup wiring without file scanning/entity or room detection.
- #462 (`setup-hooks` one-step install)  
  Not folded into this branch: larger installer/UX scope than #185 path control; should remain a dedicated follow-up.
