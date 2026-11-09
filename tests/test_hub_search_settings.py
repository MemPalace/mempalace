"""One table decides which settings keep a CLI search off the hub and which
settings the hub's search fingerprint hashes (``hub_client.SEARCH_SETTINGS``)."""

import argparse
import json
import re
from pathlib import Path

import pytest

import mempalace
from mempalace import cli
from mempalace.config import MempalaceConfig
from mempalace.hub_client import (
    SEARCH_OVERRIDE_ENV_VARS,
    SEARCH_SETTINGS,
    search_setting_in_scope,
)


def _search_args():
    return argparse.Namespace(
        query="needle",
        palace=None,
        backend=None,
        global_backend=None,
        wing=None,
        room=None,
        results=5,
        since=None,
        before=None,
    )


def _fingerprint(tmp_path, payload):
    config_dir = tmp_path / "config"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "config.json").write_text(json.dumps(payload), encoding="utf-8")
    return MempalaceConfig(
        config_dir=str(config_dir), palace_path=str(tmp_path / "palace")
    ).search_config_fingerprint


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in SEARCH_OVERRIDE_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


# Env vars these modules read that provably don't change what a search returns.
_NOT_SEARCH_SETTINGS = {
    # Where writes and hook work go.
    "MEMPALACE_CLI_WRITE_ROUTING",
    "MEMPALACE_HOOK_WRITE_ROUTING",
    "MEMPALACE_WRITE_ROUTING",
    "MEMPALACE_HOOKS_AUTO_SAVE",
    "MEMPALACE_HOOKS_DAEMON",
    # Pick the palace or the config file; the hub is looked up per palace and
    # the fingerprint hashes the values that file resolves to.
    "MEMPALACE_CONFIG_DIR",
    "MEMPALACE_PALACE",
    "MEMPALACE_PALACE_PATH",
    "MEMPAL_PALACE_PATH",
    # Mining, graph building and backups only.
    "MEMPALACE_CONVO_SPELLCHECK",
    "MEMPALACE_ENTITY_LANGUAGES",
    "MEMPAL_ENTITY_LANGUAGES",
    "MEMPALACE_KG_HALLWAY_MIN_COUNT",
    "MEMPALACE_TOPIC_TUNNEL_MIN_COUNT",
    "MEMPALACE_MAX_BACKUPS",
    # Embedding batch size changes throughput, not the vectors.
    "MEMPALACE_EMBEDDINGGEMMA_BATCH_SIZE",
    "MEMPALACE_EMBEDDINGGEMMA2_BATCH_SIZE",
}

_SETTING_SOURCES = (
    "config.py",
    "paths.py",
    "embedding.py",
    "backends/chroma.py",
    "backends/milvus.py",
    "backends/pgvector.py",
    "backends/qdrant.py",
    "backends/sqlite_exact.py",
)


def test_every_env_setting_is_in_the_table_or_known_not_to_affect_search():
    root = Path(mempalace.__file__).parent
    read = set()
    for name in _SETTING_SOURCES:
        text = (root / name).read_text(encoding="utf-8")
        read.update(re.findall(r"[\"'](MEMPAL[A-Z0-9_]*)[\"']", text))
    missing = read - set(SEARCH_OVERRIDE_ENV_VARS) - _NOT_SEARCH_SETTINGS
    assert not missing, (
        f"{sorted(missing)}: add a row to hub_client.SEARCH_SETTINGS, or to "
        "_NOT_SEARCH_SETTINGS here if it cannot change search results"
    )
    assert _NOT_SEARCH_SETTINGS <= read, "stale _NOT_SEARCH_SETTINGS entries"


@pytest.mark.parametrize("attr", [row[0] for row in SEARCH_SETTINGS])
def test_every_row_names_a_config_setting(attr):
    assert isinstance(getattr(MempalaceConfig, attr, None), property)


@pytest.mark.parametrize("env_name", SEARCH_OVERRIDE_ENV_VARS)
def test_every_search_override_env_var_keeps_the_search_local(monkeypatch, env_name):
    assert cli._search_args_forwardable(_search_args()) is True
    monkeypatch.setenv(env_name, "1")
    assert cli._search_args_forwardable(_search_args()) is False


@pytest.mark.parametrize(
    "env_name",
    [
        "MEMPALACE_HYBRID_VECTOR_WEIGHT",
        "MEMPALACE_HYBRID_BM25_WEIGHT",
        "MEMPALACE_PGVECTOR_SHARED_NAMESPACE",
        "MEMPALACE_EMBEDDINGGEMMA2_DIMENSION",
        "MEMPALACE_EMBEDDINGGEMMA2_MODALITIES",
        "MEMPALACE_EMBEDDINGGEMMA2_REVISION",
    ],
)
def test_overrides_the_hub_would_ignore_keep_the_search_local(monkeypatch, env_name):
    monkeypatch.setenv(env_name, "1")
    assert cli._search_args_forwardable(_search_args()) is False


@pytest.mark.parametrize(
    "key, value",
    [("hybrid_rank_vector_weight", 0.9), ("hybrid_rank_bm25_weight", 0.1)],
)
def test_hybrid_weight_edits_change_the_fingerprint(tmp_path, key, value):
    assert _fingerprint(tmp_path, {}) != _fingerprint(tmp_path, {key: value})


def test_pgvector_shared_namespace_changes_the_fingerprint(monkeypatch, tmp_path):
    monkeypatch.setattr("mempalace.palace.resolve_backend_name", lambda _path: "pgvector")
    base = {"backend": "pgvector", "pgvector_dsn": "postgresql://localhost/mp"}
    assert _fingerprint(tmp_path, base) != _fingerprint(
        tmp_path, dict(base, pgvector_shared_namespace="fleet")
    )


def test_an_invalid_embeddinggemma2_setting_keeps_the_fingerprint_total(tmp_path):
    payload = {"embedding_model": "embeddinggemma2", "embeddinggemma2_dimension": 7}
    first = _fingerprint(tmp_path, payload)
    assert first == _fingerprint(tmp_path, payload)
    assert first != _fingerprint(tmp_path, dict(payload, embeddinggemma2_dimension=512))


@pytest.mark.parametrize("attr, scope", [(row[0], row[2]) for row in SEARCH_SETTINGS])
def test_every_row_in_scope_feeds_the_fingerprint(monkeypatch, tmp_path, attr, scope):
    backend, model = "chroma", "minilm"
    if scope is not None:
        kind, value = scope
        if kind == "backend":
            backend = value
        elif not value.startswith("!"):
            model = value
    monkeypatch.setattr("mempalace.palace.resolve_backend_name", lambda _path: backend)
    monkeypatch.setattr(MempalaceConfig, "embedding_model", property(lambda self: model))
    assert search_setting_in_scope(scope, backend, model)

    monkeypatch.setattr(MempalaceConfig, attr, property(lambda self: "one"))
    first = MempalaceConfig(palace_path=str(tmp_path)).search_config_fingerprint
    monkeypatch.setattr(MempalaceConfig, attr, property(lambda self: "two"))
    assert MempalaceConfig(palace_path=str(tmp_path)).search_config_fingerprint != first


def test_search_settings_overridden_by_env_or_explicit_backend(monkeypatch):
    from mempalace.hub_client import SEARCH_OVERRIDE_ENV_VARS, search_settings_overridden

    for name in SEARCH_OVERRIDE_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    assert not search_settings_overridden()
    assert search_settings_overridden("sqlite_exact")
    monkeypatch.setenv("MEMPALACE_HYBRID_VECTOR_WEIGHT", "0.2")
    assert search_settings_overridden()
    monkeypatch.setenv("MEMPALACE_HYBRID_VECTOR_WEIGHT", "  ")
    assert not search_settings_overridden()


def test_search_fingerprint_matches_compares_with_this_process(monkeypatch, tmp_path):
    from mempalace.config import MempalaceConfig
    from mempalace.hub_client import search_fingerprint_matches

    monkeypatch.setattr(
        MempalaceConfig, "search_config_fingerprint", property(lambda self: "abc"), raising=False
    )
    assert search_fingerprint_matches({"search_config_fingerprint": "abc"}, str(tmp_path))
    assert not search_fingerprint_matches({"search_config_fingerprint": "xyz"}, str(tmp_path))
    assert not search_fingerprint_matches({}, str(tmp_path))
