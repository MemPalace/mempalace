# Behavior Spec: `mempalace/__main__.py`

## Purpose

This file is the package execution entry point. It makes the package runnable as
an executable module (e.g. `python -m mempalace`), delegating all behavior to the
package's CLI dispatcher (mempalace/__main__.py:L1-L5).

## Public Surface

This module exposes no functions, classes, or constants of its own. Its only
observable behavior is the side effect produced when the module is loaded/executed
as the program entry point (mempalace/__main__.py:L3-L5).

## Behavior

On execution, the module obtains the CLI dispatcher entry point named `main` from
the package's CLI component and invokes it with no arguments
(mempalace/__main__.py:L3-L5). All command-line argument parsing, dispatch,
input/output, exit codes, and side effects are therefore defined entirely by that
CLI entry point, not by this file (mempalace/__main__.py:L3-L5).

The invocation occurs unconditionally at module load time — invoking the module as
the program entry point runs the CLI immediately (mempalace/__main__.py:L5).

## Inputs / Outputs

- Inputs: none consumed directly by this module; it forwards no explicit arguments
  to the CLI entry point (mempalace/__main__.py:L5).
- Outputs / exit code: this module returns or produces nothing of its own; the
  process exit code and all output are determined by the delegated CLI entry point
  (mempalace/__main__.py:L3-L5).

## Invariants / Ordering

- The CLI entry point is resolved before it is called (import precedes invocation)
  (mempalace/__main__.py:L3-L5).
- Exactly one CLI invocation happens per module execution
  (mempalace/__main__.py:L5).

## Error / Edge-Case Behavior

This module adds no error handling of its own. Any failure to resolve the CLI
entry point, or any error raised by it, propagates unchanged to the caller
(mempalace/__main__.py:L3-L5).

## Side Effects

No filesystem, network, process, or environment side effects originate in this
file; all such effects are those of the delegated CLI entry point
(mempalace/__main__.py:L3-L5).
