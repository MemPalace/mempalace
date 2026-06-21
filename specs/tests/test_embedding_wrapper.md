# Spec: EmbeddingCollection OneOrMany Handling

Behavior specification distilled from `tests/test_embedding_wrapper.py`. This test
suite pins the contract of an embedding wrapper (`EmbeddingCollection`) over a
storage backend, focused on correctly handling "OneOrMany" argument shapes —
arguments that may be either a single bare value or a collection of values
(tests/test_embedding_wrapper.py:L1-L7).

## Unit Under Test

The subject is `EmbeddingCollection`, a wrapper constructed around an inner
backend object that it delegates to (tests/test_embedding_wrapper.py:L9-L9,
L72-L72). Two free helpers also belong to the same module: `_as_list` (the
normalization helper) and `_embed_texts` (the embedding function, which the
tests stub) (tests/test_embedding_wrapper.py:L57-L57, L62-L62).

## Normalization helper: `_as_list`

`_as_list` converts an input into a list, treating bare scalar/atomic values as a
single-element list rather than iterating their characters or keys
(tests/test_embedding_wrapper.py:L61-L66):

- A bare string `"hello world"` becomes `["hello world"]` — it is wrapped, NOT
  split into characters (tests/test_embedding_wrapper.py:L62-L62).
- A bare dictionary `{"k": 1}` becomes `[{"k": 1}]` — wrapped as a single
  element, NOT iterated into its keys (e.g. not `["k"]`)
  (tests/test_embedding_wrapper.py:L63-L63).
- A list input is returned unchanged and uncopied — the exact same object
  identity is returned (no defensive copy) (tests/test_embedding_wrapper.py:L64-L65).
- Other ordered iterables (e.g. a tuple `("a", "b")`) are materialized into a
  list `["a", "b"]` (tests/test_embedding_wrapper.py:L66-L66).

## Delegation contract to the inner backend

The wrapper forwards normalized calls to an inner backend that exposes keyword-only
methods `add`, `upsert`, `update`, and `query`
(tests/test_embedding_wrapper.py:L18-L46):

- `add(documents, ids, metadatas=None, embeddings=None)`
  (tests/test_embedding_wrapper.py:L18-L24).
- `upsert(documents, ids, metadatas=None, embeddings=None)`
  (tests/test_embedding_wrapper.py:L26-L32).
- `update(ids, documents=None, metadatas=None, embeddings=None)` — note `ids` is
  required while `documents` is optional (tests/test_embedding_wrapper.py:L34-L40).
- `query(query_texts=None, query_embeddings=None, ...)` returns a `QueryResult`;
  in tests it returns an empty result (tests/test_embedding_wrapper.py:L42-L46).

## Embedding contract: `_embed_texts`

The wrapper computes embeddings by calling `_embed_texts(texts)` where `texts` is
a list of strings, producing one vector per input text in input order
(tests/test_embedding_wrapper.py:L49-L58). The tests stub this to return a list of
fixed 2-dimensional zero vectors, one per input, and record the exact `texts` list
it was invoked with (tests/test_embedding_wrapper.py:L53-L58). The recorded `texts`
is the observable proof of how documents/queries were normalized before embedding.

## `add` behavior

When `documents` is a bare string, `add` wraps it into a single-element list before
embedding and delegation. Given `documents="hello world"`
(tests/test_embedding_wrapper.py:L72-L72):

- The embedder is called with the whole string as one document: `["hello world"]`,
  never per-character (tests/test_embedding_wrapper.py:L74-L74).
- The inner backend receives `documents == ["hello world"]` and exactly one
  embedding, length-aligned with the single id
  (tests/test_embedding_wrapper.py:L76-L77).

`add` also normalizes `ids` and `metadatas` as OneOrMany shapes. Given
`ids="d1"` and `metadatas={"src": "web"}`
(tests/test_embedding_wrapper.py:L120-L120):

- A bare string id is wrapped: `ids == ["d1"]`, NOT split into `["d", "1"]`
  (tests/test_embedding_wrapper.py:L122-L122).
- A bare dict metadata is wrapped: `metadatas == [{"src": "web"}]`, NOT iterated
  into `["src"]` (tests/test_embedding_wrapper.py:L123-L123).
- All of `documents`, `embeddings`, `ids`, and `metadatas` are length-aligned at
  count 1 (tests/test_embedding_wrapper.py:L124-L126).

When `documents` is already a list, it passes through unchanged: given
`documents=["one", "two"]`, the embedder sees `["one", "two"]` and the backend
receives two embeddings in order (tests/test_embedding_wrapper.py:L108-L113).

## `upsert` behavior

`upsert` applies the same bare-string document wrapping. Given `documents="solo"`,
the embedder sees `["solo"]`, the backend receives `documents == ["solo"]`, and
exactly one embedding is produced (tests/test_embedding_wrapper.py:L80-L86).

## `update` behavior

`update` applies the same bare-string document wrapping. Given `ids=["d1"]` and
`documents="changed"`, the embedder sees `["changed"]`, the backend receives
`documents == ["changed"]`, and exactly one embedding is produced
(tests/test_embedding_wrapper.py:L89-L95).

## `query` behavior

`query` wraps a bare `query_texts` string into a single query, embeds it, and
delegates as an embedding rather than as text. Given `query_texts="find me"`
(tests/test_embedding_wrapper.py:L101-L101):

- The embedder is called with `["find me"]` (tests/test_embedding_wrapper.py:L102-L102).
- The wrapper produces exactly one query embedding passed to the backend as
  `query_embeddings` of length 1 (tests/test_embedding_wrapper.py:L104-L104).
- The backend receives `query_texts == None` — the text input is consumed and
  converted into the embedding, so text is not forwarded
  (tests/test_embedding_wrapper.py:L105-L105).

## Core invariant

Across all entry points, the critical invariant is length alignment: when a bare
scalar is supplied for `documents`, `ids`, `metadatas`, or `query_texts`, it must
be treated as a single item so the resulting `documents`/`ids`/`metadatas`/
`embeddings` collections all have matching length (count 1 for a single input),
rather than being expanded character-by-character or key-by-key
(tests/test_embedding_wrapper.py:L3-L7, L124-L126).
