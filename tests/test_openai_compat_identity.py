"""The openai-compat embedder identity names the endpoint model (patch 1, D).

Every openai-compat palace used to record the bare ``openai-compat``, so
switching ``embedding_api_model`` to another model of the same dimension was
accepted on every backend and filed vectors from two models in one palace
(matrix F1 / S2b, scope S1). The identity is now
``openai-compat:<embedding_api_model>``; the endpoint URL is not part of it.

A legacy bare stamp cannot say which model embedded the vectors, so it is
handled like a missing record (option B): reads warn, a write into an empty
collection records the configured model, a write into a populated one
refuses until ``palace set-embedder`` confirms the model.

These tests drive the real factory against a small in-process
OpenAI-compatible ``/v1/embeddings`` server.
"""

import hashlib
import json
import threading
import warnings
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from mempalace import embedding
from mempalace.backends.base import (
    EmbedderIdentity,
    EmbedderIdentityMismatchError,
    EmbedderIdentityUnconfirmedError,
    EmbedderIdentityUnknownWarning,
    PalaceRef,
)

from test_embedding_model_fallback import _ADD, _mcp_caller
from test_embedding_model_fallback import unknown_model_palace  # noqa: F401  (fixture)


class _Embeddings(BaseHTTPRequestHandler):
    """Deterministic per (model, word); a model id ending ``-d<N>`` is N-wide."""

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        model = body["model"]
        texts = body["input"] if isinstance(body["input"], list) else [body["input"]]
        dim = int(model.rsplit("-d", 1)[1]) if "-d" in model else 384
        data = []
        for index, text in enumerate(texts):
            vec = [0.0] * dim
            for word in str(text).lower().split() or [""]:
                digest = hashlib.sha256(f"{model}|{word}".encode()).digest()
                vec[int.from_bytes(digest[:4], "little") % dim] += 1.0
            data.append({"index": index, "embedding": vec, "object": "embedding"})
        out = json.dumps({"data": data, "model": model, "object": "list"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


def _serve():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Embeddings)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


@pytest.fixture
def endpoint():
    server, url = _serve()
    yield url
    server.shutdown()
    server.server_close()


def _use(monkeypatch, url, api_model):
    monkeypatch.setenv("MEMPALACE_EMBEDDING_MODEL", "openai-compat")
    monkeypatch.setenv("MEMPALACE_EMBEDDING_API_URL", url)
    monkeypatch.setenv("MEMPALACE_EMBEDDING_API_MODEL", api_model)


def _forget_validated():
    from mempalace import palace as palace_mod

    palace_mod._VALIDATED_IDENTITY.clear()


def _run_cli(monkeypatch, *argv):
    """Run the CLI; the exit code, or 0 when it returns without SystemExit."""
    import sys

    from mempalace.cli import main

    monkeypatch.setattr(sys, "argv", ["mempalace", *argv])
    try:
        main()
    except SystemExit as exc:
        return exc.code or 0
    return 0


def _count(palace) -> int:
    from mempalace import palace as P

    return P.get_collection(str(palace), create=False, _skip_identity_check=True).count()


def _recorded(palace, collection="mempalace_drawers"):
    from mempalace import palace as P

    col = P.get_collection(
        str(palace), collection_name=collection, create=False, _skip_identity_check=True
    )
    return col.get_stored_embedder_identity()


def _add_note(project, name):
    (project / "notes" / f"{name}.md").write_text(
        f"The {name} roof leaks near the potting bench every spring.\n" * 8
    )


@pytest.fixture
def openai_palace(request, monkeypatch, endpoint):
    """A palace configured openai-compat with ``fake-a-384``, on ``request.param``."""
    project, palace, config = request.getfixturevalue("unknown_model_palace")
    backend = getattr(request, "param", "chroma")
    monkeypatch.setenv("MEMPALACE_BACKEND", backend)
    _use(monkeypatch, endpoint, "fake-a-384")
    return project, palace, endpoint


def _OPENAI(*backends):
    """Run on an openai-compat palace for each backend in ``backends``."""

    def wrap(test):
        test = pytest.mark.parametrize("openai_palace", backends, indirect=True)(test)
        test = pytest.mark.usefixtures("unknown_model_palace")(test)
        return pytest.mark.parametrize("unknown_model_palace", ["minilm"], indirect=True)(test)

    return wrap


def _mine(monkeypatch, palace, project):
    return _run_cli(monkeypatch, "--palace", str(palace), "mine", str(project))


# ── the identity string ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "configured, expected",
    [
        ("fake-a-384", "openai-compat:fake-a-384"),
        ("  Qwen3-Embedding-0.6B  ", "openai-compat:Qwen3-Embedding-0.6B"),
        ("nomic-embed-text:latest", "openai-compat:nomic-embed-text:latest"),
        ("", "openai-compat"),
    ],
)
def test_the_identity_names_the_endpoint_model(monkeypatch, configured, expected):
    _use(monkeypatch, "http://unused", configured)
    assert embedding.current_model_name() == expected


@pytest.mark.parametrize(
    "stored, expected",
    [
        ("openai-compat:fake-a-384", "openai-compat:fake-a-384"),
        ("openai-compat:Nomic-Embed-Text:latest", "openai-compat:Nomic-Embed-Text:latest"),
        ("OpenAI-Compat:BGE-M3", "openai-compat:BGE-M3"),
        (" openai-compat: bge-m3 ", "openai-compat:bge-m3"),
        ("openai-compat", "openai-compat"),
        ("openai-compat:", "openai-compat"),
    ],
)
def test_a_recorded_endpoint_model_survives_normalization(stored, expected):
    """Without the prefix rule a full identity read back as legacy 'minilm'."""
    assert embedding._normalize_stored_model_name(stored) == expected


def test_the_probed_dimension_is_per_endpoint_model(monkeypatch, endpoint):
    monkeypatch.setattr(embedding, "_DIM_CACHE", {})
    monkeypatch.setattr(embedding, "_EF_CACHE", {})
    _use(monkeypatch, endpoint, "fake-a-384")
    assert embedding.probe_dimension() == 384
    _use(monkeypatch, endpoint, "fake-wide-d768")
    assert embedding.probe_dimension() == 768
    assert embedding.get_embedder_identity() == EmbedderIdentity(
        "openai-compat:fake-wide-d768", 768
    )


# ── S1/S2b: a same-dimension endpoint model swap refuses ─────────────────


@_OPENAI("chroma", "sqlite_exact")
def test_a_same_dimension_model_swap_refuses_and_switching_back_works(
    openai_palace, monkeypatch, capfd, kg
):
    project, palace, url = openai_palace
    assert _mine(monkeypatch, palace, project) == 0
    assert _recorded(palace).model_name == "openai-compat:fake-a-384"
    rows = _count(palace)

    _use(monkeypatch, url, "fake-b-384")
    _forget_validated()
    _add_note(project, "shed")
    capfd.readouterr()
    assert _mine(monkeypatch, palace, project) == 1
    err = capfd.readouterr().err
    assert "mempalace: " in err and "Traceback" not in err
    assert "'openai-compat:fake-a-384'" in err and "'openai-compat:fake-b-384'" in err
    assert _count(palace) == rows

    call = _mcp_caller(monkeypatch, palace, kg)
    result, body = call("mempalace_add_drawer", dict(_ADD))
    assert result.get("isError") is True, body
    assert body["error"] == "Embedder identity mismatch", body
    result, body = call("mempalace_search", {"query": "greenhouse tomatoes"})
    assert result.get("isError") is True, body
    assert _count(palace) == rows

    _use(monkeypatch, url, "fake-a-384")
    _forget_validated()
    assert _mine(monkeypatch, palace, project) == 0
    assert _count(palace) > rows


def _remote_collection(kind, tmp_path):
    """A qdrant / pgvector collection whose identity lives in the local marker."""
    ref = PalaceRef(id=str(tmp_path), local_path=str(tmp_path))
    if kind == "qdrant":
        from mempalace.backends.qdrant import QdrantBackend, QdrantCollection, _QdrantConfig

        backend = QdrantBackend()
        config = _QdrantConfig(url="http://localhost:6333", api_key=None, namespace=None)
        backend._write_marker(ref, config)
        return QdrantCollection(
            backend=backend,
            client=object(),
            config=config,
            palace=ref,
            collection_name="mempalace_drawers",
            remote_collection="mp_drawers_remote",
        )
    from mempalace.backends.pgvector import PgVectorBackend, PgVectorCollection, _PgVectorConfig

    backend = PgVectorBackend()
    config = _PgVectorConfig(dsn="postgresql://example", namespace=None)
    backend._write_marker(ref, config)
    return PgVectorCollection(
        backend=backend,
        client=object(),
        config=config,
        palace=ref,
        collection_name="mempalace_drawers",
        table="mp_drawers_t",
    )


@pytest.mark.parametrize("kind", ["qdrant", "pgvector"])
def test_a_model_swap_refuses_on_the_marker_backends(kind, tmp_path, monkeypatch, endpoint):
    """qdrant and pgvector (and milvus) record the identity in the shared
    sidecar marker: the record of a palace mined with A refuses B."""
    from mempalace import palace as P

    monkeypatch.setattr(embedding, "_DIM_CACHE", {})
    _use(monkeypatch, endpoint, "fake-a-384")
    col = _remote_collection(kind, tmp_path)
    col.set_embedder_identity(embedding.get_embedder_identity())
    _forget_validated()
    _use(monkeypatch, endpoint, "fake-b-384")
    with pytest.raises(EmbedderIdentityMismatchError, match="fake-b-384"):
        P._enforce_embedder_identity(col, str(tmp_path), "mempalace_drawers", create=False)
    with pytest.raises(EmbedderIdentityMismatchError):
        P._enforce_embedder_identity(col, str(tmp_path), "mempalace_drawers", create=True)


@_OPENAI("chroma")
def test_changing_only_the_endpoint_url_is_accepted(openai_palace, monkeypatch, capfd):
    """The URL is not part of the vector space: the same model from another
    host, port or tunnel produces the same vectors."""
    project, palace, url = openai_palace
    assert _mine(monkeypatch, palace, project) == 0
    rows = _count(palace)
    other, other_url = _serve()
    try:
        _use(monkeypatch, other_url, "fake-a-384")
        _forget_validated()
        _add_note(project, "shed")
        with warnings.catch_warnings():
            warnings.simplefilter("error", EmbedderIdentityUnknownWarning)
            assert _mine(monkeypatch, palace, project) == 0
    finally:
        other.shutdown()
        other.server_close()
    assert _count(palace) > rows
    assert _recorded(palace).model_name == "openai-compat:fake-a-384"


# ── legacy bare `openai-compat` stamps (option B) ────────────────────────


def _stamp_bare(palace):
    """Rewrite every record as builds before D left it: the bare name."""
    sidecar = palace / "mempalace_embedder.json"
    data = json.loads(sidecar.read_text())
    for entry in data.values():
        entry["model_name"] = "openai-compat"
    sidecar.write_text(json.dumps(data))
    _forget_validated()


@_OPENAI("chroma")
def test_a_bare_stamp_reads_with_a_warning_and_refuses_writes(
    openai_palace, monkeypatch, capfd, kg
):
    project, palace, _ = openai_palace
    assert _mine(monkeypatch, palace, project) == 0
    rows = _count(palace)
    _stamp_bare(palace)

    from mempalace.searcher import search_memories

    with pytest.warns(EmbedderIdentityUnknownWarning, match="without the endpoint model"):
        found = search_memories("greenhouse tomatoes watering", str(palace))
    assert found.get("results"), found

    _forget_validated()
    _add_note(project, "shed")
    capfd.readouterr()
    assert _mine(monkeypatch, palace, project) == 1
    err = capfd.readouterr().err
    assert "without the endpoint model" in err, err
    assert "palace set-embedder --model openai-compat:fake-a-384" in err, err
    assert "Traceback" not in err
    assert _count(palace) == rows

    call = _mcp_caller(monkeypatch, palace, kg)
    result, body = call("mempalace_add_drawer", dict(_ADD))
    assert result.get("isError") is True, body
    assert body["error"] == "Embedder identity unconfirmed", body
    assert _count(palace) == rows
    assert _recorded(palace).model_name == "openai-compat"


@_OPENAI("chroma")
def test_set_embedder_upgrades_a_bare_stamp_without_force(openai_palace, monkeypatch, capfd):
    project, palace, _ = openai_palace
    assert _mine(monkeypatch, palace, project) == 0
    _stamp_bare(palace)
    capfd.readouterr()

    code = _run_cli(
        monkeypatch, "--palace", str(palace), "palace", "set-embedder", "--model", "openai-compat"
    )
    out = capfd.readouterr().out
    assert code == 0, out
    assert "openai-compat → openai-compat:fake-a-384" in out, out
    assert "configured model is" not in out, out
    for name in ("mempalace_drawers", "mempalace_closets"):
        assert _recorded(palace, name).model_name == "openai-compat:fake-a-384"

    _forget_validated()
    _add_note(project, "shed")
    assert _mine(monkeypatch, palace, project) == 0


@_OPENAI("sqlite_exact")
def test_a_bare_stamp_on_an_empty_collection_records_the_model(openai_palace, tmp_path):
    from mempalace import palace as P
    from mempalace.backends.sqlite_exact import SQLiteExactBackend

    _, palace, _ = openai_palace
    col = SQLiteExactBackend().get_collection(
        palace=PalaceRef(id=str(palace), local_path=str(palace)),
        collection_name="mempalace_drawers",
        create=True,
    )
    col.set_embedder_identity(EmbedderIdentity("openai-compat", 0))
    _forget_validated()
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        P.get_collection(str(palace), create=True)
    assert _recorded(palace).model_name == "openai-compat:fake-a-384"


# ── set-embedder --model openai-compat:<id> ──────────────────────────────


@_OPENAI("chroma")
def test_set_embedder_records_an_explicit_endpoint_model_as_written(
    openai_palace, monkeypatch, capfd
):
    """Confirm the model an old palace was built with, without changing the
    config: the configured model then refuses as a swap."""
    project, palace, _ = openai_palace
    assert _mine(monkeypatch, palace, project) == 0
    _stamp_bare(palace)
    capfd.readouterr()

    argv = ["--palace", str(palace), "palace", "set-embedder", "--model"]
    assert _run_cli(monkeypatch, *argv, "openai-compat:Old-Model:v1") == 0
    out = capfd.readouterr().out
    assert "openai-compat:Old-Model:v1" in out, out
    assert "MEMPALACE_EMBEDDING_API_MODEL=Old-Model:v1" in out, out
    assert _recorded(palace).model_name == "openai-compat:Old-Model:v1"

    _forget_validated()
    _add_note(project, "shed")
    capfd.readouterr()
    assert _mine(monkeypatch, palace, project) == 1
    assert "'openai-compat:Old-Model:v1'" in capfd.readouterr().err

    # A recorded endpoint model is a real identity: replacing it needs --force.
    capfd.readouterr()
    assert _run_cli(monkeypatch, *argv, "openai-compat") == 2
    assert "--force" in capfd.readouterr().out
    assert _run_cli(monkeypatch, *argv, "openai-compat", "--force") == 0
    assert _recorded(palace).model_name == "openai-compat:fake-a-384"


@_OPENAI("chroma")
def test_set_embedder_refuses_openai_compat_without_an_endpoint_model(
    openai_palace, monkeypatch, capfd
):
    project, palace, url = openai_palace
    assert _mine(monkeypatch, palace, project) == 0
    _use(monkeypatch, url, "")
    capfd.readouterr()
    code = _run_cli(
        monkeypatch, "--palace", str(palace), "palace", "set-embedder", "--model", "openai-compat"
    )
    out = capfd.readouterr().out
    assert code == 2, out
    assert "no endpoint model to record" in out
    assert _recorded(palace).model_name == "openai-compat:fake-a-384"


# ── a deliberate model change goes through rebuild ───────────────────────


@_OPENAI("chroma")
def test_a_rebuild_with_the_new_model_records_it(openai_palace, monkeypatch):
    from mempalace.repair import rebuild_from_sqlite
    from mempalace.searcher import search_memories

    project, palace, url = openai_palace
    assert _mine(monkeypatch, palace, project) == 0
    _use(monkeypatch, url, "fake-b-384")
    _forget_validated()
    rebuild_from_sqlite(str(palace), str(palace), archive_existing_dest=True)
    assert _recorded(palace).model_name == "openai-compat:fake-b-384"
    _forget_validated()
    with warnings.catch_warnings():
        warnings.simplefilter("error", EmbedderIdentityUnknownWarning)
        found = search_memories("greenhouse tomatoes watering", str(palace))
    assert found.get("results"), found


def test_the_unconfirmed_error_is_a_mismatch_for_callers():
    assert issubclass(EmbedderIdentityUnconfirmedError, EmbedderIdentityMismatchError)
