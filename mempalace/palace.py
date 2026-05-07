"""
palace.py — Shared palace operations.

Consolidates ChromaDB access patterns used by both miners and the MCP server.
"""

import os
import chromadb

SKIP_DIRS = {
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    "env",
    "dist",
    "build",
    ".next",
    "coverage",
    ".mempalace",
    ".ruff_cache",
    ".mypy_cache",
    ".pytest_cache",
    ".cache",
    ".tox",
    ".nox",
    ".idea",
    ".vscode",
    ".ipynb_checkpoints",
    ".eggs",
    "htmlcov",
    "target",
}


def _get_best_embedding_function():
    """Return a GPU embedding function if available, else None for CPU default."""
    # Path 1: ONNX Runtime GPU (preferred — zero code change, just install onnxruntime-gpu)
    try:
        import onnxruntime as ort
        providers = ort.get_available_providers()
        if "CUDAExecutionProvider" in providers or "ROCMExecutionProvider" in providers:
            # ChromaDB's default EF will use the GPU provider automatically
            return None
    except ImportError:
        pass

    # Path 2: Sentence Transformers with torch CUDA
    try:
        import torch
        if torch.cuda.is_available():
            from chromadb.utils.embedding_functions import (
                SentenceTransformerEmbeddingFunction,
            )

            print("  Using GPU-accelerated embeddings (CUDA via sentence-transformers)")
            return SentenceTransformerEmbeddingFunction(
                model_name="all-MiniLM-L6-v2",
                device="cuda",
            )
    except ImportError:
        pass

    # Path 3: Default CPU
    return None


def get_collection(palace_path: str, collection_name: str = "mempalace_drawers"):
    """Get or create the palace ChromaDB collection."""
    os.makedirs(palace_path, exist_ok=True)
    try:
        os.chmod(palace_path, 0o700)
    except (OSError, NotImplementedError):
        pass
    client = chromadb.PersistentClient(path=palace_path)
    try:
        return client.get_collection(collection_name)
    except Exception:
        ef = _get_best_embedding_function()
        if ef:
            return client.create_collection(collection_name, embedding_function=ef)
        return client.create_collection(collection_name)


def file_already_mined(collection, source_file: str, check_mtime: bool = False) -> bool:
    """Check if a file has already been filed in the palace.

    When check_mtime=True (used by project miner), returns False if the file
    has been modified since it was last mined, so it gets re-mined.
    When check_mtime=False (used by convo miner), just checks existence.
    """
    try:
        results = collection.get(where={"source_file": source_file}, limit=1)
        if not results.get("ids"):
            return False
        if check_mtime:
            stored_meta = results.get("metadatas", [{}])[0]
            stored_mtime = stored_meta.get("source_mtime")
            if stored_mtime is None:
                return False
            current_mtime = os.path.getmtime(source_file)
            return float(stored_mtime) == current_mtime
        return True
    except Exception:
        return False
