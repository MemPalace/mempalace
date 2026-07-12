# FIXMIX01 Clean Clone Handoff

Clean repository:

```text
C:\Users\Admin\Desktop\CHAT-ALOHA\CHAT-ALOHA\mempalace-clean
```

Source branch:

```text
develop
```

Working branch:

```text
fixmix01-audit-drift
```

Why this clone exists:

- The audited folder `mempalace-develop` resolves its Git root to `C:/Users/Admin`.
- A clean clone prevents commits, resets, and status checks from touching unrelated user-profile files.
- The FIXMIX01 patch was transferred by explicit file list only.

Clean-clone verification:

```powershell
python -m pip install -e ".[dev]"
python -m pytest tests/test_readme_claims.py::TestToolCount tests/test_readme_claims.py::TestMCPToolSurfaceDocs tests/test_ci_policy.py tests/test_config.py::test_backend_from_config_wins_over_env tests/test_config.py::test_backend_from_env_when_config_absent -q
ruff check .
ruff format --check .
python -m mempalace --help
```

Observed results:

- Focused regression: `8 passed`.
- Ruff: `All checks passed!` and `190 files already formatted`.
- CLI help includes `MemPalace 3.5.0`.
- Full Windows non-benchmark suite with Git Bash first in `PATH`: `3026 passed, 279 skipped`, coverage `80.13%`.
