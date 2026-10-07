"""An unknown ``embedding_model`` falls back to MiniLM everywhere (#2694).

Mine, search and the MCP ``add_drawer`` tool each resolve the embedder on
their own path (the miner and Chroma backend, the searcher, the MCP
session). This drives all three against one throwaway palace with an
unrecognized model configured and checks they embed with the same function,
so a palace mined under the fallback stays searchable and writable.

The module is listed in ``conftest._REAL_EMBEDDING_TEST_MODULES`` so the
real :func:`mempalace.embedding.get_embedding_function` runs; only the MiniLM
ONNX class underneath it is replaced, by a deterministic 384-dim stand-in.
"""

import hashlib
import json
import logging
import math
import re

import pytest
import yaml

import mempalace.embedding as embedding

_DIM = 384
_TOKEN = re.compile(r"\w+", re.UNICODE)


class _MiniLMStandIn:
    """Deterministic bag-of-words vectors behind the MiniLM factory slot."""

    instances = []
    calls = []

    def __init__(self, preferred_providers=None, intra_op_num_threads=0, *, _factory=True):
        if _factory:
            _MiniLMStandIn.instances.append(self)

    @staticmethod
    def name() -> str:
        return "default"

    @staticmethod
    def build_from_config(config):
        # chromadb rebuilds a function from the collection's stored config on
        # open; only instances the mempalace factory builds are counted.
        return _MiniLMStandIn(_factory=False)

    @staticmethod
    def validate_config(config) -> None:
        return

    def get_config(self) -> dict:
        return {}

    def is_legacy(self) -> bool:
        return False

    def default_space(self) -> str:
        return "cosine"

    def supported_spaces(self) -> list:
        return ["cosine", "l2", "ip"]

    def embed_query(self, input):
        return self(input=input)

    def __call__(self, input):
        _MiniLMStandIn.calls.append(id(self))
        out = []
        for text in list(input or []):
            vec = [0.0] * _DIM
            for token in _TOKEN.findall(str(text).lower()) or [""]:
                digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
                vec[int.from_bytes(digest[:4], "little") % _DIM] += 1.0
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            out.append([v / norm for v in vec])
        return out


@pytest.fixture
def unknown_model_palace(monkeypatch, tmp_path, request):
    configured = request.param
    palace = tmp_path / "palace"
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.json").write_text(
        json.dumps({"palace_path": str(palace), "embedding_model": configured})
    )
    monkeypatch.setenv("MEMPALACE_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("MEMPALACE_EMBEDDING_DEVICE", "cpu")
    monkeypatch.delenv("MEMPALACE_EMBEDDING_MODEL", raising=False)
    monkeypatch.setattr(embedding, "_EF_CACHE", {})
    monkeypatch.setattr(embedding, "_WARNED", set())
    monkeypatch.setattr(embedding, "_DIM_CACHE", {})
    _MiniLMStandIn.instances = []
    _MiniLMStandIn.calls = []
    monkeypatch.setattr(embedding, "_build_ef_class", lambda: _MiniLMStandIn)

    # Before the fallback, the Chroma backend caught the factory's error and
    # used chromadb's own default function, so a write could succeed with a
    # different embedder than mine and search. Fail loudly if that happens.
    from chromadb.api.types import DefaultEmbeddingFunction

    def _chroma_default_used(self, input):
        raise AssertionError("chromadb's default embedding function was used")

    monkeypatch.setattr(DefaultEmbeddingFunction, "__call__", _chroma_default_used)

    project = tmp_path / "project"
    (project / "notes").mkdir(parents=True)
    (project / "notes" / "garden.md").write_text(
        "The greenhouse tomatoes need watering every morning before nine.\n" * 8
    )
    with open(project / "mempalace.yaml", "w") as fh:
        yaml.dump({"wing": "garden", "rooms": [{"name": "notes", "description": "Notes"}]}, fh)

    from mempalace.config import MempalaceConfig

    return project, palace, MempalaceConfig(config_dir=str(config_dir))


@pytest.mark.parametrize(
    "unknown_model_palace",
    ["all-minilm-l6-v2", "", None],
    ids=["non-canonical", "empty", "null"],
    indirect=True,
)
def test_mine_search_and_add_drawer_share_the_fallback_embedder(
    unknown_model_palace, monkeypatch, caplog, kg
):
    from _mcp_server_helpers import _patch_mcp_server

    from mempalace.mcp_server import tool_add_drawer
    from mempalace.miner import mine
    from mempalace.searcher import search_memories

    project, palace, config = unknown_model_palace
    with caplog.at_level(logging.WARNING, logger=embedding.logger.name):
        mine(str(project), str(palace))
        mined = search_memories("greenhouse tomatoes watering", str(palace))

        _patch_mcp_server(monkeypatch, config, kg)
        added = tool_add_drawer(
            wing="garden",
            room="notes",
            content="The compost bin by the shed is turned every Sunday afternoon.",
        )
        found = search_memories("compost bin turned on Sunday", str(palace))

    assert mined.get("results"), mined
    assert "greenhouse tomatoes" in mined["results"][0]["text"]
    assert added["success"] is True, added
    assert found.get("results"), found
    assert any("compost bin" in r["text"] for r in found["results"])

    # One embedder for the whole round trip: built once, used by every path.
    assert len(_MiniLMStandIn.instances) == 1
    assert set(_MiniLMStandIn.calls) == {id(_MiniLMStandIn.instances[0])}
    assert embedding.get_embedder_identity().dimension == _DIM
    # One warning per process, not per call.
    warnings = [r for r in caplog.records if "Unknown embedding_model" in r.getMessage()]
    assert len(warnings) == 1


@pytest.mark.parametrize("unknown_model_palace", ["minilm"], indirect=True)
@pytest.mark.parametrize("typo", ["embeddinggemm2", "embeddinggema"])
def test_embeddinggemma_typo_stops_mine_search_and_add_drawer(
    unknown_model_palace, monkeypatch, kg, typo
):
    """A palace mined with a known model, then a misspelled EmbeddingGemma in
    the config: every path refuses with the hint and nothing is written."""
    import chromadb
    from _mcp_server_helpers import _patch_mcp_server

    from mempalace.config import MempalaceConfig
    from mempalace.mcp_server import tool_add_drawer
    from mempalace.miner import mine
    from mempalace.searcher import search_memories

    project, palace, config = unknown_model_palace
    mine(str(project), str(palace))
    client = chromadb.PersistentClient(path=str(palace))
    before = client.get_collection("mempalace_drawers").count()
    client.close()

    config_file = project.parent / "config" / "config.json"
    config_file.write_text(json.dumps({"palace_path": str(palace), "embedding_model": typo}))
    embedding._EF_CACHE.clear()
    (project / "notes" / "more.md").write_text("The rain barrel overflows in April.\n" * 8)

    with pytest.raises(embedding.UnknownEmbeddingModelError, match="did you mean"):
        mine(str(project), str(palace))
    searched = search_memories("greenhouse tomatoes", str(palace))
    assert searched["results"] == []
    assert searched["error"] == "Unknown embedding_model", searched
    assert "did you mean 'embeddinggemma' or 'embeddinggemma2'?" in searched["details"]

    _patch_mcp_server(monkeypatch, MempalaceConfig(config_dir=str(config_file.parent)), kg)
    added = tool_add_drawer(wing="garden", room="notes", content="The compost bin is turned.")
    assert added.get("success") is not True, added
    assert added["error"] == "Unknown embedding_model", added
    assert "did you mean 'embeddinggemma' or 'embeddinggemma2'?" in added["details"]

    client = chromadb.PersistentClient(path=str(palace))
    assert client.get_collection("mempalace_drawers").count() == before
    client.close()


@pytest.mark.parametrize("unknown_model_palace", ["minilm"], indirect=True)
def test_openai_typo_stops_mine_search_and_add_drawer(unknown_model_palace, monkeypatch, kg):
    """``embedding_model: "openai"`` reads as meaning remote embeddings: mine,
    search and add_drawer refuse with the openai-compat hint, nothing written."""
    import chromadb
    from _mcp_server_helpers import _patch_mcp_server

    from mempalace.config import MempalaceConfig
    from mempalace.mcp_server import tool_add_drawer
    from mempalace.miner import mine
    from mempalace.searcher import search_memories

    project, palace, config = unknown_model_palace
    mine(str(project), str(palace))
    client = chromadb.PersistentClient(path=str(palace))
    before = client.get_collection("mempalace_drawers").count()
    client.close()

    config_file = project.parent / "config" / "config.json"
    config_file.write_text(json.dumps({"palace_path": str(palace), "embedding_model": "openai"}))
    embedding._EF_CACHE.clear()
    (project / "notes" / "more.md").write_text("The rain barrel overflows in April.\n" * 8)

    hint = "did you mean 'openai-compat'?"
    with pytest.raises(embedding.UnknownEmbeddingModelError, match=hint.replace("?", r"\?")):
        mine(str(project), str(palace))
    searched = search_memories("greenhouse tomatoes", str(palace))
    assert searched["error"] == "Unknown embedding_model", searched
    assert hint in searched["details"]

    _patch_mcp_server(monkeypatch, MempalaceConfig(config_dir=str(config_file.parent)), kg)
    added = tool_add_drawer(wing="garden", room="notes", content="The compost bin is turned.")
    assert added.get("success") is not True, added
    assert hint in added["details"]

    client = chromadb.PersistentClient(path=str(palace))
    assert client.get_collection("mempalace_drawers").count() == before
    client.close()


def _set_model(config, palace, model):
    # The fixture keeps config.json next to the palace: tmp_path/config, tmp_path/palace.
    config_file = palace.parent / "config" / "config.json"
    config_file.write_text(json.dumps({"palace_path": str(palace), "embedding_model": model}))
    embedding._EF_CACHE.clear()


def _recorded_identity(palace):
    sidecar = palace / "mempalace_embedder.json"
    return json.loads(sidecar.read_text())


@pytest.mark.parametrize("unknown_model_palace", ["embeddinggemm2", "openai"], indirect=True)
def test_fresh_mine_with_a_near_miss_leaves_nothing_behind(unknown_model_palace, monkeypatch, kg):
    """The guard fires before the palace folder, a collection or the identity
    sidecar exists, so fixing the config afterwards mines normally."""
    from _mcp_server_helpers import _patch_mcp_server

    from mempalace.config import MempalaceConfig
    from mempalace.mcp_server import tool_add_drawer
    from mempalace.miner import mine
    from mempalace.searcher import search_memories

    project, palace, config = unknown_model_palace
    with pytest.raises(embedding.UnknownEmbeddingModelError, match="did you mean"):
        mine(str(project), str(palace))
    assert not palace.exists()
    searched = search_memories("greenhouse tomatoes", str(palace))
    assert searched.get("error"), searched
    _patch_mcp_server(monkeypatch, MempalaceConfig(config_dir=str(palace.parent / "config")), kg)
    added = tool_add_drawer(wing="garden", room="notes", content="The compost bin is turned.")
    assert added.get("success") is not True, added
    assert "did you mean" in added.get("details", ""), added
    assert not palace.exists()
    assert not (palace / "mempalace_embedder.json").exists()

    _set_model(config, palace, "minilm")
    mine(str(project), str(palace))
    assert _recorded_identity(palace)["mempalace_drawers"]["model_name"] == "minilm"
    found = search_memories("greenhouse tomatoes watering", str(palace))
    assert found.get("results"), found


@pytest.mark.parametrize(
    "unknown_model_palace",
    ["notamodel", "", None],
    ids=["unknown", "empty", "null"],
    indirect=True,
)
def test_fresh_mine_with_a_fallback_name_records_minilm(unknown_model_palace, caplog):
    """A fresh palace mined under the fallback records the model that embedded
    it, so correcting the config to minilm keeps mining and searching it."""
    import chromadb

    from mempalace.miner import mine
    from mempalace.searcher import search_memories

    project, palace, config = unknown_model_palace
    mine(str(project), str(palace))
    identity = _recorded_identity(palace)
    assert {entry["model_name"] for entry in identity.values()} == {"minilm"}, identity

    _set_model(config, palace, "minilm")
    (project / "notes" / "rain.md").write_text("The rain barrel overflows in April.\n" * 8)
    with caplog.at_level(logging.WARNING):
        mine(str(project), str(palace))
        found = search_memories("rain barrel overflows", str(palace))
    assert found.get("results"), found
    assert "rain barrel" in found["results"][0]["text"]
    assert not [r for r in caplog.records if "embedder identity" in r.getMessage()]
    client = chromadb.PersistentClient(path=str(palace))
    assert client.get_collection("mempalace_drawers").count() >= 2
    client.close()


@pytest.mark.parametrize("unknown_model_palace", ["minilm"], indirect=True)
def test_rebuild_with_a_near_miss_refuses_before_archiving(unknown_model_palace):
    """``repair rebuild-index`` used to archive the palace and only then hit
    the typo, leaving a half-built palace beside the archive."""
    from mempalace.miner import mine
    from mempalace.repair import rebuild_from_sqlite

    project, palace, config = unknown_model_palace
    mine(str(project), str(palace))
    before = sorted(p.name for p in palace.parent.iterdir())
    _set_model(config, palace, "embeddinggemm2")

    with pytest.raises(embedding.UnknownEmbeddingModelError, match="did you mean"):
        rebuild_from_sqlite(str(palace), str(palace), archive_existing_dest=True)
    assert sorted(p.name for p in palace.parent.iterdir()) == before
    assert _recorded_identity(palace)["mempalace_drawers"]["model_name"] == "minilm"


def _stamp_legacy_identity(palace, model_name):
    """Rewrite the sidecar as develop / older builds left it for a palace
    configured with a non-standard name: the raw name, MiniLM vectors."""
    sidecar = palace / "mempalace_embedder.json"
    data = json.loads(sidecar.read_text())
    for entry in data.values():
        entry["model_name"] = model_name
    sidecar.write_text(json.dumps(data))


_LEGACY_OPENS = [
    ("all-minilm-l6-v2", "all-minilm-l6-v2"),
    ("all-minilm-l6-v2", "minilm"),
    ("none", None),
    ("none", "minilm"),
    ("minilm-l6", "minilm-l6"),
    ("minilm-l6", "minilm"),
    ("embedinggemma2", "minilm"),
]


@pytest.mark.parametrize("unknown_model_palace", ["minilm"], indirect=True)
@pytest.mark.parametrize(
    "stored, configured", _LEGACY_OPENS, ids=[f"{s}-cfg-{c}" for s, c in _LEGACY_OPENS]
)
def test_legacy_palace_with_a_raw_stored_name_keeps_working(
    unknown_model_palace, monkeypatch, stored, configured
):
    """Older builds embedded an unrecognized name with MiniLM but recorded the
    raw name. Opening such a palace with that name (or with minilm) must keep
    mining and searching without a re-embed; a write open records minilm."""
    import chromadb

    from mempalace.miner import mine
    from mempalace.palace import _VALIDATED_IDENTITY
    from mempalace.searcher import search_memories

    project, palace, config = unknown_model_palace
    mine(str(project), str(palace))
    _stamp_legacy_identity(palace, stored)
    _VALIDATED_IDENTITY.clear()
    _set_model(config, palace, configured)

    found = search_memories("greenhouse tomatoes watering", str(palace))
    assert found.get("results"), found
    assert _recorded_identity(palace)["mempalace_drawers"]["model_name"] == stored  # read-only

    (project / "notes" / "rain.md").write_text("The rain barrel overflows in April.\n" * 8)
    mine(str(project), str(palace))
    found = search_memories("rain barrel overflows", str(palace))
    assert "rain barrel" in found["results"][0]["text"], found
    recorded = _recorded_identity(palace)
    assert {entry["model_name"] for entry in recorded.values()} == {"minilm"}, recorded
    client = chromadb.PersistentClient(path=str(palace))
    assert client.get_collection("mempalace_drawers").count() == 2
    client.close()


@pytest.mark.parametrize("unknown_model_palace", ["minilm"], indirect=True)
def test_legacy_near_miss_name_as_config_raises_with_the_minilm_hint(unknown_model_palace):
    """``embedinggemma2`` is a near miss as a config value and refuses by
    design; the error says how to keep using a palace older builds filled."""
    from mempalace.miner import mine
    from mempalace.palace import _VALIDATED_IDENTITY

    project, palace, config = unknown_model_palace
    mine(str(project), str(palace))
    _stamp_legacy_identity(palace, "embedinggemma2")
    _VALIDATED_IDENTITY.clear()
    _set_model(config, palace, "embedinggemma2")
    with pytest.raises(embedding.UnknownEmbeddingModelError) as excinfo:
        mine(str(project), str(palace))
    assert (
        "Older builds embedded unrecognized names with MiniLM; set embedding_model to "
        "minilm to keep using such a palace." in str(excinfo.value)
    )


@pytest.mark.parametrize("unknown_model_palace", ["minilm"], indirect=True)
def test_a_known_stored_model_still_refuses_a_swap(unknown_model_palace):
    """Only unrecognized stored names are read as MiniLM; a palace that
    records embeddinggemma stays strict against a minilm config."""
    from mempalace.backends.base import EmbedderIdentityMismatchError
    from mempalace.miner import mine
    from mempalace.palace import _VALIDATED_IDENTITY
    from mempalace.searcher import search_memories

    project, palace, config = unknown_model_palace
    mine(str(project), str(palace))
    _stamp_legacy_identity(palace, "embeddinggemma")
    _VALIDATED_IDENTITY.clear()
    (project / "notes" / "rain.md").write_text("The rain barrel overflows in April.\n" * 8)
    with pytest.raises(EmbedderIdentityMismatchError):
        mine(str(project), str(palace))
    searched = search_memories("greenhouse tomatoes", str(palace))
    assert searched.get("error"), searched
    assert _recorded_identity(palace)["mempalace_drawers"]["model_name"] == "embeddinggemma"


@pytest.mark.parametrize("unknown_model_palace", ["minilm"], indirect=True)
def test_set_embedder_resolves_the_model_name(unknown_model_palace):
    """`set-embedder --model all-minilm-l6-v2 --force` records the model that
    name embeds with, minilm; on a legacy palace no --force is needed."""
    from mempalace.miner import mine
    from mempalace.palace import set_palace_embedder_identity

    project, palace, config = unknown_model_palace
    mine(str(project), str(palace))
    _stamp_legacy_identity(palace, "all-minilm-l6-v2")

    old, new = set_palace_embedder_identity(str(palace), model="all-minilm-l6-v2", force=True)
    assert old.model_name == "all-minilm-l6-v2"
    assert new.model_name == "minilm"
    assert _recorded_identity(palace)["mempalace_drawers"]["model_name"] == "minilm"

    _stamp_legacy_identity(palace, "minilm-l6")
    old, new = set_palace_embedder_identity(str(palace), model="minilm")
    assert new.model_name == "minilm"
    assert _recorded_identity(palace)["mempalace_drawers"]["model_name"] == "minilm"


@pytest.mark.parametrize("unknown_model_palace", ["embeddinggemm2"], indirect=True)
def test_mcp_marks_the_near_miss_refusal_as_a_tool_error(unknown_model_palace, monkeypatch, kg):
    """Clients that only check ``isError`` must see the refusal as an error;
    a successful call keeps the plain result."""
    from _mcp_server_helpers import _patch_mcp_server

    from mempalace.config import MempalaceConfig
    from mempalace.mcp_server import handle_request

    project, palace, config = unknown_model_palace
    _patch_mcp_server(monkeypatch, MempalaceConfig(config_dir=str(palace.parent / "config")), kg)

    def call(name, arguments):
        return handle_request(
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
        )

    refused = call(
        "mempalace_add_drawer", {"wing": "garden", "room": "notes", "content": "Compost."}
    )
    assert refused["result"]["isError"] is True, refused
    body = json.loads(refused["result"]["content"][0]["text"])
    assert body["error"] == "Unknown embedding_model"
    assert "did you mean" in body["details"]
    searched = call("mempalace_search", {"query": "compost"})
    assert searched["result"]["isError"] is True, searched
    assert not palace.exists()

    _set_model(config, palace, "minilm")
    _patch_mcp_server(monkeypatch, MempalaceConfig(config_dir=str(palace.parent / "config")), kg)
    added = call("mempalace_add_drawer", {"wing": "garden", "room": "notes", "content": "Compost."})
    assert "isError" not in added["result"], added
    assert json.loads(added["result"]["content"][0]["text"])["success"] is True
