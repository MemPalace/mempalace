# Spec: ChromaCollection palace-write-lock integration

This specification describes the behavioral contract that the test suite
`tests/test_chroma_collection_lock.py` asserts of the storage-collection write
adapter (`ChromaCollection`) and its interaction with the palace write lock
(`mine_palace_lock` / `MineAlreadyRunning`). The contract closes the gap where
the palace write lock previously only protected the dedicated mine pipeline,
leaving direct/MCP writers free to race into the underlying vector store and
corrupt its index under concurrency (tests/test_chroma_collection_lock.py:L1-L24).

## Subject under contract

A write-collection adapter constructed over an underlying vector collection
object. It is built two ways: with an optional palace path
(`ChromaCollection(c, palace_path=p)`) or without one (`ChromaCollection(c)`)
(tests/test_chroma_collection_lock.py:L34-L35, L112, L159).

The palace write lock is a named exclusive lock keyed by a palace path. It can
be held by `with mine_palace_lock(p): ...`; attempting to acquire it while
another distinct holder owns it surfaces a `MineAlreadyRunning` error
(tests/test_chroma_collection_lock.py:L35, L87, L94-L95).

## Lock directory location

The lock infrastructure derives its on-disk lock directory from the `HOME`
environment variable; tests redirect `HOME` to a temporary directory so the
lock state is isolated per test, and child processes re-export `HOME` before
importing so they route to the same lock directory as the parent
(tests/test_chroma_collection_lock.py:L110, L143, L190, L229-L231, L256, L286).

## Write surface and guarantees

### Write operations are lock-gated when a palace path is supplied

The adapter exposes four write operations: `add`, `upsert`, `update`, and
`delete`. Each accepts keyword arguments that are forwarded verbatim to the
underlying collection's same-named method (e.g. `documents`, `ids`,
`metadatas`) (tests/test_chroma_collection_lock.py:L62-L72, L200-L203).

When the adapter is built with a `palace_path`, every write operation must
acquire the palace write lock for that path before forwarding to the underlying
collection (tests/test_chroma_collection_lock.py:L11-L13, L136-L141).

If another holder currently owns the lock for that palace path, each of the
four write operations must raise `MineAlreadyRunning` and must NOT call into the
underlying collection at all. After such failures the underlying collection has
recorded zero `add`, `upsert`, `update`, or `delete` calls — the gate fires
before reaching the storage layer (tests/test_chroma_collection_lock.py:L158-L175).

### Successful forwarding

When the lock is free (or acquirable), a write operation completes by invoking
the underlying collection's corresponding method exactly once with the same
keyword payload it received. For example, `upsert(documents=["doc"],
ids=["id-1"])` results in the underlying collection recording exactly one
upsert with payload `{"documents": ["doc"], "ids": ["id-1"]}`
(tests/test_chroma_collection_lock.py:L129-L130, L205-L208).

### No palace path: legacy no-lock behavior

When the adapter is built without a `palace_path`, it must not touch the lock
infrastructure at all. A write must succeed even while another process holds the
palace write lock for some palace path — the lock does not gate this caller. In
that case `upsert(...)` forwards directly to the underlying collection and the
call is recorded (tests/test_chroma_collection_lock.py:L16-L19, L103-L130).

## Re-entrancy invariant

The palace write lock must be re-entrant within a single thread so that a writer
already holding the lock can issue lock-gated writes without self-deadlocking.
Concretely, inside `with mine_palace_lock(p):`, calls to `upsert`, `add`,
`update`, and `delete` on an adapter built with the same `palace_path=p` each
run to completion and forward to the underlying collection. After the block, the
underlying collection has recorded exactly one of each operation
(tests/test_chroma_collection_lock.py:L15-L16, L181-L208).

This invariant mirrors the production composition where the mine pipeline holds
the lock for its full duration and then performs collection writes inside that
held lock (tests/test_chroma_collection_lock.py:L181-L189).

## Concurrency / serialization contract

Two independent processes that each call a write operation on adapters bound to
the same palace path must be serialized: at most one enters the underlying
collection at any time. The contended writer must raise `MineAlreadyRunning`
rather than proceeding concurrently. When two processes contend (one starting
slightly earlier and holding the lock long enough to guarantee contention), the
set of outcomes is exactly one success and one busy failure — i.e. the sorted
status pair is `["busy", "ok"]` (tests/test_chroma_collection_lock.py:L243-L275).

The contended writer reports `"busy"` precisely when its write raised
`MineAlreadyRunning`; the winner reports `"ok"` when its write returned normally
(tests/test_chroma_collection_lock.py:L236-L240).

This serialization is the property that prevents concurrent parallel inserts
into the vector index from corrupting it under fan-out write load
(tests/test_chroma_collection_lock.py:L243-L255).

## Read path contract

Read operations must NOT be gated by the write lock. While another process holds
the palace write lock, reads must still complete instantly. The read operations
covered are `query`, `get`, and `count` (tests/test_chroma_collection_lock.py:L278-L285).

The structural invariant asserted is: each of the write methods `add`, `upsert`,
`update`, `delete` engages the write-lock path (identified by the marker
`_write_lock`), while each of the read methods `query`, `get`, `count` (when
present) does NOT engage the write-lock path
(tests/test_chroma_collection_lock.py:L311-L322).

## Platform note

The cross-process contention behavior is POSIX/Unix oriented. The underlying
lock uses Unix advisory file locking on POSIX systems and a different mechanism
on Windows, whose contention semantics differ; the cross-process tests are
skipped on Windows runners (tests/test_chroma_collection_lock.py:L21-L23).

## Inter-process synchronization protocol (test harness contract)

A lock-holder helper acquires `mine_palace_lock(palace_path)`, then signals
readiness by creating a `ready` flag file, then polls (up to ~5 seconds, in
10ms increments) for a `release` flag file before releasing the lock. It returns
`0` on normal acquire/release and `1` if acquisition raised
`MineAlreadyRunning` (tests/test_chroma_collection_lock.py:L80-L95). Callers wait
for the `ready` flag to appear before proceeding, then create the `release` flag
to let the holder finish (tests/test_chroma_collection_lock.py:L120-L133).

Child processes are spawned using the `spawn` start method (not `fork`),
because `fork` deadlocks under a multi-threaded parent and macOS forbids
fork-without-exec (tests/test_chroma_collection_lock.py:L38-L45).
