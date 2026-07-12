# FIXMIX01 Audit Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the documentation, CI, tooling, and workspace-safety drift found during the MemPalace folder audit.

**Architecture:** These fixes should preserve existing runtime behavior. Documentation claims are brought back in sync with `mempalace/mcp_server.py`, CI policy is aligned with project config, and the local Git-root hazard is documented as an operational guard instead of being silently mutated.

**Tech Stack:** Python 3.9+, pytest, ruff, GitHub Actions, pre-commit, Markdown docs.

---

## Tracking

- [x] Created `FIXMIX01/`.
- [x] Wrote this implementation plan.
- [x] Execute Task 1: Add failing documentation drift tests.
- [x] Execute Task 2: Fix MCP tool-count and agent-tool docs.
- [x] Execute Task 3: Align Ruff pins.
- [x] Execute Task 4: Align coverage policy.
- [x] Execute Task 5: Clarify backend config precedence.
- [x] Execute Task 6: Record Git workspace safety guard.
- [x] Execute Task 7: Run final verification.

When a task is completed, change its checkbox from `- [ ]` to `- [x]`.

---

## Execution Results

- Dev dependencies: `uv` was not installed, so the environment was prepared with `python -m pip install -e ".[dev]"`.
- TDD red check: `python -m pytest tests/test_readme_claims.py::TestMCPToolSurfaceDocs -q` failed as expected before docs were fixed.
- Docs regression: `python -m pytest tests/test_readme_claims.py::TestToolCount tests/test_readme_claims.py::TestMCPToolSurfaceDocs -q` passed with `4 passed`.
- CI policy red check: `python -m pytest tests/test_ci_policy.py -q` failed as expected before CI coverage was fixed.
- CI policy regression: `python -m pytest tests/test_ci_policy.py -q` passed with `2 passed`.
- Backend precedence regression: `python -m pytest tests/test_config.py::test_backend_from_config_wins_over_env tests/test_config.py::test_backend_from_env_when_config_absent -q` passed with `2 passed`.
- Focused final regression: the combined focused command passed with `8 passed`.
- Lint and format: `ruff check .` and `ruff format --check .` passed.
- Full Windows verification: an earlier full run selected WSL `bash.exe` from `C:\Windows\System32` and failed only in hook-wrapper tests; rerunning with Git Bash first in `PATH` passed with `3026 passed, 279 skipped`, coverage `80.13%`. The fresh final full-suite rerun with the same Git Bash `PATH` had one Windows Chroma file-lock failure in `tests/test_backends.py::test_chroma_close_palace_releases_sqlite_lock_for_reopen` (`3025 passed, 279 skipped`, coverage `80.12%`); rerunning that exact test alone passed with `1 passed`.
- Git commits: skipped by safety rule because `git rev-parse --show-toplevel` returns `C:/Users/Admin`, not the project directory.

---

## Files To Touch

- Modify: `tests/test_readme_claims.py`
  - Add explicit tests for MCP tool-count references and unknown backticked MCP tool names.
- Modify: `README.md`
  - Change `35 MCP tools` to the actual count.
  - Replace the nonexistent `mempalace_list_agents` reference with shipped diary tools.
- Modify: `website/reference/mcp-tools.md`
  - Change the top-line tool count to the actual count.
- Modify: `mempalace/README.md`
  - Change `mcp_server.py` module description from stale count to the actual count.
- Modify: `.pre-commit-config.yaml`
  - Align `ruff-pre-commit` revision with `pyproject.toml`.
- Modify: `.github/workflows/ci.yml`
  - Align Ruff install pin.
  - Align Linux/macOS coverage threshold with `pyproject.toml`; keep Windows at the documented lower threshold.
- Create: `tests/test_ci_policy.py`
  - Add a small parser test so Linux/macOS CI coverage threshold cannot drift again.
- Modify: `mempalace/config.py`
  - Clarify the global config priority comment and the intentional backend exception.
- Create: `FIXMIX01/GIT_WORKSPACE_SAFETY.md`
  - Record the observed local Git-root hazard and safe command rules for this workspace.

---

### Task 1: Add Failing Documentation Drift Tests

**Files:**
- Modify: `tests/test_readme_claims.py`

- [x] **Step 1: Add explicit tests near `TestReadmeToolCountConsistency`**

Add this class after `TestReadmeToolCountConsistency`:

```python
class TestMCPToolSurfaceDocs:
    """Public docs must describe the registered MCP tool surface exactly."""

    def test_mcp_reference_count_matches_tools_dict(self):
        actual_count = len(_tools_dict_keys())
        doc = _read(MCP_TOOLS_DOC_PATH)

        assert f"all {actual_count} MCP tools" in doc, (
            f"{MCP_TOOLS_DOC_PATH.relative_to(REPO_ROOT)} should say "
            f"'all {actual_count} MCP tools' because TOOLS has {actual_count} entries."
        )

    def test_package_readme_count_matches_tools_dict(self):
        actual_count = len(_tools_dict_keys())
        package_readme = _read(MEMPALACE_PKG / "README.md")

        assert f"MCP server — {actual_count} tools" in package_readme, (
            "mempalace/README.md should keep the mcp_server.py module count "
            f"in sync with TOOLS ({actual_count})."
        )

    def test_backticked_mcp_tool_names_exist(self):
        code_tools = set(_tools_dict_keys())
        docs = "\n".join(
            [
                _read(README_PATH),
                _read(MCP_TOOLS_DOC_PATH),
                _read(MEMPALACE_PKG / "README.md"),
            ]
        )
        mentioned_tools = sorted(set(re.findall(r"`(mempalace_\w+)`", docs)))
        unknown_tools = [name for name in mentioned_tools if name not in code_tools]

        assert unknown_tools == [], (
            "Docs mention MCP tool names that are not registered in TOOLS: "
            f"{unknown_tools}."
        )
```

- [x] **Step 2: Run the focused failing tests**

Run:

```powershell
python -m pytest tests/test_readme_claims.py::TestMCPToolSurfaceDocs -q
```

Expected before docs are fixed:

```text
FAILED tests/test_readme_claims.py::TestMCPToolSurfaceDocs::test_mcp_reference_count_matches_tools_dict
FAILED tests/test_readme_claims.py::TestMCPToolSurfaceDocs::test_package_readme_count_matches_tools_dict
FAILED tests/test_readme_claims.py::TestMCPToolSurfaceDocs::test_backticked_mcp_tool_names_exist
```

If the command fails with `ModuleNotFoundError: No module named 'chromadb'`, install the dev environment first:

```powershell
uv sync --extra dev
```

- [x] **Step 3: Commit the failing test (skipped by Git-root safety rule)**

Run:

```powershell
git -C C:\Users\Admin\Desktop\CHAT-ALOHA\CHAT-ALOHA\mempalace-develop add tests/test_readme_claims.py
git -C C:\Users\Admin\Desktop\CHAT-ALOHA\CHAT-ALOHA\mempalace-develop commit -m "test: capture mcp docs drift"
```

If `git rev-parse --show-toplevel` still returns `C:/Users/Admin`, do not commit from this workspace. Record that in `FIXMIX01/GIT_WORKSPACE_SAFETY.md` in Task 6.

---

### Task 2: Fix MCP Tool-Count And Agent-Tool Docs

**Files:**
- Modify: `README.md`
- Modify: `website/reference/mcp-tools.md`
- Modify: `mempalace/README.md`

- [x] **Step 1: Update root README MCP count and agent section**

In `README.md`, replace:

```markdown
35 MCP tools cover palace reads/writes, knowledge-graph operations,
```

with:

```markdown
36 MCP tools cover palace reads/writes, knowledge-graph operations,
```

In the same file, replace:

```markdown
Each specialist agent gets its own wing and diary in the palace.
Discoverable at runtime via `mempalace_list_agents` — no bloat in your
system prompt:
```

with:

```markdown
Each specialist agent can get its own wing and diary in the palace.
Use `mempalace_diary_write` to record entries and `mempalace_diary_read`
to retrieve them — no bloat in your system prompt:
```

- [x] **Step 2: Update MCP tools reference count**

In `website/reference/mcp-tools.md`, replace:

```markdown
Detailed parameter schemas for all 35 MCP tools.
```

with:

```markdown
Detailed parameter schemas for all 36 MCP tools.
```

- [x] **Step 3: Update package README module table**

In `mempalace/README.md`, replace:

```markdown
| `mcp_server.py` | MCP server — 34 tools, AAAK auto-teach, Palace Protocol, agent diary |
```

with:

```markdown
| `mcp_server.py` | MCP server — 36 tools, AAAK auto-teach, Palace Protocol, agent diary |
```

- [x] **Step 4: Run documentation tests**

Run:

```powershell
python -m pytest tests/test_readme_claims.py::TestToolCount tests/test_readme_claims.py::TestMCPToolSurfaceDocs -q
```

Expected:

```text
4 passed
```

- [x] **Step 5: Commit docs fix (skipped by Git-root safety rule)**

Run:

```powershell
git -C C:\Users\Admin\Desktop\CHAT-ALOHA\CHAT-ALOHA\mempalace-develop add README.md website/reference/mcp-tools.md mempalace/README.md
git -C C:\Users\Admin\Desktop\CHAT-ALOHA\CHAT-ALOHA\mempalace-develop commit -m "docs: sync mcp tool surface claims"
```

If the Git root hazard is still present, skip the commit and continue with file verification only.

---

### Task 3: Align Ruff Pins

**Files:**
- Modify: `.pre-commit-config.yaml`
- Modify: `.github/workflows/ci.yml`

- [x] **Step 1: Update pre-commit Ruff revision**

In `.pre-commit-config.yaml`, replace:

```yaml
    rev: v0.15.14
```

with:

```yaml
    rev: v0.15.20
```

- [x] **Step 2: Update CI Ruff install pin**

In `.github/workflows/ci.yml`, replace:

```yaml
      - run: pip install "ruff==0.15.14"
```

with:

```yaml
      - run: pip install "ruff==0.15.20"
```

- [x] **Step 3: Verify all Ruff pins match**

Run:

```powershell
rg -n "ruff==0\.15\.20|rev: v0\.15\.20|ruff==0\.15\.14|rev: v0\.15\.14" pyproject.toml .pre-commit-config.yaml .github\workflows\ci.yml
```

Expected output contains only `0.15.20` matches and no `0.15.14` matches.

- [x] **Step 4: Run lint commands**

Run:

```powershell
python -m pip install "ruff==0.15.20"
ruff check .
ruff format --check .
```

Expected:

```text
All checks passed!
```

and `ruff format --check .` exits with code `0`.

- [x] **Step 5: Commit tooling fix (skipped by Git-root safety rule)**

Run:

```powershell
git -C C:\Users\Admin\Desktop\CHAT-ALOHA\CHAT-ALOHA\mempalace-develop add .pre-commit-config.yaml .github/workflows/ci.yml
git -C C:\Users\Admin\Desktop\CHAT-ALOHA\CHAT-ALOHA\mempalace-develop commit -m "ci: align ruff pins"
```

If the Git root hazard is still present, skip the commit and continue with file verification only.

---

### Task 4: Align Coverage Policy

**Files:**
- Create: `tests/test_ci_policy.py`
- Modify: `.github/workflows/ci.yml`

- [x] **Step 1: Add a CI policy regression test**

Create `tests/test_ci_policy.py` with this content:

```python
import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
CI_PATH = REPO_ROOT / ".github" / "workflows" / "ci.yml"
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"


def _coverage_fail_under() -> str:
    pyproject = PYPROJECT_PATH.read_text(encoding="utf-8")
    match = re.search(r"^fail_under = (\d+)$", pyproject, re.MULTILINE)
    assert match, "Could not parse coverage fail_under from pyproject.toml"
    return match.group(1)


def _job_block(name: str) -> str:
    ci = CI_PATH.read_text(encoding="utf-8")
    match = re.search(rf"^  {name}:\n(?P<body>(?:    .*\n?)*)", ci, re.MULTILINE)
    assert match, f"Could not find CI job {name}"
    return match.group("body")


def test_linux_and_macos_coverage_match_pyproject_threshold():
    threshold = _coverage_fail_under()

    for job_name in ("test-linux", "test-macos"):
        job = _job_block(job_name)
        assert f"--cov-fail-under={threshold}" in job, (
            f"{job_name} must use --cov-fail-under={threshold} to match "
            "pyproject.toml coverage policy."
        )


def test_windows_keeps_documented_lower_coverage_threshold():
    job = _job_block("test-windows")
    assert "--cov-fail-under=80" in job
    assert "Windows" in job
```

- [x] **Step 2: Run the new test before CI is fixed**

Run:

```powershell
python -m pytest tests/test_ci_policy.py -q
```

Expected before CI is fixed:

```text
FAILED tests/test_ci_policy.py::test_linux_and_macos_coverage_match_pyproject_threshold
```

- [x] **Step 3: Update Linux coverage threshold**

In `.github/workflows/ci.yml`, in the `test-linux` job, replace:

```yaml
--cov-fail-under=80
```

with:

```yaml
--cov-fail-under=85
```

- [x] **Step 4: Update macOS coverage threshold**

In `.github/workflows/ci.yml`, in the `test-macos` job, replace:

```yaml
--cov-fail-under=80
```

with:

```yaml
--cov-fail-under=85
```

Leave the Windows job at `--cov-fail-under=80` because `CLAUDE.md` documents the lower Windows threshold.

- [x] **Step 5: Run CI policy test**

Run:

```powershell
python -m pytest tests/test_ci_policy.py -q
```

Expected:

```text
2 passed
```

- [x] **Step 6: Commit coverage policy fix (skipped by Git-root safety rule)**

Run:

```powershell
git -C C:\Users\Admin\Desktop\CHAT-ALOHA\CHAT-ALOHA\mempalace-develop add tests/test_ci_policy.py .github/workflows/ci.yml
git -C C:\Users\Admin\Desktop\CHAT-ALOHA\CHAT-ALOHA\mempalace-develop commit -m "ci: align coverage policy"
```

If the Git root hazard is still present, skip the commit and continue with file verification only.

---

### Task 5: Clarify Backend Config Precedence

**Files:**
- Modify: `mempalace/config.py`

- [x] **Step 1: Update module docstring**

At the top of `mempalace/config.py`, replace:

```python
"""
MemPalace configuration system.

Priority: env vars > config file (~/.mempalace/config.json) > defaults
"""
```

with:

```python
"""
MemPalace configuration system.

Most settings resolve as env vars > config file (~/.mempalace/config.json) > defaults.
The storage backend is the intentional exception: a persisted config backend wins
over MEMPALACE_BACKEND so an existing palace does not silently switch backends.
Explicit CLI --backend still wins by setting MEMPALACE_BACKEND_EXPLICIT.
"""
```

- [x] **Step 2: Update `MempalaceConfig` class docstring**

In `mempalace/config.py`, inside `class MempalaceConfig`, replace:

```python
    Load order: env vars > config file > defaults.
```

with:

```python
    Load order: env vars > config file > defaults for most settings.
    The backend property intentionally reads config before MEMPALACE_BACKEND.
```

- [x] **Step 3: Confirm backend behavior remains unchanged**

Run:

```powershell
python -m pytest tests/test_config.py::test_backend_from_config_wins_over_env tests/test_config.py::test_backend_from_env_when_config_absent -q
```

Expected:

```text
2 passed
```

- [x] **Step 4: Commit config documentation fix (skipped by Git-root safety rule)**

Run:

```powershell
git -C C:\Users\Admin\Desktop\CHAT-ALOHA\CHAT-ALOHA\mempalace-develop add mempalace/config.py
git -C C:\Users\Admin\Desktop\CHAT-ALOHA\CHAT-ALOHA\mempalace-develop commit -m "docs: clarify backend config precedence"
```

If the Git root hazard is still present, skip the commit and continue with file verification only.

---

### Task 6: Record Git Workspace Safety Guard

**Files:**
- Create: `FIXMIX01/GIT_WORKSPACE_SAFETY.md`

- [x] **Step 1: Re-check the current Git root**

Run:

```powershell
git -C C:\Users\Admin\Desktop\CHAT-ALOHA\CHAT-ALOHA\mempalace-develop rev-parse --show-toplevel
```

Expected in the current audited workspace:

```text
C:/Users/Admin
```

- [x] **Step 2: Create the safety note**

Create `FIXMIX01/GIT_WORKSPACE_SAFETY.md` with this content:

````markdown
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
````

- [x] **Step 3: Verify the note exists**

Run:

```powershell
Test-Path FIXMIX01\GIT_WORKSPACE_SAFETY.md
```

Expected:

```text
True
```

---

### Task 7: Final Verification

**Files:**
- No new source files beyond tasks above.

- [x] **Step 1: Verify CLI import still works**

Run:

```powershell
python -m mempalace --help
```

Expected output includes:

```text
MemPalace 3.5.0
```

- [x] **Step 2: Run focused regression tests**

Run:

```powershell
python -m pytest tests/test_readme_claims.py::TestToolCount tests/test_readme_claims.py::TestMCPToolSurfaceDocs tests/test_ci_policy.py tests/test_config.py::test_backend_from_config_wins_over_env tests/test_config.py::test_backend_from_env_when_config_absent -q
```

Expected:

```text
8 passed
```

- [x] **Step 3: Run lint and format checks**

Run:

```powershell
ruff check .
ruff format --check .
```

Expected:

```text
All checks passed!
```

and `ruff format --check .` exits with code `0`.

- [x] **Step 4: Run full non-benchmark suite after dependencies are installed**

Run:

```powershell
python -m pytest tests/ -v --ignore=tests/benchmarks --cov=mempalace --cov-report=term-missing --cov-fail-under=85 --durations=10
```

Expected on Linux/macOS:

```text
passed
```

with coverage at or above `85%`.

On Windows, use the CI-equivalent command:

```powershell
python -m pytest tests/ -v --ignore=tests/benchmarks --cov=mempalace --cov-report=term-missing --cov-fail-under=80 --durations=10 --reruns 2 --reruns-delay 5 --only-rerun "Failed to apply logs to the hnsw segment writer"
```

Expected on Windows:

```text
passed
```

with coverage at or above `80%`.

---

## Self-Review

- Spec coverage: all five audit findings are mapped to tasks.
- Placeholder scan: this plan contains concrete file paths, exact snippets, exact commands, and expected results.
- Type/name consistency: tests reuse existing helpers `_tools_dict_keys`, `_doc_tool_names`, `_read`, `README_PATH`, `MCP_TOOLS_DOC_PATH`, `MEMPALACE_PKG`, and `REPO_ROOT` from `tests/test_readme_claims.py`.
