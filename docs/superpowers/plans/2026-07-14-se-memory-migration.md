# Sales-Enablement Memory Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Configure canonical sales-enablement mining and produce an auditable dry-run migration manifest that safely separates `se`, `se-code`, and `se-sessions` while identifying only exact duplicate deletion candidates.

**Architecture:** Check in repository mining policy in the sales-enablement repo. Add a read-only-first migration planner to MemPalace that inventories Chroma SQLite without loading HNSW, classifies records, and writes an owner-only manifest. Apply/activation remains gated on review of the manifest and a verified palace copy.

**Tech Stack:** YAML, Python stdlib SQLite/hashlib/json, Chroma metadata schema, pytest.

## Global Constraints

- Run against the canonical sales-enablement checkout, never a linked worktree.
- Initial execution is dry-run only; do not delete or rewrite the active palace.
- Delete eligibility requires canonical identity plus exact verbatim content evidence.
- Preserve unique/uncertain worktree artifacts in `se-sessions` as cold.
- Sessions older than 90 days are cold, not deleted; pinned sessions stay hot.
- Backups/manifests are owner-only and remain local.

---

### Task 1: Canonical repository mining policy

**Files (sales-enablement repository):**
- Create: `mempalace.yaml`
- Modify: `AGENTS.md`

**Interfaces:**
- Consumes: source-kind and worktree configuration from the metadata plan.
- Produces canonical wing `se-code` and deterministic rooms/exclusions.

- [x] **Step 1: Add the checked-in configuration**

```yaml
wing: se-code
source_kind: code
reject_linked_worktrees: true
rooms:
  - name: backend
    description: NestJS and Fastify API
    keywords: [apps/sales-enablement-be, controller, service, dto]
  - name: workers
    description: Temporal workers, workflows, and activities
    keywords: [apps/sales-enablement-workers, temporal, workflow, activity]
  - name: helpers
    description: Shared infrastructure and models
    keywords: [libs/helpers, mongo, gcs, pubsub]
  - name: architecture_docs
    description: Architecture and operating documentation
    source_kind: documentation
    keywords: [docs, architecture, agents]
  - name: tests_operations
    description: Tests, migrations, scripts, and operations
    keywords: [test, spec, scripts, migration, docker]
exclude_patterns: [node_modules/, .nx/, dist/, coverage/, "*.map", workflow-bundle.js, package-lock.json]
```

- [x] **Step 2: Update AGENTS usage routing**

Document that decisions use `se`, canonical code uses `se-code`, sessions use `se-sessions`, ambiguous questions search `se` + `se-code`, and cold sessions require explicit historical lookup.

- [x] **Step 3: Validate config without mining**

Run: `/private/tmp/mempalace-fix/.venv/bin/python -c "from mempalace.miner import load_config; print(load_config('.')['wing'])"`

Expected: `se-code`.

- [x] **Step 4: Commit in sales-enablement repo**

The repository-local `AGENTS.md` is intentionally excluded by that checkout's
`.git/info/exclude`, so the routing guidance remains local while the checked-in
`mempalace.yaml` was committed as `deb1792`.

```bash
git add mempalace.yaml AGENTS.md
git commit -m "chore: configure canonical MemPalace mining"
```

### Task 2: Read-only inventory and classification engine

**Files (MemPalace repository):**
- Create: `mempalace/reorganize.py`
- Create: `tests/test_reorganize.py`

**Interfaces:**
- Produces: `InventoryRecord` and `MigrationAction` dataclasses.
- Produces: `inventory_palace(palace_path, canonical_root, worktree_roots, session_roots) -> list[InventoryRecord]`.
- Produces: `plan_actions(records, hot_days=90, now=None) -> list[MigrationAction]`.

- [x] **Step 1: Write synthetic SQLite inventory/classification tests**

```python
def test_plan_classifies_canonical_sessions_and_worktree_candidates():
    actions = plan_actions([
        record("canonical", source="/repo/src/a.ts", content="A"),
        record("worktree", source="/tmp/wt/src/a.ts", content="A"),
        record("session", source="/codex/session.jsonl", content="talk", authored_at="2026-01-01"),
    ], hot_days=90, now=date(2026, 7, 14))
    assert actions[0].destination_wing == "se-code"
    assert actions[1].action == "duplicate_candidate"
    assert actions[2].metadata["memory_tier"] == "cold"
```

- [x] **Step 2: Run and verify missing module failure**

Run: `.venv/bin/pytest tests/test_reorganize.py -q`

Expected: FAIL importing `mempalace.reorganize`.

- [x] **Step 3: Implement read-only SQLite extraction and pure classification**

```python
@dataclass(frozen=True)
class MigrationAction:
    drawer_id: str
    action: str
    destination_wing: str
    reason: str
    content_sha256: str
    metadata: dict[str, Any]

def exact_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()
```

Open `chroma.sqlite3` with `mode=ro`, reconstruct each drawer's document and scalar metadata from Chroma tables, normalize source roots without mutating them, and return actions in stable drawer-ID order.

- [x] **Step 4: Run tests and commit**

Run: `.venv/bin/pytest tests/test_reorganize.py -q`

Expected: PASS.

```bash
git add mempalace/reorganize.py tests/test_reorganize.py
git commit -m "feat: plan palace reorganization without mutations"
```

### Task 3: Exact duplicate evidence and manifest writer

**Files:**
- Modify: `mempalace/reorganize.py`
- Modify: `tests/test_reorganize.py`

**Interfaces:**
- Produces: `prove_worktree_duplicate(worktree, canonical) -> DuplicateEvidence | None`.
- Produces: `write_manifest(path, inventory, actions, evidence) -> None`.

- [x] **Step 1: Write evidence rejection and owner-only manifest tests**

```python
def test_duplicate_requires_same_relative_identity_chunk_and_content_hash():
    assert prove_worktree_duplicate(worktree(content="A", chunk=0), canonical(content="B", chunk=0)) is None
    assert prove_worktree_duplicate(worktree(content="A", chunk=1), canonical(content="A", chunk=0)) is None

def test_manifest_is_owner_only(tmp_path):
    path = tmp_path / "manifest.json"
    write_manifest(path, [], [], [])
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
```

- [x] **Step 2: Run tests and confirm failure**

Run: `.venv/bin/pytest tests/test_reorganize.py -k 'duplicate or manifest' -q`

Expected: FAIL on missing evidence/writer functions.

- [x] **Step 3: Implement strict proof and deterministic manifest**

Evidence requires equal canonical relative path, source hash when both records have it, chunk index, and content SHA-256. If any required identity is missing or ambiguous, emit `preserve_uncertain`, not delete eligibility. Manifest includes version, palace path hash, snapshot SQLite size/mtime, counts, every action, and evidence IDs; write via `os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)`.

- [x] **Step 4: Run tests and commit**

Run: `.venv/bin/pytest tests/test_reorganize.py -q`

Expected: PASS.

```bash
git add mempalace/reorganize.py tests/test_reorganize.py
git commit -m "feat: emit evidence-backed memory migration manifests"
```

### Task 4: Dry-run CLI and current-palace report

**Files:**
- Modify: `mempalace/cli.py`
- Modify: `mempalace/reorganize.py`
- Modify: `tests/test_cli.py`
- Modify: `tests/test_reorganize.py`

**Interfaces:**
- Adds: `mempalace reorganize plan --canonical-root PATH --worktree-root PATH --session-root PATH --manifest PATH`.
- No `--apply` option in this implementation checkpoint.

- [x] **Step 1: Write CLI no-mutation test**

```python
def test_reorganize_plan_writes_manifest_without_changing_sqlite(tmp_path):
    before = sha256_file(palace / "chroma.sqlite3")
    result = runner.invoke(app, ["reorganize", "plan", "--palace", str(palace), "--manifest", str(manifest)])
    assert result.exit_code == 0
    assert sha256_file(palace / "chroma.sqlite3") == before
```

- [x] **Step 2: Run and verify missing command failure**

Run: `.venv/bin/pytest tests/test_cli.py tests/test_reorganize.py -k 'reorganize' -q`

Expected: FAIL because command is absent.

- [x] **Step 3: Add plan-only CLI and report summary**

The command prints counts for canonical, session, worktree candidate, verified duplicate candidate, unique artifact, unclassified, hot, and cold. It exits non-zero on SQLite integrity failure and refuses a linked canonical root.

- [x] **Step 4: Run focused/full verification**

Run: `.venv/bin/pytest tests/test_cli.py tests/test_reorganize.py -q && .venv/bin/ruff check mempalace/reorganize.py tests/test_reorganize.py`

Expected: PASS/exit 0.

- [x] **Step 5: Commit**

```bash
git add mempalace/cli.py mempalace/reorganize.py tests/
git commit -m "feat: add dry-run palace reorganization command"
```

- [x] **Step 6: Run against a copied/current palace in dry-run mode**

Run with the active palace read-only and write the manifest under an owner-only temporary directory. Confirm the database SHA-256 is identical before and after. Report exact projected counts to the user and stop before any mutation or deletion.

Completed against 13,653 drawers. The database SHA-256 remained
`0e17be92a4cc7583054127ab4d295aa961ef8f06f565ffc6fd8a5958ebf9734b`.
The manifest proves 3,515 duplicate candidates and preserves 59 unique plus 14
uncertain worktree artifacts. It retains 59 source-free curated records and
preserves 33 provenance-bearing records outside known roots as unclassified.
No drawer was changed or deleted.
