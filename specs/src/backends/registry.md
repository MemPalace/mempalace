# Backend Registry — Behavior Specification

Source: `mempalace/backends/registry.py`

## Purpose

This module maintains a process-global registry mapping backend names to backend
classes, discovers third-party backends declared via packaging entry points, caches
long-lived backend instances, and resolves which backend a palace should use
(mempalace/backends/registry.py:L1-L13). All backend classes are subclasses of a
shared abstract base type `BaseBackend` (mempalace/backends/registry.py:L22-L22).

## Global State and Concurrency

The registry holds four pieces of mutable process-global state: a name→class map, a
name→instance cache, a set of explicitly registered names, and a one-time
"discovered" flag (mempalace/backends/registry.py:L28-L31). All mutations to this
state are serialized under a single process-wide lock (mempalace/backends/registry.py:L32-L32),
so concurrent callers observe consistent state.

The entry-point group identifier used for discovery is the literal string
`mempalace.backends` (mempalace/backends/registry.py:L26-L26).

## Public Surface

### register(name, backend_cls) -> none

Registers `backend_cls` (a `BaseBackend` subclass) under the string `name`
(mempalace/backends/registry.py:L35-L43). The name is added to the set of explicit
registrations, marking it as taking precedence over any entry-point-discovered
backend with the same name (mempalace/backends/registry.py:L38-L43). Any previously
cached instance for that name is dropped so the newly registered class is used on the
next instance request (mempalace/backends/registry.py:L44-L45).

### unregister(name) -> none

Removes a backend registration: deletes the name from the class map, removes it from
the explicit set, and drops any cached instance (mempalace/backends/registry.py:L48-L53).
Removing a name that is not registered is a no-op (no error)
(mempalace/backends/registry.py:L51-L53).

### available_backends() -> list of strings

Triggers one-time entry-point discovery, then returns the registered backend names
sorted in ascending order (mempalace/backends/registry.py:L94-L97).

### get_backend_class(name) -> backend class

Triggers discovery, then returns the registered class for `name`
(mempalace/backends/registry.py:L100-L104). If `name` is not registered, raises a
lookup error whose message names the unknown backend and lists the currently
available (sorted) backend names (mempalace/backends/registry.py:L105-L106).

### get_backend(name) -> backend instance

Triggers discovery, then returns a long-lived, per-name-cached instance of the named
backend (mempalace/backends/registry.py:L109-L125). The first call for a name
constructs the instance (no-argument construction) and stores it; subsequent calls
return the identical cached object (mempalace/backends/registry.py:L116-L125). If
`name` is not registered, raises a lookup error whose message names the unknown
backend and lists the sorted registered names (mempalace/backends/registry.py:L120-L122).

### detect_backends_for_path(path) -> list of strings

Triggers discovery, then iterates registered backends in ascending name order and
asks each backend class to detect whether its artifacts are present at `path`,
returning every name that detects a match (mempalace/backends/registry.py:L128-L144).
The iteration order is registry-name order, so the returned list is deterministic
even when artifacts from multiple backends coexist at the path
(mempalace/backends/registry.py:L131-L137). If a backend's detection check raises an
error, that error is swallowed (logged) and that backend is simply omitted from the
result; it does not abort detection of other backends
(mempalace/backends/registry.py:L139-L143).

### detect_backend_for_path(path) -> string or none

Returns a single detected backend name for `path`, or none if no backend's artifacts
are present (mempalace/backends/registry.py:L147-L157). When multiple backends are
detected, the first name in ascending registry order wins
(mempalace/backends/registry.py:L151-L156).

### reset_backends() -> none

Closes and discards all cached backend instances (mempalace/backends/registry.py:L160-L168).
Each cached instance is asked to close; if closing one instance raises, the error is
swallowed (logged) and remaining instances are still closed
(mempalace/backends/registry.py:L163-L167). After this call the instance cache is
empty, so later `get_backend` calls reconstruct instances
(mempalace/backends/registry.py:L168-L168). The class registry and explicit set are
not affected.

### resolve_backend_for_palace(...) -> string

Resolves the backend name for a palace using a fixed priority order
(mempalace/backends/registry.py:L171-L199). Keyword inputs: `explicit`,
`config_value`, `env_value` (each optional strings), `palace_path` (optional path),
and `default` (string, defaulting to `"chroma"`) (mempalace/backends/registry.py:L171-L178).

Resolution priority, highest first (mempalace/backends/registry.py:L179-L199):
1. `explicit` if truthy (mempalace/backends/registry.py:L191-L193).
2. `config_value` if truthy (mempalace/backends/registry.py:L191-L193).
3. `env_value` if truthy (mempalace/backends/registry.py:L191-L193).
4. Auto-detection from on-disk artifacts at `palace_path`, only when `palace_path`
   is provided and no earlier rule selected a backend; uses single-backend detection
   (mempalace/backends/registry.py:L195-L198).
5. `default` (mempalace/backends/registry.py:L199-L199).

Auto-detection is strictly a migration aid: it fires only when a local path is
presented and the path already contains backend-identifiable artifacts; otherwise the
default wins for new palaces (mempalace/backends/registry.py:L187-L198).

## Entry-Point Discovery (internal, runs once)

Discovery is performed lazily and at most once per process; the first call performs
the work and sets a flag, and all later calls return immediately
(mempalace/backends/registry.py:L56-L63, L91-L91). It enumerates entry points in the
`mempalace.backends` group (mempalace/backends/registry.py:L64-L71). If enumeration
fails, the error is swallowed (logged) and an empty group is used, so discovery never
propagates an exception (mempalace/backends/registry.py:L72-L74).

For each entry point (mempalace/backends/registry.py:L75-L90):
- If the entry point's name is already explicitly registered, it is skipped —
  explicit registration wins on conflict (mempalace/backends/registry.py:L76-L77).
- The entry point is loaded; if loading fails the error is swallowed (logged) and
  that entry point is skipped (mempalace/backends/registry.py:L78-L82).
- If the loaded object is not a class that is a subclass of `BaseBackend`, it is
  rejected (a warning is logged) and skipped (mempalace/backends/registry.py:L83-L89).
- Otherwise the class is registered under its entry-point name only if no class is
  already registered under that name (existing registration is not overwritten)
  (mempalace/backends/registry.py:L90-L90).

## Built-in Backends (registered at import time)

When this module is loaded, four built-in backends are registered under fixed names:
`chroma`, `qdrant`, `sqlite_exact`, and `pgvector`
(mempalace/backends/registry.py:L207-L225). Each is registered only if a backend is
not already registered under that name, so a pre-registration (e.g. from a test) is
preserved (mempalace/backends/registry.py:L214-L222). These built-ins are registered
into the class map but are NOT added to the explicit set, so an entry-point backend
sharing one of these names would still be skipped during discovery only via the
"already in map" guard, not via the explicit-wins guard
(mempalace/backends/registry.py:L207-L222, L76-L77, L90-L90).

The default backend name throughout resolution is `chroma`
(mempalace/backends/registry.py:L177-L177).

## Invariants and Edge Cases

- Backend name listings are always returned in ascending sorted order
  (mempalace/backends/registry.py:L97-L97, L137-L137).
- Instances are singletons per name within a process until `register`,
  `unregister`, or `reset_backends` clears the relevant cache entry
  (mempalace/backends/registry.py:L116-L125, L44-L45, L51-L53, L162-L168).
- Discovery and detection never raise due to a misbehaving third-party backend;
  failures are isolated per backend (mempalace/backends/registry.py:L72-L74, L78-L82,
  L139-L143).
- Lookups of unknown backend names raise a lookup error rather than returning a
  default (mempalace/backends/registry.py:L105-L106, L120-L122).

## Side Effects

- Reads packaging/installed-distribution metadata to enumerate entry points
  (mempalace/backends/registry.py:L65-L71).
- Emits log records (exceptions/warnings) on discovery, load, detection, and close
  failures (mempalace/backends/registry.py:L24-L24, L73-L73, L81-L81, L84-L88,
  L143-L143, L167-L167).
- Constructs and closes backend instances, which may have their own external side
  effects (mempalace/backends/registry.py:L123-L123, L165-L165).
- Reads the filesystem indirectly via each backend's detection check against a path
  (mempalace/backends/registry.py:L140-L140).
