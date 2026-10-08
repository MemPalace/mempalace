"""The recorded vector dimension and dimension errors (patch 1, F).

* A first mine recorded the identity with dimension 0 while a rebuild of the
  same palace recorded 384 or 768: the record did not say what the vectors
  were. Every first write now records the probed dimension.
* chromadb rejects a vector of the wrong width with a bare
  ``InvalidArgumentError``; it is now a :class:`DimensionMismatchError` with
  the same recovery hint the other backends give, so the CLI prints one line
  and MCP search flags ``isError``.
"""

import json

import chromadb
import pytest

import test_embedding_model_fallback as fallback
from mempalace import embedding
from mempalace.backends.base import DimensionMismatchError, PalaceRef

from test_embedding_model_fallback import _mcp_caller
from test_embedding_model_fallback import unknown_model_palace  # noqa: F401  (fixture)
from test_openai_compat_identity import _serve, _use


def _MINILM(test):
    test = pytest.mark.usefixtures("unknown_model_palace")(test)
    return pytest.mark.parametrize("unknown_model_palace", ["minilm"], indirect=True)(test)


def _palace(request):
    return request.getfixturevalue("unknown_model_palace")


def _recorded(palace):
    return json.loads((palace / "mempalace_embedder.json").read_text())


def _forget_validated():
    from mempalace import palace as palace_mod

    palace_mod._VALIDATED_IDENTITY.clear()


def _run_cli(monkeypatch, *argv):
    import sys

    from mempalace.cli import main

    monkeypatch.setattr(sys, "argv", ["mempalace", *argv])
    try:
        main()
    except SystemExit as exc:
        return exc.code or 0
    return 0


# ── a first write records the real dimension ─────────────────────────────


@_MINILM
@pytest.mark.parametrize("model", ["minilm", "openai-compat"])
def test_mine_and_rebuild_record_the_same_dimension(request, monkeypatch, model):
    from mempalace.miner import mine
    from mempalace.repair import rebuild_from_sqlite

    project, palace, _ = _palace(request)
    expected = fallback._DIM
    server = None
    if model == "openai-compat":
        server, url = _serve()
        _use(monkeypatch, url, "fake-wide-d768")
        expected = 768
    try:
        mine(str(project), str(palace))
        mined = _recorded(palace)
        assert set(mined) == {"mempalace_drawers", "mempalace_closets"}, mined
        for entry in mined.values():
            assert entry["dimension"] == expected, mined

        _forget_validated()
        rebuild_from_sqlite(str(palace), str(palace), archive_existing_dest=True)
        assert _recorded(palace) == mined
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()


def test_sqlite_exact_first_write_records_the_dimension(tmp_path, monkeypatch):
    """sqlite_exact reads the dimension from its table; the open still probes
    once so every backend records the same way."""
    from mempalace import palace as P

    monkeypatch.setenv("MEMPALACE_BACKEND", "sqlite_exact")
    server, url = _serve()
    try:
        _use(monkeypatch, url, "fake-a-384")
        monkeypatch.setattr(embedding, "_DIM_CACHE", {})
        _forget_validated()
        col = P.get_collection(str(tmp_path), create=True)
        col.upsert(documents=["the shed roof leaks"], ids=["a"], metadatas=[{"wing": "w"}])
        stored = col.get_stored_embedder_identity()
    finally:
        server.shutdown()
        server.server_close()
    assert (stored.model_name, stored.dimension) == ("openai-compat:fake-a-384", 384)


def test_a_failed_probe_is_not_cached(monkeypatch):
    """A dead endpoint records dimension 0 (unknown) but does not pin it."""
    monkeypatch.setattr(embedding, "_DIM_CACHE", {})
    monkeypatch.setattr(embedding, "_EF_CACHE", {})
    _use(monkeypatch, "http://127.0.0.1:9", "fake-a-384")
    assert embedding.probe_dimension() == 0
    server, url = _serve()
    try:
        _use(monkeypatch, url, "fake-a-384")
        assert embedding.probe_dimension() == 384
    finally:
        server.shutdown()
        server.server_close()


@_MINILM
def test_set_embedder_records_a_bundled_models_width_without_loading_it(
    request, monkeypatch, capfd
):
    from mempalace.miner import mine

    project, palace, _ = _palace(request)
    server, url = _serve()
    try:
        _use(monkeypatch, url, "fake-a-384")
        mine(str(project), str(palace))
        fallback._MiniLMStandIn.instances = []
        argv = ["--palace", str(palace), "palace", "set-embedder", "--model", "minilm", "--force"]
        assert _run_cli(monkeypatch, *argv) == 0
    finally:
        server.shutdown()
        server.server_close()
    assert "minilm (dim=384)" in capfd.readouterr().out
    assert _recorded(palace)["mempalace_drawers"] == {"model_name": "minilm", "dimension": 384}
    assert fallback._MiniLMStandIn.instances == []


# ── chroma dimension errors are typed ────────────────────────────────────


@pytest.fixture
def raw_chroma(tmp_path):
    from mempalace.backends.chroma import ChromaCollection

    client = chromadb.PersistentClient(path=str(tmp_path / "chroma"))
    col = client.get_or_create_collection("mempalace_drawers", embedding_function=None)
    wrapped = ChromaCollection(col)
    wrapped.add(documents=["a"], ids=["a"], metadatas=[{"w": "x"}], embeddings=[[0.1] * 4])
    return wrapped


@pytest.mark.parametrize("op", ["add", "upsert", "update", "query"])
def test_chroma_dimension_rejections_are_typed(raw_chroma, op):
    wide = [[0.1] * 6]
    with pytest.raises(DimensionMismatchError) as excinfo:
        if op == "query":
            raw_chroma.query(query_embeddings=wide, n_results=1)
        elif op == "update":
            raw_chroma.update(ids=["a"], embeddings=wide)
        else:
            getattr(raw_chroma, op)(
                documents=["b"], ids=["b"], metadatas=[{"w": "x"}], embeddings=wide
            )
    message = str(excinfo.value)
    assert "chroma collection 'mempalace_drawers' expects embedding dimension 4, got 6" in message
    assert "repair rebuild-index" in message
    assert raw_chroma.count() == 1


def test_other_chroma_errors_pass_through(raw_chroma):
    with pytest.raises(Exception) as excinfo:
        raw_chroma.update(ids=["a"], embeddings=[[0.1] * 4, [0.2] * 4])
    assert not isinstance(excinfo.value, DimensionMismatchError)


def test_the_hint_does_not_offer_rebuild_index_off_chroma(tmp_path):
    from mempalace.backends.sqlite_exact import SQLiteExactBackend

    col = SQLiteExactBackend().get_collection(
        palace=PalaceRef(id=str(tmp_path), local_path=str(tmp_path)),
        collection_name="mempalace_drawers",
        create=True,
    )
    col.add(documents=["a"], ids=["a"], metadatas=[{}], embeddings=[[0.1] * 4])
    with pytest.raises(DimensionMismatchError) as excinfo:
        col.add(documents=["b"], ids=["b"], metadatas=[{}], embeddings=[[0.1] * 6])
    message = str(excinfo.value)
    assert "expects embedding dimension 4, got 6" in message
    assert "`mempalace repair rebuild-index` is Chroma-only" in message


@_MINILM
@pytest.mark.parametrize("backend", ["chroma", "sqlite_exact"])
def test_a_width_change_refuses_cleanly_on_every_path(request, monkeypatch, capfd, kg, backend):
    """Matrix S3a/S3b: the stand-in keeps its name and changes width, as a
    legacy palace whose record carries no dimension meets another-width model.
    Chroma printed a traceback from mine and 'Search error' on stdout from
    search; MCP add_drawer returned success:false without isError."""
    from mempalace.miner import mine

    monkeypatch.setenv("MEMPALACE_BACKEND", backend)
    project, palace, _ = _palace(request)
    mine(str(project), str(palace))
    monkeypatch.setattr(fallback, "_DIM", fallback._DIM * 2)
    _forget_validated()
    (project / "notes" / "shed.md").write_text("The shed roof leaks near the bench.\n" * 8)
    capfd.readouterr()

    assert _run_cli(monkeypatch, "--palace", str(palace), "mine", str(project)) == 1
    err = capfd.readouterr().err
    assert "expects embedding dimension 384, got 768" in err, err
    assert "Traceback" not in err and "InvalidArgumentError" not in err

    assert _run_cli(monkeypatch, "--palace", str(palace), "search", "greenhouse") == 1
    out, err = capfd.readouterr()
    assert "expects embedding dimension 384, got 768" in err, (out, err)
    assert "expects embedding dimension" not in out and "Search error" not in out, out

    call = _mcp_caller(monkeypatch, palace, kg)
    result, body = call(
        "mempalace_add_drawer", {"wing": "garden", "room": "notes", "content": "Compost."}
    )
    assert result.get("isError") is True, body
    assert body["error"] == "Embedding dimension mismatch", body
    result, body = call("mempalace_search", {"query": "greenhouse tomatoes"})
    assert result.get("isError") is True, body
    assert body["error"] == "Embedding dimension mismatch", body


def test_the_rebuild_record_warning_quotes_the_palace_path(tmp_path, monkeypatch, capsys):
    """A copy-pasted command must survive a palace path with spaces."""
    from mempalace import repair
    from mempalace.backends.base import EmbedderIdentityRecordError

    class _Unrecordable:
        def set_embedder_identity(self, identity):
            raise EmbedderIdentityRecordError("disk full")

    _use(monkeypatch, "http://unused", "Nomic Embed:latest")
    monkeypatch.setattr(embedding, "probe_dimension", lambda *a, **k: 768)
    palace = tmp_path / "my palace"
    repair._record_rebuilt_embedder_identity(_Unrecordable(), str(palace))
    out = capsys.readouterr().out
    assert f"mempalace --palace '{palace}' palace set-embedder" in out, out
    assert "--model 'openai-compat:Nomic Embed:latest'" in out, out
