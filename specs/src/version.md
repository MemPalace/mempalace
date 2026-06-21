# Spec: `mempalace/version.py`

## Purpose

Single source of truth for the MemPalace package version string (mempalace/version.py:L1-L1).

## Public Surface

### `__version__` (module-level string constant)

The module exposes one public symbol, `__version__`, a string whose value is exactly `"3.4.1"` (mempalace/version.py:L3-L3).

- Type: string.
- Value contract: the literal three-part dotted version `3.4.1` (mempalace/version.py:L3-L3).
- This is the canonical version value that the rest of the package is expected to read from this single location rather than redefining it elsewhere (mempalace/version.py:L1-L1).

## Inputs / Outputs

- Inputs: none. The module takes no arguments and reads no external state (mempalace/version.py:L1-L3).
- Output: the constant string `__version__` available to any importer (mempalace/version.py:L3-L3).

## Invariants

- The version string is fixed at definition time and does not change at runtime (mempalace/version.py:L3-L3).
- There is exactly one version definition; no alternate or computed version values are present in this file (mempalace/version.py:L1-L3).

## Error / Edge-Case Behavior

- The module performs no computation, validation, parsing, or branching, so it has no failure modes of its own (mempalace/version.py:L1-L3).

## Side Effects

- None. No filesystem access, no network, no process spawning, no environment variable reads or writes (mempalace/version.py:L1-L3).

## Externally Observable Contracts

- Any consumer importing this module obtains a version identifier equal to `3.4.1` (mempalace/version.py:L3-L3).
