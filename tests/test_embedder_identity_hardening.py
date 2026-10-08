"""Embedder identity hardening (3.11.x patch 1).

A collection whose embedder identity cannot be confirmed refuses writes and
keeps reads working with a warning:

* no recorded identity while the collection holds vectors (a palace from
  before identity tracking, a lost sidecar): a write could add vectors from
  another model of the same dimension, and nothing would notice afterwards
  (scope S3, matrix S4b/S4d);
* an unreadable record (a truncated ``mempalace_embedder.json``): it used to
  read as "nothing recorded" and the write went through (scope S4).

An empty collection with no record still records the current model on its
first write. ``palace set-embedder`` is the confirmation: it records the
model for every collection, including over an unreadable record.

The module is listed in ``conftest._REAL_EMBEDDING_TEST_MODULES``; the MiniLM
ONNX class is replaced by the deterministic stand-in of
``test_embedding_model_fallback``.
"""

import json
import sqlite3
import warnings

import pytest

from mempalace.backends.base import (
    EmbedderIdentity,
    EmbedderIdentityUnconfirmedError,
    EmbedderIdentityUnknownWarning,
    EmbedderIdentityUnreadableError,
    EmbedderIdentityUnreadableWarning,
    PalaceRef,
)

from test_embedding_model_fallback import _ADD, _mcp_caller
from test_embedding_model_fallback import unknown_model_palace  # noqa: F401  (fixture)


def _MINILM(test):
    """A MiniLM palace fixture (``_palace(request)``), configured ``minilm``."""
    test = pytest.mark.usefixtures("unknown_model_palace")(test)
    return pytest.mark.parametrize("unknown_model_palace", ["minilm"], indirect=True)(test)


def _palace(request):
    return request.getfixturevalue("unknown_model_palace")


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


def _rows(palace) -> int:
    with sqlite3.connect(str(palace / "chroma.sqlite3")) as db:
        return db.execute("select count(*) from embeddings").fetchone()[0]


def _sidecar(palace):
    return palace / "mempalace_embedder.json"


def _forget_validated():
    from mempalace import palace as palace_mod

    palace_mod._VALIDATED_IDENTITY.clear()


def _mined(fixture):
    from mempalace.miner import mine

    project, palace, config = fixture
    mine(str(project), str(palace))
    _forget_validated()
    return project, palace, config


def _drop_record(palace):
    _sidecar(palace).unlink()
    _forget_validated()


def _truncate_record(palace):
    _sidecar(palace).write_text('{"mempalace_drawers": {"model_name": "openai-com')
    _forget_validated()


def _add_note(project, name="shed"):
    (project / "notes" / f"{name}.md").write_text(
        f"The {name} roof leaks near the potting bench every spring.\n" * 8
    )


# ── the sidecar reader tells "nothing recorded" from "unreadable" ────────


@pytest.mark.parametrize(
    "content",
    [
        '{"mempalace_drawers": {"model_name": "openai-com',
        "[1, 2, 3]",
        '{"mempalace_drawers": "minilm"}',
        '{"mempalace_drawers": {"dimension": 384}}',
        '{"mempalace_drawers": {"model_name": "minilm", "dimension": "wide"}}',
        b"\xff\xfe\x00garbage",
    ],
    ids=[
        "truncated",
        "not-an-object",
        "entry-not-an-object",
        "no-model-name",
        "bad-dimension",
        "bytes",
    ],
)
def test_an_unreadable_sidecar_raises_instead_of_reading_as_unrecorded(tmp_path, content):
    from mempalace.backends._sidecar import read_embedder_sidecar

    path = tmp_path / "mempalace_embedder.json"
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content)
    with pytest.raises(EmbedderIdentityUnreadableError, match="unreadable"):
        read_embedder_sidecar(str(path), "mempalace_drawers")


def test_a_missing_sidecar_or_entry_reads_as_unrecorded(tmp_path):
    from mempalace.backends._sidecar import read_embedder_sidecar

    path = tmp_path / "mempalace_embedder.json"
    assert read_embedder_sidecar(str(path), "mempalace_drawers") is None
    path.write_text(json.dumps({"mempalace_closets": {"model_name": "minilm", "dimension": 384}}))
    assert read_embedder_sidecar(str(path), "mempalace_drawers") is None
    assert read_embedder_sidecar(str(path), "mempalace_closets") == EmbedderIdentity("minilm", 384)


def test_replacing_an_unreadable_sidecar_keeps_a_copy(tmp_path):
    from mempalace.backends._sidecar import read_embedder_sidecar, write_embedder_sidecar

    path = tmp_path / "mempalace_embedder.json"
    path.write_text('{"mempalace_drawers": {"model_name": "openai-com')
    write_embedder_sidecar(str(path), "mempalace_drawers", EmbedderIdentity("minilm", 384))
    assert read_embedder_sidecar(str(path), "mempalace_drawers") == EmbedderIdentity("minilm", 384)
    copies = list(tmp_path.glob("mempalace_embedder.json.corrupt-*"))
    assert len(copies) == 1, copies
    assert copies[0].read_text() == '{"mempalace_drawers": {"model_name": "openai-com'


# ── enforcement: reads warn, writes refuse, empty collections record ─────


def _sqlite_palace(tmp_path, monkeypatch, *, rows, record):
    from mempalace.backends.sqlite_exact import SQLiteExactBackend

    monkeypatch.setenv("MEMPALACE_EMBEDDING_MODEL", "minilm")
    monkeypatch.setenv("MEMPALACE_BACKEND", "sqlite_exact")
    ref = PalaceRef(id=str(tmp_path), local_path=str(tmp_path))
    col = SQLiteExactBackend().get_collection(
        palace=ref, collection_name="mempalace_drawers", create=True
    )
    if rows:
        col.add(documents=["x"], ids=["a"], metadatas=[{}], embeddings=[[0.1, 0.2, 0.3, 0.4]])
    if record:
        col.set_embedder_identity(EmbedderIdentity(record, 4))
    _forget_validated()
    return col


def test_sqlite_populated_unrecorded_refuses_writes_and_warns_reads(tmp_path, monkeypatch):
    from mempalace import palace as P

    _sqlite_palace(tmp_path, monkeypatch, rows=True, record=None)
    with pytest.raises(EmbedderIdentityUnconfirmedError) as excinfo:
        P.get_collection(str(tmp_path), create=True)
    message = str(excinfo.value)
    assert "no recorded embedder identity" in message
    assert "palace set-embedder --model minilm" in message
    assert "Reads and search keep working" in message

    with pytest.warns(EmbedderIdentityUnknownWarning, match="Writes are refused"):
        col = P.get_collection(str(tmp_path), create=False)
    assert col.count() == 1
    # Nothing was recorded by either open.
    assert col.get_stored_embedder_identity() is None


def test_sqlite_empty_unrecorded_collection_records_on_a_write_open(tmp_path, monkeypatch):
    from mempalace import palace as P

    _sqlite_palace(tmp_path, monkeypatch, rows=False, record=None)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        col = P.get_collection(str(tmp_path), create=True)
    assert col.get_stored_embedder_identity().model_name == "minilm"


def test_a_read_verdict_does_not_let_a_later_write_through(tmp_path, monkeypatch):
    from mempalace import palace as P

    _sqlite_palace(tmp_path, monkeypatch, rows=True, record=None)
    with pytest.warns(EmbedderIdentityUnknownWarning):
        P.get_collection(str(tmp_path), create=False)
    # Same process, the read verdict is cached: the write must still refuse.
    with pytest.raises(EmbedderIdentityUnconfirmedError):
        P.get_collection(str(tmp_path), create=True)


def test_unknown_row_count_refuses_a_write(tmp_path, monkeypatch):
    from mempalace import palace as P

    _sqlite_palace(tmp_path, monkeypatch, rows=False, record=None)
    monkeypatch.setattr(P, "_collection_has_rows", lambda *a, **k: None)
    with pytest.raises(EmbedderIdentityUnconfirmedError, match="row count could not be read"):
        P.get_collection(str(tmp_path), create=True)


def test_a_failed_identity_read_refuses_writes(tmp_path, monkeypatch):
    from mempalace import palace as P
    from mempalace.backends.sqlite_exact import SQLiteExactCollection

    _sqlite_palace(tmp_path, monkeypatch, rows=True, record="minilm")

    def _broken(self):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(SQLiteExactCollection, "get_stored_embedder_identity", _broken)
    with pytest.raises(EmbedderIdentityUnconfirmedError, match="disk I/O error"):
        P.get_collection(str(tmp_path), create=True)
    with pytest.warns(EmbedderIdentityUnreadableWarning):
        P.get_collection(str(tmp_path), create=False)


def test_a_backend_without_identity_hooks_keeps_writing():
    from mempalace import palace as P
    from mempalace.backends.base import BaseCollection

    class _NoIdentity(BaseCollection):
        def add(self, **k): ...
        def upsert(self, **k): ...
        def query(self, **k): ...
        def get(self, **k): ...
        def delete(self, **k): ...
        def count(self):
            return 3

    _forget_validated()
    with pytest.warns(EmbedderIdentityUnknownWarning):
        P._enforce_embedder_identity(_NoIdentity(), "/nowhere", "mempalace_drawers", create=True)


# ── legacy palaces with no record on every backend (matrix S4b) ──────────


def _marker_collection(kind, tmp_path, monkeypatch, rows):
    """A qdrant / pgvector collection as develop left it: the backend marker,
    no ``mempalace_embedder.json``, and ``rows`` vectors on the server."""
    ref = PalaceRef(id=str(tmp_path), local_path=str(tmp_path))
    if kind == "qdrant":
        from mempalace.backends.qdrant import QdrantBackend, QdrantCollection, _QdrantConfig

        backend = QdrantBackend()
        config = _QdrantConfig(url="http://localhost:6333", api_key=None, namespace=None)
        backend._write_marker(ref, config)
        col = QdrantCollection(
            backend=backend,
            client=object(),
            config=config,
            palace=ref,
            collection_name="mempalace_drawers",
            remote_collection="mp_drawers_remote",
        )
    else:
        from mempalace.backends.pgvector import (
            PgVectorBackend,
            PgVectorCollection,
            _PgVectorConfig,
        )

        backend = PgVectorBackend()
        config = _PgVectorConfig(dsn="postgresql://example", namespace=None)
        backend._write_marker(ref, config)
        col = PgVectorCollection(
            backend=backend,
            client=object(),
            config=config,
            palace=ref,
            collection_name="mempalace_drawers",
            table="mp_drawers_t",
        )
    # The vectors live on the server; only the count is needed here.
    monkeypatch.setattr(type(col), "count", lambda self: rows)
    _forget_validated()
    return col


@pytest.mark.parametrize("kind", ["qdrant", "pgvector"])
def test_a_legacy_unrecorded_palace_refuses_writes_on_marker_backends(kind, tmp_path, monkeypatch):
    """Every qdrant/pgvector palace develop mined without a pre-made folder
    has a marker but no identity record. A write used to take any model of
    the same dimension (S4b: SILENT-MIXED-WRITE)."""
    from mempalace import palace as P

    monkeypatch.setenv("MEMPALACE_EMBEDDING_MODEL", "minilm")
    col = _marker_collection(kind, tmp_path, monkeypatch, rows=76)
    assert not _sidecar(tmp_path).exists()
    with pytest.raises(EmbedderIdentityUnconfirmedError, match="palace set-embedder --model"):
        P._enforce_embedder_identity(col, str(tmp_path), "mempalace_drawers", create=True)
    with pytest.warns(EmbedderIdentityUnknownWarning, match="Writes are refused"):
        P._enforce_embedder_identity(col, str(tmp_path), "mempalace_drawers", create=False)
    assert col.get_stored_embedder_identity() is None

    # Confirming the model is what lets writes through again.
    col.set_embedder_identity(EmbedderIdentity("minilm", 384))
    _forget_validated()
    P._enforce_embedder_identity(col, str(tmp_path), "mempalace_drawers", create=True)


@pytest.mark.parametrize("kind", ["qdrant", "pgvector"])
def test_an_empty_unrecorded_marker_collection_records_on_first_write(kind, tmp_path, monkeypatch):
    from mempalace import palace as P

    monkeypatch.setenv("MEMPALACE_EMBEDDING_MODEL", "minilm")
    col = _marker_collection(kind, tmp_path, monkeypatch, rows=0)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        P._enforce_embedder_identity(col, str(tmp_path), "mempalace_drawers", create=True)
    assert col.get_stored_embedder_identity().model_name == "minilm"


def _drop_sqlite_records(palace):
    from mempalace.backends.sqlite_exact import SQLiteExactBackend

    col = SQLiteExactBackend().get_collection(
        palace=PalaceRef(id=str(palace), local_path=str(palace)),
        collection_name="mempalace_drawers",
        create=False,
    )
    with col._cursor(write=True) as cur:
        cur.execute("DELETE FROM meta WHERE key LIKE 'embedder_model:%'")
    _forget_validated()


def _sqlite_count(palace):
    from mempalace import palace as P

    return P.get_collection(str(palace), create=False, _skip_identity_check=True).count()


@_MINILM
def test_sqlite_exact_legacy_palace_refuses_cli_and_mcp_writes(request, monkeypatch, capfd, kg):
    monkeypatch.setenv("MEMPALACE_BACKEND", "sqlite_exact")
    project, palace, _ = _mined(_palace(request))
    rows = _sqlite_count(palace)
    _drop_sqlite_records(palace)
    _add_note(project)
    capfd.readouterr()

    assert _run_cli(monkeypatch, "--palace", str(palace), "mine", str(project)) == 1
    err = capfd.readouterr().err
    assert "no recorded embedder identity" in err and "Traceback" not in err, err
    assert _sqlite_count(palace) == rows

    call = _mcp_caller(monkeypatch, palace, kg)
    result, payload = call("mempalace_add_drawer", dict(_ADD))
    assert result.get("isError") is True, payload
    assert payload["error"] == "Embedder identity unconfirmed", payload
    assert _sqlite_count(palace) == rows

    search_result, found = call("mempalace_search", {"query": "greenhouse tomatoes"})
    assert not search_result.get("isError"), found
    assert found.get("results"), found


# ── end to end on Chroma: S3 (no record) and S4 (truncated record) ───────


_BREAKS = {"missing": _drop_record, "truncated": _truncate_record}


@_MINILM
@pytest.mark.parametrize("damage", sorted(_BREAKS))
def test_cli_mine_refuses_and_search_still_works(request, monkeypatch, capfd, damage):
    project, palace, _ = _mined(_palace(request))
    rows = _rows(palace)
    _BREAKS[damage](palace)
    before = _sidecar(palace).read_bytes() if _sidecar(palace).exists() else None
    _add_note(project)
    capfd.readouterr()

    assert _run_cli(monkeypatch, "--palace", str(palace), "mine", str(project)) == 1
    err = capfd.readouterr().err
    assert "mempalace: " in err and "Traceback" not in err
    assert "palace set-embedder --model minilm" in err
    assert _rows(palace) == rows
    after = _sidecar(palace).read_bytes() if _sidecar(palace).exists() else None
    assert after == before

    _forget_validated()
    from mempalace.searcher import search_memories

    with pytest.warns(EmbedderIdentityUnknownWarning):
        found = search_memories("greenhouse tomatoes watering", str(palace))
    assert found.get("results"), found


@_MINILM
@pytest.mark.parametrize("damage", sorted(_BREAKS))
def test_mcp_add_drawer_refuses_with_is_error_and_status_still_works(
    request, monkeypatch, kg, damage
):
    project, palace, _ = _mined(_palace(request))
    rows = _rows(palace)
    _BREAKS[damage](palace)
    call = _mcp_caller(monkeypatch, palace, kg)

    result, payload = call("mempalace_add_drawer", dict(_ADD))
    assert result.get("isError") is True, payload
    assert payload["error"] == "Embedder identity unconfirmed", payload
    assert payload["error_class"] == "EmbedderIdentityUnconfirmedError"
    assert "set-embedder" in payload["details"]
    assert _rows(palace) == rows

    status_result, status = call("mempalace_status", {})
    assert not status_result.get("isError"), status
    assert status.get("total_drawers") == 1, status

    search_result, found = call("mempalace_search", {"query": "greenhouse tomatoes"})
    assert not search_result.get("isError"), found
    assert found.get("results"), found


@_MINILM
@pytest.mark.parametrize("damage", sorted(_BREAKS))
def test_set_embedder_confirms_every_collection_and_writes_resume(
    request, monkeypatch, capfd, damage
):
    project, palace, _ = _mined(_palace(request))
    _BREAKS[damage](palace)
    capfd.readouterr()

    assert _run_cli(monkeypatch, "--palace", str(palace), "palace", "set-embedder") == 0
    out = capfd.readouterr().out
    assert "recorded embedder identity: minilm" in out, out
    assert "mempalace_closets: recorded embedder identity: minilm" in out, out
    if damage == "truncated":
        assert "previous record was unreadable" in out, out
        assert list(palace.glob("mempalace_embedder.json.corrupt-*"))
    recorded = json.loads(_sidecar(palace).read_text())
    assert recorded["mempalace_drawers"]["model_name"] == "minilm"
    assert recorded["mempalace_closets"]["model_name"] == "minilm"

    _forget_validated()
    _add_note(project)
    capfd.readouterr()
    assert _run_cli(monkeypatch, "--palace", str(palace), "mine", str(project)) == 0
    assert "mempalace: " not in capfd.readouterr().err


@_MINILM
def test_fresh_palace_still_records_on_first_mine(request):
    project, palace, _ = _mined(_palace(request))
    recorded = json.loads(_sidecar(palace).read_text())
    assert recorded["mempalace_drawers"]["model_name"] == "minilm"
    assert recorded["mempalace_closets"]["model_name"] == "minilm"


# ── H: long-running processes re-check a changed record (scope S6) ───────


def _restamp_elsewhere(palace, model_name):
    """Rewrite the record as another process would (set-embedder --force, an
    in-place rebuild with another model) WITHOUT touching this process's cache."""
    sidecar = _sidecar(palace)
    data = json.loads(sidecar.read_text())
    for entry in data.values():
        entry["model_name"] = model_name
    sidecar.write_text(json.dumps(data))


def test_a_record_changed_by_another_process_is_checked_again(tmp_path, monkeypatch):
    from mempalace import palace as P
    from mempalace.backends.base import EmbedderIdentityMismatchError
    from mempalace.backends.sqlite_exact import SQLiteExactBackend

    _sqlite_palace(tmp_path, monkeypatch, rows=True, record="minilm")
    P.get_collection(str(tmp_path), create=True)  # validated and cached

    other = SQLiteExactBackend().get_collection(
        palace=PalaceRef(id=str(tmp_path), local_path=str(tmp_path)),
        collection_name="mempalace_drawers",
        create=True,
    )
    other.set_embedder_identity(EmbedderIdentity("embeddinggemma", 384))
    with pytest.raises(EmbedderIdentityMismatchError):
        P.get_collection(str(tmp_path), create=True)
    with pytest.raises(EmbedderIdentityMismatchError):
        P.get_collection(str(tmp_path), create=False)


def test_an_unchanged_record_keeps_the_cached_verdict(tmp_path, monkeypatch):
    from mempalace import palace as P

    _sqlite_palace(tmp_path, monkeypatch, rows=True, record=None)
    calls = []
    real = P._collection_has_rows
    monkeypatch.setattr(P, "_collection_has_rows", lambda *a: calls.append(1) or real(*a))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        for _ in range(3):
            P.get_collection(str(tmp_path), create=False)
    assert len(calls) == 1


@_MINILM
@pytest.mark.parametrize("change", ["another-model", "record-deleted"])
def test_mcp_server_refuses_after_the_record_changes_under_it(request, monkeypatch, kg, change):
    project, palace, _ = _mined(_palace(request))
    call = _mcp_caller(monkeypatch, palace, kg)
    result, payload = call("mempalace_add_drawer", dict(_ADD))
    assert payload.get("success") is True, payload
    search_result, _ = call("mempalace_search", {"query": "greenhouse tomatoes"})
    assert not search_result.get("isError")
    rows = _rows(palace)

    # Another process changes the record while this server keeps running.
    if change == "another-model":
        _restamp_elsewhere(palace, "embeddinggemma")
        kind = "Embedder identity mismatch"
    else:
        _sidecar(palace).unlink()
        kind = "Embedder identity unconfirmed"

    more = dict(_ADD, content="The rain barrel overflows after every storm in April.")
    result, payload = call("mempalace_add_drawer", more)
    assert result.get("isError") is True, payload
    assert payload["error"] == kind, payload
    assert _rows(palace) == rows
    if change == "another-model":
        search_result, found = call("mempalace_search", {"query": "greenhouse tomatoes"})
        assert search_result.get("isError") is True, found
