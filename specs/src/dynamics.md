# Spec: `dynamics.py` — Living-connection math for halls + tunnels

## Purpose & Scope

This module implements "living connection" math shared by both hall records and tunnel records: Hebbian potentiation (strength grows on co-access), Ebbinghaus exponential decay (strength fades with elapsed time), and the Cepeda spacing effect (stability grows only under spaced reinforcement) (mempalace/dynamics.py:L1-L12).

The module is **pure**: it performs no I/O, no database access, and no storage-backend access. It operates only on plain dictionary-like records and mutates them in place (mempalace/dynamics.py:L8-L12). Callers in `hallways.py` and `palace_graph.py` invoke these functions; this module is the single source of the math so both connection kinds share identical semantics (mempalace/dynamics.py:L9-L12).

## Record Schema (added fields)

The functions read and write four fields on a connection record. All are default-safe: records lacking them are made valid by `initialize_dynamics_fields` (mempalace/dynamics.py:L14-L22):

- `strength` (float): Hebbian connection weight, floored at `STRENGTH_FLOOR`, capped at `MAX_STRENGTH` (mempalace/dynamics.py:L17-L18).
- `stability` (float): decay resistance; grows with spaced reinforcement (mempalace/dynamics.py:L19).
- `last_activated` (string, ISO-8601 datetime): updated on potentiation (mempalace/dynamics.py:L20).
- `access_count` (int): cumulative co-access events (mempalace/dynamics.py:L21).

A record may also carry `created_at` (ISO-8601 string), used as a fallback timestamp (mempalace/dynamics.py:L93-L96).

## Constants (observable numeric contract)

These constants define the math and are part of the contract:

- `STRENGTH_FLOOR = 0.05` — lower bound; strength never decays below this (mempalace/dynamics.py:L41-L44).
- `MAX_STRENGTH = 5.0` — upper bound; potentiation above this is a no-op (mempalace/dynamics.py:L46-L49).
- `DEFAULT_STABILITY = 1.0` — initial stability for a new connection (mempalace/dynamics.py:L51-L53).
- `DEFAULT_STRENGTH = 1.0` — initial strength for a new connection (mempalace/dynamics.py:L55-L57).
- `POTENTIATION_INCREMENT = 0.05` — default strength increase per co-access (mempalace/dynamics.py:L59-L61).
- `SPACED_INTERVAL_HOURS = 1.0` — minimum gap in hours between potentiations to count as "spaced" reinforcement (mempalace/dynamics.py:L63-L66).
- `STABILITY_INCREMENT = 0.1` — stability growth per spaced reinforcement (mempalace/dynamics.py:L68-L71).

All seven constants plus the three functions below are the module's public surface (mempalace/dynamics.py:L245-L256).

## Public Function: `initialize_dynamics_fields(connection, *, now=None) -> connection`

Populates `strength`, `stability`, `last_activated`, and `access_count` only if missing; existing fields are never overwritten (backfill semantics) (mempalace/dynamics.py:L79-L88). Safe to call on any record; a no-op when all four fields are already present (mempalace/dynamics.py:L84-L85).

Behavior:
- If `now` is not supplied, it defaults to the current UTC time (mempalace/dynamics.py:L89-L90).
- `now` is normalized to an ISO string; if a datetime is passed it is formatted via ISO, otherwise used as-is (mempalace/dynamics.py:L91).
- The fallback for `last_activated` is the record's `created_at` if present, else `now` as ISO — so a brand-new record's decay clock starts from creation, not from initialization-call time (mempalace/dynamics.py:L93-L96).
- Defaults applied when absent: `strength` ← `DEFAULT_STRENGTH`, `stability` ← `DEFAULT_STABILITY`, `last_activated` ← the `created_at`/now fallback, `access_count` ← `0` (mempalace/dynamics.py:L98-L101).
- Returns the same (mutated) record (mempalace/dynamics.py:L102).

## Public Function: `potentiate(connection, *, increment=POTENTIATION_INCREMENT, now=None) -> connection`

Strengthens a connection on a co-access event (mempalace/dynamics.py:L110-L126).

Behavior and ordering:
1. If `now` is not supplied, defaults to current UTC time (mempalace/dynamics.py:L127-L128).
2. Calls `initialize_dynamics_fields` first, so partial records are valid before processing (mempalace/dynamics.py:L130-L131).
3. Computes the gap since last activation: takes `last_activated` (or, if falsy, `created_at`), parses it, and computes hours elapsed as `(now - last) / 3600 seconds`. If the timestamp cannot be parsed, `hours_since` is treated as `0.0` (mempalace/dynamics.py:L133-L140).
4. Increases `strength` by `increment`, capped at `MAX_STRENGTH`: `strength = min(MAX_STRENGTH, current_strength + increment)` (mempalace/dynamics.py:L142-L144).
5. **Spacing effect**: stability grows by `STABILITY_INCREMENT` only if `hours_since >= SPACED_INTERVAL_HOURS`. Rapid bursts (gap < the threshold) do not increase stability (mempalace/dynamics.py:L146-L149).
6. Always sets `last_activated` to `now` in ISO format, and increments `access_count` by 1 (mempalace/dynamics.py:L151-L153).
7. Returns the same (mutated) record for chaining (mempalace/dynamics.py:L154-L155).

Edge cases: `current_strength` is read defaulting to `DEFAULT_STRENGTH`; `increment` is coerced to a float (mempalace/dynamics.py:L143-L144). `access_count` is coerced to int with default 0 before increment (mempalace/dynamics.py:L153).

## Public Function: `apply_decay(connection, *, now=None) -> connection`

Applies Ebbinghaus exponential decay to `strength` (mempalace/dynamics.py:L163-L178).

Decay model: `new_strength = current_strength * exp(-days_since_last / stability)`, floored at `STRENGTH_FLOOR`. Higher stability means slower decay (mempalace/dynamics.py:L164-L169, L203-L206).

Behavior and ordering:
1. If `now` is not supplied, defaults to current UTC time (mempalace/dynamics.py:L179-L180).
2. Calls `initialize_dynamics_fields` first to backfill missing fields (mempalace/dynamics.py:L182-L183).
3. Reads `last_activated` (or `created_at` if falsy) and parses it. If it cannot be parsed, the record is returned **unchanged** (strength is not corrupted) (mempalace/dynamics.py:L185-L191).
4. Computes `days_since = (now - last) / 86400 seconds`. If `days_since <= 0` (no time passed or clock skew), returns unchanged — i.e. idempotent at the same instant (mempalace/dynamics.py:L193-L196).
5. Reads `stability` (default `DEFAULT_STABILITY`); if `stability <= 0` it is reset to `DEFAULT_STABILITY` to avoid division issues (mempalace/dynamics.py:L198-L200).
6. Computes the decay factor and new strength, then writes `strength = max(STRENGTH_FLOOR, new_strength)` (mempalace/dynamics.py:L202-L206).
7. Returns the same (mutated) record (mempalace/dynamics.py:L207).

Invariants:
- Strength never goes below `STRENGTH_FLOOR` after decay (mempalace/dynamics.py:L206).
- Idempotent when called twice at the same `now` with no intervening potentiation (mempalace/dynamics.py:L170-L172, L193-L196).

## Helper: `_parse_iso(value) -> datetime | None` (internal)

Liberal ISO-8601 parser used by `potentiate` and `apply_decay`. Not part of the exported public surface (mempalace/dynamics.py:L245-L256) but defines observable timestamp-handling behavior (mempalace/dynamics.py:L215-L242):

- Returns `None` if `value` is `None` (mempalace/dynamics.py:L222-L223).
- If `value` is already a datetime: returns it as-is when timezone-aware, otherwise stamped as UTC (mempalace/dynamics.py:L224-L225).
- Returns `None` if `value` is not a non-empty string (mempalace/dynamics.py:L226-L227).
- A trailing `Z` is converted to `+00:00` before parsing, so Z-suffixed timestamps are accepted (mempalace/dynamics.py:L232-L234).
- Parsed values lacking timezone info are forced to UTC, so subtraction against the timezone-aware `now` never fails (mempalace/dynamics.py:L235-L240).
- On any parse failure, returns `None` rather than raising; callers treat `None` as "unknown timestamp" (mempalace/dynamics.py:L218-L220, L241-L242).

## Side Effects

None beyond in-place mutation of the passed record. No filesystem, network, process, or environment access (mempalace/dynamics.py:L8-L12, L124-L125, L174-L175).
