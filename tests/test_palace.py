"""Tests for mempalace.palace shared helpers."""

import chromadb

from _chroma_palace_helper import make_minimal_chroma_sqlite

import pytest

from mempalace.backends import CollectionNotInitializedError, PalaceNotFoundError
from mempalace.palace import (
    CLOSETS_COLLECTION_NAME,
    CollectionNameMismatchError,
    _allowed_wrapper_collection_names,
    _candidate_entity_words,
    _metadata_matches_extract_mode,
    _open_collection_or_explain,
    backend_requires_single_writer,
    get_collection,
)


def test_backend_writer_ownership_distinguishes_milvus_lite_from_server(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))

    monkeypatch.delenv("MEMPALACE_MILVUS_URI", raising=False)
    assert backend_requires_single_writer("milvus") is True

    monkeypatch.setenv("MEMPALACE_MILVUS_URI", str(tmp_path / "milvus.db"))
    assert backend_requires_single_writer("milvus") is True

    for uri in (
        "https://zilliz.example",
        "http://milvus.example:19530",
        "tcp://milvus.example:19530",
        "grpc://milvus.example:19530",
    ):
        monkeypatch.setenv("MEMPALACE_MILVUS_URI", uri)
        assert backend_requires_single_writer("milvus") is False


def test_backend_writer_ownership_remains_conservative_for_unknown_backend():
    assert backend_requires_single_writer("plugin_backend") is True
    assert backend_requires_single_writer("qdrant") is False
    assert backend_requires_single_writer("pgvector") is False


def _capture():
    """Return (emit, lines) — emit appends to lines for inspection."""
    lines: list[str] = []
    return lines.append, lines


def test_open_collection_or_explain_state_a_missing_dir(tmp_path):
    """State A: palace dir does not exist."""
    emit, lines = _capture()
    missing = tmp_path / "no-such-palace"

    result = _open_collection_or_explain(str(missing), out=emit)

    assert result is None
    assert any("No palace found" in line for line in lines)
    assert any("mempalace init" in line for line in lines)
    # Helper must not create the directory.
    assert not missing.exists()


class TestMetadataMatchesExtractMode:
    """#104: a missing extract_mode must only be treated as a legacy
    exchange-mode row when the drawer is otherwise convo_miner's own —
    never for a drawer positively identified as another producer's
    (e.g. the sweeper's ingest_mode="sweep"), which never set
    extract_mode because it was never meant to carry one."""

    def test_no_extract_mode_requested_matches_everything(self):
        assert _metadata_matches_extract_mode({"ingest_mode": "sweep"}, None) is True

    def test_exact_match(self):
        assert _metadata_matches_extract_mode({"extract_mode": "general"}, "general") is True

    def test_mismatched_explicit_extract_mode_never_matches(self):
        assert _metadata_matches_extract_mode({"extract_mode": "general"}, "exchange") is False

    def test_legacy_convo_row_with_no_ingest_mode_matches_exchange(self):
        """Pre-ingest_mode-schema convo_miner drawers: no extract_mode,
        no ingest_mode at all — the original legacy-compat case."""
        assert _metadata_matches_extract_mode({"source_file": "chat.txt"}, "exchange") is True

    def test_convo_miners_own_ingest_mode_matches_exchange(self):
        assert _metadata_matches_extract_mode({"ingest_mode": "convos"}, "exchange") is True

    def test_sweeper_row_never_matches_exchange(self):
        """The actual #104 bug: a sweeper drawer has no extract_mode but
        DOES carry ingest_mode="sweep" — it must not be swept into
        convo_miner's default "exchange" purge/idempotency scope."""
        sweeper_meta = {
            "ingest_mode": "sweep",
            "session_id": "s1",
            "role": "assistant",
        }
        assert _metadata_matches_extract_mode(sweeper_meta, "exchange") is False

    def test_sweeper_row_never_matches_general(self):
        assert _metadata_matches_extract_mode({"ingest_mode": "sweep"}, "general") is False


def test_open_collection_or_explain_state_b_no_db(tmp_path):
    """State B: dir exists but chroma.sqlite3 does not.

    Critical invariant: the helper must NOT trigger chromadb's lazy DB
    creation by reaching the backend. The dir must remain empty after
    the call so a read-only inspection stays read-only.
    """
    emit, lines = _capture()
    palace = tmp_path / "palace"
    palace.mkdir()
    assert not (palace / "chroma.sqlite3").exists()

    result = _open_collection_or_explain(str(palace), out=emit)

    assert result is None
    assert any("has no chroma.sqlite3 yet" in line for line in lines)
    # No side-effect: backend was not invoked.
    assert list(palace.iterdir()) == []


def test_open_collection_or_explain_state_c_no_collection(tmp_path):
    """State C: DB file exists but the collection has never been created."""
    emit, lines = _capture()
    palace = tmp_path / "palace"
    palace.mkdir()
    chromadb.PersistentClient(path=str(palace))  # creates DB, no collection
    assert (palace / "chroma.sqlite3").is_file()

    result = _open_collection_or_explain(str(palace), out=emit)

    assert result is None
    assert any("initialized but empty" in line for line in lines)
    assert any("mempalace mine" in line for line in lines)


def test_open_collection_or_explain_unknown_backend(tmp_path, monkeypatch):
    """An unknown backend name (typo in MEMPALACE_BACKEND/--backend) must
    surface as a CLI state message, not an escaping KeyError stack trace."""
    emit, lines = _capture()
    palace = tmp_path / "palace"
    palace.mkdir()
    monkeypatch.setenv("MEMPALACE_BACKEND", "does_not_exist")

    result = _open_collection_or_explain(str(palace), out=emit)

    assert result is None
    assert any("Unknown backend selected" in line for line in lines)
    assert any("does_not_exist" in line for line in lines)


def test_open_collection_or_explain_state_d_healthy(tmp_path):
    """State D: healthy palace — returns the opened collection silently."""
    emit, lines = _capture()
    palace = tmp_path / "palace"
    palace.mkdir()
    get_collection(str(palace), create=True)  # bootstrap collection

    result = _open_collection_or_explain(str(palace), out=emit)

    assert result is not None
    assert lines == []  # healthy path is silent


def test_open_collection_or_explain_state_e_unexpected_error(tmp_path, monkeypatch):
    """State E: unexpected error opening the backend routes to repair hint."""
    emit, lines = _capture()
    palace = tmp_path / "palace"
    palace.mkdir()
    make_minimal_chroma_sqlite(palace)  # pass the isfile guard

    def boom(*args, **kwargs):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr("mempalace.palace.get_collection", boom)

    result = _open_collection_or_explain(str(palace), out=emit)

    assert result is None
    assert any("Error opening palace" in line for line in lines)
    assert any("repair-status" in line for line in lines)


def test_open_collection_or_explain_default_sink_is_print(tmp_path, capsys):
    """When out is None, messages go through builtin print → stdout."""
    missing = tmp_path / "no-such-palace"

    result = _open_collection_or_explain(str(missing))

    assert result is None
    assert "No palace found" in capsys.readouterr().out


def test_open_collection_or_explain_propagates_palace_not_found_from_backend(tmp_path, monkeypatch):
    """If the backend raises bare PalaceNotFoundError after our filesystem
    guards (rare race or backend-internal "not found"), the helper still
    prints the State A message and returns None."""
    emit, lines = _capture()
    palace = tmp_path / "palace"
    palace.mkdir()
    make_minimal_chroma_sqlite(palace)

    def raise_pnf(*args, **kwargs):
        raise PalaceNotFoundError(str(palace))

    monkeypatch.setattr("mempalace.palace.get_collection", raise_pnf)

    result = _open_collection_or_explain(str(palace), out=emit)

    assert result is None
    assert any("No palace found" in line for line in lines)


def test_open_collection_or_explain_reraises_backend_closed_error(tmp_path, monkeypatch):
    """BackendClosedError is a programmer error (caller violated the backend
    lifecycle), not a palace-state UX condition. The helper must propagate
    it instead of swallowing it into the State E "repair-status" hint.

    Without this re-raise, a closed default backend would silently mask
    every call site as "Error opening palace ... Try: repair-status"
    even when the actual fix is to stop using a closed backend handle.
    """
    from mempalace.backends import BackendClosedError

    palace = tmp_path / "palace"
    palace.mkdir()
    make_minimal_chroma_sqlite(palace)

    def raise_closed(*args, **kwargs):
        raise BackendClosedError("ChromaBackend has been closed")

    monkeypatch.setattr("mempalace.palace.get_collection", raise_closed)

    import pytest

    with pytest.raises(BackendClosedError):
        _open_collection_or_explain(str(palace))


def test_open_collection_or_explain_distinguishes_collection_subclass(tmp_path, monkeypatch):
    """The helper must surface CollectionNotInitializedError as the
    'empty' message rather than the broader 'No palace found' message,
    even though the former subclasses the latter."""
    emit, lines = _capture()
    palace = tmp_path / "palace"
    palace.mkdir()
    make_minimal_chroma_sqlite(palace)

    def raise_cnie(*args, **kwargs):
        raise CollectionNotInitializedError(str(palace))

    monkeypatch.setattr("mempalace.palace.get_collection", raise_cnie)

    result = _open_collection_or_explain(str(palace), out=emit)

    assert result is None
    assert any("initialized but empty" in line for line in lines)
    assert not any("No palace found" in line for line in lines)


def test_candidate_entity_words_drops_overlong_blob():
    """#2063: a long unbroken ASCII run must be collapsed before matching so the
    candidate patterns cannot backtrack catastrophically; such runs are never
    entity names. Normal names are still returned."""
    longtok = "Aa" + "Bb" * 30  # 62-char unbroken ASCII run
    words = _candidate_entity_words(longtok + " and Lantern")
    assert longtok not in words
    assert "Lantern" in words


class TestGetCollectionNameValidation:
    """#2347: get_collection must reject names the palace wrapper does not own.

    The wrapper routes reads/writes through exactly two collections: the
    configured drawers name (default ``mempalace_drawers``) and
    ``mempalace_closets``. Any other name silently creates an orphan store
    invisible to search/CLI/MCP. The check must fail loudly at the wrapper,
    before the backend is touched.
    """

    def _bootstrap(self, tmp_path):
        palace = tmp_path / "palace"
        palace.mkdir()
        get_collection(str(palace), create=True)
        return palace

    def test_configured_drawers_name_passes(self, tmp_path):
        palace = self._bootstrap(tmp_path)
        col = get_collection(str(palace), collection_name="mempalace_drawers")
        assert col is not None

    def test_closets_name_passes(self, tmp_path):
        palace = self._bootstrap(tmp_path)
        col = get_collection(str(palace), collection_name=CLOSETS_COLLECTION_NAME)
        assert col is not None
        assert CLOSETS_COLLECTION_NAME == "mempalace_closets"

    def test_none_name_resolves_to_configured_and_passes(self, tmp_path):
        palace = self._bootstrap(tmp_path)
        col = get_collection(str(palace), collection_name=None)
        assert col is not None

    def test_short_alias_drawers_rejected(self, tmp_path):
        palace = self._bootstrap(tmp_path)
        with pytest.raises(CollectionNameMismatchError) as exc_info:
            get_collection(str(palace), collection_name="drawers", create=True)
        assert exc_info.value.requested == "drawers"
        assert "mempalace_drawers" in exc_info.value.allowed
        assert "mempalace_closets" in exc_info.value.allowed

    def test_bare_mempalace_rejected(self, tmp_path):
        palace = self._bootstrap(tmp_path)
        with pytest.raises(CollectionNameMismatchError):
            get_collection(str(palace), collection_name="mempalace", create=True)

    def test_adhoc_name_rejected(self, tmp_path):
        palace = self._bootstrap(tmp_path)
        with pytest.raises(CollectionNameMismatchError):
            get_collection(str(palace), collection_name="my_custom_store", create=True)

    def test_error_is_valueerror_and_carries_context(self, tmp_path):
        palace = self._bootstrap(tmp_path)
        with pytest.raises(ValueError) as exc_info:
            get_collection(str(palace), collection_name="drawers", create=True)
        err = exc_info.value
        assert isinstance(err, CollectionNameMismatchError)
        assert err.requested == "drawers"
        assert isinstance(err.allowed, list)
        assert "mempalace_drawers" in err.allowed
        assert str(palace) in str(err)

    def test_skip_name_check_bypasses_validation(self, tmp_path):
        """The maintenance escape hatch — a name outside the wrapper's route is
        accepted when the caller explicitly opts in (repair-encoding tool)."""
        palace = self._bootstrap(tmp_path)
        # Must NOT raise CollectionNameMismatchError; it may raise a
        # collection-not-found (create=False) from the backend, which proves
        # the name check was bypassed.
        try:
            col = get_collection(
                str(palace),
                collection_name="totally_custom_name",
                create=True,
                _skip_name_check=True,
            )
            assert col is not None
        except CollectionNameMismatchError:  # pragma: no cover
            raise AssertionError("_skip_name_check=True must bypass the check")

    def test_configured_override_is_accepted(self, tmp_path, monkeypatch):
        """If the user configures a custom drawers name, that name must pass."""
        palace = self._bootstrap(tmp_path)

        import mempalace.config as config_mod

        monkeypatch.setattr(config_mod, "get_configured_collection_name", lambda: "custom_drawers")
        col = get_collection(str(palace), collection_name="custom_drawers", create=True)
        assert col is not None

        # Meanwhile the now-unconfigured default drawers name is no longer
        # on the allowed list for this process.
        with pytest.raises(CollectionNameMismatchError):
            get_collection(str(palace), collection_name="mempalace_drawers", create=True)

    def test_allowed_names_helper_lists_both(self):
        allowed = _allowed_wrapper_collection_names()
        assert "mempalace_drawers" in allowed
        assert "mempalace_closets" in allowed
        assert len(allowed) == 2
