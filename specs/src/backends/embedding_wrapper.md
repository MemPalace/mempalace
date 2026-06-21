# Spec: backends/embedding_wrapper

Core-side adapter that lets generic callers use text-based collection operations against storage backends that require explicit precomputed vectors (e.g. pgvector, sqlite_exact). It computes embeddings locally and delegates everything else to a wrapped backend collection (mempalace/backends/embedding_wrapper.py:L1-L1, mempalace/backends/embedding_wrapper.py:L38-L44).

## Helper: text embedding

A text-embedding helper takes a list of strings and returns a list of float vectors, one vector per input string, in the same order (mempalace/backends/embedding_wrapper.py:L10-L18). If the input list is empty, it returns an empty list without invoking any embedding machinery (mempalace/backends/embedding_wrapper.py:L12-L13). Otherwise it obtains the configured local embedding function and applies it to the full batch of texts at once, returning each produced vector materialized as a list of floats (mempalace/backends/embedding_wrapper.py:L14-L18). The embedding function is the process-configured local one; this is the only place network/model side effects can occur (mempalace/backends/embedding_wrapper.py:L14-L17).

## Helper: input normalization

A normalization helper coerces a "one-or-many" argument shape into a list (mempalace/backends/embedding_wrapper.py:L21-L35). A bare string or a single mapping/dict value is wrapped into a single-element list rather than being iterated; this is an observable correctness contract — a lone string must not be split into its characters and a lone dict must not be reduced to its keys, because either would desynchronize the parallel `documents`/`metadatas`/`embeddings` arrays from `ids` on explicit-vector backends (mempalace/backends/embedding_wrapper.py:L22-L32). An existing list is returned unchanged (no copy) (mempalace/backends/embedding_wrapper.py:L33-L34). Any other iterable is materialized once into a list (mempalace/backends/embedding_wrapper.py:L35).

## Wrapper construction and delegation

The wrapper is constructed around a single inner backend collection and stores it as its sole state (mempalace/backends/embedding_wrapper.py:L46-L47). Any attribute or method not explicitly defined on the wrapper is transparently forwarded to the inner collection (mempalace/backends/embedding_wrapper.py:L49-L50). Backends opt into this wrapping via a `requires_explicit_embeddings` capability; callers continue to pass `documents=`/`query_texts=` and the wrapper supplies vectors (mempalace/backends/embedding_wrapper.py:L41-L43).

The `distance_metric` value is explicitly forwarded to the inner collection rather than defaulting; the wrapper must report whatever metric the wrapped backend uses, not a fixed default (mempalace/backends/embedding_wrapper.py:L52-L58). The embedder-identity operations — read stored identity, set identity, and effective identity — are each explicitly forwarded to the inner collection (mempalace/backends/embedding_wrapper.py:L63-L70). Maintenance state read and maintenance run (by `kind`) are forwarded to the inner collection (mempalace/backends/embedding_wrapper.py:L72-L76).

## add

`add` accepts `documents`, `ids`, optional `metadatas`, and optional `embeddings`. It normalizes `documents` and `ids` to lists, and normalizes `metadatas` to a list only when provided (mempalace/backends/embedding_wrapper.py:L78-L82). If `embeddings` is not supplied, it computes them from the normalized `documents` (mempalace/backends/embedding_wrapper.py:L83-L84). It then delegates to the inner collection's `add` with the normalized/derived `documents`, `ids`, `metadatas`, and `embeddings`, returning the inner result (mempalace/backends/embedding_wrapper.py:L85-L90). Because embeddings derive from `documents` in order, the produced vectors align positionally with `ids` (mempalace/backends/embedding_wrapper.py:L79-L90).

## upsert

`upsert` behaves identically to `add` in its argument handling: normalize `documents`/`ids`, normalize `metadatas` when present, compute `embeddings` from `documents` when not supplied, then delegate to the inner collection's `upsert` and return its result (mempalace/backends/embedding_wrapper.py:L92-L104).

## query

`query` accepts optional `query_texts` (a string or list of strings), optional `query_embeddings`, `n_results` (default 10), optional `where`, optional `where_document`, and optional `include` (mempalace/backends/embedding_wrapper.py:L106-L115). When `query_texts` is provided and `query_embeddings` is absent, it normalizes the texts to a list, embeds them, sets `query_embeddings` to the result, and clears `query_texts` so the inner backend receives vectors only (mempalace/backends/embedding_wrapper.py:L116-L118). If `query_embeddings` is already provided, the texts are passed through untouched and no embedding occurs (mempalace/backends/embedding_wrapper.py:L116-L118). It delegates to the inner collection's `query` with all parameters and returns its result (mempalace/backends/embedding_wrapper.py:L119-L126).

## get / delete / count / estimated_count / close / health / lexical_search

`get` (with `ids`, `where`, `where_document`, `limit`, `offset`, `include`) is forwarded verbatim to the inner collection with no embedding or normalization (mempalace/backends/embedding_wrapper.py:L128-L138). `delete` (with `ids`, `where`) is forwarded verbatim (mempalace/backends/embedding_wrapper.py:L140-L141). `count` and `estimated_count` return the inner collection's respective integer counts (mempalace/backends/embedding_wrapper.py:L143-L147). `close` and `health` are forwarded to the inner collection (mempalace/backends/embedding_wrapper.py:L149-L153). `lexical_search` (with `query`, `n_results` default 10, optional `where`) is forwarded verbatim — no vector computation is performed for lexical search (mempalace/backends/embedding_wrapper.py:L155-L156).

## update

`update` accepts `ids` plus optional `documents`, `metadatas`, and `embeddings` (mempalace/backends/embedding_wrapper.py:L158-L158). It always normalizes `ids` to a list (mempalace/backends/embedding_wrapper.py:L159-L159). When `documents` is provided it is normalized to a list, and if `embeddings` was not supplied they are computed from those documents (mempalace/backends/embedding_wrapper.py:L160-L163). When `metadatas` is provided it is normalized to a list (mempalace/backends/embedding_wrapper.py:L164-L165). It delegates to the inner collection's `update` with `ids`, `documents`, `metadatas`, and `embeddings`, returning its result (mempalace/backends/embedding_wrapper.py:L166-L171). When `documents` is omitted, no embeddings are computed and any caller-supplied `embeddings` pass through unchanged (mempalace/backends/embedding_wrapper.py:L160-L171).

## Invariants

- Output vectors from text embedding are positionally aligned 1:1 with their input texts, preserving order (mempalace/backends/embedding_wrapper.py:L10-L18).
- Single string and single dict inputs are wrapped as one-element lists, never iterated/split, to keep parallel arrays (`documents`/`ids`/`metadatas`/`embeddings`) length-consistent (mempalace/backends/embedding_wrapper.py:L22-L32).
- Embeddings are only auto-computed when the caller does not supply them; caller-supplied embeddings are always preferred (mempalace/backends/embedding_wrapper.py:L83-L84, mempalace/backends/embedding_wrapper.py:L97-L98, mempalace/backends/embedding_wrapper.py:L116-L118, mempalace/backends/embedding_wrapper.py:L162-L163).
- On `query`, supplying vectors and clearing texts is mutually exclusive: the inner backend never receives both texts and the derived vectors simultaneously (mempalace/backends/embedding_wrapper.py:L116-L118).
