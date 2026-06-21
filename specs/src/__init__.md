# Behavior Specification: `mempalace/__init__.py`

This file is the package initialization module for MemPalace. It runs at import
time and performs environment hygiene and telemetry suppression before exposing
the package version (`mempalace/__init__.py:L1-L1`).

## Public Surface

The package exports exactly one public symbol: `__version__`, a version string
re-exported from the package's version module (`mempalace/__init__.py:L38-L38`,
`mempalace/__init__.py:L60-L60`). No other names are part of the public API
contract; the public name list contains only `__version__`
(`mempalace/__init__.py:L60-L60`).

## Import-Time Side Effects (Ordering Guarantees)

On import, the module executes the following steps in this exact order:

1. Strips leaked interpreter search-path entries originating from the
   `PYTHONPATH` environment variable (`mempalace/__init__.py:L36-L36`).
2. Imports and binds the version string (`mempalace/__init__.py:L38-L38`).
3. Silences a specific telemetry logger by raising its level to the most
   critical threshold (`mempalace/__init__.py:L44-L44`).

The path-stripping step (1) must run before any further imports so that
subsequent imports resolve packages only from the environment's own
installation rather than from externally-injected paths
(`mempalace/__init__.py:L9-L24`, `mempalace/__init__.py:L36-L38`).

## Behavior: PYTHONPATH Search-Path Sanitization

A function performs the following observable contract when the package is
imported (`mempalace/__init__.py:L8-L33`):

- **Input:** The current value of the `PYTHONPATH` environment variable and the
  interpreter's current module search path list (`mempalace/__init__.py:L25-L25`,
  `mempalace/__init__.py:L33-L33`).
- **No-op condition:** If `PYTHONPATH` is unset or empty, the function returns
  immediately and the search path is left unchanged
  (`mempalace/__init__.py:L25-L27`).
- **Action:** The `PYTHONPATH` value is split on the platform path separator
  into individual entries; empty entries are discarded
  (`mempalace/__init__.py:L32-L32`). Each search-path entry is then removed from
  the interpreter search path if it matches one of those `PYTHONPATH`-derived
  entries (`mempalace/__init__.py:L33-L33`).
- **Matching rule:** Comparison is performed on a normalized form of each path
  that collapses case differences and path-separator/normalization quirks, so
  that case-insensitive filesystems and trailing-separator differences are
  treated as equal (`mempalace/__init__.py:L29-L30`, `mempalace/__init__.py:L13-L14`).
- **Preservation invariant:** The empty-string entry on the search path (the
  marker representing the implicit current working directory) is always
  preserved and never removed, even if `PYTHONPATH` contains a value referring
  to the current directory (`mempalace/__init__.py:L15-L17`,
  `mempalace/__init__.py:L33-L33`).
- **Environment invariant:** The `PYTHONPATH` environment variable itself is NOT
  modified by this function. Only the in-process search path is altered. This
  keeps an embedding host application's `PYTHONPATH` intact for its own
  unrelated subprocesses (`mempalace/__init__.py:L19-L24`). (Entry-point
  programs separately drop `PYTHONPATH` from the environment themselves; that
  behavior is external to this file — `mempalace/__init__.py:L19-L22`.)

## Behavior: Telemetry Logger Suppression

The logger named `chromadb.telemetry.product.posthog` has its level set to the
most-critical (highest) severity threshold at import time, which suppresses
noisy telemetry-related warning output on the standard error stream
(`mempalace/__init__.py:L40-L44`).

## Edge Cases

- Empty or unset `PYTHONPATH`: search path untouched
  (`mempalace/__init__.py:L26-L27`).
- `PYTHONPATH` containing only empty segments (e.g. a lone separator): those
  empty segments are filtered out, so no real entries are matched, but the
  current-directory marker on the search path is still preserved
  (`mempalace/__init__.py:L32-L33`).
- Paths differing only by letter case or by trailing separators are still
  matched and removed due to the normalization rule
  (`mempalace/__init__.py:L29-L30`).

## Notes for Reimplementation

The version string is the single externally observable output of this module;
everything else is environment/process hygiene with no return value
(`mempalace/__init__.py:L38-L38`, `mempalace/__init__.py:L60-L60`). The
search-path scrubbing is a side effect on interpreter-global state and produces
no return value (`mempalace/__init__.py:L8-L8`).
