# Behavior Spec: Palace-Scoped Hallway-File Migration

This document specifies the externally observable behavior verified by the test suite for the palace-scoped hallway-file migration. The system under test resolves the hallway storage file relative to the configured palace location, so that distinct palaces on one host never share a single hallway file (the pre-3.4 bug, where the store was hardcoded at `~/.mempalace/hallways.json` regardless of palace path) (tests/test_hallways_palace_scoped.py:L1-L13).

## Components Under Test

Two collaborating units are exercised: a configuration object exposing a `hallway_file` property, and a hallways module exposing path-resolution and load/save operations (tests/test_hallways_palace_scoped.py:L19-L21).

## Hallway File Resolution

The configuration object exposes a `hallway_file` value. By default (no custom palace configured), it resolves to a file named `hallways.json` sitting in the same directory as the default palace path — i.e. the sibling of the default palace location. The hallways module's path resolver, given that same config, must return the identical path (tests/test_hallways_palace_scoped.py:L30-L34).

When a custom palace path is configured, the resolved hallway file must sit beside that custom palace (in the palace's parent directory) as `hallways.json`, not at the hardcoded legacy location. The config property and the module resolver must agree on this path (tests/test_hallways_palace_scoped.py:L36-L43).

When the palace path is set via the `MEMPALACE_PALACE_PATH` environment variable, that redirection must also apply to the hallway file: the hallway file resolves to `hallways.json` in the parent directory of the env-supplied palace path (tests/test_hallways_palace_scoped.py:L45-L50).

## Orphaned Legacy-File Detection

When the configured hallway file is absent but a legacy hallway file exists at a *different* resolved path, the load operation must: (1) emit exactly one warning-level log line (on logger `mempalace_hallways`) that names both the legacy path and the configured path, and (2) return an empty list. It must NOT auto-migrate or merge the legacy file's contents — silent merging risks clobbering newer data (tests/test_hallways_palace_scoped.py:L59-L85).

The legacy file format observed in this scenario is a JSON object with a `schema_version` integer and a `hallways` array; each hallway record carries `id`, `wing`, `entity_a`, `entity_b`, `co_occurrence_count`, and `rooms` (a list of room identifiers) (tests/test_hallways_palace_scoped.py:L67-L73).

When the configured path and the legacy path resolve to the same location (the default install case), and that file does not yet exist, the load operation must return an empty list and must NOT emit a misleading "Legacy hallways file" warning (tests/test_hallways_palace_scoped.py:L87-L98).

## Multi-Palace Isolation

Switching the palace path (via `MEMPALACE_PALACE_PATH`) between two distinct palace directories must yield two distinct hallway file paths, each `hallways.json` in its respective palace's parent directory — never one shared file (tests/test_hallways_palace_scoped.py:L107-L124).

End-to-end isolation guarantee: saving hallway records while palace-A is active writes a `hallways.json` beside palace-A, and a subsequent load while palace-B is active must return an empty list (none of palace-A's records leak into palace-B) (tests/test_hallways_palace_scoped.py:L126-L159). The save operation accepts a list of hallway records of the shape described above (`id`, `wing`, `entity_a`, `entity_b`, `co_occurrence_count`, `rooms`) and creates the destination file on disk (tests/test_hallways_palace_scoped.py:L144-L156).

## Observable Contracts Summary

- Hallway file is always named `hallways.json` and located in the parent directory of the active palace path (tests/test_hallways_palace_scoped.py:L32-L33,L42-L43,L50).
- Load returns an empty list when no configured file exists, regardless of legacy presence (tests/test_hallways_palace_scoped.py:L83-L83,L96-L96,L159-L159).
- Exactly one warning naming both paths is emitted only when an orphaned legacy file exists at a different path (tests/test_hallways_palace_scoped.py:L80-L85); no warning when paths coincide (tests/test_hallways_palace_scoped.py:L98-L98).
- Hallway records are never auto-migrated from legacy locations (tests/test_hallways_palace_scoped.py:L83-L83).
