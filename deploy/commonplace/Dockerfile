FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    COMMONPLACE_DATA=/data \
    HOME=/home/commonplace \
    MEMPALACE_EMBEDDING_DEVICE=cpu \
    ANONYMIZED_TELEMETRY=False

RUN groupadd --gid 10001 commonplace \
    && useradd --uid 10001 --gid commonplace --create-home commonplace \
    && mkdir /data \
    && chown commonplace:commonplace /data

WORKDIR /opt/commonplace
COPY pyproject.toml README.md ./
COPY mempalace/ ./mempalace/
COPY commonplace/ ./commonplace/
ARG COMMONPLACE_BUILD_COMMIT=""
RUN python -c "import os, pathlib, re; sha = os.environ['COMMONPLACE_BUILD_COMMIT']; assert not sha or re.fullmatch(r'(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})', sha), 'Invalid build commit'; pathlib.Path('commonplace/web/_build_commit.txt').write_text(sha + chr(10), encoding='ascii')"
RUN pip install ".[commonplace]"

USER 10001:10001
# Warm MiniLM through the same factory the workers use. HOME's cache is
# outside /data; workers.py links each workspace's Chroma cache here.
# A failed download/inference fails the build rather than delaying first use.
RUN python -c "from mempalace.embedding import get_embedding_function; vectors = get_embedding_function(device='cpu', model='minilm')(input=['Commonplace build check']); assert len(vectors[0]) == 384"

WORKDIR /data
VOLUME ["/data"]
EXPOSE 8765
CMD ["commonplace", "serve", "--host", "0.0.0.0", "--port", "8765"]
