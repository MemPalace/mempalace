FROM python:3.11-slim AS builder

WORKDIR /build
RUN apt-get update && apt-get install -y --no-install-recommends build-essential && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY mempalace ./mempalace
RUN pip install --no-cache-dir --prefix=/install .

FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

COPY --from=builder /install /usr/local
COPY mempalace /app/mempalace

WORKDIR /app
RUN useradd -u 1000 -m mempalace && \
    mkdir -p /home/mempalace/.mempalace && \
    chown -R mempalace:mempalace /home/mempalace

USER mempalace
ENV PYTHONUNBUFFERED=1
ENV MEMPALACE_PALACE_PATH=/home/mempalace/.mempalace/palace

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8080/healthz || exit 1

ENTRYPOINT ["python", "-m", "mempalace.mcp_server"]
CMD ["--serve-http", "--host", "0.0.0.0", "--port", "8080", "--auth", "none"]
