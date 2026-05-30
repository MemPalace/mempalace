#!/usr/bin/env python3
"""Example: query a local public-shim embeddings endpoint and pass vectors into Chroma."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from urllib import request

try:
    from chromadb import PersistentClient
except ImportError as exc:  # pragma: no cover
    raise SystemExit("Install chromadb before running this example.") from exc


BASE_URL = os.environ.get("MEMPALACE_MX3_PUBLIC_SHIM_BASE_URL", "http://127.0.0.1:9000/v1")
EMBEDDINGS_URL = BASE_URL.rstrip("/") + "/embeddings"
EMBED_MODEL = os.environ.get(
    "MEMPALACE_MX3_PUBLIC_SHIM_EMBEDDING_MODEL",
    "text-embedding-nomic-embed-text-v1.5",
)


def fetch_embeddings(texts: list[str]) -> list[list[float]]:
    payload = json.dumps({"model": EMBED_MODEL, "input": texts}).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    api_key = os.environ.get("MEMPALACE_MX3_PUBLIC_SHIM_API_KEY")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = request.Request(
        EMBEDDINGS_URL,
        data=payload,
        headers=headers,
        method="POST",
    )
    with request.urlopen(req, timeout=20) as response:
        body = json.loads(response.read().decode("utf-8"))
    data = body.get("data", [])
    if len(data) != len(texts):
        raise RuntimeError("Embedding response count did not match input count")
    return [row["embedding"] for row in data]


def main() -> None:
    documents = [
        "A local retrieval queue keeps notes that still need verification.",
        "The workstation stores a short MX3 setup checklist for repeatable bring-up.",
        "This note is about watering tomatoes in the backyard.",
    ]
    metadatas = [
        {"wing": "verification"},
        {"wing": "operations"},
        {"wing": "personal"},
    ]
    query = "How does the system track notes that still need verification?"

    with tempfile.TemporaryDirectory() as tmpdir:
        client = PersistentClient(path=str(Path(tmpdir)))
        collection = client.get_or_create_collection("mempalace_drawers")
        doc_vectors = fetch_embeddings(documents)
        collection.add(
            ids=["1", "2", "3"],
            documents=documents,
            embeddings=doc_vectors,
            metadatas=metadatas,
        )
        query_vector = fetch_embeddings([query])[0]
        result = collection.query(query_embeddings=[query_vector], n_results=3)
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
