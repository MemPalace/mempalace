# Choosing, changing and upgrading the embedding model

The embedding model decides how well MemPalace finds a memory when the words of the question differ from the words of the note: across languages, across paraphrases, between a question and a code file. It is also the most expensive thing to change later, because every drawer and closet has to be re-embedded. This guide covers the choice, the switch without downtime or lost writes, and the rollback.

It is written for a person or an AI agent doing the work. Each step says what to check before moving on.

## The options

`embedding_model` in `~/.mempalace/config.json` (or `MEMPALACE_EMBEDDING_MODEL`) takes one of four values:

| Value | What runs | Languages | Where it runs | Notes |
| --- | --- | --- | --- | --- |
| `minilm` (default) | `all-MiniLM-L6-v2`, 384-d, ONNX | Trained on English | CPU (or ONNX accelerators) | What every palace starts with. Weak for non-English and cross-language queries. |
| `embeddinggemma` | `embeddinggemma-300m` ONNX q8, 384-d | 100+ languages | CPU / ONNX accelerators | Small download (~300 MB), multilingual. Onboarding offers it as the default. |
| `embeddinggemma2` | Google `embeddinggemma-2` via Sentence Transformers (torch) | Multilingual | CUDA, Apple MPS, CPU | Only option with **media** (image/audio/video) search and asymmetric query/document prompts. Heavy on Apple Silicon (see measurements). Details: [embeddinggemma2.md](embeddinggemma2.md). |
| `openai-compat` | Any OpenAI-compatible `/v1/embeddings` server: Ollama, LM Studio, llama.cpp, vLLM | Depends on the model you serve | Wherever the server runs (your GPU) | Text only. No task prompts are sent. The dimension is whatever the server returns. Stays local when the server is on your machine or LAN. |

### How to choose

1. **English-only notes, small palace, no GPU:** stay on `minilm`.
2. **Any non-English content, or questions asked in one language about notes written in another:** leave `minilm`. `embeddinggemma` is the light choice. `embeddinggemma-2` served by Ollama is the stronger one when you have a GPU.
3. **You need to search images, audio or video:** `embeddinggemma2` (built-in), with `embeddinggemma2_modalities` enabled. `openai-compat` does not embed media, even if the served model supports it.
4. **Apple Silicon Mac with a large palace (tens of thousands of drawers or more):** serve `embeddinggemma-2` from Ollama through `openai-compat`. On the machine below this was about 11 times faster than the built-in torch path, for the same model.
5. **Linux or Windows with an NVIDIA GPU:** the built-in `embeddinggemma2` on CUDA is a reasonable first try. Measure both paths on your machine before rebuilding a large palace (see "Measure before you commit").

## Measurements (one machine, for orientation)

Apple M1, MemPalace 3.11.0, a mixed palace of about 153,000 rows (drawers + closets, mostly English and Portuguese chat transcripts, notes and code):

| Path | Throughput | Full rebuild |
| --- | --- | --- |
| `embeddinggemma2` built-in, torch on MPS, default batch 4 | 0.92 docs/s | ~46 h (estimated) |
| same, batch 16 | out of MPS memory, fell back to CPU: 0.23 docs/s | — |
| `openai-compat` → Ollama 0.40 `embeddinggemma-2` (MLX runner, `ollama ps` shows `100% GPU`) | 10.1 docs/s alone, 6.4 with another embedding job running | ~4.5 h (measured) |

With the palace on the Ollama path behind a `mempalace serve` hub, an MCP search took 0.5–1.1 s end to end.

Retrieval quality, observed rather than benchmarked: a Portuguese question ranked the Portuguese notes that answered it at the top (similarity 0.85–0.87). The same question asked in English still found the topic, but ranked same-language (English) notes about it higher. Cross-language retrieval works, and same-language matches still win. No A/B against `minilm` was run on this corpus.

## Mac: serving the model from Ollama on the GPU

```bash
brew install --cask ollama-app      # or download the app from ollama.com; it starts at login
ollama pull embeddinggemma-2        # needs Ollama >= 0.36
ollama show embeddinggemma-2        # capabilities: embedding (+ vision, audio)
```

Check that it answers and runs on the GPU:

```bash
curl -s http://127.0.0.1:11434/v1/embeddings \
  -d '{"model":"embeddinggemma-2","input":["olá mundo","hello world"]}' \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print(len(d['data']), 'vectors of', len(d['data'][0]['embedding']))"
ollama ps                           # PROCESSOR must say 100% GPU on Apple Silicon
```

The dimension the API returns is the one that counts; `ollama show` may report a different "embedding length". On Apple Silicon, Ollama uses the GPU (Metal/MLX) without any setting. On an Intel Mac it runs on the CPU, and you lose most of the speed advantage.

Ollama unloads an idle model after a few minutes (5 by default), so the first search after a pause pays the model load. To keep it resident, set `OLLAMA_KEEP_ALIVE` (for example `launchctl setenv OLLAMA_KEEP_ALIVE 24h`, then restart the app).

> ⚠ **Ollama becomes a runtime dependency.** Every search and every write embeds through it. If Ollama is down, palace calls fail with `EmbeddingAPIError`. Before suspecting the palace, check `curl -s http://127.0.0.1:11434/api/tags`.

## Measure before you commit

Rebuilding a large palace takes hours. Time a sample first, with the candidate settings in the environment so your live config is untouched:

```bash
export MEMPALACE_EMBEDDING_MODEL=openai-compat
export MEMPALACE_EMBEDDING_API_URL=http://127.0.0.1:11434/v1
export MEMPALACE_EMBEDDING_API_MODEL=embeddinggemma-2
# copy a few hundred real files into /tmp/sample, then:
time mempalace --palace /tmp/palace-trial mine /tmp/sample --wing trial
```

Divide the drawer count by the elapsed seconds. Multiply your palace's total row count (`mempalace status`) by the inverse to estimate the rebuild.

## Switching an existing palace (no downtime, no lost writes)

The palace records the model that built it and refuses to open with a different one. Changing the model therefore means re-embedding everything. **Never** use `palace set-embedder` to relabel old vectors. Build a new palace next to the live one, then swap.

### 1. Snapshot the live palace

```bash
mkdir -p ~/.mempalace/palace.snapshot
sqlite3 ~/.mempalace/palace/chroma.sqlite3 ".backup '$HOME/.mempalace/palace.snapshot/chroma.sqlite3'"
date -u +%FT%TZ > ~/.mempalace/palace.snapshot/SNAPSHOT_AT_UTC
```

`.backup` gives a consistent copy while the live palace keeps serving.

### 2. Build the new palace from the snapshot

```bash
MEMPALACE_EMBEDDING_MODEL=openai-compat \
MEMPALACE_EMBEDDING_API_URL=http://127.0.0.1:11434/v1 \
MEMPALACE_EMBEDDING_API_MODEL=embeddinggemma-2 \
nohup caffeinate -is mempalace --palace ~/.mempalace/palace-new \
  repair --mode from-sqlite --source ~/.mempalace/palace.snapshot --yes \
  > ~/rebuild.log 2>&1 &
```

- Use `caffeinate -is` on a Mac so it does not sleep. Elsewhere, use `nohup` or a terminal multiplexer.
- **The log stays empty until the process exits**: Python buffers redirected output. Follow progress from the database instead:
  `sqlite3 "file:$HOME/.mempalace/palace-new/chroma.sqlite3?mode=ro" "select count(*) from embeddings"`
- When it finishes, the log ends with `Rebuild complete. N total rows` and `quick_check is clean`. Check that N matches the snapshot.

### 3. Stop every writer

```bash
# if you run a hub: stop its service (launchd/systemd), then
pkill -f mempalace-mcp; pkill -f mempalace.mcp_server
```

Open agent sessions keep their MCP servers alive, and those hold the old files open.

### 4. Copy what was written during the build

Rows written to the live palace after the snapshot exist only there. Compare both palaces and upsert the difference into the new one. The script below uses MemPalace's internal recovery helpers (as of 3.11). Run it with the **new** model settings in the environment, and run it without `--apply` first to see the counts:

```python
# delta_copy.py — usage: python3 delta_copy.py [--apply]
import os, sys
from mempalace.repair import extract_via_sqlite, _recoverable_collections
from mempalace.palace import mine_palace_lock
from mempalace.backends.chroma import ChromaBackend

SRC = os.path.expanduser("~/.mempalace/palace")       # live, old model
DST = os.path.expanduser("~/.mempalace/palace-new")   # rebuilt, new model
apply = "--apply" in sys.argv
backend = ChromaBackend()
for name in _recoverable_collections(SRC):
    src = {i: (d, m) for i, d, m in extract_via_sqlite(SRC, name)}
    dst = {i: (d, m) for i, d, m in extract_via_sqlite(DST, name)}
    new = [i for i in src if i not in dst]
    changed = [i for i in src if i in dst and src[i] != dst[i]]
    gone = [i for i in dst if i not in src]
    print(f"[{name}] src={len(src)} dst={len(dst)} new={len(new)} changed={len(changed)} gone={len(gone)}")
    if not apply:
        continue
    ids = new + changed
    with mine_palace_lock(DST):
        col = backend.get_collection(DST, name)
        for k in range(0, len(ids), 64):
            batch = ids[k:k + 64]
            col.upsert(ids=batch, documents=[src[i][0] for i in batch],
                       metadatas=[src[i][1] or None for i in batch])
        if gone:
            col.delete(ids=gone)
```

"Changed" rows are normal: re-mined files get new `filed_at` / `source_mtime` metadata. The live values are the newer ones. Run it again after `--apply`: every line must report `new=0 changed=0 gone=0`.

### 5. Carry over the files a rebuild does not create

A rebuild into a fresh folder creates the Chroma store and the embedder record only. If the old palace folder has `logstream.sqlite3` (the event log) and `replica.json` (the replica identity), copy them over. Copy them only when the new palace **replaces** the old one; two live palaces must never share a replica id.

```bash
sqlite3 ~/.mempalace/palace/logstream.sqlite3 ".backup '$HOME/.mempalace/palace-new/logstream.sqlite3'"
cp -p ~/.mempalace/palace/replica.json ~/.mempalace/palace-new/
```

The knowledge graph (`~/.mempalace/knowledge_graph.sqlite3`) lives outside the palace folder and is not affected.

### 6. Swap and configure

```bash
cd ~/.mempalace
cp config.json config.json.before-embedder-switch
mv palace palace.old-embedder && mv palace-new palace
```

Then set the model in `config.json`:

```json
"embedding_model": "openai-compat",
"embedding_api_url": "http://127.0.0.1:11434/v1",
"embedding_api_model": "embeddinggemma-2"
```

### 7. Restart and verify

- Start the hub again if you run one, and check its health endpoint.
- `mempalace status`: the header shows the new device (`openai-compat (http://127.0.0.1:11434/v1)`) and the old row counts.
- `palace/mempalace_embedder.json` names the new model and dimension.
- Run a few real searches, including one in each language you write in, and **one write** (`mempalace_add_drawer` or a diary entry) to prove the write path embeds too.
- Reload or restart every agent session so its MCP client reconnects.

### Rollback

Stop writers (step 3), then:

```bash
cd ~/.mempalace
mv palace palace.new-embedder && mv palace.old-embedder palace
cp config.json.before-embedder-switch config.json
```

Restart as in step 7. Anything written after the swap exists only in `palace.new-embedder`; copy it back with the delta script, swapping `SRC` and `DST`. Keep the old palace and the snapshot until the new one has run cleanly for a while.

## Upgrading MemPalace itself

- Read the release notes for anything that touches the vector space (model, dimension, prompts, revision). Changing any of those means a rebuild as above, not an in-place upgrade.
- Upgrade the package in the same Python environment that the MCP server and hooks use. A separate tool venv leaves the hooks importing the old version.
- After an upgrade, restart the hub and every MCP server: running processes keep the old code.
- If a release adds a new embedder you want, measure it first ("Measure before you commit"). Switching is the procedure above.

## Agent checklist

For an AI agent asked to "upgrade the embedding model" or "make memory work better in my language":

1. Ask which languages the notes are in, whether media search is needed, and what hardware the palace runs on.
2. Recommend from "How to choose". On Apple Silicon with a large palace, recommend Ollama + `openai-compat`.
3. Measure throughput on a sample and give the user the rebuild estimate **before** starting.
4. Build into a new folder. Never rebuild in place while writers are active.
5. Copy the delta, carry over `logstream.sqlite3` / `replica.json`, swap, set the config, restart, verify search **and** one write.
6. Tell the user the rollback, and say plainly when a new runtime dependency (Ollama, a remote server) now has to be running.
