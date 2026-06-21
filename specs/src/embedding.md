# Behavior Spec: `mempalace/embedding.py`

Factory and implementations for ChromaDB-compatible embedding functions bound to a
user-selected ONNX Runtime execution provider. Two embedding models are supported,
selected via the `MEMPALACE_EMBEDDING_MODEL` env var or `embedding_model` in
`~/.mempalace/config.json`: `minilm` (default, `all-MiniLM-L6-v2`, 384-dim) and
`embeddinggemma` (`onnx-community/embeddinggemma-300m-ONNX` q8, 384-dim via Matryoshka
truncation, multilingual) (mempalace/embedding.py:L1-L30). Switching models on an existing
palace requires a `mempalace repair rebuild-index` because the vector spaces differ
(mempalace/embedding.py:L15-L17).

## Device resolution (`_resolve_providers`)

Input is a device string; output is a tuple `(provider_list, effective_device_name)`
(mempalace/embedding.py:L68-L74). The input is normalized by trimming whitespace and
lowercasing, with `None`/empty treated as `"auto"` (mempalace/embedding.py:L74).

Supported device names and their provider lists: `cpu` → `[CPUExecutionProvider]`;
`cuda` → `[CUDAExecutionProvider, CPUExecutionProvider]`; `coreml` →
`[CoreMLExecutionProvider, CPUExecutionProvider]`; `dml` →
`[DmlExecutionProvider, CPUExecutionProvider]` (mempalace/embedding.py:L41-L46).

If the ONNX runtime cannot be loaded at all, the result is unconditionally
`([CPUExecutionProvider], "cpu")` (mempalace/embedding.py:L76-L81).

For `auto`, providers are probed in priority order CUDA ▸ CoreML ▸ DirectML; the first
available accelerator yields `([<accel>, CPUExecutionProvider], <name>)`, and if none are
available the result is `([CPUExecutionProvider], "cpu")` (mempalace/embedding.py:L54-L58,L83-L87).

An unknown device name emits a one-shot `WARNING` log ("Unknown embedding_device ...
falling back to cpu") and returns `([CPUExecutionProvider], "cpu")`
(mempalace/embedding.py:L89-L94). A known accelerator device whose provider is not
available in the installed runtime emits a one-shot `WARNING` naming the missing provider
and the `pip` extra to install (`mempalace[gpu]` for cuda, `mempalace[coreml]` for coreml,
`mempalace[dml]` for dml), then falls back to `([CPUExecutionProvider], "cpu")`
(mempalace/embedding.py:L48-L52,L100-L111). Each device name is warned at most once per
process (deduplicated via a module-level set) (mempalace/embedding.py:L65,L91-L93,L101-L110).
When the requested accelerator IS available, the full provider list and the requested
device name are returned unchanged (mempalace/embedding.py:L96-L113).

Requesting an unavailable accelerator never hard-fails; it always degrades to CPU
(mempalace/embedding.py:L28-L29).

## Thread cap configuration

`_resolve_intra_op_threads` reads `MempalaceConfig().embedding_threads`; on any error it
logs at DEBUG and returns `0` meaning "uncapped" (mempalace/embedding.py:L134-L142).
A value `<= 0` (or falsy) means the ONNX runtime keeps its default intra-op thread pool
(approximately physical core count); a positive value caps the pool at that count
(mempalace/embedding.py:L116-L131). The cap is applied at session construction, not via
environment variables (mempalace/embedding.py:L117-L123).

## MiniLM embedding function (`_build_ef_class`)

Produces a subclass of ChromaDB's `ONNXMiniLM_L6_V2` whose `name()` returns the literal
string `"default"` (not the upstream `onnx_mini_lm_l6_v2`) so the same class can read both
palaces created by ChromaDB's default embedding function and palaces this code creates
(mempalace/embedding.py:L145-L166). The vectors and model are identical to upstream; only
the persisted EF-identity tag differs (mempalace/embedding.py:L150-L153). When a positive
intra-op thread cap is configured, the model session is rebuilt with that cap applied,
CoreML provider pruned, log severity 3, and full graph optimization, loading
`model.onnx` from the download path; if that rebuild fails it logs a WARNING and falls back
to the upstream uncapped session (mempalace/embedding.py:L168-L198). With no cap it uses the
upstream session unchanged (mempalace/embedding.py:L177-L179).

## EmbeddinggemmaONNX class

A ChromaDB-compatible embedding function whose `name()` returns the stable literal
`"embeddinggemma_300m"`; this is persisted on the collection and a mismatch forces a
rebuild on model switch (mempalace/embedding.py:L223-L242).

Constructor inputs: `preferred_providers` (defaults to `[CPUExecutionProvider]` when empty),
`batch_size` (default 32), and `intra_op_num_threads` (default 0). A `batch_size < 1` raises
a `ValueError` (mempalace/embedding.py:L244-L264). The default batch size of 32 bounds memory
so a large repair-scale batch (e.g. 5000 docs) does not OOM-kill the process
(mempalace/embedding.py:L211-L220).

Model loading is lazy and thread-safe (double-checked under a lock): the ~300 MB ONNX model
(`onnx/model_quantized.onnx` plus its `_data` sidecar) and `tokenizer.json` are downloaded
from HuggingFace repo `onnx-community/embeddinggemma-300m-ONNX` on first embed call and
cached by huggingface_hub (mempalace/embedding.py:L203-L207,L266-L319). A logger `INFO`
message announces the download (mempalace/embedding.py:L285-L289). Missing
`huggingface_hub`/`tokenizers`/`numpy` dependencies raise an `ImportError` with a reinstall
instruction (mempalace/embedding.py:L272-L283). The pooled output is selected by output name
`sentence_embedding`, falling back to output index 1 if absent
(mempalace/embedding.py:L303-L308). The tokenizer enables padding and truncation to max
length 2048 (mempalace/embedding.py:L310-L312). The session attribute is assigned last so the
unlocked fast-path sees a fully-initialized instance (mempalace/embedding.py:L316-L319).

### Embedding contract (`__call__`)

Input is a single string, a list of strings, or `None`. A bare string is wrapped into a
one-element list (mempalace/embedding.py:L321-L325). `None` or an empty input returns an
empty list `[]` without triggering the model download (mempalace/embedding.py:L326-L330).
Output is a list of float vectors, one per input document, each of dimension 384
(mempalace/embedding.py:L209,L321-L352).

Processing: documents are handled in sub-batches of `batch_size`; each document is prefixed
with `"task: sentence similarity | query: "` before tokenization
(mempalace/embedding.py:L208,L338-L341). The model output is truncated to the first 384 of
768 dimensions (Matryoshka), then each vector is L2-normalized (norm + 1e-12 to avoid
division by zero) so cosine similarity equals dot product
(mempalace/embedding.py:L209,L347-L351). Sub-batch padding does not change any row's vector
because the output is attention-masked (mempalace/embedding.py:L217-L219).
`embed_query(input)` and `embed_documents(input)` both delegate to `__call__`
(mempalace/embedding.py:L354-L360).

## Factory and caching (`get_embedding_function`)

Inputs: optional `device` and `model`. `device=None` resolves to
`MempalaceConfig().embedding_device`; `model=None` resolves to
`MempalaceConfig().embedding_model` (mempalace/embedding.py:L363-L378). The cache key is
`(model, tuple(providers))` where providers come from `_resolve_providers`; a matching cached
embedding function is returned without rebuilding (lock-free fast path, then double-checked
under a lock) so model-load cost is paid at most once per process per key
(mempalace/embedding.py:L380-L388). When `model == "embeddinggemma"` an `EmbeddinggemmaONNX`
is built; any other value (including unrecognized names) falls back to the MiniLM EF class —
back-compatible default (mempalace/embedding.py:L390-L398). On a cache miss an `INFO` log
records the model, effective device, and providers (mempalace/embedding.py:L399-L405).

## `describe_device`

Input optional `device` (defaults to `MempalaceConfig().embedding_device`); returns the
short resolved effective-device label string (e.g. `cpu`, `cuda`) used by the miner CLI
header (mempalace/embedding.py:L408-L419).

## Model name and dimension probing

`current_model_name(model=None)` returns the canonical configured model name lowercased and
trimmed (e.g. `minilm`/`embeddinggemma`), reading `MempalaceConfig().embedding_model` when
`model` is `None`. This is the config name, NOT the spoofed internal `name()` of `"default"`
(mempalace/embedding.py:L427-L438).

`probe_dimension(device=None, model=None)` returns the output dimension by embedding the
single probe string `"probe"` and measuring the resulting vector length, cached per resolved
model name so the probe runs at most once per process (mempalace/embedding.py:L422-L461). On
any failure it logs at DEBUG and returns `0` ("dimension unknown"), which the identity check
treats as non-blocking (mempalace/embedding.py:L441-L460).

`get_embedder_identity(device=None, model=None)` returns an `EmbedderIdentity` with
`model_name` from `current_model_name` and `dimension` from `probe_dimension` (RFC 001)
(mempalace/embedding.py:L464-L475).

## Side effects and observable contracts

- Network/filesystem: embeddinggemma downloads model and tokenizer files from HuggingFace on
  first use, cached by huggingface_hub (mempalace/embedding.py:L290-L296).
- Config files: device, model, and thread settings are read from
  `MEMPALACE_EMBEDDING_DEVICE`/`MEMPALACE_EMBEDDING_MODEL` env vars or
  `~/.mempalace/config.json` (mempalace/embedding.py:L6-L20,L134-L139,L363-L378).
- Persisted EF identity names are externally observable contracts: MiniLM EF reports
  `"default"`, embeddinggemma reports `"embeddinggemma_300m"`; ChromaDB rejects reads with a
  mismatched name, forcing rebuild on model change (mempalace/embedding.py:L164-L166,L237-L242).
- All embeddings are 384-dimensional regardless of model, allowing drop-in collection reuse by
  vector width (mempalace/embedding.py:L9-L11,L209,L228-L230,L347).
- Embedding functions are process-global cached/shared across threads and concurrency-safe via
  locks (mempalace/embedding.py:L60-L64,L264,L385-L388).
