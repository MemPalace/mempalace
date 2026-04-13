# syntax=docker/dockerfile:1.7
FROM python:3.12-slim-bookworm AS builder

WORKDIR /build
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY mempalace ./mempalace
RUN pip install --no-cache-dir --prefix=/install .

FROM ubuntu:24.04

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3.12 \
        python3-pip \
        ca-certificates \
        curl \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/bin/python3.12 /usr/local/bin/python \
    && ln -sf /usr/bin/python3.12 /usr/local/bin/python3

# Ubuntu's python3.12 reads from /usr/local/lib/python3.12/dist-packages
# but the builder's pip writes to /install/lib/python3.12/site-packages.
# Copy binaries into /usr/local (entry_points, headers, etc) and merge the
# packages into the path Ubuntu's Python actually searches.
COPY --from=builder /install/bin /usr/local/bin
COPY --from=builder /install/lib/python3.12/site-packages /usr/local/lib/python3.12/dist-packages

COPY mempalace /app/mempalace

WORKDIR /app

# Default rootless UID per home-operations/containers standard.
# k8s runtime may override via securityContext.runAsUser / fsGroup.
USER 65534:65534

ENV PYTHONUNBUFFERED=1
# Palace lives on a PVC mounted at /data. HOME also points here so the
# existing WAL path (~/.mempalace/wal) resolves inside the mount.
ENV HOME=/data
ENV MEMPALACE_PALACE_PATH=/data/palace

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8080/healthz || exit 1

ENTRYPOINT ["python", "-m", "mempalace.mcp_server"]
CMD ["--serve-http", "--host", "0.0.0.0", "--port", "8080", "--auth", "none"]
