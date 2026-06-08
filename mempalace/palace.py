"""
palace.py — Shared palace operations.

Consolidates collection access patterns used by both miners and the MCP server.
"""

import os

from .backends.chroma import ChromaBackend

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

_DEFAULT_BACKEND = ChromaBackend()


def get_collection(
    palace_path: str,
    collection_name: str = "mempalace_drawers",
    create: bool = True,
):
    """Get the palace collection through the backend layer."""
    return _DEFAULT_BACKEND.get_collection(
        palace_path,
        collection_name=collection_name,
        create=create,
    )


def get_remote_collection(url: str, token: str) -> "RemoteCollection":
    """Get a collection that writes to a remote HTTP mempalace server."""
    from .backends.remote import RemoteCollection
    return RemoteCollection(url=url, token=token)


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
            return abs(float(stored_mtime) - current_mtime) < 0.001
        return True
    except Exception:
        return False
