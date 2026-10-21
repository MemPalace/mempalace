# MemPalace small-model evaluation harness

Evaluates ≤4B-parameter Ollama models (plus optional cloud reference models) on MemPalace's classification and extraction tasks. Outputs accuracy, latency (TTFT, TPS, e2e p50/p95), and VRAM per `(model, task, mode)` triple. Replaces vibe-based model selection with data.

If you want to validate the findings on your own hardware, this README walks you end-to-end. The whole local matrix takes ~60 min on an RTX 3090.

---

## Reproducing the published results

### Prerequisites

- **GPU**: NVIDIA card, ~10 GB VRAM minimum for the Tier 1 set, ~14 GB for the FP16 variant. The published numbers are from an RTX 3090 (24 GB).
- **Ollama**: install per [ollama.com/download](https://ollama.com/download). Tested against Ollama 0.23.2. Newer should work; older may break the `think` parameter or the `/api/ps` endpoint shape (the harness has a fallback for the latter).
- **Python**: 3.10+ (project uses `uv` for env management).
- **Disk**: ~30 GB free for the full local candidate set, ~24 GB for Tier 1 only.
- **Ollama Cloud account** (optional): only needed if you want to re-run the cloud-tier ceiling measurements. Sign in via `ollama signin` before running.

### 1. Set up the environment

```bash
git clone https://github.com/MemPalace/mempalace.git
cd mempalace
uv sync
```

### 2. Pull candidate models

The full candidate list is in `benchmarks/model_eval/candidates.yaml`. Pull what you want; tier filters in the orchestrator only run what's installed locally.

**Bulk-pull the Tier 1 set (the must-evaluate set, ~24 GB total)**:

```bash
# This script reads candidates.yaml and pulls every tier-1 model + the embedding model
uv run python -c "
import yaml
import subprocess
with open('benchmarks/model_eval/candidates.yaml') as f:
    cands = yaml.safe_load(f)['candidates']
for c in cands:
    if c.get('tier') == 1:
        print(f'pulling {c[\"tag\"]}')
        subprocess.run(['ollama', 'pull', c['tag']], check=True)
subprocess.run(['ollama', 'pull', 'nomic-embed-text'], check=True)
"
```

To also pull Tier 2 (sub-3B sizes), the `modern` tier (Gemma 4, Granite 4.1, Ministral 3, Qwen 3.5), or Tier 3 (FP16 ceiling), substitute the filter: `c.get('tier') in (1, 2)`, `c.get('tier') == 'modern'`, etc.

**For the cloud comparison** (optional, requires `ollama signin`):

```bash
for tag in gpt-oss:20b-cloud gpt-oss:120b-cloud qwen3-coder:480b-cloud \
           deepseek-v3.1:671b-cloud deepseek-v4-flash:cloud deepseek-v4-pro:cloud \
           kimi-k2.6:cloud; do
  ollama pull "$tag"
done
```

### 3. Smoke test (under 30 seconds)

Confirm the harness works against one model and one task before committing to a full run:

```bash
uv run python -m benchmarks.model_eval.runner \
  --model qwen3:4b-instruct-2507-q4_K_M \
  --task calibration \
  --mode default \
  --dataset-dir benchmarks/model_eval/datasets
```

You should see ~20 inferences and JSON output with `accuracy: 0.95` (or close). If accuracy is much lower, check that `ollama list` shows the model loaded and that `nomic-embed-text` is pulled (needed for any open-set or memory task even though calibration doesn't use it).

### 4. Run the matrix

**Tier 1 only (~30-40 min on RTX 3090)**:

```bash
uv run python -m benchmarks.model_eval.orchestrator \
  --candidates tier1 \
  --tasks all \
  --dataset-dir benchmarks/model_eval/datasets \
  --output benchmarks/model_eval/results/$(date -u +%Y-%m-%d)-$(hostname).csv
```

**Everything local (~60-80 min)**:

```bash
uv run python -m benchmarks.model_eval.orchestrator \
  --candidates local \
  --tasks all \
  --dataset-dir benchmarks/model_eval/datasets \
  --output benchmarks/model_eval/results/$(date -u +%Y-%m-%d)-$(hostname).csv
```

**Cloud only (~25-50 min, n=30 to control cost)**:

```bash
uv run python -m benchmarks.model_eval.orchestrator \
  --candidates cloud \
  --tasks all \
  --n 30 \
  --dataset-dir benchmarks/model_eval/datasets \
  --output benchmarks/model_eval/results/$(date -u +%Y-%m-%d)-cloud-$(hostname).csv
```

The orchestrator prints `[i/N]` progress, `acc=… e2e_p50=…ms vram=…` after each run, and writes the CSV incrementally — safe to Ctrl-C if you only want partial data.

### 5. Render a report

```bash
uv run python -m benchmarks.model_eval.summarize \
  --csv benchmarks/model_eval/results/$(date -u +%Y-%m-%d)-$(hostname).csv \
  --output benchmarks/model_eval/reports/$(date -u +%Y-%m-%d)-$(hostname).md
```

Output: a markdown report with per-task rankings, production picks, open-set viability, and an instruct-vs-reasoning comparison.

### 6. Compare with the published baseline

The committed baseline CSVs are in `benchmarks/model_eval/results/`:

- `2026-05-10-z690-ex-glacial.csv` — full local matrix (RTX 3090)
- `2026-05-11-cloud-z690-ex-glacial.csv` — cloud tier
- `2026-05-11-modern-z690-ex-glacial.csv` — modern tier (Gemma 4, Granite 4.1, Ministral 3, Qwen 3.5)
- `2026-05-10-spotcheck-qwen3.csv` — reproducibility spot-check

For **accuracy**, your numbers should land within ~1% of the baseline. Bigger drift suggests something different in your setup (different Ollama version, different model digest, different system prompt rendering).

For **speed**, your numbers will differ — they depend on your GPU, driver, thermal state, and concurrent load. Use **relative rank within your machine** as the comparable signal, not absolute milliseconds.

For **VRAM resident**, expect close agreement on the same Ollama version. Peak VRAM is noisier (it depends on other GPU activity at measurement time).

### 7. Sharing your results

If your results disagree with the published baseline in a meaningful way:

1. Run the smoke test (step 3) on the specific model that disagrees, save the JSON output
2. Open an issue against this repo with: your `ollama --version`, GPU model, CSV file attached or pasted, and the smoke-test JSON
3. If the harness has a bug, we'll fix it. If the model behavior genuinely changed (Ollama Cloud model rotation, new quantization upstream), we'll re-run and document the drift in the report

To attach your CSV to a follow-up PR or comparison study, drop it in `benchmarks/model_eval/results/` with a name like `YYYY-MM-DD-yourhostname.csv` and reference it in the analysis report.

---

## What it measures

For each `(model, task, mode)`:

- **Accuracy** — task-specific scoring against the labeled dataset
- **TTFT** (time-to-first-token) — approximation from Ollama's `prompt_eval_duration + load_duration`, p50 and p95 over N=20 sample runs
- **TPS** (tokens/second) — from Ollama's `eval_count / eval_duration`, p50 and p95
- **e2e latency** — full single-classification time, p50 and p95
- **VRAM resident** — model memory after warmup (read from `/api/ps`)
- **VRAM peak** — peak GPU memory during inference (polled via `nvidia-smi` every 500ms)

The first run of each model is discarded (cache + GPU clock ramp).

## Tasks

- `room_classification` — closed-set (model picks from a provided room list) and open-set (model invents a slug). 101 samples.
- `entity_extraction` — JSON list of entities per sample. 50 samples, 247 ground-truth entities.
- `memory_extraction` — structured memory items per sample. 40 samples, 55 ground-truth memories across 5 types.
- `calibration` — simple 5-class sentence-type. 20 samples. Sanity check the harness.

All datasets are synthetic (no real-person info). Generated once and frozen so benchmark numbers stay comparable across runs.

If you need to extend the dataset, **add** samples; don't replace existing ones, otherwise prior numbers stop being comparable.

## CLI references

```bash
# Single (model, task, mode)
uv run python -m benchmarks.model_eval.runner --help

# Matrix runs with tier filtering
uv run python -m benchmarks.model_eval.orchestrator --help

# CSV → markdown report
uv run python -m benchmarks.model_eval.summarize --help
```

Tier filter values: `tier1`, `tier2`, `tier3`, `tier<=N`, `local` (everything not tier=cloud), `cloud` (everything tier=cloud), `modern` (the Gemma 4 / Granite 4.1 / Ministral 3 / Qwen 3.5 additions), or any exact model tag for a single-model run.

## Reusing existing infrastructure

The harness uses `mempalace.llm_client.get_provider("ollama", model=tag)` and `provider.classify(...)` — the same code path as production. For thinking-capable models, the runner always passes `think=False` so hybrid models stay in fast-classification mode.

No new HTTP plumbing. No reimplementation of provider abstraction. The harness benchmarks the same code that ships.

## Hardware reporting

Every result file includes the test machine's metadata (CPU, GPU, VRAM total, Ollama version, OS, hostname). Speed numbers are **not portable across machines** — use them for relative ranking on a single setup. Accuracy numbers cross-port cleanly.

## Contributions welcome

### Adding models we missed

The published candidate list is what one engineer + one search pass surfaced. There are absolutely small instruct models we didn't catch. If you know of a competitive ≤4B-parameter model that should be in the comparison, please:

1. Add an entry to `candidates.yaml` following the existing schema (`tag`, `family`, `size_b`, `variant`, `quantization`, `expected_vram_mb`, `tier`, `notes`)
2. Run it through the smoke test then the matrix using the existing `--candidates <your-tag>` filter
3. Open a PR with the new candidate row + CSV results + a one-line analysis-report addendum

Particularly interested in: function-calling-tuned models (Phi-4 mini, Nemotron-mini), recent instruction-tuned variants from research labs (Hermes, Dolphin, OpenHermes), and quantization-aware-trained variants of established families. If the model has multiple competitive quantizations, pick the smaller one that's within accuracy noise (per the project finding "newer ≠ better" and the quantization sweet-spot rule from the analysis report).

### Adding other inference backends

The harness is wired through `mempalace.llm_client.get_provider()`, which already supports three provider types: `ollama` (currently used here), `openai-compat`, and `anthropic`. That means any **OpenAI-compatible** local server should plug in with minimal work:

- **LM Studio** — serves an OpenAI-compatible API on `http://localhost:1234/v1` by default
- **llama.cpp server** — `./server` exposes OpenAI-compat on `http://localhost:8080/v1`
- **vLLM** — `--port 8000` runs an OpenAI-compatible endpoint
- **unsloth studio** — likewise serves OpenAI-compat for inference
- **Docker Model Runner** — exposes models over OpenAI-compat on a per-model port
- **Hugging Face TGI / TEI** — OpenAI-compatible endpoints

The plumbing pieces a contributor would need to add:

1. **A backend-selection flag** in `runner.py` and `orchestrator.py` (e.g. `--backend ollama|openai-compat|anthropic|lm-studio|...`) that constructs the right provider via `get_provider(backend_name, model=tag, endpoint=...)`.
2. **Backend-specific timing extraction in `metrics.py`.** The current `extract_timing()` reads Ollama's `eval_count`, `prompt_eval_duration`, etc. Other backends report timing differently (OpenAI's `usage.completion_tokens`, llama.cpp's `tokens_per_second`, etc.). The harness will currently fill those columns with zeros for non-Ollama backends — degrades gracefully but loses the per-request timing breakdown.
3. **Backend-specific VRAM probe.** Ollama exposes `/api/ps`; LM Studio has its own status endpoint; llama.cpp doesn't expose model memory directly. For non-Ollama backends, `vram_resident_mb` would return `None` (already handled). Peak-VRAM via `nvidia-smi` still works regardless.
4. **A `candidates.yaml` field** to mark backend per candidate (e.g. `backend: lm-studio`).

If you implement a new backend, the existing dataset and scoring code applies unchanged — the harness's accuracy numbers stay comparable across backends. Open a PR with the runner/orchestrator changes plus one CSV from your backend so we can validate the integration on a known model.

If you build something niche that's worth comparing (Apple MLX, Intel OpenVINO, AMD ROCm-specific runtimes, edge-device runtimes like Termux + llama.cpp on phones), please share the methodology. Cross-runtime comparisons are exactly the kind of follow-up this harness is designed to enable.

## Notes for harness maintainers

When modifying the harness internals:

- **`format: json` mode is enforced locally but ignored on Ollama Cloud.** Per Ollama's docs. Cloud models that "happen to" emit JSON do so by default behavior, not because Ollama enforces it. The `kimi-k2.6:cloud` memory-extraction `valid_json_rate: 0.37` is a documented manifestation.
- **The memory-extraction `hallucination_rate` metric over-penalizes thorough models.** See the analysis report. Trust `mean_coverage` until a follow-up refines the scoring.
- **Cloud reproducibility is worse than local.** ~6 points of drift on `gpt-oss:20b` between runs. Cloud numbers should be reported as ranges, not point estimates.

Read `reports/2026-05-10-analysis.md` for the full set of findings, surprises, and methodology notes from the original run.
