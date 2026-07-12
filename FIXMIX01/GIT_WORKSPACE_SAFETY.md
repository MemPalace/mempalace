# Git Workspace Safety

Observed command:

```powershell
git -C C:\Users\Admin\Desktop\CHAT-ALOHA\CHAT-ALOHA\mempalace-develop rev-parse --show-toplevel
```

Observed root:

```text
C:/Users/Admin
```

Impact:

- `git status` from `mempalace-develop` can report unrelated files from the whole user profile.
- Broad commands such as `git reset`, `git clean`, `git checkout -- .`, or unscoped commits are unsafe in this workspace.
- Work on FIXMIX01 should use direct file paths and explicit verification commands.

Rules for this workspace:

- Do not run destructive Git commands from this tree.
- Do not trust unscoped `git status`.
- Before committing, move the project into a clean clone or get explicit owner approval to create a nested repository.
- If commits are required before the workspace is repaired, use path-scoped `git add` commands only for the files listed in `FIXMIX01/IMPLEMENTATION_PLAN.md`.

Recommended clean setup:

```powershell
cd C:\Users\Admin\Desktop\CHAT-ALOHA\CHAT-ALOHA
git clone https://github.com/MemPalace/mempalace.git mempalace-clean
```

Then copy the intended patch files into `mempalace-clean` and run verification there.
