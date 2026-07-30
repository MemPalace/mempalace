"""Shared backend isolation conformance assertions (RFC 001 isolation contract).

Any backend's test module can import these to prove the isolation guarantees
declared on :class:`mempalace.backends.PalaceRef`:

* per-``PalaceRef.id`` isolation — required of every backend;
* per-``PalaceRef.namespace`` isolation — required of backends advertising the
  ``supports_namespace_isolation`` capability.

This module is intentionally not a ``test_*`` file: it ships assertions, not
test cases, so pytest does not collect it directly.
"""

_PROBE_ID = "conformance-isolation-probe"
_PROBE_DOC = "partition isolation probe document"
_PROBE_EMBEDDING = [1.0, 0.0, 0.0, 0.0]


def assert_partition_isolation(backend, writer, other, *, embedding=None):
    """Assert ``writer`` and ``other`` are isolated partitions of ``backend``.

    A record written to ``writer`` MUST NOT be returned, modified, or deleted
    through ``other`` (query / get / count / delete), and ``writer`` MUST still
    hold it afterwards. ``writer`` and ``other`` are two collections that the
    isolation contract says must not see each other — distinct palace ids (the
    universal guarantee) or distinct namespaces (the namespace guarantee).

    Embeddings are supplied only for backends that require explicit vectors, so
    the same assertion works for text-embedding backends (Chroma) and
    explicit-vector backends (qdrant, sqlite_exact) alike.
    """
    explicit = "requires_explicit_embeddings" in backend.capabilities
    vector = list(embedding if embedding is not None else _PROBE_EMBEDDING)

    baseline_other = other.count()

    add_kwargs = {
        "ids": [_PROBE_ID],
        "documents": [_PROBE_DOC],
        "metadatas": [{"wing": "conformance"}],
    }
    if explicit:
        add_kwargs["embeddings"] = [vector]
    writer.add(**add_kwargs)

    if explicit:
        leaked = other.query(query_embeddings=[vector], n_results=10)
    else:
        leaked = other.query(query_texts=[_PROBE_DOC], n_results=10)
    hit_ids = leaked.ids[0] if leaked.ids else []
    assert _PROBE_ID not in hit_ids, "query() leaked a record across the isolation boundary"

    assert other.get(ids=[_PROBE_ID]).ids == [], (
        "get() leaked a record across the isolation boundary"
    )
    assert other.count() == baseline_other, "count() leaked a record across the isolation boundary"

    # A delete issued against the other partition MUST NOT touch writer's record.
    other.delete(ids=[_PROBE_ID])
    survivor = writer.get(ids=[_PROBE_ID])
    assert survivor.ids == [_PROBE_ID], "delete() crossed the isolation boundary"
