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
    monkeypatch.setattr(embedding, "_REFUSALS_LOGGED", set())
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


def _search_warnings(query, palace):
    import warnings

    from mempalace.backends.base import EmbedderIdentityUnknownWarning
    from mempalace.searcher import search_memories

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        found = search_memories(query, str(palace))
    unknown = [w for w in caught if issubclass(w.category, EmbedderIdentityUnknownWarning)]
    return found, unknown


@pytest.mark.parametrize("unknown_model_palace", ["minilm"], indirect=True)
@pytest.mark.parametrize("mode", ["rebuild-index", "from-sqlite"])
def test_sqlite_rebuild_records_the_identity_of_every_collection(unknown_model_palace, mode):
    """#2709: ``repair rebuild-index`` (an in-place SQLite rebuild that
    archives the palace first) and ``repair --mode from-sqlite`` re-embed
    every row but left the rebuilt palace without mempalace_embedder.json,
    so every later open warned that the identity was unknown."""
    from mempalace.miner import mine
    from mempalace.repair import rebuild_from_sqlite

    project, palace, config = unknown_model_palace
    mine(str(project), str(palace))
    if mode == "rebuild-index":
        rebuild_from_sqlite(str(palace), str(palace), archive_existing_dest=True)
        rebuilt = palace
    else:
        rebuilt = palace.parent / "rebuilt"
        rebuild_from_sqlite(str(palace), str(rebuilt))

    identity = _recorded_identity(rebuilt)
    assert set(identity) == {"mempalace_drawers", "mempalace_closets"}, identity
    for entry in identity.values():
        assert entry == {"model_name": "minilm", "dimension": _DIM}
    found, unknown = _search_warnings("greenhouse tomatoes", rebuilt)
    assert found.get("results"), found
    assert unknown == []


@pytest.mark.parametrize("unknown_model_palace", ["minilm"], indirect=True)
def test_temp_collection_rebuild_records_the_identity(unknown_model_palace):
    """The temp-collection rebuild (``repair.rebuild_index``, the daemon's
    path) records the identity on every model, not only EmbeddingGemma 2."""
    from mempalace.miner import mine
    from mempalace.repair import rebuild_index

    project, palace, config = unknown_model_palace
    mine(str(project), str(palace))
    # rebuild_index touches only the drawers; drop just their entry.
    sidecar = palace / "mempalace_embedder.json"
    recorded = json.loads(sidecar.read_text())
    del recorded["mempalace_drawers"]
    sidecar.write_text(json.dumps(recorded))

    rebuild_index(str(palace), progress=lambda *_args: None)

    entry = _recorded_identity(palace)["mempalace_drawers"]
    assert entry == {"model_name": "minilm", "dimension": _DIM}
    found, unknown = _search_warnings("greenhouse tomatoes", palace)
    assert found.get("results"), found
    assert unknown == []


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


# ── MCP writes go through the same embedder-identity check as the CLI ────


def _mcp_caller(monkeypatch, palace, kg):
    from _mcp_server_helpers import _patch_mcp_server

    from mempalace.config import MempalaceConfig
    from mempalace.mcp_server import handle_request

    _patch_mcp_server(monkeypatch, MempalaceConfig(config_dir=str(palace.parent / "config")), kg)

    def call(name, arguments):
        response = handle_request(
            {
                "jsonrpc": "2.0",
                "id": 9,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
        )
        result = response["result"]
        return result, json.loads(result["content"][0]["text"])

    return call


def _restamp_as_another_process(palace, model_name):
    """Stamp the sidecar, then forget this process's validated identities, as a
    separate MCP server process would start without them."""
    from mempalace import palace as palace_mod

    _stamp_legacy_identity(palace, model_name)
    palace_mod._VALIDATED_IDENTITY.clear()


def _embedding_rows(palace) -> int:
    import sqlite3

    with sqlite3.connect(str(palace / "chroma.sqlite3")) as db:
        return db.execute("select count(*) from embeddings").fetchone()[0]


_ADD = {"wing": "garden", "room": "notes", "content": "The compost bin is turned every Sunday."}


@pytest.mark.parametrize("unknown_model_palace", ["minilm"], indirect=True)
@pytest.mark.parametrize(
    "configured, kind",
    [
        ("minilm", "Embedder identity mismatch"),
        # Chroma's own embedding-function name check fires first here.
        ("embeddinggemma2", "Embedding model mismatch"),
    ],
)
def test_mcp_add_drawer_refuses_a_palace_recorded_with_another_model(
    unknown_model_palace, monkeypatch, caplog, capfd, kg, configured, kind
):
    """#2694 adds a second 768-dim model, so a same-dimension write with the
    wrong model would be silent. A palace recorded as embeddinggemma must
    refuse an MCP write under minilm or embeddinggemma2, as the CLI does."""
    from mempalace.miner import mine

    project, palace, config = unknown_model_palace
    mine(str(project), str(palace))
    _restamp_as_another_process(palace, "embeddinggemma")
    rows = _embedding_rows(palace)
    _set_model(config, palace, configured)

    call = _mcp_caller(monkeypatch, palace, kg)
    with caplog.at_level(logging.WARNING):
        result, body = call("mempalace_add_drawer", _ADD)
    assert result["isError"] is True, body
    assert body["error"] == kind, body
    assert "repair rebuild-index" in body["details"]
    assert "from-sqlite" in body["details"]
    assert _embedding_rows(palace) == rows
    assert _recorded_identity(palace)["mempalace_drawers"]["model_name"] == "embeddinggemma"
    # One clean log line, no traceback.
    assert not [r for r in caplog.records if r.exc_info], [r.getMessage() for r in caplog.records]
    assert "Traceback" not in capfd.readouterr().err

    # A second call refuses too: the refused collection is not cached.
    result, body = call("mempalace_add_drawer", _ADD)
    assert result["isError"] is True and body["error"] == kind
    assert _embedding_rows(palace) == rows


@pytest.mark.parametrize("unknown_model_palace", ["minilm"], indirect=True)
@pytest.mark.parametrize("stored", ["all-minilm-l6-v2", "none", "minilm-l6", "embeddinggemma2"])
def test_mcp_write_rewrites_a_legacy_raw_stamp_to_minilm(
    unknown_model_palace, monkeypatch, kg, stored
):
    from mempalace.miner import mine

    project, palace, config = unknown_model_palace
    mine(str(project), str(palace))
    _restamp_as_another_process(palace, stored)
    rows = _embedding_rows(palace)

    call = _mcp_caller(monkeypatch, palace, kg)
    result, body = call("mempalace_add_drawer", _ADD)
    assert "isError" not in result and body["success"] is True, body
    assert _embedding_rows(palace) == rows + 1
    assert _recorded_identity(palace)["mempalace_drawers"]["model_name"] == "minilm"


@pytest.mark.parametrize("unknown_model_palace", ["minilm"], indirect=True)
def test_mcp_write_records_the_identity_of_a_fresh_palace(unknown_model_palace, monkeypatch, kg):
    project, palace, config = unknown_model_palace
    call = _mcp_caller(monkeypatch, palace, kg)
    result, body = call("mempalace_add_drawer", _ADD)
    assert body["success"] is True, body
    assert _recorded_identity(palace)["mempalace_drawers"]["model_name"] == "minilm"


@pytest.mark.parametrize("unknown_model_palace", ["minilm"], indirect=True)
def test_mcp_search_of_a_palace_recorded_with_another_model_is_a_tool_error(
    unknown_model_palace, monkeypatch, kg
):
    from mempalace.miner import mine

    project, palace, config = unknown_model_palace
    mine(str(project), str(palace))
    _restamp_as_another_process(palace, "embeddinggemma")
    call = _mcp_caller(monkeypatch, palace, kg)
    result, body = call("mempalace_search", {"query": "greenhouse tomatoes"})
    assert result.get("isError") is True, body
    assert "embeddinggemma" in json.dumps(body)


@pytest.mark.parametrize("unknown_model_palace", ["minilm"], indirect=True)
def test_mcp_chroma_embedding_function_conflict_is_a_clean_tool_error(
    unknown_model_palace, monkeypatch, caplog, kg
):
    """Chroma's own name check (persisted EF name differs) must come back as
    the explained mismatch, without the retry and its logged traceback."""
    from mempalace.miner import mine

    project, palace, config = unknown_model_palace
    mine(str(project), str(palace))
    call = _mcp_caller(monkeypatch, palace, kg)

    import mempalace.mcp_server as mcp

    opens = []

    class _Client:
        def get_collection(self, name, **kwargs):
            opens.append(name)
            raise ValueError(
                "An embedding function already exists in the collection configuration, and a "
                "new one is provided. Embedding function conflict: new: embeddinggemma2 vs "
                "persisted: default"
            )

    monkeypatch.setattr(mcp, "_get_client", lambda: _Client())
    with caplog.at_level(logging.WARNING):
        result, body = call("mempalace_add_drawer", _ADD)
    assert result["isError"] is True, body
    assert body["error"] == "Embedding model mismatch"
    assert "repair --mode from-sqlite" in body["details"]
    assert len(opens) == 1  # no retry
    assert not [r for r in caplog.records if r.exc_info]


def _model_errors():
    from mempalace.backends.base import (
        DimensionMismatchError,
        EmbedderIdentityMismatchError,
        EmbeddingFunctionMismatchError,
    )

    return {
        "Unknown embedding_model": embedding.UnknownEmbeddingModelError("typo"),
        "Embedder identity mismatch": EmbedderIdentityMismatchError("identity"),
        "Embedding dimension mismatch": DimensionMismatchError("dimension"),
        "Embedding model mismatch": EmbeddingFunctionMismatchError("chroma ef"),
        "Embedding API unavailable": embedding.EmbeddingAPIError("connection refused"),
    }


@pytest.mark.parametrize("kind", sorted(_model_errors()))
def test_model_errors_set_is_error_by_exception_class(kind):
    from mempalace.mcp_server import _tool_call_response

    exc = _model_errors()[kind]
    result = embedding.model_error_result(exc)
    assert result["error"] == kind
    assert result["error_class"] == type(exc).__name__
    assert result["details"] == str(exc)
    assert _tool_call_response(1, result)["result"]["isError"] is True
    # The flag follows the exception class, not the wording of ``error``.
    assert "isError" not in _tool_call_response(1, {"error": kind, "details": "x"})["result"]


def test_model_error_subclasses_report_the_model_error_class():
    from mempalace.backends.base import EmbedderIdentityMismatchError

    class _Narrower(EmbedderIdentityMismatchError):
        pass

    result = embedding.model_error_result(_Narrower("x"))
    assert result["error_class"] == "EmbedderIdentityMismatchError"
    assert embedding.model_error_result(ValueError("other")) is None


def test_open_failure_sets_is_error_and_plain_errors_do_not():
    from mempalace.mcp_server import _tool_call_response

    assert _tool_call_response(1, {"error": "Backend open failed"})["result"]["isError"] is True
    assert "isError" not in _tool_call_response(1, {"error": "No palace found"})["result"]
    assert "isError" not in _tool_call_response(1, {"success": True})["result"]


def test_dimension_mismatch_gets_its_own_error_kind():
    from mempalace.backends.base import DimensionMismatchError, EmbedderIdentityMismatchError
    from mempalace.mcp_server import _model_mismatch_error

    assert _model_mismatch_error(DimensionMismatchError("d"))["error"] == (
        "Embedding dimension mismatch"
    )
    assert _model_mismatch_error(EmbedderIdentityMismatchError("i"))["error"] == (
        "Embedder identity mismatch"
    )


# ── follow-up polish ─────────────────────────────────────────────────────────


def test_bare_embeddinggemma2_stored_name_reads_as_legacy_minilm():
    """#2694 always records EmbeddingGemma 2 by its full identity, so a bare
    "embeddinggemma2" comes from a build that embedded the name with MiniLM."""
    norm = embedding._normalize_stored_model_name
    assert norm("embeddinggemma2") == "minilm"
    assert norm(" EmbeddingGemma2 ") == "minilm"
    full = "embeddinggemma2:google/embeddinggemma-2@abc:768:text:retrieval-v1"
    assert norm(full) == full
    for name in ("minilm", "embeddinggemma", "openai-compat"):
        assert norm(name) == name


@pytest.mark.parametrize("unknown_model_palace", ["minilm"], indirect=True)
def test_set_embedder_records_the_full_embeddinggemma2_identity(unknown_model_palace):
    """Recording the bare name would now read back as MiniLM."""
    from mempalace.miner import mine
    from mempalace.palace import set_palace_embedder_identity

    project, palace, config = unknown_model_palace
    mine(str(project), str(palace))
    old, new = set_palace_embedder_identity(str(palace), model="embeddinggemma2", force=True)
    assert new.model_name.startswith("embeddinggemma2:google/embeddinggemma-2@")
    assert new.model_name.endswith(":768:text:retrieval-v1")
    assert new.dimension == 768
    assert _recorded_identity(palace)["mempalace_drawers"]["model_name"] == new.model_name


def _run_cli(monkeypatch, *argv):
    import sys

    from mempalace.cli import main

    monkeypatch.setattr(sys, "argv", ["mempalace", *argv])
    with pytest.raises(SystemExit) as excinfo:
        main()
    return excinfo.value.code


@pytest.mark.parametrize("unknown_model_palace", ["minilm"], indirect=True)
@pytest.mark.parametrize("existing", [False, True], ids=["missing-palace", "existing-palace"])
def test_set_embedder_refuses_a_near_miss_model_before_opening(
    unknown_model_palace, monkeypatch, capfd, existing
):
    from mempalace.miner import mine

    project, palace, config = unknown_model_palace
    if existing:
        mine(str(project), str(palace))
        sidecar = (palace / "mempalace_embedder.json").read_text()
        db_mtime = (palace / "chroma.sqlite3").stat().st_mtime_ns
    capfd.readouterr()
    code = _run_cli(
        monkeypatch, "--palace", str(palace), "palace", "set-embedder", "--model", "embeddinggemm2"
    )
    out, err = capfd.readouterr()
    assert code == 2
    assert "✗ Unknown embedding_model 'embeddinggemm2'; did you mean" in out
    assert "Traceback" not in out + err
    if existing:
        assert (palace / "mempalace_embedder.json").read_text() == sidecar
        assert (palace / "chroma.sqlite3").stat().st_mtime_ns == db_mtime
    else:
        assert not palace.exists()


@pytest.mark.parametrize("unknown_model_palace", ["embeddinggemm2"], indirect=True)
def test_mine_source_media_refuses_a_near_miss_cleanly(unknown_model_palace, monkeypatch, capfd):
    project, palace, config = unknown_model_palace
    code = _run_cli(monkeypatch, "--palace", str(palace), "mine", str(project), "--source", "media")
    out, err = capfd.readouterr()
    assert code == 1
    assert err.startswith("mempalace: Unknown embedding_model 'embeddinggemm2'; did you mean"), err
    assert "Traceback" not in out + err
    assert not palace.exists()


@pytest.mark.parametrize("unknown_model_palace", ["minilm"], indirect=True)
def test_mcp_media_search_model_errors_are_tool_errors(unknown_model_palace, monkeypatch, kg):
    from mempalace.miner import mine

    project, palace, config = unknown_model_palace
    mine(str(project), str(palace))
    call = _mcp_caller(monkeypatch, palace, kg)
    media = {"query": "greenhouse tomatoes", "include_media": True}

    # Not a model error: media search needs embeddinggemma2. A plain result.
    result, body = call("mempalace_search", media)
    assert "isError" not in result and "embeddinggemma2" in body["error"], body

    # A palace built with MiniLM under an embeddinggemma2 config.
    _set_model(config, palace, "embeddinggemma2")
    result, body = call("mempalace_search", media)
    assert result["isError"] is True, body
    assert body["error_class"] in embedding.MODEL_ERROR_CLASS_NAMES
    assert body["results"] == []

    _set_model(config, palace, "embeddinggemm2")
    result, body = call("mempalace_search", media)
    assert result["isError"] is True, body
    assert body["error_class"] == "UnknownEmbeddingModelError"
    assert "did you mean" in body["details"]


@pytest.mark.parametrize("unknown_model_palace", ["minilm"], indirect=True)
def test_mcp_refusal_logs_the_full_message_once_then_a_short_line(
    unknown_model_palace, monkeypatch, caplog, kg
):
    from mempalace.miner import mine

    project, palace, config = unknown_model_palace
    mine(str(project), str(palace))
    _restamp_as_another_process(palace, "embeddinggemma")
    call = _mcp_caller(monkeypatch, palace, kg)
    with caplog.at_level(logging.ERROR):
        for _ in range(3):
            result, body = call("mempalace_add_drawer", _ADD)
            assert result["isError"] is True
    lines = [
        r.getMessage() for r in caplog.records if "Embedder identity mismatch" in r.getMessage()
    ]
    assert len(lines) == 3, lines
    assert "repair rebuild-index" in lines[0]
    for line in lines[1:]:
        assert "refused again" in line and str(palace) in line
        assert "repair rebuild-index" not in line
    # Each call still returns the full details.
    assert "repair rebuild-index" in body["details"]


@pytest.mark.parametrize("unknown_model_palace", ["minilm"], indirect=True)
@pytest.mark.parametrize(
    "case, expected",
    [
        ("identity", "mempalace: collection was built with embedder 'embeddinggemma'"),
        ("dimension", "mempalace: collection was built with a 768-dim embedder"),
        ("chroma-ef", "mempalace: Embedding model mismatch reading palace at"),
        ("unknown-model", "mempalace: Unknown embedding_model 'embeddinggemm2'; did you mean"),
    ],
)
def test_cli_search_prints_model_errors_cleanly(
    unknown_model_palace, monkeypatch, capfd, case, expected
):
    """Like mine: one `mempalace: <message>` line on stderr, exit 1, no
    exception repr (no ``EmbedderIdentityMismatchError(...)``, no ``\\n``)."""
    from mempalace.miner import mine

    project, palace, config = unknown_model_palace
    mine(str(project), str(palace))
    if case == "identity":
        _restamp_as_another_process(palace, "embeddinggemma")
    elif case == "dimension":
        # Chroma opens never compare dimensions (the probe is skipped at open);
        # the pgvector/milvus backends raise this from their own open.
        import mempalace.searcher as searcher
        from mempalace.backends.base import DimensionMismatchError

        def refuse(*args, **kwargs):
            raise DimensionMismatchError(
                "collection was built with a 768-dim embedder ('embeddinggemma') but the "
                "current embedder produces 384-dim vectors ('minilm')."
            )

        monkeypatch.setattr(searcher, "get_collection", refuse)
    elif case == "chroma-ef":
        _set_model(config, palace, "embeddinggemma2")
    else:
        _set_model(config, palace, "embeddinggemm2")
    capfd.readouterr()
    code = _run_cli(monkeypatch, "--palace", str(palace), "search", "greenhouse")
    out, err = capfd.readouterr()
    assert code == 1
    assert err.startswith(expected), err
    assert "Error opening palace" not in out + err
    assert "MismatchError(" not in out + err and "\\n" not in out + err
    assert "Traceback" not in out + err


# ── C: never fall back to chromadb's default embedding function (S2) ─────


def _break_openai_compat(palace, monkeypatch):
    """Record the palace as openai-compat, then configure openai-compat with
    no endpoint: the configured function cannot be built."""
    monkeypatch.delenv("MEMPALACE_EMBEDDING_API_URL", raising=False)
    monkeypatch.delenv("MEMPALACE_EMBEDDING_API_MODEL", raising=False)
    _restamp_as_another_process(palace, "openai-compat")
    config_file = palace.parent / "config" / "config.json"
    config_file.write_text(
        json.dumps(
            {
                "palace_path": str(palace),
                "embedding_model": "openai-compat",
                "embedding_api_model": "text-embed-a",
            }
        )
    )
    embedding._EF_CACHE.clear()


@pytest.mark.parametrize("unknown_model_palace", ["minilm"], indirect=True)
def test_mcp_add_drawer_refuses_when_the_embedding_function_cannot_be_built(
    unknown_model_palace, monkeypatch, kg
):
    """S2: the MCP write used to succeed on chromadb's default MiniLM
    function (rows 4 -> 5, success true) while the palace records
    openai-compat. It now refuses as a tool error and writes nothing."""
    from mempalace.miner import mine

    project, palace, config = unknown_model_palace
    mine(str(project), str(palace))
    rows = _embedding_rows(palace)
    _break_openai_compat(palace, monkeypatch)

    call = _mcp_caller(monkeypatch, palace, kg)
    result, body = call("mempalace_add_drawer", _ADD)
    assert result["isError"] is True, body
    assert body["error"] == "Embedding function unavailable"
    assert body["error_class"] == "EmbeddingFunctionUnavailableError"
    assert "embedding_api_url" in body["details"]
    assert "does not fall back" in body["details"]
    assert _embedding_rows(palace) == rows

    result, body = call("mempalace_search", {"query": "greenhouse tomatoes"})
    assert result["isError"] is True, body
    assert body["error_class"] == "EmbeddingFunctionUnavailableError"

    # Reads that never embed keep working.
    result, body = call("mempalace_status", {})
    assert "isError" not in result, body
    assert body.get("total_drawers", 0) >= 1, body


@pytest.mark.parametrize("unknown_model_palace", ["minilm"], indirect=True)
def test_cli_search_and_mine_refuse_when_the_embedding_function_cannot_be_built(
    unknown_model_palace, monkeypatch, capfd
):
    from mempalace.miner import mine

    project, palace, config = unknown_model_palace
    mine(str(project), str(palace))
    rows = _embedding_rows(palace)
    _break_openai_compat(palace, monkeypatch)
    capfd.readouterr()

    assert _run_cli(monkeypatch, "--palace", str(palace), "search", "greenhouse") == 1
    err = capfd.readouterr().err
    assert "mempalace: Could not build the embedding function" in err
    assert "Traceback" not in err

    (project / "notes" / "shed.md").write_text("The shed roof leaks near the bench.\n" * 8)
    assert _run_cli(monkeypatch, "--palace", str(palace), "mine", str(project)) == 1
    err = capfd.readouterr().err
    assert "mempalace: Could not build the embedding function" in err
    assert "Traceback" not in err
    assert _embedding_rows(palace) == rows


@pytest.mark.parametrize("unknown_model_palace", ["minilm"], indirect=True)
def test_fresh_mine_refuses_before_creating_the_palace(unknown_model_palace, monkeypatch):
    from mempalace.miner import mine

    project, palace, config = unknown_model_palace
    monkeypatch.delenv("MEMPALACE_EMBEDDING_API_URL", raising=False)
    (palace.parent / "config" / "config.json").write_text(
        json.dumps({"palace_path": str(palace), "embedding_model": "openai-compat"})
    )
    embedding._EF_CACHE.clear()
    with pytest.raises(embedding.EmbeddingFunctionUnavailableError):
        mine(str(project), str(palace))
    assert not (palace / "chroma.sqlite3").exists()
    assert not (palace / "mempalace_embedder.json").exists()


@pytest.mark.parametrize("unknown_model_palace", ["minilm"], indirect=True)
def test_chroma_resolver_never_returns_none(unknown_model_palace, monkeypatch):
    """The resolver refuses (write) or hands back a refusing stand-in (read);
    it never returns None, which made chromadb use its default function."""
    from mempalace.backends.chroma import ChromaBackend

    def _broken(**_kwargs):
        raise OSError("onnxruntime failed to load")

    monkeypatch.setattr(embedding, "get_embedding_function", _broken)
    with pytest.raises(embedding.EmbeddingFunctionUnavailableError, match="onnxruntime"):
        ChromaBackend._resolve_embedding_function()
    stand_in = ChromaBackend._resolve_embedding_function(read_only=True)
    assert isinstance(stand_in, embedding.UnavailableEmbeddingFunction)
    for embed in (stand_in, stand_in.embed_query, stand_in.embed_documents):
        with pytest.raises(embedding.EmbeddingFunctionUnavailableError):
            embed(input=["x"])
    # Refusal classes stay model errors, so MCP sets isError for them.
    assert "EmbeddingFunctionUnavailableError" in embedding.MODEL_ERROR_CLASS_NAMES


# ── A dead openai-compat endpoint is a tool error, like a missing URL ────


class _EmbeddingsHandler:
    """Deterministic ``/v1/embeddings`` answers (the MiniLM stand-in's vectors)."""

    @staticmethod
    def make():
        from http.server import BaseHTTPRequestHandler

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                vectors = _MiniLMStandIn(_factory=False)(body["input"])
                out = json.dumps(
                    {"data": [{"index": i, "embedding": v} for i, v in enumerate(vectors)]}
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def log_message(self, *args):
                pass

        return Handler


def _openai_compat_palace_with_a_dead_endpoint(unknown_model_palace, monkeypatch):
    """Mine an openai-compat palace against a live local endpoint, then stop
    the server: the configured URL now refuses the connection."""
    import threading
    from http.server import ThreadingHTTPServer

    from mempalace.miner import mine

    project, palace, config = unknown_model_palace
    monkeypatch.delenv("MEMPALACE_EMBEDDING_API_URL", raising=False)
    monkeypatch.delenv("MEMPALACE_EMBEDDING_API_MODEL", raising=False)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _EmbeddingsHandler.make())
    url = f"http://127.0.0.1:{server.server_address[1]}/v1"
    (palace.parent / "config" / "config.json").write_text(
        json.dumps(
            {
                "palace_path": str(palace),
                "embedding_model": "openai-compat",
                "embedding_api_url": url,
                "embedding_api_model": "text-embed-a",
            }
        )
    )
    embedding._EF_CACHE.clear()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        mine(str(project), str(palace))
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    assert _recorded_identity(palace)["mempalace_drawers"]["model_name"] == "openai-compat"
    return palace


@pytest.mark.parametrize("unknown_model_palace", ["minilm"], indirect=True)
def test_mcp_dead_openai_compat_endpoint_is_a_tool_error(unknown_model_palace, monkeypatch, kg):
    """Eve's recheck of c9b6814: with the URL set but nothing listening, MCP
    returned ``success: false`` without ``isError``, while a missing URL set
    ``isError``. Both now refuse as tool errors, and nothing is written."""
    palace = _openai_compat_palace_with_a_dead_endpoint(unknown_model_palace, monkeypatch)
    rows = _embedding_rows(palace)
    call = _mcp_caller(monkeypatch, palace, kg)

    result, body = call("mempalace_add_drawer", _ADD)
    assert result["isError"] is True, body
    assert body["success"] is False
    assert body["error"] == "Embedding API unavailable"
    assert body["error_class"] == "EmbeddingAPIError"
    assert "refused" in body["details"].lower(), body["details"]
    assert "embedding_api_url" in body["hint"]
    assert _embedding_rows(palace) == rows

    for name, arguments in (
        ("mempalace_search", {"query": "greenhouse tomatoes"}),
        ("mempalace_check_duplicate", {"content": "greenhouse tomatoes"}),
        ("mempalace_diary_write", {"agent_name": "eve", "entry": "Watered the tomatoes."}),
    ):
        result, body = call(name, arguments)
        assert result.get("isError") is True, (name, body)
        assert body["error_class"] == "EmbeddingAPIError", (name, body)
    assert _embedding_rows(palace) == rows

    # Reads that never embed keep working.
    result, body = call("mempalace_status", {})
    assert "isError" not in result, body
    assert body.get("total_drawers", 0) >= 1, body


def test_an_unrelated_write_failure_stays_a_plain_error():
    from mempalace.mcp_server import _embed_failure, _tool_call_response

    result = _embed_failure(RuntimeError("disk full"), success=False)
    assert result == {"success": False, "error": "disk full"}
    assert "isError" not in _tool_call_response(1, result)["result"]

    api_error = embedding.EmbeddingAPIError("Embedding API request to http://x failed")
    refused = _embed_failure(api_error, success=False)
    assert refused["error_class"] == "EmbeddingAPIError" and refused["success"] is False
    assert _tool_call_response(1, refused)["result"]["isError"] is True
