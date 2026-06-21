# Spec: test_kg_thread_safety

Source: `tests/test_kg_thread_safety.py`

## Purpose

This is a TDD regression test that enforces a thread-safety invariant on the knowledge graph component: its `close` operation must acquire the same lock used to guard reads and writes, so that closing the resource concurrently with an in-progress read/write cannot corrupt data (tests/test_kg_thread_safety.py:L1-L1, tests/test_kg_thread_safety.py:L10-L13).

## Subject Under Test

The subject is a knowledge-graph component exposing a `close` operation and an internal mutual-exclusion lock named `_lock` that serializes read and write operations against the underlying store (tests/test_kg_thread_safety.py:L4-L4, tests/test_kg_thread_safety.py:L9-L10).

## Test Case: close must hold the lock

A single test verifies that the source text of the `close` operation references the instance lock `self._lock` (tests/test_kg_thread_safety.py:L7-L13).

- Inputs: none; the test inspects the implementation of `close` rather than executing it (tests/test_kg_thread_safety.py:L8-L9).
- Pass condition: the literal substring `self._lock` appears somewhere within the body of the `close` operation (tests/test_kg_thread_safety.py:L10-L10).
- Fail condition: if `close` does not reference the lock, the test fails with a diagnostic stating that `close()` does not acquire `self._lock` and that closing while a read/write is in progress can corrupt data (tests/test_kg_thread_safety.py:L11-L13).

## Observable Contract

The enforced contract is structural rather than runtime-behavioral: the `close` operation of the knowledge graph must, by inspection of its implementation, engage the lock guarding read/write access (tests/test_kg_thread_safety.py:L9-L10). The underlying intent is that closing must be mutually exclusive with reads and writes to prevent data corruption during concurrent access (tests/test_kg_thread_safety.py:L1-L1, tests/test_kg_thread_safety.py:L11-L13).

## Side Effects and Dependencies

The test performs no filesystem, network, or process side effects; it only reads the source definition of the `close` operation from the knowledge-graph module (tests/test_kg_thread_safety.py:L3-L9).
