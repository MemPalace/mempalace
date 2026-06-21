# Spec: Source Adapter Registry

Source: `mempalace/sources/registry.py`

A process-global registry that tracks named source-adapter classes, discovers third-party adapters from packaging entry points, instantiates and caches adapter objects, and resolves which adapter name applies to a given operation. All adapter classes are subtypes of a shared base adapter type `BaseSourceAdapter` (mempalace/sources/registry.py:L27-L27).

## Global Constants / Observable Defaults

- The entry-point discovery group is the literal string `mempalace.sources` (mempalace/sources/registry.py:L31-L31).
- The default adapter name is the literal string `filesystem`, used when no adapter is otherwise selected (mempalace/sources/registry.py:L32-L32).

## Internal State (shared across all calls in the process)

The registry holds: a name→class map, a name→instance cache, a set of names that were explicitly registered, and a one-time discovery-completed flag. All mutations are serialized under a single lock so concurrent callers see consistent state (mempalace/sources/registry.py:L34-L38).

## Public Surface

### `register(name, adapter_cls)` → none

Registers `adapter_cls` under `name`, marks the name as an explicit registration, and removes any previously cached instance for that name so the next `get_adapter(name)` constructs fresh (mempalace/sources/registry.py:L41-L49). Explicit registration takes precedence over entry-point discovery on name conflict (mempalace/sources/registry.py:L44-L44, L78-L79, L92-L92).

### `unregister(name)` → none

Removes the class mapping, removes the name from the explicit set, and drops any cached instance for that name. Removing a name that is not present is a no-op (does not raise) (mempalace/sources/registry.py:L52-L57).

### `available_adapters()` → list of strings

Triggers entry-point discovery (once), then returns the names of all registered adapters sorted in ascending lexicographic order (mempalace/sources/registry.py:L96-L99).

### `get_adapter_class(name)` → adapter class

Triggers discovery, then returns the class registered under `name`. If `name` is not registered, raises a lookup error whose message is `unknown source adapter '<name>'; available: <sorted list of names>` (mempalace/sources/registry.py:L102-L108).

### `get_adapter(name)` → adapter instance

Triggers discovery, then returns a long-lived cached instance for `name`. The first call for a given name constructs the instance by calling the registered class with no arguments and caches it; subsequent calls return the identical cached object (mempalace/sources/registry.py:L111-L129). If `name` is not registered, raises a lookup error with message `unknown source adapter '<name>'; available: <sorted list of names>` (mempalace/sources/registry.py:L122-L126).

### `reset_adapters()` → none

Closes every cached instance by calling its `close()` method, then clears the instance cache. The class registry and explicit set are left intact. If a `close()` call fails, the error is logged and reset continues to the remaining instances (mempalace/sources/registry.py:L132-L140).

### `resolve_adapter_for_source(*, explicit=None, config_value=None, default="filesystem")` → string

Resolves an adapter name by priority: the first truthy value of `explicit` then `config_value` is returned; if neither is truthy, `default` is returned (which defaults to `filesystem`) (mempalace/sources/registry.py:L143-L162). There is no auto-detection on the read side — selection is always explicit/config/default (mempalace/sources/registry.py:L155-L157).

## Entry-Point Discovery Behavior

Discovery runs at most once per process; after it completes the discovered flag is set and subsequent invocations return immediately without rescanning (mempalace/sources/registry.py:L60-L66, L93-L93). The double-checked flag is read before and after acquiring the lock (mempalace/sources/registry.py:L62-L66).

Discovery enumerates entry points in the `mempalace.sources` group. If enumeration of the entry points fails for any reason, the failure is logged and discovery proceeds with an empty group (no exception propagates) (mempalace/sources/registry.py:L67-L76).

For each discovered entry point:
- If a name was already explicitly registered, the entry point with that name is skipped entirely (explicit wins) (mempalace/sources/registry.py:L77-L79).
- The entry point's target object is loaded. If loading fails, the error is logged and that entry point is skipped (mempalace/sources/registry.py:L80-L84).
- If the loaded object is not a class, or is a class that is not a subtype of `BaseSourceAdapter`, a warning is logged and the entry point is skipped (mempalace/sources/registry.py:L85-L91).
- An otherwise-valid entry point is registered under its name only if that name is not already present in the registry; existing registrations are never overwritten by discovery (mempalace/sources/registry.py:L92-L92).

## Side Effects

- Reads installed-package entry-point metadata for the `mempalace.sources` group during first discovery (mempalace/sources/registry.py:L68-L73).
- Constructs adapter objects (calling the class with no arguments) and may call `close()` on them during reset (mempalace/sources/registry.py:L127-L127, L137-L137).
- Emits log records on discovery enumeration failure, entry-point load failure, non-conforming entry-point targets, and close failures (mempalace/sources/registry.py:L75-L75, L83-L83, L86-L90, L139-L139).
- No filesystem writes, network calls, environment variable reads, or process exits occur in this file.

## Invariants

- Adapter names map to at most one class at a time; the most recent explicit registration for a name replaces any prior mapping (mempalace/sources/registry.py:L47-L47).
- Instance caching guarantees referential identity per name until `register`, `unregister`, or `reset_adapters` clears it (mempalace/sources/registry.py:L119-L121, L49-L49, L57-L57, L140-L140).
- `available_adapters()` output is always sorted ascending (mempalace/sources/registry.py:L99-L99).
