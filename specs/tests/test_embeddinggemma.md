# Spec: EmbeddingGemma ONNX embedding behavior

This spec describes the observable behavior of the `EmbeddinggemmaONNX` embedding
function and the `get_embedding_function` factory, as asserted by the test suite
`tests/test_embeddinggemma.py`. The tests stand in for the module's contract: the
real implementation lives in `mempalace.embedding`, but every behavior below is
pinned by an assertion in this test file. Where the test only observes behavior
(rather than fully specifying it), the spec notes that.

## Test environment / preconditions

The suite is skipped entirely unless three runtime capabilities are present: a
numeric array library (`numpy`), a model-download capability (`huggingface_hub`),
and a tokenizer capability (`tokenizers`) (tests/test_embeddinggemma.py:L17-L19).
These correspond to a "multilingual extra" install; under a core-only install the
tests do not run (tests/test_embeddinggemma.py:L7-L8).

The embedding module maintains two pieces of process-global state that must be
isolatable for correctness: an embedding-function cache `_EF_CACHE` (a mapping) and
a warned-set `_WARNED` (a set). Tests reset both to empty before each case
(tests/test_embeddinggemma.py:L24-L27), establishing that these are module-level
caches the implementation reads and writes.

## Class: `EmbeddinggemmaONNX`

### Stable model name

The class exposes a `name()` accessor (callable on the class itself) that returns
the exact string `"embeddinggemma_300m"` (tests/test_embeddinggemma.py:L121-L123).
This name is persisted onto a storage collection, so it is a hard on-disk contract:
changing it breaks reads of existing data (tests/test_embeddinggemma.py:L122).

### Constructor

`EmbeddinggemmaONNX()` can be constructed with no arguments
(tests/test_embeddinggemma.py:L127). It accepts an optional `batch_size` keyword
argument controlling the forward-pass sub-batch size
(tests/test_embeddinggemma.py:L242).

`batch_size` must be at least 1. Constructing with `batch_size=0` or any negative
value (e.g. `-3`) raises an error whose message references `"batch_size"`
(tests/test_embeddinggemma.py:L248-L253). The rationale recorded is that a
zero/negative batch size would either loop forever or embed nothing
(tests/test_embeddinggemma.py:L249).

Construction does not load the model; model loading is deferred (see lazy load).

### Callable interface (embedding a batch)

An instance is callable: `ef(texts)` returns a sequence of embedding vectors. The
input is a sequence of documents (strings); the output is one vector per input
document, in input order.

Inputs and their handling:

- A list of N strings yields N vectors (tests/test_embeddinggemma.py:L227-L229).
- A bare single string is treated as exactly one document, not as a sequence of
  characters: `ef("standalone document")` yields shape `(1, 384)`
  (tests/test_embeddinggemma.py:L264-L268).
- Empty input yields empty output: both `ef([])` and `ef(None)` return an empty
  list (tests/test_embeddinggemma.py:L256-L260). Empty input must not trigger any
  model download or load — the download counter stays at 0
  (tests/test_embeddinggemma.py:L261).

Output shape and dimensionality:

- Each output vector has exactly 384 dimensions. For 3 input documents the output
  array shape is `(3, 384)` (tests/test_embeddinggemma.py:L136-L140). The 384-dim
  result is a truncation (Matryoshka / MRL-style) of a larger model output
  (tests/test_embeddinggemma.py:L140); the underlying model produces a wider
  tensor (the test fake emits 768-wide rows) that is truncated to 384
  (tests/test_embeddinggemma.py:L30-L34,L51).

Output normalization:

- Every output vector is L2-normalized to unit length. The L2 norm of each row is
  1.0 within tolerance `1e-5` (tests/test_embeddinggemma.py:L143-L148).

Ordering guarantee:

- Output row order matches input document order. Sub-batches cover the input in
  order and each chunk's results are appended in order, pinning output order to
  input order (tests/test_embeddinggemma.py:L195-L199).

### Query prefix

Before tokenization, every input document is prefixed with a fixed task-instruction
string. The prefix is the constant `_EMBEDDINGGEMMA_PREFIX`, whose value is
`"task: sentence similarity | query: "` (tests/test_embeddinggemma.py:L162,L197).
The text actually fed to the tokenizer is `prefix + document`, with the original
document text preserved verbatim after the prefix
(tests/test_embeddinggemma.py:L161-L164,L197). Every tokenized text begins with the
prefix (tests/test_embeddinggemma.py:L162).

### Lazy load (load-once)

The model, weights, and tokenizer are loaded lazily on first embedding call, not at
construction. The load is performed once and cached for the lifetime of the
instance. After three separate calls on one instance, the load actions have each
run exactly once:

- The model-asset download function runs 3 times total — once each for the model
  graph, the weights, and the tokenizer file — and does not repeat per call
  (tests/test_embeddinggemma.py:L126-L131).
- The inference-session constructor runs exactly once
  (tests/test_embeddinggemma.py:L132).
- The tokenizer-from-file load runs exactly once
  (tests/test_embeddinggemma.py:L133).

(The "3 downloads" reflects three distinct downloaded artifacts at first load, not
three calls; subsequent calls reuse the loaded session and tokenizer.)

### Batched / chunked forward passes

Large inputs must be processed in bounded sub-batches rather than a single forward
pass. The default sub-batch bound is the constant `_EMBEDDINGGEMMA_BATCH_SIZE`,
whose value is 32 (tests/test_embeddinggemma.py:L203,L217). The rationale recorded
is that one unchunked forward pass over a repair-scale batch (e.g. 5000 docs)
allocates attention buffers beyond available RAM and gets the process killed by the
kernel; therefore no single forward pass may see more than the batch-size limit of
documents (tests/test_embeddinggemma.py:L167-L173).

Chunking algorithm (observable via the sequence of per-call sub-batch sizes):

- The input is split into consecutive sub-batches of size `_EMBEDDINGGEMMA_BATCH_SIZE`,
  with a final smaller remainder batch if the count is not an exact multiple. For
  `2 * 32 + 6 = 70` documents the sub-batch sizes are `[32, 32, 6]`
  (tests/test_embeddinggemma.py:L185-L194).
- The combined per-chunk tokenized texts cover the whole input in order, each
  prefixed: the concatenation of all sub-batches equals
  `[prefix + d for d in docs]` over the original documents
  (tests/test_embeddinggemma.py:L197).
- Chunked outputs concatenate back to a single result of shape `(n, 384)` and
  remain L2-normalized (tests/test_embeddinggemma.py:L198-L200).

Boundary behavior — no empty and no oversized sub-batches
(tests/test_embeddinggemma.py:L206-L229):

- 1 document → sub-batches `[1]`
- exactly 32 → `[32]`
- 33 → `[32, 1]`
- 64 → `[32, 32]`

In every case the total number of output vectors equals the number of input
documents (tests/test_embeddinggemma.py:L229).

Custom batch size: a constructor `batch_size` overrides the default bound and drives
the split. With `batch_size=10` over 24 documents the sub-batch sizes are
`[10, 10, 4]`, and the output count is 24 (tests/test_embeddinggemma.py:L232-L245).

### Tokenizer configuration (observed)

The tokenizer is configured for padding and truncation when loaded. The
implementation calls `enable_padding()` and `enable_truncation(max_length=...)` on
the tokenizer, and encodes batches with `encode_batch(texts)`; within a batch all
sequences are padded to a common length (the longest in the batch)
(tests/test_embeddinggemma.py:L58-L81). The model is fed `input_ids` (and an
attention mask) with batch dimension equal to the sub-batch document count
(tests/test_embeddinggemma.py:L48-L53). These are observed integration points; the
test fakes them and does not assert exact max-length or pad-token values.

### Concurrency: load-once under concurrent cold calls

When two threads make their first (cold) embedding calls concurrently on the same
shared instance, the model session is built exactly once — the inference-session
constructor count is 1 — and both threads receive valid results (each a single
vector) (tests/test_embeddinggemma.py:L271-L302). The recorded rationale is that
instances are shared across threads via the cache, and without an internal load
lock two cold callers would transiently hold two full model sessions
(tests/test_embeddinggemma.py:L272-L276). The download is artificially slowed to
widen the race window (tests/test_embeddinggemma.py:L281-L285), so the single-load
guarantee must hold even under a slow load.

## Factory: `get_embedding_function`

`get_embedding_function(device=..., model=...)` returns a cached embedding-function
instance selected by model name.

### Dispatch by model name

`model="embeddinggemma"` builds an `EmbeddinggemmaONNX` instance (not the default
MiniLM embedding function), and that instance's `name()` is `"embeddinggemma_300m"`
(tests/test_embeddinggemma.py:L332-L339). `model="minilm"` builds the MiniLM
embedding function class instead (tests/test_embeddinggemma.py:L349-L358). Model
selection ultimately resolves through an internal `_resolve_providers(device)` step
that maps a device to an execution-provider list and a normalized device string
(tests/test_embeddinggemma.py:L312-L314,L334-L336); the implementation also has an
internal `_build_ef_class()` that supplies the MiniLM class
(tests/test_embeddinggemma.py:L353).

### Caching keyed by (model, providers)

Repeated calls with the same model return the identical cached instance: two
`model="minilm"` calls return the same object (cache hit)
(tests/test_embeddinggemma.py:L358-L362). The cache key includes the model name (it
is `(model, providers)`, not `providers` alone), so switching models does not return
the wrong cached instance — an `embeddinggemma` request after a `minilm` request
returns a distinct `EmbeddinggemmaONNX` and never collides with the `minilm` cache
entry (tests/test_embeddinggemma.py:L342-L366).

### Concurrency: single instance under concurrent cache misses

When two threads call the factory concurrently with the same arguments and both miss
the cache, they must converge on one shared instance: both threads receive the exact
same object (tests/test_embeddinggemma.py:L305-L329). The recorded rationale is that
an unsynchronized check-then-construct in the factory would let each thread keep its
own instance, each later loading its own copy of the model
(tests/test_embeddinggemma.py:L306-L311).

## Error handling: missing / broken dependencies

If a required runtime dependency is unavailable at embedding time (e.g. the
tokenizer capability is missing or its import is blocked) while construction still
succeeded, the failure surfaces only when embedding is attempted. Calling the
instance raises an import-style error whose message instructs the user to reinstall
via something matching `pip install ... mempalace`, rather than leaking a bare
low-level import error (tests/test_embeddinggemma.py:L369-L380). The recorded intent
is that multilingual deps ship in core, but a broken install must yield a recovery
hint (tests/test_embeddinggemma.py:L370-L372).

## Configuration: embedding-model selection (`MempalaceConfig`)

The configuration object `MempalaceConfig` exposes an `embedding_model` property
selecting which embedding model is used.

- The environment variable `MEMPALACE_EMBEDDING_MODEL` overrides the config-file
  default (tests/test_embeddinggemma.py:L383-L388).
- The value is normalized case-insensitively to a lowercase canonical token: a value
  of `"MiniLM"` resolves to `"minilm"` (tests/test_embeddinggemma.py:L390-L391), and
  `"embeddinggemma"` resolves to `"embeddinggemma"`
  (tests/test_embeddinggemma.py:L387-L388).
- When the environment variable is unset and there is no explicit config, the
  default is `"minilm"` (back-compatibility for existing installs)
  (tests/test_embeddinggemma.py:L394-L399).

## Named constants (observable contract)

- `_EMBEDDINGGEMMA_PREFIX` = `"task: sentence similarity | query: "`
  (tests/test_embeddinggemma.py:L162,L197)
- `_EMBEDDINGGEMMA_BATCH_SIZE` = `32` (tests/test_embeddinggemma.py:L203,L217)
- Output embedding dimension after truncation = `384`
  (tests/test_embeddinggemma.py:L140,L199)
- Model name string = `"embeddinggemma_300m"`
  (tests/test_embeddinggemma.py:L123,L339)
