# Local room-decision pilot

This pilot compares the existing embedding room decider, Ollama `d3:4b`,
`d1:3b`, and a structured-output `qwen3.5:4b` baseline. It produces predictions
and measurements. It never applies a room change.

## Data and interpretation

The checked-in fixture contains 120 agent-authored cases, equally split between
English and Portuguese. They cover the twelve MemPalace room definitions, plus
out-of-taxonomy content, insufficient evidence, negation, competing topics, and
instructions embedded in the input. These are controlled examples, not an
independently reviewed estimate of production accuracy.

The private evaluation snapshot also contains 480 real drawers. Sampling is
balanced across available approved rooms and excludes both exemplar IDs and
all other drawers from the exemplar source files. Existing room labels are
reference labels: agreement with them does not establish correctness.

Original drawer text is copied verbatim into a private snapshot outside the
palace. It is never included in the checked-in fixture or result report. A
before/after audit compares the sampled drawers' text, metadata, and stored
vectors. Concurrent changes by another writer are reported as drift.

## Run locally

The runtime needs installed local models and supports the typed decision API.
The pilot was developed with Ollama 0.40.12 on Apple Silicon. Downloading models
contacts the model registry; inference uses the local runtime.

```sh
ollama pull d3:4b
ollama pull d1:3b
ollama pull qwen3.5:4b
```

Choose a private result directory **outside** the live palace. Replace the
paths below with your actual palace and private benchmark directory.

```sh
uv run python -m mempalace.room_benchmark prepare \
  --palace "$HOME/.mempalace/palace" --wing mempalace \
  --cases benchmarks/fixtures/room_decision_cases.json \
  --real-limit 480 --seed 20261028 \
  --output "$HOME/.mempalace/benchmarks/room-pilot/dataset.json"

uv run mempalace rooms benchmark \
  --dataset "$HOME/.mempalace/benchmarks/room-pilot/dataset.json" \
  --output "$HOME/.mempalace/benchmarks/room-pilot/results" \
  --algorithm embeddings
```

Repeat the benchmark command for `--algorithm embedding-descriptions`, `d3`,
`d1`, and `qwen`, serially. The additional embedding baseline uses the room
definitions provided to the decision models instead of exemplar centroids,
helping distinguish representation problems from model capabilities.
The equivalent standalone entry point is `python -m mempalace.room_benchmark
run`. `--limit 5` provides a smoke run; rerunning without it resumes the same
dataset. Results are saved after every case. A changed dataset, installed
model digest, or rubric version refuses a resume.

```sh
uv run python -m mempalace.room_benchmark report \
  --dataset "$HOME/.mempalace/benchmarks/room-pilot/dataset.json" \
  --output "$HOME/.mempalace/benchmarks/room-pilot/results"
```

Snapshot preparation currently supports the SQLite exact backend and recorded
local MiniLM or EmbeddingGemma identities. It opens SQLite using `mode=ro` and
`PRAGMA query_only=ON`. Model runs read the snapshot alone. The production
`rooms apply` command continues to use its existing embedding decider.

## Classifier contract

`mempalace.room_decision.DecisionRoomDecider` implements `RoomDecider` and
requests full drawer text through `requires_documents = True`. It evaluates
the approved room descriptions and keywords, plus an explicit `__none__`
option. It does not invent room names. A reject returns an empty room so a
dry-run plan retains the existing filing. Text deciders require an explicit
acceptance threshold when used with `plan_rooms`; the default cosine threshold
is reserved for the existing embedding path.

The client permits HTTP loopback endpoints only, disables proxies and
redirects, rejects cloud tags and remote aliases, checks the model capability,
and pins the installed digest and reported parameters across predictions.
No external fallback is implemented. Oversized inputs fail rather than being
shortened by this code. Qwen explicitly disables input truncation and context
shifting, uses a closed JSON schema, and runs with thinking disabled.
Its fixed rubric and room definitions precede the changing drawer in the
system prompt, allowing prefix-cache reuse during repeated classification.

The adapters share the room rubric and evidence; original room assignments and
benchmark judgments are not sent as features. Decision models use their loaded
context settings (4K for the tested d3 tag and 16K for the tested d1 tag). Qwen
uses a 4K context. Context failures count as errors. Model identity, runtime
version, and dataset hash are recorded with predictions.

## Measurements

- Controlled accuracy, in-taxonomy accuracy, rejection accuracy, and language
  breakdowns use the fixture's explicit expected answers. Errors count as wrong.
- Real-drawer reference agreement measures how often labels match existing
  filing, including model rejects and inference errors in the denominator.
  Existing labels may have originated from the embedding classifier itself;
  high agreement can therefore reflect consistency with that classifier.
- Latencies include local model inspection and full HTTP inference. The first
  request is recorded separately as a cold/warmup measurement; the remaining
  measurements are serial requests with the model retained. There is no claim
  of isolated-hardware timing. A resumed invocation can include a model reload
  in its first timed case. The completed pilot model runs were uninterrupted.
- These are installed-package comparisons: the tested default d3 package uses
  MXFP8, d1 uses Q8_0, and Qwen uses Q4_K_M. Weight formats, execution backends,
  context limits, and prefix caching all affect timings.
- Embedding classification uses the existing room prototypes and stored drawer
  vectors. Its decision latency excludes embedding generation. Controlled-case
  embedding preparation time is recorded in the snapshot.
- Decision scores are the chosen label's probability, not Ollama's separate
  `confidence` field. Embedding scores are cosine similarities. Qwen supplies
  an unscored label. These scales are not interchangeable.
- Precision/coverage curves, ten-bin ECE, and multiclass Brier scores describe
  the controlled cases only. They do not calibrate a deployment policy.
- The initial reject/threshold policy remains experimental. No automatic moves,
  deletions, ontology changes, or production hook changes are enabled.

## Validation

```sh
uv run pytest tests/test_room_decision.py tests/test_room_benchmark.py tests/test_rooms.py -q
uv run ruff check .
uv run ruff format --check .
```

The boundary tests cover read-only SQLite access, exact snapshot content,
source-file holdout, cloud and redirect boundaries, malformed probability
distributions, model identity drift, source drift, independent confidence
scales, resume behavior, and CLI routing.

## Results: October 28, 2026

Hardware: Apple M5, 16 GiB unified memory. Ollama 0.40.12; Python 3.12.13.
The source palace records MiniLM, 384 dimensions. Each method processed the same
600 cases: 120 authored controlled examples and 480 real drawers. The controlled
set contains 60 English and 60 Portuguese cases, including 12 intended rejects.
These figures describe this pilot; independent production judgments are still needed.

| Method | Correct / 120 | English | Portuguese | Median decision | p95 |
| --- | ---: | ---: | ---: | ---: | ---: |
| MiniLM / exemplar centroids | 43/120 (35.8%) | 48.3% | 23.3% | 0.254 ms | 0.276 ms |
| MiniLM / room descriptions | 80/120 (66.7%) | 81.7% | 51.7% | 0.619 ms | 4.449 ms |
| d3:4b | 116/120 (96.7%) | 98.3% | 95.0% | 2.993 s | 8.199 s |
| d1:3b | 56/120 (46.7%) | 51.7% | 41.7% | 0.948 s | 1.317 s |
| qwen3.5:4b / cached rubric | 111/120 (92.5%) | 95.0% | 90.0% | 1.453 s | 2.137 s |

Embedding decision times use precomputed drawer vectors and exclude embedding
generation. The model times include inspection and HTTP inference. These runs
share a desktop environment; their timing distributions include normal system
activity and differing package formats. Controlled-case median inference was
1.610 s for d3, 0.724 s for d1, and 1.145 s for Qwen.

### Real-drawer observations

| Method | Agreement with existing room | Rejected / 480 | Inference errors |
| --- | ---: | ---: | ---: |
| MiniLM / exemplar centroids | 96.2% | 0 (0.0%) | 0 |
| MiniLM / room descriptions | 40.4% | 0 (0.0%) | 0 |
| d3:4b | 21.0% | 295 (61.5%) | 1 |
| d1:3b | 4.8% | 445 (92.7%) | 1 |
| qwen3.5:4b / cached rubric | 26.0% | 182 (37.9%) | 1 |

Agreement measures consistency with existing filing. It does not determine
which classifier is right. Existing labels can share their origin with the
embedding baseline. All three model errors concern the same 85,472-character
drawer: the decision adapters refused its request size; Qwen returned an input
error with truncation disabled. Its text was retained intact.

### Interpretation and next use

- d3 had the highest controlled match rate and provided useful probability
  distributions. At a diagnostic probability threshold of 0.7, it accepted
  87/120 cases (72.5% coverage), all matching the intended labels. This threshold
  has not been calibrated on human-reviewed real drawers.
- Qwen offered a faster alternative and matched 111/120 controlled labels.
  It assigned the isolated acknowledgments “Yes.” and “Sim.” to replication,
  while d3 rejected both. Qwen supplies no measured label probability here.
- d1 matched 56/120 controlled labels. It correctly classified 44 known-room
  examples, rejected 64 intended known-room examples, and rejected all 12
  intended unknowns. Its low coverage makes this package/rubric combination a
  poor primary classifier. Supplementary small routing checks are kept separately.
- The description-based embedding baseline improved controlled matching from
  35.8% to 66.7%. Room representation deserves review independently of model choice.
- Keep model predictions in shadow mode. Review real disagreements and rejects,
  including how much surrounding source context is needed. Preserve the fast
  embedding path and use background model work for uncertain cases. No live
  reorganization is enabled by this pilot.

### Integrity, artifacts, and checks

Final audit: **480 sampled drawers, zero changed or missing rows**. The audit
compares document text, complete metadata, and stored vectors. Preparation and
audit use enforced read-only SQLite; inference reads the private snapshot.
The three model tags were unloaded after their passes.

Private snapshot and full per-case predictions are in
`~/.mempalace/benchmarks/decision-room-pilot-20261028/`.
`results/report.json` contains metrics, model digests, reported parameters,
runtime version, and dataset hashes. The initial uncached Qwen partial run
and supplementary d1 checks are under `diagnostics/` and excluded from scores.

Dataset SHA-256:
`2c89218cd903a6a39d371d1da77d09cd52fdf611899caa50a9a69c85707e5995`.

Validation: **165 targeted tests passed**, new-module coverage **88.6%**, full
repository Ruff lint and formatting passed, and `git diff --check` passed.
