# Behavior Spec — `tests/test_embedding.py`

This is a test module that pins down the observable contract of the `embedding`
module's device-provider resolution, session-options, embedding-function caching,
and thread-cap behavior. The behaviors below are the contract the tests enforce on
the system under test.

## Test isolation (fixture)

Before every test in this module, the embedding module's internal caches are reset:
the embedding-function cache is cleared to empty and the set tracking already-emitted
warnings is cleared to empty (`tests/test_embedding.py:L6-L9`). This fixture is applied
automatically to every test, guaranteeing each test starts with no cached embedding
functions and no prior warnings recorded (`tests/test_embedding.py:L6-L9`).

## Provider resolution — `_resolve_providers(device) -> (providers_list, effective_device)`

`_resolve_providers` takes a requested device string and returns a pair: an ordered
list of execution-provider names and an effective-device label string
(`tests/test_embedding.py:L12-L21`).

- Device `"auto"` when a CUDA provider is available resolves to the providers list
  `["CUDAExecutionProvider", "CPUExecutionProvider"]` (CUDA first, CPU as fallback)
  with effective device `"cuda"` (`tests/test_embedding.py:L12-L21`).
- Device `"auto"` when only the CPU provider is available resolves to
  `["CPUExecutionProvider"]` with effective device `"cpu"`
  (`tests/test_embedding.py:L24-L27`).
- Device `"cuda"` requested but unavailable falls back to `["CPUExecutionProvider"]`
  with effective device `"cpu"`, and emits a warning whose text references the install
  extra `mempalace[gpu]` (`tests/test_embedding.py:L30-L34`).
- Device `"coreml"` requested but unavailable falls back to `["CPUExecutionProvider"]`
  / `"cpu"`, and emits a warning referencing the extra `mempalace[coreml]`
  (`tests/test_embedding.py:L37-L41`).
- Device `"dml"` requested but unavailable falls back to `["CPUExecutionProvider"]`
  / `"cpu"`, and emits a warning referencing the extra `mempalace[dml]`
  (`tests/test_embedding.py:L44-L48`).
- An unknown/unrecognized device string (e.g. `"bogus"`) falls back to
  `["CPUExecutionProvider"]` / `"cpu"`, and emits a warning containing the text
  `"Unknown embedding_device"`. This warning is emitted at most once even across
  repeated calls with the same unknown device — a second identical call produces no
  additional warning (warning text count remains 1)
  (`tests/test_embedding.py:L51-L56`).
- If the underlying onnxruntime provider-discovery import fails (import error), the
  resolution falls back to `["CPUExecutionProvider"]` / `"cpu"` without raising
  (`tests/test_embedding.py:L59-L71`).

The available-providers source is the onnxruntime `get_available_providers` call,
which the tests substitute to control which providers appear
(`tests/test_embedding.py:L14-L16`, `tests/test_embedding.py:L25`).

## Session options — `_intra_op_session_options(threads) -> options | none`

`_intra_op_session_options` takes a thread-count integer and returns either a
session-options object or nothing (`tests/test_embedding.py:L91-L99`).

- A positive thread count (e.g. `3`) returns a non-null options object whose
  intra-op thread count equals the requested value (`3`)
  (`tests/test_embedding.py:L91-L94`).
- A thread count of `0` returns nothing (no options object)
  (`tests/test_embedding.py:L97-L98`).
- A negative thread count (e.g. `-1`) also returns nothing — i.e. only strictly
  positive counts produce options (`tests/test_embedding.py:L99`).

## Embedding-function retrieval — `get_embedding_function(device, model) -> ef`

`get_embedding_function` takes a device string and a model name and returns an
embedding-function instance (`tests/test_embedding.py:L84-L88`).

### Caching keyed by resolved provider tuple

The returned embedding function is cached by the *resolved* provider tuple, not by
the raw device argument. Two calls whose device arguments differ (`"cpu"` and
`"auto"`) but which resolve to the same providers/effective-device pair return the
exact same instance (identity-equal), and that instance carries the resolved
providers list `["CPUExecutionProvider"]` (`tests/test_embedding.py:L74-L88`).

### Thread cap forwarded to the embedding function constructor

The effective intra-op thread cap (obtained from `_resolve_intra_op_threads`) is
passed into the constructed embedding function:

- For the `"minilm"` model, the resolved thread cap (e.g. `2`) is passed as the
  intra-op thread count to the constructed embedding function
  (`tests/test_embedding.py:L102-L117`).
- For the `"embeddinggemma"` model, the function is constructed via the
  `EmbeddinggemmaONNX` type and the resolved thread cap (e.g. `4`) is passed as the
  intra-op thread count to it (`tests/test_embedding.py:L120-L135`).

The MiniLM embedding-function class is produced by `_build_ef_class`, and the
EmbeddingGemma path uses the `EmbeddinggemmaONNX` type
(`tests/test_embedding.py:L79`, `tests/test_embedding.py:L127`).

## MiniLM model session construction — `_build_ef_class()` model override

The class returned by `_build_ef_class` constructs its model lazily; accessing the
model attribute triggers building an inference session
(`tests/test_embedding.py:L154-L156`).

- When the embedding function is constructed with a positive thread cap (e.g. `2`),
  building the model constructs the inference session with non-null session options
  whose intra-op thread count equals the cap (`2`), and the providers passed to the
  session do not include `"CoreMLExecutionProvider"`
  (`tests/test_embedding.py:L154-L160`).
- When the embedding function is constructed with an uncapped thread setting (`0`),
  building the model defers to the parent/upstream builder rather than applying a
  custom session-options object; the resulting session is non-null and the session
  options carry the default intra-op thread count of `0` (unset)
  (`tests/test_embedding.py:L177-L184`).

The session-construction point is the onnxruntime `InferenceSession`, which the tests
substitute to capture the session options and provider list rather than load a real
model (`tests/test_embedding.py:L143-L152`, `tests/test_embedding.py:L167-L175`).

## Device description — `describe_device(device) -> str`

`describe_device` returns the effective-device label that `_resolve_providers`
resolves for the given device argument. For device `"auto"` resolving to a CUDA
provider list, `describe_device("auto")` returns `"cuda"`
(`tests/test_embedding.py:L187-L194`).

## Externally observable contracts summary

- `_resolve_providers` return shape is a 2-tuple `(ordered_provider_name_list,
  effective_device_string)` (`tests/test_embedding.py:L18-L21`).
- Provider ordering places the accelerated provider before `"CPUExecutionProvider"`
  when accelerated is available (`tests/test_embedding.py:L18-L21`).
- Fallback to CPU is non-fatal in all failure/unavailable cases
  (`tests/test_embedding.py:L33`, `tests/test_embedding.py:L40`,
  `tests/test_embedding.py:L47`, `tests/test_embedding.py:L54`,
  `tests/test_embedding.py:L71`).
- Warning de-duplication for unknown devices is per-process and persists across calls
  until the warned-set is reset (`tests/test_embedding.py:L51-L56`).
