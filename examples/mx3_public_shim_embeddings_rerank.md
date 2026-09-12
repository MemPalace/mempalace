# MX3 Public Shim Embeddings and Rerank

MemPalace can optionally route Chroma embeddings and post-retrieval rerank through the public
MX3 shim helpers.

This path is local by default. It sends the live search query and candidate hits to the configured
shim endpoint, so remote endpoints are blocked unless you explicitly opt in, and any remote
endpoint must use HTTPS.

Public repo:

- `https://github.com/grtninja/mx3-public-shim`

## Hardware required

This setup requires a MemryX MX3 accelerator in an M.2 slot behind the configured public-shim
endpoint.

## Install

```bash
pip install "mx3-public-shim[mempalace] @ git+https://github.com/grtninja/mx3-public-shim.git"
```

## Validate before enabling

Use this path only with a hardware-backed public-shim deployment.

```bash
python -m mx3_public_shim.doctor
```

MemPalace rejects the explicit `cpu_reference` fallback, but you should still validate the
configured endpoint before enabling it:

- the runtime must expose an embeddings provider
- the selected provider must not be `cpu_reference`
- the endpoint should be the local public shim unless you intentionally opt in to a remote HTTPS
  target

On Windows, a healthy doctor report normally shows:

- `mx3_linux` unavailable because the direct official Python runtime is Linux-only
- `openai_compat` available and selected for `embeddings`
- `openai_compat` available and selected for `chat`

That means MemPalace is using the local public-shim OpenAI-compatible surface while the required
hardware lives behind that endpoint.

## Enable embeddings + rerank in MemPalace

```bash
export MEMPALACE_EMBEDDING_BACKEND=mx3_public_shim
export MEMPALACE_SEARCH_RERANKER=mx3_public_shim
export MEMPALACE_MX3_PUBLIC_SHIM_BASE_URL=http://127.0.0.1:9000/v1
export MEMPALACE_MX3_PUBLIC_SHIM_EMBEDDING_MODEL=text-embedding-nomic-embed-text-v1.5
export MEMPALACE_MX3_PUBLIC_SHIM_PROVIDER_ORDER=mx3_linux,openai_compat
export MEMPALACE_MX3_PUBLIC_SHIM_REQUIRE_HARDWARE=1
```

On Windows, the direct `mx3_linux` provider is not expected to resolve; the normal path is a local
public-shim OpenAI-compatible endpoint that is itself backed by the required hardware.

If your endpoint requires authentication, also set:

```bash
export MEMPALACE_MX3_PUBLIC_SHIM_API_KEY=...
```

`MEMPALACE_EMBEDDING_BACKEND` and `MEMPALACE_SEARCH_RERANKER` can be enabled independently. Use
both when you want MX3-backed Chroma embeddings plus post-retrieval rerank.

If you intentionally point `MEMPALACE_MX3_PUBLIC_SHIM_BASE_URL` at a non-localhost endpoint, also
set:

```bash
export MEMPALACE_MX3_PUBLIC_SHIM_ALLOW_REMOTE=1
```

## Optional example script

The companion example below demonstrates the narrow Chroma seam directly:

```bash
python examples/mx3_public_shim_embeddings_rerank.py
```

Then search normally:

```bash
mempalace search "auth decisions"
```
