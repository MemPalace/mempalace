"""Configuration contract for the opt-in EmbeddingGemma 2 model."""

import json

import pytest

from mempalace.config import DEFAULT_EMBEDDINGGEMMA2_REVISION, MempalaceConfig


_ENV_KEYS = (
    "MEMPALACE_EMBEDDINGGEMMA2_DIMENSION",
    "MEMPALACE_EMBEDDINGGEMMA2_MODALITIES",
    "MEMPALACE_EMBEDDINGGEMMA2_REVISION",
)
_ALT_REVISION = "a" * 40


@pytest.fixture(autouse=True)
def _clear_embeddinggemma2_env(monkeypatch):
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def _config(tmp_path, payload=None):
    if payload is not None:
        (tmp_path / "config.json").write_text(json.dumps(payload), encoding="utf-8")
    return MempalaceConfig(config_dir=str(tmp_path))


def test_embeddinggemma2_defaults(tmp_path):
    cfg = _config(tmp_path)

    assert cfg.embeddinggemma2_dimension == 768
    assert cfg.embeddinggemma2_modalities == "text"
    assert cfg.embeddinggemma2_revision == DEFAULT_EMBEDDINGGEMMA2_REVISION


def test_embeddinggemma2_values_read_from_config(tmp_path):
    cfg = _config(
        tmp_path,
        {
            "embeddinggemma2_dimension": 256,
            "embeddinggemma2_modalities": "text+vision",
            "embeddinggemma2_revision": _ALT_REVISION,
        },
    )

    assert cfg.embeddinggemma2_dimension == 256
    assert cfg.embeddinggemma2_modalities == "text+vision"
    assert cfg.embeddinggemma2_revision == _ALT_REVISION


def test_embeddinggemma2_env_overrides_config(tmp_path, monkeypatch):
    cfg = _config(
        tmp_path,
        {
            "embeddinggemma2_dimension": 512,
            "embeddinggemma2_modalities": "text+audio",
            "embeddinggemma2_revision": _ALT_REVISION,
        },
    )
    monkeypatch.setenv("MEMPALACE_EMBEDDINGGEMMA2_DIMENSION", "128")
    monkeypatch.setenv("MEMPALACE_EMBEDDINGGEMMA2_MODALITIES", "ALL")
    monkeypatch.setenv("MEMPALACE_EMBEDDINGGEMMA2_REVISION", "B" * 40)

    assert cfg.embeddinggemma2_dimension == 128
    assert cfg.embeddinggemma2_modalities == "all"
    assert cfg.embeddinggemma2_revision == "b" * 40


@pytest.mark.parametrize("dimension", [768, 512, 256, 128])
def test_embeddinggemma2_supported_dimensions(tmp_path, dimension):
    assert (
        _config(tmp_path, {"embeddinggemma2_dimension": dimension}).embeddinggemma2_dimension
        == dimension
    )


@pytest.mark.parametrize("dimension", [0, 127, 384, 1024, "nope", None])
def test_embeddinggemma2_rejects_invalid_dimensions(tmp_path, dimension):
    cfg = _config(tmp_path, {"embeddinggemma2_dimension": dimension})
    with pytest.raises(ValueError, match="embeddinggemma2_dimension"):
        _ = cfg.embeddinggemma2_dimension


@pytest.mark.parametrize("modalities", ["text", "text+vision", "text+audio", "all"])
def test_embeddinggemma2_supported_modalities(tmp_path, modalities):
    cfg = _config(tmp_path, {"embeddinggemma2_modalities": modalities})
    assert cfg.embeddinggemma2_modalities == modalities


@pytest.mark.parametrize("modalities", ["vision", "audio", "text+video", "", None])
def test_embeddinggemma2_rejects_invalid_modalities(tmp_path, modalities):
    cfg = _config(tmp_path, {"embeddinggemma2_modalities": modalities})
    with pytest.raises(ValueError, match="embeddinggemma2_modalities"):
        _ = cfg.embeddinggemma2_modalities


@pytest.mark.parametrize("revision", ["main", "", "a" * 39, "g" * 40, None])
def test_embeddinggemma2_rejects_non_commit_revisions(tmp_path, revision):
    cfg = _config(tmp_path, {"embeddinggemma2_revision": revision})
    with pytest.raises(ValueError, match="40-character hexadecimal commit"):
        _ = cfg.embeddinggemma2_revision


def test_embeddinggemma2_config_changes_affect_fingerprint_only_when_selected(
    tmp_path,
):
    palace_path = str(tmp_path / "palace")
    config_dir = tmp_path / "config"
    base = {
        "embedding_model": "embeddinggemma2",
        "embeddinggemma2_dimension": 768,
        "embeddinggemma2_modalities": "text",
        "embeddinggemma2_revision": DEFAULT_EMBEDDINGGEMMA2_REVISION,
    }

    def fingerprint(payload):
        config_dir.mkdir(exist_ok=True)
        (config_dir / "config.json").write_text(json.dumps(payload), encoding="utf-8")
        return MempalaceConfig(
            config_dir=str(config_dir), palace_path=palace_path
        ).search_config_fingerprint

    original = fingerprint(base)
    for key, value in (
        ("embeddinggemma2_dimension", 512),
        ("embeddinggemma2_modalities", "text+vision"),
        ("embeddinggemma2_revision", _ALT_REVISION),
    ):
        changed = dict(base, **{key: value})
        assert fingerprint(changed) != original

    # Inactive model-specific settings must not invalidate a Hub search snapshot.
    assert fingerprint({**base, "embedding_model": "minilm"}) == fingerprint(
        {"embedding_model": "minilm", "embeddinggemma2_dimension": 512}
    )
