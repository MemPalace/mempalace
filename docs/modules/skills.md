## skills

- **Paths:** `commonplace/skills/`
- **Owns:**

  - Workspace skills exposed through scoped MCP tools and owner-managed routes.
  - Skill bundles: `SKILL.md` plus up to 20 small UTF-8 helper files, stored verbatim by content hash and never run.
  - Shares: the owner serves a skill to the whole workspace or to one project, and revokes it.

- **Promises:**

  - Skills are versioned; only the owner publishes them; reads follow the caller's scopes.
  - Agents and members are served a skill only while it is shared with the workspace or with a project whose wing they read; the share is checked on every read, so a revoke takes effect at once.
  - Skills stored before sharing existed start as workspace shares; new ones are workspace-shared unless the owner says otherwise.

- **How it fails:**

  - A missing skill version is refused, not silently replaced.
  - Oversized, binary, duplicate or escaping helper files are refused and nothing is stored; an unshared or other-workspace skill answers "not found".

- **Tests:**

  - `tests/test_commonplace_skills.py`, `tests/test_commonplace_skill.py`, `tests/test_commonplace_skills_library.py`.
