"""ChromaDB vector store backend for MemPalace."""

import os
import types

import chromadb

_client_cache = None
_collection_cache = None
_config = None


def _get_collection(create=False):
    global _client_cache, _collection_cache
    if _config is None:
        return None
    try:
        if _client_cache is None:
            _client_cache = chromadb.PersistentClient(path=_config.palace_path)
        if create:
            _collection_cache = _client_cache.get_or_create_collection(_config.collection_name)
        elif _collection_cache is None:
            _collection_cache = _client_cache.get_collection(_config.collection_name)
        return _collection_cache
    except Exception:
        return None


def get_chroma_collection(palace_path: str, collection_name: str, create: bool = False):
    global _config, _client_cache, _collection_cache
    if (
        _config is None
        or _config.palace_path != palace_path
        or _config.collection_name != collection_name
    ):
        _config = types.SimpleNamespace(palace_path=palace_path, collection_name=collection_name)
        _client_cache = None
        _collection_cache = None
    os.makedirs(palace_path, exist_ok=True)
    return _get_collection(create=create)


def reset_chroma_collection(palace_path: str, collection_name: str):
    global _client_cache, _collection_cache

    client = chromadb.PersistentClient(path=palace_path)
    _client_cache = None
    _collection_cache = None
    try:
        client.delete_collection(collection_name)
    except Exception:
        pass
    return client.create_collection(collection_name)
