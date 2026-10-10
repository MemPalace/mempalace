# Experimental EmbeddingGemma 2 and local media

This optional backend uses [Google's `embeddinggemma-2`](https://huggingface.co/google/embeddinggemma-2) through Sentence Transformers. It supports asymmetric text retrieval, native code queries and local image/audio/video assets in one embedding space. Existing MiniLM, EmbeddingGemma ONNX and OpenAI-compatible backends remain available; the default model and drawer-only search are unchanged.

## Install and isolate

The new extras require Python 3.10 or newer. Use a separate environment, configuration directory and palace for the experiment:

```bash
uv venv --python 3.12 .venv/embeddinggemma2
uv pip install --python .venv/embeddinggemma2/bin/python -e '.[dev,multimodal]'
source .venv/embeddinggemma2/bin/activate

export MEMPALACE_CONFIG_DIR="$HOME/.config/mempalace-eg2"
export MEMPALACE_PALACE_PATH="$HOME/.local/share/mempalace/palace-eg2"
export MEMPALACE_EMBEDDING_MODEL=embeddinggemma2
export MEMPALACE_EMBEDDINGGEMMA2_MODALITIES=all
```

Use `.[embeddinggemma2]` for text/image inference, or `.[multimodal]` for audio/video decoding too. TorchCodec needs compatible FFmpeg libraries. Install FFmpeg using your platform's package manager before using audio/video file inputs.

Model loading is lazy. First inference downloads the pinned model to the Hugging Face cache; subsequent inference is local. After the model is cached, set `HF_HUB_OFFLINE=1` to disable further model downloads. This does not configure other MemPalace services or providers.

## Configuration

Environment values override the corresponding `config.json` values:

| Config key | Environment variable | Values/default |
| --- | --- | --- |
| `embedding_model` | `MEMPALACE_EMBEDDING_MODEL` | `embeddinggemma2` to opt in |
| `embeddinggemma2_dimension` | `MEMPALACE_EMBEDDINGGEMMA2_DIMENSION` | `768` (default), `512`, `256`, `128` |
| `embeddinggemma2_modalities` | `MEMPALACE_EMBEDDINGGEMMA2_MODALITIES` | `text` (default), `text+vision`, `text+audio`, `all` |
| `embedding_device` | `MEMPALACE_EMBEDDING_DEVICE` | `auto` (default), `mps`, `cpu` |
| `embeddinggemma2_revision` | `MEMPALACE_EMBEDDINGGEMMA2_REVISION` | Immutable 40-character model commit SHA |

The default revision is [`914f7f89142e33e77833254d9c9b90c3cef7303b`](https://huggingface.co/google/embeddinggemma-2/tree/914f7f89142e33e77833254d9c9b90c3cef7303b). Model, dimension, modalities, revision and prompt version identify the vector space. Changing these values on a populated palace requires rebuilding, not relabelling old vectors.

`auto` uses Apple MPS when available and CPU otherwise. Weights and inference use float32. MPS runs a repeatability witness on first use; initialization, MPS/Metal inference and invalid-vector failures warn and retry on CPU. Unrelated decoder errors fail the item without moving a healthy model to CPU. Explicit `mps` requires MPS support.

Documents use `Document`, with a real title or source filename when present. Queries use `SearchQuery`, or `CodeRetrieval` for explicit code search. All output vectors are dimension-checked, finite and L2-normalized.

## Mine and search

With the environment above:

```bash
# Replace the example paths with your own directories.
mempalace mine /path/to/text-or-code --wing ExampleProject
mempalace mine /path/to/media --source media --wing ExampleProject --dry-run
mempalace mine /path/to/media --source media --wing ExampleProject

mempalace search "a screenshot showing an authentication error" --include-media
mempalace search "function that checks bearer token headers" --query-task code
```

A one-command palace override goes before the command: `mempalace --palace /path/to/isolated-palace search "query" --include-media`.

The media adapter supports PNG/JPG/JPEG/WebP images, WAV/MP3/M4A/FLAC audio, and MP4/MOV/M4V/WebM video. It skips hidden/cache/build directories and symlinks. `--wing` supplies the project label. Add `--related-drawer-id EXISTING_DRAWER_ID` to explicitly associate assets with an already indexed drawer; invalid links are rejected.

Assets live in the fixed `mempalace_assets` collection, separate from verbatim drawers. Metadata includes stable path-derived ID, source path, type, MIME type, filename title, project/wing, modification time, size, optional drawer link and embedding identity. Supported audio headers also supply duration. Media bytes remain in their original local files, and embeddings come from the native media encoder without generated captions or descriptor-text substitutions.

`--include-media` merges text/code and assets by cosine distance in the verified shared vector space. It requires compatible cosine collections; default drawer-only search retains its existing hybrid ranking. MCP `mempalace_search` accepts optional `include_media: true` and `query_task: "code"`, returning typed asset references with path, type, project, drawer link and live `available` status. Existing clients keep the default result behavior when these options are omitted. Experimental CLI routes run locally rather than forwarding unsupported options to an older hub.

Corrupt items are reported individually while healthy files continue. All discovered assets failing exits nonzero; missing dependencies or incompatible provider settings are fatal setup errors. Repeated ingestion upserts the same path ID and retains its original filing time.

## Rebuild safely

Keep a backup and stop writers on the source before rebuilding. Prefer a separate destination:

```bash
# With the new backend settings above, replace source/destination paths.
mempalace --palace /path/to/new-palace repair --mode from-sqlite \
  --source /path/to/source-palace --dry-run
mempalace --palace /path/to/new-palace repair --mode from-sqlite \
  --source /path/to/source-palace
```

This Chroma-specific recovery preserves drawer/closet IDs, verbatim content and metadata, reconstructs text vectors with metadata-aware document prompts, and re-embeds assets from their local files. It records verified new collection identities. Native assets require the corresponding modality to be enabled. Compare IDs, documents, metadata, counts and representative queries before switching clients to the destination.

`mempalace --palace /path/to/selected-palace repair rebuild-index --yes` is the in-place full-palace recovery alias. It archives the original palace before rebuilding, so retain the archive. Missing or invalid asset references are refused before archival. A missing source file remains a searchable reference with `available: false` until restored; it cannot be re-embedded from its descriptor. Do not use `palace set-embedder` to bypass the identity gate.

## Validation and limits

Normal tests mock model inference and require no downloaded weights:

```bash
python -m pytest tests/ -q --ignore=tests/benchmarks \
  --cov=mempalace --cov-report=term --cov-fail-under=80
ruff check .
ruff format --check .
```

Real-model integration tests are explicitly opt in:

```bash
MEMPALACE_RUN_EG2_INTEGRATION=1 MEMPALACE_RUN_EG2_MULTIMODAL=1 \
  python -m pytest tests/test_embeddinggemma2_integration.py -q
```

The generated speech/video codec test needs macOS `say` and FFmpeg; other platforms skip that fixture. These integration tests may download weights unless `HF_HUB_OFFLINE=1` is set after caching them.

A local Mac acceptance trial used ten existing text/code files and ten existing media assets, with twenty locked known-answer questions. Both 768-dimensional and rebuilt 512-dimensional palaces retrieved all twenty expected files within five results. Repeated ingestion, restarted CLI/MCP, corrupt files, missing references and full record/identity comparisons passed. This hand-picked corpus is an acceptance test, not a general retrieval benchmark or cross-platform validation.

Assets are file-level references. Renames/moves produce a new path ID; old references need manual cleanup. There is no automatic relinking, garbage collection or segment retrieval. Video follows the upstream processor's 1-fps sampling, maximum 32 frames and uniform subsampling on overflow. Audio normalizes to mono 16 kHz. Arbitrary codecs, long recordings, sustained concurrency and live remote database deployments need separate validation. Native asset source/document edits require re-ingestion or explicit vectors; descriptive title-only edits retain native vectors.
