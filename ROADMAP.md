# MemPalace Roadmap

## Current Status - 2026-06-02

This roadmap is a dated planning guide, not a live PR queue. Refresh live state
before sequencing work:

```bash
gh pr list --repo MemPalace/mempalace --state open --limit 100
git fetch --prune --tags
git describe --tags --always
```

Evidence from the 2026-06-02 refresh:

- `develop` is the default remote branch and the active development target.
- `pyproject.toml` reports version `3.3.6`.
- `git describe --tags --always` on `develop` returned
  `v3.3.6-17-g9b7cfc9`.
- `git tag --list 'v*' --sort=-v:refname` includes `v3.3.6`.
- `gh release list --repo MemPalace/mempalace --limit 10` still listed
  `v3.3.5` as the latest GitHub release, so do not claim v3.3.6 has a current
  GitHub release without rechecking releases.

## Active PR Queue - 2026-06-02

`gh pr list --repo MemPalace/mempalace --state open --limit 100` returned a
large open queue. The most recently updated PRs at this refresh were:

- #1675 `fix/wing-normalize-strip-sep`
- #1673 `fix/sanitize-documents-chromadb-chokepoint`
- #1671 `feat/openai-compat-embeddings`
- #1670 `fix/repair-rebuild-index-alias`
- #1667 `fix/embeddinggemma-external-data`
- #1666 `fix/80-drawer-id-collision-delimiter`
- #1664 `perf/1657-read-path-o1`
- #1661 `fix/windows-hooks-bash-path-mangling`
- #1658 `fix/persist-directory-config`
- #1655 `claude/stoic-zhukovsky-98db3a`

## Current Themes

- **Release hygiene:** reconcile the v3.3.6 tag/repo version with GitHub release
  status before publishing or announcing it.
- **Backend correctness:** ChromaDB chokepoints, HNSW repair/quarantine,
  SQLite/FTS5 repair, and persistent directory handling remain active review
  themes.
- **Embedding and retrieval:** OpenAI-compatible embeddings, embeddinggemma
  external data, candidate strategies, recency ordering, and read-path
  performance are active review themes.
- **Platform compatibility:** Windows path/encoding fixes and hook launcher
  hardening remain active review themes.

## Historical Snapshot

The old v3.1.1 and v4.0.0-alpha "this week" plan has been superseded by the
v3.3.x release line. Keep those references in old changelog or issue context
only; do not use them as current schedule language.

## Branch Model

```
main            <- tagged production releases
develop         <- active development; PRs normally target here
release/3.3.6   <- current 3.3.6 release branch evidence
release/v4-prep <- v4 preparation branch evidence
older release/* <- historical maintenance branches
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines. PRs should target
`develop` unless a maintainer explicitly names a release branch. Review all
contributions for correctness, security, and compatibility before merging.
