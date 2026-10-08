"""Error paths around a failing embedder (patch 1, items 1-9).

Each test pins one refusal: what it says, which channel it uses (one
``mempalace:`` line on the CLI, ``isError`` on MCP), and that nothing is
written or deleted when the write it belongs to fails.
"""

import re

import pytest

from test_embedding_model_fallback import (
    _mcp_caller,
    _openai_compat_palace_with_a_dead_endpoint,
    unknown_model_palace,  # noqa: F401  (fixture)
)


def _MINILM(test):
    test = pytest.mark.usefixtures("unknown_model_palace")(test)
    return pytest.mark.parametrize("unknown_model_palace", ["minilm"], indirect=True)(test)


def _dead_endpoint_palace(request, monkeypatch):
    palace = request.getfixturevalue("unknown_model_palace")
    return _openai_compat_palace_with_a_dead_endpoint(palace, monkeypatch)


def _run_cli(monkeypatch, *argv):
    import sys

    from mempalace.cli import main

    monkeypatch.setattr(sys, "argv", ["mempalace", *argv])
    try:
        main()
    except SystemExit as exc:
        return exc.code or 0
    return 0


def _closet_count(palace) -> int:
    from mempalace.palace import get_closets_collection

    return get_closets_collection(str(palace), create=False).count()


def _a_mined_drawer(palace):
    from mempalace.palace import get_collection

    got = get_collection(str(palace), create=False).get(include=["metadatas"], limit=1)
    assert got["ids"] and got["metadatas"][0].get("source_file"), got
    return got["ids"][0]


# ── (1) update_drawer keeps the closets when the update fails ────────────


@_MINILM
@pytest.mark.parametrize(
    "content",
    ["Rewritten note.", "The rewritten note runs long. " * 60],
    ids=["update", "rechunk-upsert"],
)
def test_a_failed_update_drawer_keeps_the_source_closets(request, monkeypatch, kg, content):
    """MCP update_drawer purged the source file's closets before the write,
    so a dead endpoint left the drawer unchanged and its closets gone."""
    palace = _dead_endpoint_palace(request, monkeypatch)
    closets = _closet_count(palace)
    assert closets > 0
    drawer_id = _a_mined_drawer(palace)

    call = _mcp_caller(monkeypatch, palace, kg)
    result, body = call("mempalace_update_drawer", {"drawer_id": drawer_id, "content": content})
    assert result.get("isError") is True, body
    assert body["error_class"] == "EmbeddingAPIError", body
    assert _closet_count(palace) == closets


# ── (2) no chromadb " in upsert." tail on the error text ─────────────────

_CHROMA_TAIL = re.compile(r" in (?:add|get|query|update|upsert|delete)\.$")


@_MINILM
def test_embedder_errors_lose_chromadbs_method_suffix(request, monkeypatch, kg, capfd):
    """chromadb appends " in <method>." to any error raised inside a collection
    call, so the text read '...embedding_api_url is correct. in upsert.'."""
    palace = _dead_endpoint_palace(request, monkeypatch)
    drawer_id = _a_mined_drawer(palace)
    call = _mcp_caller(monkeypatch, palace, kg)
    for name, arguments in (
        ("mempalace_add_drawer", {"wing": "garden", "room": "notes", "content": "Compost."}),
        ("mempalace_update_drawer", {"drawer_id": drawer_id, "content": "Rewritten note."}),
        ("mempalace_search", {"query": "greenhouse tomatoes"}),
    ):
        result, body = call(name, arguments)
        assert result.get("isError") is True, (name, body)
        assert body["details"].endswith("is correct."), (name, body["details"])
        assert not _CHROMA_TAIL.search(body["details"]), (name, body["details"])

    from mempalace.palace import get_collection

    col = get_collection(str(palace), create=False)
    for method, kwargs in (
        ("add", {"ids": ["x"], "documents": ["Compost."]}),
        ("upsert", {"ids": ["x"], "documents": ["Compost."]}),
        ("update", {"ids": [drawer_id], "documents": ["Compost."]}),
        ("query", {"query_texts": ["compost"], "n_results": 1}),
    ):
        with pytest.raises(Exception) as caught:
            getattr(col, method)(**kwargs)
        assert str(caught.value).endswith("is correct."), (method, str(caught.value))


# ── (3) a failed first mine leaves no palace folder behind ───────────────


def _dead_endpoint_config(project_palace_config, monkeypatch):
    import json
    import socket

    from mempalace import embedding

    _, palace, _ = project_palace_config
    with socket.socket() as sock:  # a port nothing listens on
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    monkeypatch.delenv("MEMPALACE_EMBEDDING_API_URL", raising=False)
    monkeypatch.delenv("MEMPALACE_EMBEDDING_API_MODEL", raising=False)
    (palace.parent / "config" / "config.json").write_text(
        json.dumps(
            {
                "palace_path": str(palace),
                "embedding_model": "openai-compat",
                "embedding_api_url": f"http://127.0.0.1:{port}/v1",
                "embedding_api_model": "text-embed-a",
            }
        )
    )
    embedding._EF_CACHE.clear()


@_MINILM
@pytest.mark.parametrize("backend", ["chroma", "sqlite_exact"])
@pytest.mark.parametrize("existed", [False, True], ids=["new-folder", "existing-folder"])
def test_a_failed_first_mine_removes_only_the_folder_it_created(
    request, monkeypatch, capfd, backend, existed
):
    """A mine on a dead endpoint left an empty palace folder (a database file
    and an identity record, no drawers) behind. A folder the mine created is
    removed; one that was already there is left alone."""
    project, palace, _ = request.getfixturevalue("unknown_model_palace")
    _dead_endpoint_config((project, palace, None), monkeypatch)
    monkeypatch.setenv("MEMPALACE_BACKEND", backend)
    if existed:
        palace.mkdir()
    capfd.readouterr()

    assert _run_cli(monkeypatch, "--palace", str(palace), "mine", str(project)) == 1
    err = capfd.readouterr().err
    assert "mempalace: Embedding API request to http://127.0.0.1:" in err, err
    assert "Traceback" not in err
    assert palace.exists() is existed


@_MINILM
def test_a_new_palace_that_got_drawers_before_the_failure_is_kept(request, monkeypatch, capfd):
    """Drawers filed before the endpoint died stay, and so does their folder."""
    from test_openai_compat_identity import _serve, _use

    from mempalace import embedding, miner

    project, palace, _ = request.getfixturevalue("unknown_model_palace")
    server, url = _serve()
    _use(monkeypatch, url, "text-embed-a")
    real_mine = miner.mine

    def mine_then_die(*args, **kwargs):
        real_mine(*args, **kwargs)
        raise embedding.EmbeddingAPIError("Embedding API request failed mid-mine.")

    monkeypatch.setattr(miner, "mine", mine_then_die)
    try:
        assert _run_cli(monkeypatch, "--palace", str(palace), "mine", str(project)) == 1
    finally:
        server.shutdown()
        server.server_close()
    assert "mempalace: Embedding API request failed mid-mine." in capfd.readouterr().err
    assert palace.is_dir()
    from mempalace.palace import get_collection

    assert get_collection(str(palace), create=False, _skip_identity_check=True).count() > 0


# ── (4) read-only opens do not migrate or create ─────────────────────────


def _folder_state(palace):
    import hashlib

    return {
        str(f.relative_to(palace)): hashlib.sha256(f.read_bytes()).hexdigest()
        for f in sorted(palace.rglob("*"))
        if f.is_file() and f.name.startswith(".")
    }


@_MINILM
def test_a_cold_search_of_a_fresh_palace_runs_no_migration(request, monkeypatch, caplog):
    """chromadb 1.5.x before 1.5.9 creates collections with
    ``config_json_str='{}'``, so the next open of a fresh palace logged "Fixed
    N collection(s) missing _type …", rewrote chroma.sqlite3 and wrote two
    migration markers. Only chromadb 1.5.9+ needs ``_type`` (and writes it on
    create), and a palace created by chromadb 1.x has no 0.6 BLOB seq_ids, so
    a cold read of a fresh palace has nothing to migrate."""
    import logging

    from mempalace.backends.registry import reset_backends
    from mempalace.miner import mine
    from mempalace.searcher import search_memories

    project, palace, _ = request.getfixturevalue("unknown_model_palace")
    mine(str(project), str(palace))
    reset_backends()  # drop the in-process client so the next open is cold
    markers = _folder_state(palace)
    with caplog.at_level(logging.INFO, logger="mempalace.backends.chroma"):
        search_memories("greenhouse tomatoes", str(palace))
    assert "missing _type" not in caplog.text
    assert _folder_state(palace) == markers


@_MINILM
def test_read_only_tools_do_not_create_a_database_in_an_empty_folder(request, monkeypatch, kg):
    """MCP status and search on an existing, empty palace folder created
    chroma.sqlite3 (a PersistentClient opened to look for a collection)."""
    _, palace, _ = request.getfixturevalue("unknown_model_palace")
    palace.mkdir()
    call = _mcp_caller(monkeypatch, palace, kg)
    call("mempalace_status", {})
    call("mempalace_search", {"query": "greenhouse tomatoes"})
    call("mempalace_list_drawers", {})
    assert sorted(p.name for p in palace.iterdir()) == []

    from mempalace.backends.base import CollectionNotInitializedError
    from mempalace.palace import get_collection

    with pytest.raises(CollectionNotInitializedError):
        get_collection(str(palace), create=False)
    assert sorted(p.name for p in palace.iterdir()) == []


@pytest.mark.parametrize(
    ("version", "required"),
    [("1.5.7", False), ("1.5.8", False), ("1.5.9", True), ("1.6.0", True), ("2.0.0rc1", True)],
)
def test_the_type_migration_runs_only_for_chromadb_that_needs_it(monkeypatch, version, required):
    from mempalace.backends import chroma

    monkeypatch.setattr(chroma.chromadb, "__version__", version)
    assert chroma._chromadb_requires_collection_type() is required


# ── (5) repair probes the embedder before it archives anything ───────────


@_MINILM
@pytest.mark.parametrize("argv", [["repair", "rebuild-index"], ["repair", "--yes"]])
def test_repair_on_a_dead_endpoint_refuses_before_archiving(request, monkeypatch, capfd, argv):
    """rebuild-index archived the palace, then failed at the first upsert;
    legacy repair backed it up and dropped the collection first."""
    import hashlib

    palace = _dead_endpoint_palace(request, monkeypatch)
    siblings = sorted(p.name for p in palace.parent.iterdir())
    db = hashlib.sha256((palace / "chroma.sqlite3").read_bytes()).hexdigest()
    monkeypatch.setattr("builtins.input", lambda *a: "y")
    capfd.readouterr()

    assert _run_cli(monkeypatch, "--palace", str(palace), *argv) == 1
    out, err = capfd.readouterr()
    assert "mempalace: Embedding API request to http://127.0.0.1:" in err, (out, err)
    assert "Traceback" not in out + err
    assert sorted(p.name for p in palace.parent.iterdir()) == siblings
    assert hashlib.sha256((palace / "chroma.sqlite3").read_bytes()).hexdigest() == db


@_MINILM
def test_the_rebuild_library_calls_probe_the_embedder_first(request, monkeypatch):
    from mempalace import embedding, repair

    palace = _dead_endpoint_palace(request, monkeypatch)
    siblings = sorted(p.name for p in palace.parent.iterdir())
    with pytest.raises(embedding.EmbeddingAPIError):
        repair.rebuild_from_sqlite(
            source_palace=str(palace), dest_palace=str(palace), archive_existing_dest=True
        )
    with pytest.raises(embedding.EmbeddingAPIError):
        repair.rebuild_index(str(palace), progress=lambda *_: None)
    assert sorted(p.name for p in palace.parent.iterdir()) == siblings


# ── (6) MCP mine flags a model refusal, without a traceback ──────────────


@pytest.mark.usefixtures("unknown_model_palace")
@pytest.mark.parametrize("unknown_model_palace", ["embeddinggemm2"], indirect=True)
def test_mcp_mine_with_a_near_miss_model_is_a_tool_error(request, monkeypatch, kg, caplog, capfd):
    project, palace, _ = request.getfixturevalue("unknown_model_palace")
    call = _mcp_caller(monkeypatch, palace, kg)
    result, body = call("mempalace_mine", {"source": str(project)})
    assert result.get("isError") is True, body
    assert body["error_class"] == "UnknownEmbeddingModelError", body
    assert "did you mean" in body["details"], body
    assert "Traceback" not in caplog.text + "".join(capfd.readouterr())
    assert not palace.exists()

    capfd.readouterr()
    assert _run_cli(monkeypatch, "--palace", str(palace), "mine", str(project)) == 1
    err = capfd.readouterr().err
    assert err.startswith("mempalace: ") and "did you mean" in err, err
    assert "Traceback" not in err


@_MINILM
def test_mcp_mine_on_a_dead_endpoint_logs_no_traceback(request, monkeypatch, kg, caplog):
    """tool_mine logged the full chained traceback of every refused mine."""
    palace = _dead_endpoint_palace(request, monkeypatch)
    project = palace.parent / "project"
    (project / "notes" / "shed.md").write_text("The shed roof leaks near the bench.\n" * 8)
    call = _mcp_caller(monkeypatch, palace, kg)
    result, body = call("mempalace_mine", {"source": str(project)})
    assert result.get("isError") is True, body
    assert body["error_class"] == "EmbeddingAPIError", body
    assert body["error"] == "Embedding API unavailable", body
    assert "Traceback" not in caplog.text
    assert not [r for r in caplog.records if r.exc_info], [r.getMessage() for r in caplog.records]


# ── (7) the remaining error paths: one line, no traceback, no exit 0 ─────


def _one_document(monkeypatch, src):
    from mempalace import format_miner as format_mod

    src.mkdir(exist_ok=True)
    doc = src / "notes.docx"
    doc.write_bytes(b"PK\x03\x04stub")
    text = "The quarterly review covers memory palace retention. " * 20
    monkeypatch.setattr(format_mod, "scan_formats", lambda *_a, **_k: [doc])
    monkeypatch.setattr(
        format_mod, "extract_text", lambda *_a, **_k: (text, format_mod.ExtractionStatus.OK)
    )


@_MINILM
def test_mine_extract_on_a_dead_endpoint_fails_like_the_other_modes(
    request, monkeypatch, capfd, kg
):
    """The format miner caught the refusal as a per-file or outer-loop error,
    printed "Mine aborted by exception" and returned: CLI exit 0, MCP success."""
    palace = _dead_endpoint_palace(request, monkeypatch)
    docs = palace.parent / "docs"
    _one_document(monkeypatch, docs)
    capfd.readouterr()

    rc = _run_cli(monkeypatch, "--palace", str(palace), "mine", str(docs), "--mode", "extract")
    out, err = capfd.readouterr()
    assert rc == 1, (out, err)
    assert "mempalace: Embedding API request to http://127.0.0.1:" in err, err
    assert "Traceback" not in out + err

    call = _mcp_caller(monkeypatch, palace, kg)
    result, body = call("mempalace_mine", {"source": str(docs), "mode": "extract"})
    assert result.get("isError") is True, body
    assert body["error_class"] == "EmbeddingAPIError", body


@_MINILM
def test_sweep_refuses_a_model_error_in_one_line(request, monkeypatch, capfd):
    import json

    palace = _dead_endpoint_palace(request, monkeypatch)
    config = palace.parent / "config" / "config.json"
    settings = json.loads(config.read_text())
    settings.pop("embedding_api_url")  # openai-compat without an endpoint
    config.write_text(json.dumps(settings))
    from mempalace import embedding

    embedding._EF_CACHE.clear()
    transcript = palace.parent / "session.jsonl"
    transcript.write_text(
        json.dumps({"type": "user", "message": {"role": "user", "content": "Water the tomatoes."}})
        + "\n"
        + json.dumps(
            {"type": "assistant", "message": {"role": "assistant", "content": "Done at nine."}}
        )
        + "\n"
    )
    capfd.readouterr()
    rc = _run_cli(monkeypatch, "--palace", str(palace), "sweep", "--direct", str(transcript))
    out, err = capfd.readouterr()
    assert rc == 1, (out, err)
    assert err.startswith("mempalace: Could not build the embedding function"), err
    assert "Traceback" not in out + err


@_MINILM
def test_repair_without_an_endpoint_url_refuses_in_one_line(request, monkeypatch, capfd):
    import json

    from mempalace import embedding

    palace = _dead_endpoint_palace(request, monkeypatch)
    config = palace.parent / "config" / "config.json"
    settings = json.loads(config.read_text())
    settings.pop("embedding_api_url")
    config.write_text(json.dumps(settings))
    embedding._EF_CACHE.clear()
    capfd.readouterr()
    assert _run_cli(monkeypatch, "--palace", str(palace), "repair", "rebuild-index", "--yes") == 1
    out, err = capfd.readouterr()
    assert err.startswith("mempalace: Could not build the embedding function"), err
    assert "Traceback" not in out + err


# ── (8) every set-embedder hint names a model the command accepts ────────

_GEMMA2 = "embeddinggemma2"


def _suggested_names(text):
    """The `--model X` and `MEMPALACE_EMBEDDING_MODEL=X` values a hint suggests."""
    import re
    import shlex

    models = [shlex.split(m)[0] for m in re.findall(r"--model ('[^']*'|[^\s`]+)", text)]
    models = [m for m in models if m != "<model>"]
    env = re.findall(r"MEMPALACE_EMBEDDING_MODEL=([^\s`]+)", text)
    return models, env


def _assert_accepted(models, env):
    from mempalace.embedding import _resolve_embedding_model

    for name in models:  # set-embedder --model: a known name or openai-compat:<id>
        if not name.startswith("openai-compat:"):
            _resolve_embedding_model(name)  # UnknownEmbeddingModelError on a near miss
    for name in env:  # a configured model: never a recorded identity
        _resolve_embedding_model(name)
        assert ":" not in name, name


@pytest.mark.parametrize("recorded", [_GEMMA2, "openai-compat:text-embed-a"])
def test_set_embedders_align_hint_suggests_a_configurable_model(
    recorded, tmp_path, monkeypatch, capsys
):
    """Recording EmbeddingGemma 2 with MiniLM configured told the user to set
    MEMPALACE_EMBEDDING_MODEL to the full identity, which config refuses."""
    import json

    from mempalace import embedding

    cfg = tmp_path / "cfg"
    cfg.mkdir()
    palace = tmp_path / "palace"
    (cfg / "config.json").write_text(json.dumps({"palace_path": str(palace)}))
    monkeypatch.setenv("MEMPALACE_CONFIG_DIR", str(cfg))
    for var in ("MEMPALACE_EMBEDDING_MODEL", "MEMPALACE_EMBEDDING_API_URL"):
        monkeypatch.delenv(var, raising=False)
    embedding._EF_CACHE.clear()
    if recorded.startswith("openai-compat:"):
        # Recording an endpoint model probes its width; no server here.
        monkeypatch.setattr(embedding, "known_dimension", lambda *_a, **_k: 384, raising=False)
    rc = _run_cli(
        monkeypatch, "--palace", str(palace), "palace", "set-embedder", "--model", recorded
    )
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "configured model is" in out, out
    models, env = _suggested_names(out)
    assert env or "MEMPALACE_EMBEDDING_API_MODEL=" in out, out
    _assert_accepted(models, env)


@pytest.mark.parametrize(
    "configured", [_GEMMA2, "openai-compat:text-embed-a", "all-minilm-l6-v2", "embeddinggemma"]
)
def test_the_confirm_hint_suggests_a_model_set_embedder_accepts(configured, tmp_path):
    from mempalace.embedding import current_model_name
    from mempalace.palace import _confirm_model_hint

    name = current_model_name(_GEMMA2) if configured == _GEMMA2 else configured
    models, env = _suggested_names(_confirm_model_hint(tmp_path / "my palace", name))
    assert models, name
    _assert_accepted(models, env)


def test_the_rebuild_record_failure_hint_suggests_an_accepted_model(tmp_path, monkeypatch, capsys):
    from mempalace import embedding, repair
    from mempalace.backends.base import EmbedderIdentity, EmbedderIdentityRecordError

    identity = EmbedderIdentity(model_name=embedding.current_model_name(_GEMMA2), dimension=768)
    monkeypatch.setattr(embedding, "get_embedder_identity", lambda *_a, **_k: identity)

    class _Unrecordable:
        def set_embedder_identity(self, _identity):
            raise EmbedderIdentityRecordError("the sidecar is read-only")

    repair._record_rebuilt_embedder_identity(_Unrecordable(), str(tmp_path / "my palace"))
    models, env = _suggested_names(capsys.readouterr().out)
    assert models == [_GEMMA2], models
    _assert_accepted(models, env)
