# Recovery: legacy wing names split after the normalization change

**Companion to #1675.** `normalize_wing_name` now strips leading and trailing
separators, so a path-encoded project dir like `-home-user-proj` derives the
wing `home_user_proj` instead of `_home_user_proj`. Palaces mined before that
change filed drawers under the old, separator-padded name. New mining and diary
writes land on the new name, so the two no longer meet — the history is
**split**, not lost. `mempalace migrate-wings` re-unites them.

## Symptom

After upgrading, a project that used to surface its memories returns less than
expected, and `mempalace status` shows two wings for one project — e.g. both
`_home_user_proj` (old drawers) and `home_user_proj` (newly mined). MCP writes
to the padded wing may also have been rejected, since `sanitize_name` does not
accept a leading underscore.

## Recovery

Preview first — this never modifies anything:

```bash
mempalace migrate-wings --dry-run
mempalace migrate-wings --dry-run --palace /path/to/palace
```

The plan lists each rename and flags collisions that will **merge** into an
existing wing:

```
  Wing-name migration plan:
    '_home_user_proj' -> 'home_user_proj': 1284 drawer(s), 96 closet(s)  (MERGE into existing wing)
```

Apply it:

```bash
mempalace migrate-wings           # prompts for confirmation
mempalace migrate-wings --yes     # no prompt
```

## What it does

- Re-keys the `wing` **metadata field** on drawers and closets to the normalized
  form, merging collisions into the existing wing.
- Re-keys the `topics_by_wing` registry (merging topic lists on collision).

## What it leaves alone

- **Drawer/closet IDs** are untouched. The wing in an ID (`drawer_<wing>_…`) is
  an opaque prefix that is never decoded back into a wing, so leaving it keeps
  closet `→drawer_id` pointers valid and lets future mining still skip
  already-mined files (no duplicates). The verbatim drawer content is never
  read or rewritten.
- **Tunnels** already normalize wing names at read time, so they resolve under
  the new name without a rewrite.

## Notes

- **Idempotent.** A second run reports "nothing to migrate" and changes nothing.
- **Backend-agnostic.** Works on any configured storage backend.
- Run it once per palace after upgrading. New palaces are born with normalized
  wing names and never need it.
