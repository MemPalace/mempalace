# GitHub Copilot CLI as an LLM provider

MemPalace can drive your already-installed and authenticated **GitHub Copilot
CLI** as the optional LLM used during `mempalace init`. This lets you reuse your
Copilot subscription for the two init phases that benefit from an LLM instead of
standing up a local Ollama model:

- **Pass 0 — corpus-origin detection.** Decide whether a folder is AI-dialogue
  (so agent persona names aren't misfiled as people).
- **Pass 1 — entity refinement.** Reclassify borderline capitalized tokens as
  `PERSON` / `PROJECT` / `TOPIC` / `COMMON_WORD`.

It is built on the official [`github-copilot-sdk`](https://github.com/github/copilot-sdk)
(JSON-RPC over stdio). Everything else in MemPalace — mining, indexing, search,
the knowledge graph — is unchanged and stays fully local.

> **This provider is external.** The Copilot CLI relays your prompts to GitHub's
> cloud models, even though it runs on your machine. MemPalace therefore treats
> Copilot as an **external service** and will not send any folder content through
> it without your explicit consent (see [Privacy & consent](#privacy--consent)).
> If you need a zero-egress setup, use `--llm-provider ollama` (the default) or
> `--no-llm`.

---

## Requirements

- **GitHub Copilot CLI**, installed and signed in. Run `copilot` once
  interactively to authenticate. See the
  [Copilot CLI docs](https://docs.github.com/en/copilot/github-copilot-in-the-cli).
- **Python 3.11+.** The SDK's floor. On 3.9/3.10, selecting `copilot` degrades to
  heuristics-only with a clear message — use `ollama`/`openai-compat`/`anthropic`
  there instead.
- **The `copilot` extra:**

  ```bash
  pip install "mempalace[copilot]"
  # or, with uv:
  uv pip install "mempalace[copilot]"
  ```

  Importing `mempalace` never requires this extra; the actionable install error
  only surfaces if you actually select `--llm-provider copilot` without it.

The SDK fetches a pinned Copilot runtime on first use. Control that with:

| Environment variable            | Effect                                                      |
| ------------------------------- | ---------------------------------------------------------- |
| `COPILOT_CLI_PATH`              | Reuse an already-installed Copilot CLI binary.             |
| `COPILOT_SKIP_CLI_DOWNLOAD=1`   | Never auto-download (you must supply the binary).          |
| `python -m copilot download-runtime` | Pre-fetch the runtime ahead of time.                  |

---

## Usage

```bash
# Use Copilot for LLM-assisted init. Prompts for external-egress consent first.
mempalace init ~/projects/myapp --llm-provider copilot

# Pin a specific model instead of letting Copilot choose.
mempalace init ~/projects/myapp --llm-provider copilot --llm-model gpt-5.5

# Non-interactive / CI: authorize the external send up front (no prompt).
mempalace init ~/projects/myapp --llm-provider copilot --accept-external-llm

# Opt out of the LLM entirely (fully local, no external calls).
mempalace init ~/projects/myapp --no-llm
```

### Model selection (`--llm-model`)

The default is **`auto`**, which lets Copilot route to an available model. `auto`
is the safe default because it is present on every Copilot account and never
trips a model-availability pre-flight — the exact set of pinned model ids
(`gpt-5.5`, `claude-sonnet-4.5`, …) varies by account and subscription.

Pin a model only if you know it's available to you:

```bash
mempalace init ~/projects/myapp --llm-provider copilot --llm-model claude-sonnet-4.5
```

If a pinned model isn't available to your account, MemPalace reports it and falls
back to heuristics-only rather than failing init. Reasoning effort is applied
only for models that advertise support for it and is omitted otherwise, so a
model that rejects the parameter (e.g. `auto`) still works.

---

## Privacy & consent

Copilot is always treated as external. Before any folder content is sent:

1. `init` prints an **`EXTERNAL API`** warning describing the egress.
2. It then requires explicit authorization to proceed. The LLM runs only if:
   - you answer **`y`** at the interactive prompt, **or**
   - you pass **`--accept-external-llm`** (the non-interactive / CI opt-in).

Anything else — answering `n`, a non-interactive run with no `--accept-external-llm`,
or no TTY — declines safely and `init` continues **heuristics-only**. Note that
`--yes` (entity auto-accept) does **not** authorize the external send; egress
consent is a separate, deliberate decision.

**What the model can see.** Each classification runs in an ephemeral, tool-denied
session (empty tool allowlist plus a deny-all permission handler) in a neutral
temporary working directory. The model receives only the specific text MemPalace
sends it for classification — it cannot read your project files or make edits.

MemPalace does not control how GitHub logs, retains, or uses data sent to Copilot;
review GitHub's terms if that matters for your corpus.

---

## Troubleshooting

| Symptom                                                        | Cause / fix                                                                                   |
| -------------------------------------------------------------- | --------------------------------------------------------------------------------------------- |
| `copilot provider requires: pip install "mempalace[copilot]"`  | Install the extra (and use Python 3.11+).                                                      |
| `Copilot provider needs Python 3.11+`                          | Your interpreter is < 3.11. Use `ollama`/`openai-compat`/`anthropic`, or run on 3.11+.        |
| `No LLM provider reachable` / auth errors                      | The Copilot CLI isn't signed in. Run `copilot` once to authenticate, then retry.              |
| A pinned `--llm-model` reports "not available"                 | That model isn't on your account. Drop `--llm-model` (uses `auto`) or pin an available id.    |
| `init` proceeds without the LLM after the prompt               | You declined consent (or ran non-interactively). Pass `--accept-external-llm` to authorize.   |

---

## Verifying against the real CLI

An opt-in, live end-to-end test exercises the provider against your actual
authenticated Copilot CLI (it is skipped by default). See
[CONTRIBUTING.md](../CONTRIBUTING.md#live-provider-tests-opt-in).
