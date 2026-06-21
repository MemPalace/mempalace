# Behavior Specification: `dynamics` connection-strength math

This spec is derived from the test suite at `tests/test_dynamics.py`, which exercises
the public surface of the `dynamics` module: living-connection math for halls and
tunnels. The module is pure math with no I/O and no storage dependencies; all
time (`now`) is injected explicitly so behavior is deterministic
(tests/test_dynamics.py:L1-L8).

## Connection record shape

A connection is a mutable key/value record. The minimal shape (as persisted before
dynamics fields exist) carries: `id` (string), `wing` (string), `entity_a` (string),
`entity_b` (string), `co_occurrence_count` (integer), `rooms` (list of strings),
`label` (string), `created_at` (ISO-8601 timestamp string), and `created_by` (string)
(tests/test_dynamics.py:L34-L49). Dynamics fields (`strength`, `stability`,
`last_activated`, `access_count`) may be absent on records created before this module
existed (tests/test_dynamics.py:L58-L62, L166-L173).

## Public surface

Exported names: constants `DEFAULT_STABILITY`, `DEFAULT_STRENGTH`, `MAX_STRENGTH`,
`POTENTIATION_INCREMENT`, `STABILITY_INCREMENT`, `STRENGTH_FLOOR`; functions
`apply_decay`, `initialize_dynamics_fields`, `potentiate`
(tests/test_dynamics.py:L17-L27). The constants `STABILITY_INCREMENT` and the
spacing threshold (referred to as `SPACED_INTERVAL_HOURS`, valued at 1 hour) govern
stability growth (tests/test_dynamics.py:L141-L149).

## `initialize_dynamics_fields(conn, now)` — backfill for legacy records

Mutates `conn` in place to populate missing dynamics fields. When `strength` is
absent it is set to `DEFAULT_STRENGTH` (tests/test_dynamics.py:L58-L62). When
`stability` is absent it is set to `DEFAULT_STABILITY` (tests/test_dynamics.py:L64-L67).
When `last_activated` is absent it is anchored to the record's `created_at` value (its
ISO string), not to `now` — decay starts from creation time, so passing a `now`
five days later still yields `last_activated == created_at`
(tests/test_dynamics.py:L69-L74). When `access_count` is absent it is set to `0`
(tests/test_dynamics.py:L76-L79).

This function is a backfill, not a reset: if any dynamics field already exists its
value is preserved unchanged. Given a record with `strength=2.3`, `stability=1.7`,
a custom `last_activated`, and `access_count=17`, all four pass through untouched even
when `now` is later than those values (tests/test_dynamics.py:L81-L93). If
`created_at` is missing entirely the function must not crash; it falls back to `now`
for `last_activated` and still populates the other dynamics fields
(tests/test_dynamics.py:L95-L103).

## `potentiate(conn, now, increment=POTENTIATION_INCREMENT)` — Hebbian strengthening

Mutates and returns the same `conn` record (returns the identical object, enabling
chaining) (tests/test_dynamics.py:L160-L164). Increases `strength` by the increment:
by default `strength` becomes `DEFAULT_STRENGTH + POTENTIATION_INCREMENT`
(tests/test_dynamics.py:L112-L115); a caller-supplied `increment` is honored, e.g.
`increment=0.2` yields `DEFAULT_STRENGTH + 0.2` (tests/test_dynamics.py:L117-L120).
Strength is capped at `MAX_STRENGTH`: starting at `MAX_STRENGTH - 0.01` and applying
`increment=1.0` clamps the result exactly to `MAX_STRENGTH`
(tests/test_dynamics.py:L122-L126).

It sets `last_activated` to the ISO string of `now` (tests/test_dynamics.py:L128-L132)
and increments `access_count` by one on each call (1 after first call, 2 after second)
(tests/test_dynamics.py:L134-L139).

Stability growth implements the Cepeda spacing effect. When the gap between `now` and
the prior `last_activated` is greater than or equal to the spacing threshold (1 hour),
`stability` grows by `STABILITY_INCREMENT`: potentiating at `T0 + 2 hours` yields
`stability == DEFAULT_STABILITY + STABILITY_INCREMENT` (tests/test_dynamics.py:L141-L149).
When the gap is below the threshold, stability is left unchanged: potentiating just
10 minutes after creation leaves `stability == DEFAULT_STABILITY`
(tests/test_dynamics.py:L151-L158). Rapid bursts within the spacing window grow only
`access_count`, not stability — five potentiations at 5/10/15/20/25 minutes leave
`stability == DEFAULT_STABILITY` while `access_count == 5`
(tests/test_dynamics.py:L304-L311).

Potentiate works on records lacking dynamics fields: missing fields are backfilled
safely, so a record with no `strength` becomes
`DEFAULT_STRENGTH + POTENTIATION_INCREMENT` with `access_count == 1`
(tests/test_dynamics.py:L166-L173).

## `apply_decay(conn, now)` — Ebbinghaus exponential decay

Mutates and returns the same `conn` record (returns the identical object)
(tests/test_dynamics.py:L258-L264). Reduces `strength` as an exponential function of
elapsed time scaled by stability. The decay factor is `exp(-days_since / stability)`:
with `strength=1.0`, `stability=1.0`, and one day elapsed, the result equals
`exp(-1.0)` (about 0.3679) (tests/test_dynamics.py:L182-L190). Higher stability decays
slower: for equal elapsed time, a record with `stability=2.0` retains strictly more
strength than one with `stability=1.0` (tests/test_dynamics.py:L192-L207).

Strength never decays below `STRENGTH_FLOOR`: after a 10,000-day gap the strength is
exactly `STRENGTH_FLOOR` (tests/test_dynamics.py:L209-L217). When no time has passed
(`now == last_activated`) decay is a no-op and strength is unchanged
(tests/test_dynamics.py:L237-L244). Decay is idempotent at a fixed instant: calling
`apply_decay` twice at the same `now` (with no intervening potentiation) yields the
same strength as calling it once, because the second call sees an unchanged
`last_activated` (tests/test_dynamics.py:L219-L235). Note this implies `apply_decay`
does not advance `last_activated`.

Decay handles records missing dynamics fields by backfilling safe defaults first
without crashing. With `last_activated` falling back to `created_at` and a one-day gap
at default stability, strength becomes `exp(-1.0)` (tests/test_dynamics.py:L246-L256).

## Integration / ordering guarantees

After decay reduces a connection's strength, a subsequent `potentiate` at the same
instant raises strength back above the decayed value — attention rebuilds salience
(tests/test_dynamics.py:L272-L289). Repeated spaced reinforcement grows stability
monotonically: potentiating once per day for five days produces a strictly increasing
stability sequence (tests/test_dynamics.py:L291-L302). Burst reinforcement inside the
spacing window does not grow stability regardless of count
(tests/test_dynamics.py:L304-L311).

## Invariants summary

- `strength` is bounded within `[STRENGTH_FLOOR, MAX_STRENGTH]` across potentiate
  (upper) and decay (lower) (tests/test_dynamics.py:L122-L126, L209-L217).
- Timestamps are stored as ISO-8601 strings (tests/test_dynamics.py:L74, L132).
- `potentiate` and `apply_decay` both mutate and return the same record object
  (tests/test_dynamics.py:L160-L164, L258-L264).
- `initialize_dynamics_fields` never overwrites existing dynamics values
  (tests/test_dynamics.py:L81-L93).
