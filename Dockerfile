# syntax=docker/dockerfile:1.7

# MemPalace — CPU image.
#
# Multi-stage build using uv (the project ships a uv.lock, so we install from
# the frozen lockfile for reproducible images). The default runtime is the MCP
# server over stdio; the CLI is reachable through the same entrypoint.
#
# Build:
#   docker build -t mempalace .
#   docker build -t mempalace --build-arg EXTRAS="extract,spellcheck" .
#
# Run (MCP server over stdio, palace persisted on the host):
#   docker run -i --rm -v mempalace-data:/data mempalace
#
# Run (CLI):
#   docker run --rm -v mempalace-data:/data mempalace search "why GraphQL"
#
# GPU acceleration lives in Dockerfile.gpu (it needs a CUDA base image).

ARG PYTHON_VERSION=3.12

# --- builder ----------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS builder

# uv: fast, lockfile-driven installer. Pinned by digest-less tag for clarity;
# bump deliberately.
COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /uvx /bin/

# Some transitive deps (grpcio, onnxruntime, tokenizers) ship manylinux wheels
# for cp312, but keep a compiler around so a missing wheel degrades to a source
# build instead of failing the image. Dropped from the final stage.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Optional extras baked into the image. CPU-safe by default; GPU is a separate
# Dockerfile. Pass a comma-separated list, e.g. EXTRAS="extract,spellcheck".
ARG EXTRAS="extract,spellcheck"

# Layer 1: dependencies only (no project) — cached across source changes.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=README.md,target=README.md \
    set -e; \
    flags=""; \
    for e in $(echo "${EXTRAS}" | tr ',' ' '); do flags="${flags} --extra ${e}"; done; \
    uv sync --frozen --no-install-project --no-dev ${flags}

# Layer 2: the project itself. --no-editable installs mempalace into the venv's
# site-packages (instead of an .pth pointing at /app), so the runtime stage can
# copy only /app/.venv and drop the source tree.
COPY . /app
RUN --mount=type=cache,target=/root/.cache/uv \
    set -e; \
    flags=""; \
    for e in $(echo "${EXTRAS}" | tr ',' ' '); do flags="${flags} --extra ${e}"; done; \
    uv sync --frozen --no-dev --no-editable ${flags}

# --- runtime ----------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS runtime

LABEL org.opencontainers.image.title="MemPalace" \
      org.opencontainers.image.description="Local-first AI memory — verbatim storage, MCP server + CLI." \
      org.opencontainers.image.source="https://github.com/MemPalace/mempalace" \
      org.opencontainers.image.licenses="MIT"

# /data is the single persistence root: HOME points here, so the palace
# (~/.mempalace/palace), config (~/.mempalace), and the embedding-model cache
# all land under one mountable volume. The default `minilm` model caches under
# ~/.cache/chroma (~80 MB, from ChromaDB's S3); the optional `embeddinggemma`
# model caches under ~/.cache/huggingface (~300 MB). Both lazy-download on
# first use.
ENV HOME=/data \
    PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Non-root user owning the data volume.
RUN groupadd --gid 1000 mempalace \
    && useradd --uid 1000 --gid 1000 --home-dir /data --create-home mempalace

WORKDIR /app

# The resolved virtualenv from the builder — no build toolchain in this layer.
COPY --from=builder --chown=mempalace:mempalace /app/.venv /app/.venv
COPY --chown=mempalace:mempalace docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

USER mempalace
VOLUME ["/data"]

# Default to the MCP server; `docker run` it with `-i` for stdio JSON-RPC.
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["mcp"]
