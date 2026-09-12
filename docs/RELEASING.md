# Releasing MemPalace

## Pre-release checklist

Run from the repo root before cutting a release tag.

### Verify `mempalace-mcp` entry point alignment

The plugin configs reference `mempalace-mcp` as the MCP server command, which
resolves to a console script declared under `[project.scripts]` in
`pyproject.toml`. If these disagree, `pip install mempalace` ships a plugin
config pointing at a binary that was never installed — exactly what broke
v3.3.2 ([#1093](https://github.com/MemPalace/mempalace/issues/1093)).

```bash
grep -r mempalace-mcp pyproject.toml .mcp.json .claude-plugin .codex-plugin
```

Expected on a healthy `develop`:

```
.mcp.json:      "command": "mempalace-mcp"
pyproject.toml:mempalace-mcp = "mempalace.mcp_proxy:main"
.claude-plugin/.mcp.json:    "command": "mempalace-mcp"
.claude-plugin/plugin.json:      "command": "mempalace-mcp"
.codex-plugin/README.md:2. Install the Python package so the `mempalace-mcp` script lands on
```

Two things about that list are easy to misread, and both have cost a
releaser time:

- **`pyproject.toml` points at `mcp_proxy`, not `mcp_server`.** A proxied
  stdio session loads only the forwarding path and pulls in the full server
  lazily ([#2312](https://github.com/MemPalace/mempalace/pull/2312)). A
  `mempalace.mcp_server:main` here means the branch predates that change.
- **`.codex-plugin/plugin.json` is *supposed* to have no match.** It carries
  `"mcpServers": "./.mcp.json"`, a path resolved against the marketplace
  entry's `source.path` of `./` — the repo root — so the Codex command lives
  in the root `.mcp.json` above, not under `.codex-plugin/`. This is what
  [#2178](https://github.com/MemPalace/mempalace/pull/2178) changed to make
  the marketplace plugin installable, and `tests/test_codex_plugin_manifest.py`
  pins it. A missing `.codex-plugin/.mcp.json` is not a bug.

If `pyproject.toml` has no match at all, **stop** — the entry point is
missing and any fresh `pip install` will ship a plugin config pointing at a
binary that was never installed. Investigate whether the release branch was
cut before [#340](https://github.com/MemPalace/mempalace/pull/340) landed on
`develop`.

## Publishing to PyPI

Releases publish automatically via the
[`publish.yml`](../.github/workflows/publish.yml) workflow, using PyPI
[Trusted Publishing](https://docs.pypi.org/trusted-publishers/) (OIDC). There
is **no API token** stored anywhere — GitHub mints a short-lived identity at
upload time. The workflow fires when a **GitHub Release is published**, builds
the sdist + wheel, and pauses for manual approval on the `pypi` environment
before uploading.

### One-time setup (owners only)

Done once per project; both steps require PyPI owner / GitHub admin rights.

1. **PyPI trusted publisher** — on PyPI, go to **Manage project `mempalace`
   → Publishing → Add a trusted publisher** and enter exactly:

   | Field | Value |
   | --- | --- |
   | Owner | `MemPalace` |
   | Repository name | `mempalace` |
   | Workflow filename | `publish.yml` |
   | Environment name | `pypi` |

2. **GitHub environment** — in the repo, **Settings → Environments → New
   environment** named `pypi`. Add yourself (and any other release approvers)
   under **Required reviewers**. This is the manual gate the workflow waits on
   before the upload step runs.

### Cutting a release

1. Bump the version in **all six** sources on `develop` so `version-guard.yml`
   stays green (it is the single source of truth at `mempalace/version.py`,
   mirrored in `pyproject.toml`, `.claude-plugin/marketplace.json`,
   `.claude-plugin/plugin.json`, `.codex-plugin/plugin.json`, and
   `integrations/openclaw/SKILL.md`).
2. Land everything for the release on `develop`, then merge `develop → main`.
   Releases publish **only from `main`** — the workflow refuses any tag whose
   commit is not an ancestor of `main`. Don't commit the bump directly to
   `main`: it bypasses branch protection and leaves `develop` behind.
3. Run the **entry-point alignment check** above.
4. On GitHub, **Releases → Draft a new release**:
   - **Target:** `main`
   - **Tag:** `vX.Y.Z` (must equal `mempalace/version.py`; the workflow and
     `version-guard.yml` both reject a mismatch)
   - Write the release notes, then **Publish release**.
5. The `publish.yml` run validates the tag (on `main`, matches the manifest),
   builds, and then waits for approval on the `pypi` environment. Approve it to
   upload to PyPI. Watch the run land the new version on
   <https://pypi.org/project/mempalace/>.

To stage a release candidate without shipping to end users, tag a semver
pre-release (`vX.Y.Z-rc1`) — `version-guard.yml` skips the strict manifest
match for pre-release tags. (Note: a published GitHub Release still triggers
`publish.yml`; use a **draft** release, or a plain pushed tag, for dry runs you
don't want uploaded.)
