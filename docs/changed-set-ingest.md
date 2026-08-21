# Changed-set ingest PoC

`mempalace sync` can accept a producer-supplied manifest and reindex only the
listed project-relative files. Git is not required by core; an IDE, watcher, or
build system may create the same JSON shape.

```json
{
  "changed": ["src/app.py", "README.md"],
  "deleted": ["src/old.py"]
}
```

Preview and apply:

```bash
mempalace sync /path/to/project --manifest changed.json --wing project
mempalace sync /path/to/project --manifest changed.json --wing project --apply --daemon
```

Paths must remain within the project root. Apply holds the palace writer lock,
purges old drawers and closets for every affected source, and invokes the normal
project miner only for `changed`. `deleted` sources are never opened. The daemon
payload contains the parsed manifest, avoiding a manifest-file time-of-check /
time-of-use race between client and writer.

This is intentionally a PoC contract. A production version should add a job
idempotency key and committed palace generation before making changed-set sync a
default hook path.
