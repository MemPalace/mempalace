## build-and-ci

- **Paths:** `pyproject.toml`, `uv.lock`, `.python-version`, `Cargo.toml`, `Cargo.lock`, `crates/`, `.gitattributes`, `.gitignore`, `.pre-commit-config.yaml`, `.github/`, `.devcontainer/`
- **Owns:**

  - Python and Rust packaging, pinned dependencies, the coverage gate, lint and format settings, pre-commit hooks, repository metadata and the development container.

  - `.pre-commit-config.yaml` configures the commit stage only; the push stage is the quick check in `commonplace/guards/`, installed with `python -m commonplace.guards.hooks install`.

- **Promises:**

  - Plain pytest excludes the `timing` marker from behavior checks; explicit timing selection remains available for serial informational measurements.

  - `uv sync --extra dev` reproduces the locked environment; `ruff check` and `ruff format --check` define style; the coverage threshold in `pyproject.toml` is the gate.

- **How it fails:**

  - A lock that drifts from `pyproject.toml` breaks installs; a lowered threshold or a widened lint ignore weakens every other module's tests without any test failing.

- **Tests:**

  - `tests/test_version_consistency.py` and the full suite under the configured coverage gate.
