"""Tests for end-of-mine FTS5 validation (#1537).

mempalace mine must not print "Done." and exit 0 on a palace whose
chroma.sqlite3 left FTS5 in a malformed state. The validation hook in
``palace._validate_palace_fts5_after_mine`` runs PRAGMA quick_check at
the end of every non-dry-run mine and raises ``MineValidationError`` so
``cmd_mine`` can surface the same recovery banner ``cmd_repair`` prints.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from mempalace import cli, convo_miner, miner
from mempalace.palace import (
    MineValidationError,
    _validate_palace_fts5_after_mine,
)


# ── Helpers ─────────────────────────────────────────────────────────


def _build_palace_with_drawer(palace_path: Path) -> None:
    """Create a real chromadb palace with one drawer so chroma.sqlite3 exists."""
    palace_path.mkdir(parents=True, exist_ok=True)
    from mempalace.backends.chroma import ChromaBackend

    backend = ChromaBackend()
    try:
        col = backend.create_collection(str(palace_path), "mempalace_drawers")
        col.upsert(
            ids=["d1"],
            documents=["hello world memorable phrase"],
            metadatas=[{"wing": "w", "room": "r"}],
        )
    finally:
        backend.close()


def _page_mangle(sqlite_path: Path) -> int:
    """Corrupt 4 mid-file pages so PRAGMA quick_check fails. Returns offset used."""
    PAGE = 4096
    CORRUPT_BYTES = 16384  # 4 pages
    HEADER_GUARD = PAGE * 2
    pre_size = sqlite_path.stat().st_size
    assert pre_size >= HEADER_GUARD + CORRUPT_BYTES, (
        f"sqlite db too small to mangle: {pre_size} bytes"
    )
    max_offset = (pre_size - CORRUPT_BYTES) & ~(PAGE - 1)
    corrupt_offset = min(40960, max_offset)
    assert corrupt_offset >= HEADER_GUARD
    with open(sqlite_path, "r+b") as f:
        f.seek(corrupt_offset)
        f.write(b"\xde\xad\xbe\xef" * (CORRUPT_BYTES // 4))
    return corrupt_offset


def _corrupt_fts5_segment(sqlite_path: Path) -> None:
    """Soft FTS5-only corruption: replace one segment blob with garbage.

    Mirrors the reporter's natural failure mode where chromadb opens cleanly
    but ``PRAGMA quick_check`` returns ``malformed inverted index for FTS5
    table main.embedding_fulltext_search``.
    """
    import sqlite3

    with sqlite3.connect(str(sqlite_path)) as conn:
        # Schema-drift guard: chromadb's FTS5 shadow table name is private
        # API and may rename across versions. Skip cleanly rather than error.
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE name LIKE 'embedding_fulltext_search%'"
            ).fetchall()
        }
        if "embedding_fulltext_search_data" not in tables:
            pytest.skip(
                f"chromadb FTS5 shadow table 'embedding_fulltext_search_data' not present; "
                f"found: {sorted(tables)}"
            )
        rows = conn.execute("SELECT id, block FROM embedding_fulltext_search_data").fetchall()
        if not rows:
            pytest.skip("FTS5 segments empty: cannot fabricate FTS5-only corruption")
        target = next((r for r in rows if r[0] > 10), rows[0])
        garbage = b"\xde\xad\xbe\xef" * (len(target[1]) // 4)
        try:
            conn.execute(
                "UPDATE embedding_fulltext_search_data SET block=? WHERE id=?",
                (garbage, target[0]),
            )
        except sqlite3.OperationalError as exc:
            if "may not be modified" in str(exc):
                pytest.skip(
                    "this SQLite build refuses direct FTS5 shadow-table writes; "
                    "cannot fabricate FTS5-only corruption"
                )
            raise
        conn.commit()


def _mine_args(
    palace: str, src: str, *, mode: str = "project", dry_run: bool = False
) -> SimpleNamespace:
    """Build the args namespace cmd_mine reads."""
    return SimpleNamespace(
        palace=palace,
        dir=src,
        mode=mode,
        wing=None,
        agent="mempalace",
        limit=0,
        dry_run=dry_run,
        no_gitignore=False,
        include_ignored=None,
        extract="exchange",
        redetect_origin=False,
    )


# ── 1. Helper returns silently on a clean palace ────────────────────


def test_helper_returns_silently_on_clean_palace(tmp_path):
    palace = tmp_path / "palace"
    _build_palace_with_drawer(palace)

    # Should not raise: quick_check returns ('ok',) so no errors collected.
    assert _validate_palace_fts5_after_mine(str(palace)) is None


# ── 2. Helper raises MineValidationError on corrupt sqlite ──────────


def test_helper_raises_on_page_mangled_sqlite(tmp_path):
    palace = tmp_path / "palace"
    _build_palace_with_drawer(palace)
    _page_mangle(palace / "chroma.sqlite3")

    with pytest.raises(MineValidationError) as exc_info:
        _validate_palace_fts5_after_mine(str(palace))

    err = exc_info.value
    assert err.palace_path == str(palace)
    assert err.errors, "errors list must be populated"
    combined = " ".join(err.errors).lower()
    assert "malformed" in combined or "quick_check failed" in combined


def test_helper_auto_heals_fts5_segment_corruption(tmp_path):
    """The reporter-shaped failure: FTS5 inverted index malformed, main pages
    OK. This is exactly the isolated-FTS5 condition
    ``maybe_autoheal_fts5_index`` (#1926/#1928) rebuilds in place, so the
    validator must return silently (healed) rather than raise.
    """
    import sqlite3
    from contextlib import closing

    palace = tmp_path / "palace"
    _build_palace_with_drawer(palace)
    _corrupt_fts5_segment(palace / "chroma.sqlite3")

    # Must not raise: isolated FTS5 corruption is auto-healed before the
    # validator would otherwise raise MineValidationError.
    assert _validate_palace_fts5_after_mine(str(palace)) is None

    with closing(sqlite3.connect(str(palace / "chroma.sqlite3"))) as conn:
        assert conn.execute("PRAGMA quick_check").fetchall() == [("ok",)]


# ── 3. cmd_mine surfaces MineValidationError as exit-1 + banner ─────


def test_cmd_mine_project_mode_exits_nonzero_with_banner(tmp_path, monkeypatch, capsys):
    palace = str(tmp_path / "palace")
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.md").write_text("placeholder")

    def _raise(*_, **__):
        raise MineValidationError(
            palace, ["malformed inverted index for FTS5 table main.embedding_fulltext_search"]
        )

    monkeypatch.setattr(miner, "mine", _raise)

    with pytest.raises(SystemExit) as exit_info:
        cli.cmd_mine(_mine_args(palace, str(src), mode="project"))

    assert exit_info.value.code == 1
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "SQLite-layer corruption detected" in combined
    assert "PRAGMA quick_check" in combined
    assert "malformed inverted index" in combined
    assert "mempalace repair --yes" in combined


def test_cmd_mine_convos_mode_exits_nonzero_with_banner(tmp_path, monkeypatch, capsys):
    palace = str(tmp_path / "palace")
    src = tmp_path / "convos"
    src.mkdir()

    def _raise(*_, **__):
        raise MineValidationError(palace, ["malformed inverted index for FTS5 table"])

    monkeypatch.setattr(convo_miner, "mine_convos", _raise)

    with pytest.raises(SystemExit) as exit_info:
        cli.cmd_mine(_mine_args(palace, str(src), mode="convos"))

    assert exit_info.value.code == 1
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "SQLite-layer corruption detected" in combined
    assert "mempalace repair --yes" in combined


# ── 4. Dry-run skips validation ─────────────────────────────────────


def test_validate_skipped_on_dry_run(tmp_path, monkeypatch):
    """`mine(..., dry_run=True)` must not invoke the validator (no writes happened)."""
    palace = tmp_path / "palace"
    src = tmp_path / "src"
    _build_palace_with_drawer(palace)
    src.mkdir()
    big = "lorem ipsum " * 500
    (src / "big.md").write_text(big)

    calls = []

    def _spy(palace_path):
        calls.append(palace_path)

    monkeypatch.setattr(miner, "_validate_palace_fts5_after_mine", _spy)

    miner.mine(
        project_dir=str(src),
        palace_path=str(palace),
        wing_override=None,
        agent="mempalace",
        limit=0,
        dry_run=True,
    )

    assert calls == [], f"validator must not run in dry-run mode, got: {calls}"


# ── 5. Real exception chain through _mine_impl (no monkeypatch) ────


def test_full_chain_raises_through_mine_impl(tmp_path, monkeypatch):
    """Run a real mine, then force the validator to raise on the re-mine.
    The validator inside _mine_impl must raise MineValidationError as the
    explicit source (spy verifies). The new `except MineValidationError:
    raise` clause in miner._mine_impl bypasses the partial-progress "Mine
    aborted" banner so cmd_mine prints the single, authoritative recovery
    message.

    The validator is monkeypatched to raise directly rather than corrupting
    the real sqlite file: page-level (non-isolated) corruption severe
    enough to make quick_check report non-FTS5 errors is also severe enough
    that ChromaDB's own Rust bindings panic just opening the file for the
    re-mine's ``get_collection()`` call -- before this test's own validator
    ever runs. That's a real, useful thing to know (native panic rather
    than a catchable Python exception, on a sufficiently corrupted file),
    but it's a different failure mode than what this test is about: proving
    _mine_impl's exception-passthrough logic runs cleanly when the
    validator raises. Isolated FTS5-only corruption is covered separately
    by test_full_chain_auto_heals_isolated_fts5_corruption below, and would
    never reach this raise path at all now that it's auto-healed.
    """
    from mempalace import miner as miner_mod
    from mempalace import palace as palace_mod

    palace = tmp_path / "palace"
    src = tmp_path / "src"
    src.mkdir()
    (src / "big.md").write_text("lorem ipsum " * 200)

    miner_mod.mine(
        project_dir=str(src),
        palace_path=str(palace),
        wing_override=None,
        agent="mempalace",
        limit=0,
        dry_run=False,
    )

    # Second mine needs new content to process, else file_already_mined's
    # mtime check short-circuits before the validator ever runs.
    (src / "big.md").write_text("lorem ipsum dolor " * 200)

    # Spy on the validator so we can prove IT was the raise-source, not
    # some other exception masquerading as MineValidationError.
    called = []
    real_errors = ["malformed inverted index for FTS5 table main.embedding_fulltext_search"]

    def _validator_spy(path):
        called.append(path)
        raise MineValidationError(path, real_errors)

    monkeypatch.setattr(palace_mod, "_validate_palace_fts5_after_mine", _validator_spy)
    monkeypatch.setattr(miner_mod, "_validate_palace_fts5_after_mine", _validator_spy)

    with pytest.raises(MineValidationError) as exc_info:
        miner_mod.mine(
            project_dir=str(src),
            palace_path=str(palace),
            wing_override=None,
            agent="mempalace",
            limit=0,
            dry_run=False,
        )

    assert called == [str(palace)], f"validator must be the raise-source; spy recorded: {called}"
    assert list(exc_info.value.errors) == real_errors
    assert exc_info.value.palace_path == str(palace)


def test_full_chain_auto_heals_isolated_fts5_corruption(tmp_path):
    """Run a real mine, corrupt only the FTS5 index (main pages intact),
    re-mine. The full _mine_impl chain must auto-heal and succeed silently
    -- not raise -- since this is exactly the isolated condition
    maybe_autoheal_fts5_index rebuilds in place (#1926/#1928).
    """
    from mempalace import miner as miner_mod

    palace = tmp_path / "palace"
    src = tmp_path / "src"
    src.mkdir()
    (src / "big.md").write_text("lorem ipsum " * 200)

    miner_mod.mine(
        project_dir=str(src),
        palace_path=str(palace),
        wing_override=None,
        agent="mempalace",
        limit=0,
        dry_run=False,
    )

    _corrupt_fts5_segment(palace / "chroma.sqlite3")

    # Editing big.md so the re-mine has new content to process (otherwise
    # file_already_mined's mtime check would short-circuit before the
    # validator ever runs).
    (src / "big.md").write_text("lorem ipsum dolor " * 200)

    # Must not raise: the corruption is healed before mine_impl returns.
    miner_mod.mine(
        project_dir=str(src),
        palace_path=str(palace),
        wing_override=None,
        agent="mempalace",
        limit=0,
        dry_run=False,
    )


def test_validator_suppresses_raise_when_autoheal_clears(tmp_path, monkeypatch):
    """Build-independent companion to test_full_chain_auto_heals_isolated_fts5_corruption.

    _corrupt_fts5_segment pytest.skips on SQLite builds that refuse direct
    FTS5 shadow-table writes, so on those builds the auto-heal wiring is
    never actually exercised. Stubbing sqlite_integrity_errors and
    maybe_autoheal_fts5_index directly (rather than fabricating real
    corruption) proves the wiring in _validate_palace_fts5_after_mine itself,
    deterministically, on every build.
    """
    from mempalace import repair as repair_mod

    palace = tmp_path / "palace"
    _build_palace_with_drawer(palace)

    fts_error = ["malformed inverted index for FTS5 table main.embedding_fulltext_search"]
    heal_calls = []

    monkeypatch.setattr(repair_mod, "sqlite_integrity_errors", lambda _p: list(fts_error))

    def _fake_heal(palace_path, errors, **_kwargs):
        heal_calls.append((palace_path, tuple(errors)))
        return []  # healed: no remaining errors

    monkeypatch.setattr(repair_mod, "maybe_autoheal_fts5_index", _fake_heal)

    _validate_palace_fts5_after_mine(str(palace))  # must not raise

    assert heal_calls == [(str(palace), tuple(fts_error))]


def test_validator_passes_logger_progress_not_print_to_autoheal(tmp_path, monkeypatch, capsys):
    """The actual review-comment fix (gemini-code-assist, PR #1928): this
    validator runs inside the MCP server process too (mcp_server.tool_mine ->
    miner.mine), where stdout is the JSON-RPC transport. maybe_autoheal_fts5_index
    defaults to progress=print, so _validate_palace_fts5_after_mine must
    override it with a logging call instead -- otherwise a healed mine would
    print raw progress text onto the transport stream mid-response.

    The two tests above only prove the suppress/raise wiring and would pass
    even if this line still defaulted to print (they mock
    maybe_autoheal_fts5_index entirely, discarding whatever kwargs it's
    called with). This test instead captures the actual kwargs passed and
    invokes the captured progress callable, asserting nothing lands on stdout.

    Deliberately not pinned to logger.info specifically: which severity level
    is used is a verbosity choice, not a correctness requirement -- the bug
    was "raw print() to stdout", not "wrong log level". Asserting equality to
    one specific level method would make the test fail on a reasonable future
    change (e.g. to logger.debug) that doesn't reintroduce the actual bug.
    """
    from mempalace import repair as repair_mod
    from mempalace.palace import logger as palace_logger

    palace = tmp_path / "palace"
    _build_palace_with_drawer(palace)

    fts_error = ["malformed inverted index for FTS5 table main.embedding_fulltext_search"]
    captured_kwargs = {}

    monkeypatch.setattr(repair_mod, "sqlite_integrity_errors", lambda _p: list(fts_error))

    def _fake_heal(palace_path, errors, **kwargs):
        captured_kwargs.update(kwargs)
        kwargs.get("progress", print)("probe progress message")
        return []

    monkeypatch.setattr(repair_mod, "maybe_autoheal_fts5_index", _fake_heal)

    _validate_palace_fts5_after_mine(str(palace))

    progress = captured_kwargs.get("progress")
    assert progress is not None
    assert progress is not print
    # Any severity is fine; what matters is that it's a bound method of this
    # module's own logger (routed through logging), not the raw print builtin.
    assert getattr(progress, "__self__", None) is palace_logger
    out, _ = capsys.readouterr()
    assert out == ""


def test_validator_still_raises_when_autoheal_cannot_clear(tmp_path, monkeypatch):
    """Auto-heal is a best-effort first attempt, not a reason to swallow real
    corruption: when maybe_autoheal_fts5_index can't clear the condition
    (broader corruption, lock contention, rebuild failure), the validator
    must still raise. Same build-independence rationale as the test above.
    """
    from mempalace import repair as repair_mod

    palace = tmp_path / "palace"
    _build_palace_with_drawer(palace)

    errors = ["database disk image is malformed"]
    monkeypatch.setattr(repair_mod, "sqlite_integrity_errors", lambda _p: list(errors))
    monkeypatch.setattr(
        repair_mod, "maybe_autoheal_fts5_index", lambda palace_path, errs, **_kwargs: errs
    )

    with pytest.raises(MineValidationError):
        _validate_palace_fts5_after_mine(str(palace))


def test_mine_impl_does_not_print_partial_summary_on_validation_error(
    tmp_path, capsys, monkeypatch
):
    """When _validate_palace_fts5_after_mine raises, miner._mine_impl must
    NOT print the "Mine aborted by exception" partial-progress banner that
    `except Exception` adds. That banner is reserved for true mid-loop
    failures and would double-up with cmd_mine's recovery banner.

    The validator is monkeypatched to raise directly rather than corrupting
    the real sqlite file -- see test_full_chain_raises_through_mine_impl's
    docstring for why real non-isolated corruption isn't usable here
    (ChromaDB's own Rust bindings panic opening a sufficiently corrupted
    file, before this test's validator would ever run).
    """
    from mempalace import miner as miner_mod
    from mempalace import palace as palace_mod

    palace = tmp_path / "palace"
    src = tmp_path / "src"
    src.mkdir()
    (src / "big.md").write_text("lorem ipsum " * 200)

    miner_mod.mine(
        project_dir=str(src),
        palace_path=str(palace),
        wing_override=None,
        agent="mempalace",
        limit=0,
        dry_run=False,
    )
    (src / "big.md").write_text("lorem ipsum dolor " * 200)

    def _raise(path):
        raise MineValidationError(path, ["malformed inverted index for FTS5 table"])

    monkeypatch.setattr(palace_mod, "_validate_palace_fts5_after_mine", _raise)
    monkeypatch.setattr(miner_mod, "_validate_palace_fts5_after_mine", _raise)

    with pytest.raises(MineValidationError):
        miner_mod.mine(
            project_dir=str(src),
            palace_path=str(palace),
            wing_override=None,
            agent="mempalace",
            limit=0,
            dry_run=False,
        )

    captured = capsys.readouterr()
    assert "Mine aborted by exception" not in captured.out + captured.err


def test_mine_validation_error_rejects_empty_errors():
    """Defense-in-depth: the type should not allow construction with an
    empty errors list (the message would say "0 issue(s)") or a blank path.
    """
    with pytest.raises(ValueError, match="at least one error"):
        MineValidationError("/tmp/x", [])
    with pytest.raises(ValueError, match="non-empty palace_path"):
        MineValidationError("", ["err"])


def test_mine_validation_error_errors_attribute_is_immutable():
    err = MineValidationError("/tmp/x", ["a", "b"])
    assert isinstance(err.errors, tuple)
    with pytest.raises(AttributeError):
        err.errors.append("c")  # type: ignore[attr-defined]


def test_helper_silent_on_missing_palace(tmp_path):
    """Helper returns silently when chroma.sqlite3 doesn't exist (palace
    dir missing or mine never wrote). The repair primitive `sqlite_integrity_errors`
    already short-circuits on missing path; verify the contract holds end-to-end.
    """
    missing = tmp_path / "no_palace_here"
    assert _validate_palace_fts5_after_mine(str(missing)) is None


def test_convo_miner_dry_run_skips_validator(tmp_path, monkeypatch):
    """`mine_convos(..., dry_run=True)` must not invoke the validator;
    mirrors the project-miner guarantee.
    """
    from mempalace import convo_miner as convo_mod

    palace = tmp_path / "palace"
    _build_palace_with_drawer(palace)
    src = tmp_path / "convos"
    src.mkdir()

    calls = []

    def _spy(palace_path):
        calls.append(palace_path)

    monkeypatch.setattr(convo_mod, "_validate_palace_fts5_after_mine", _spy)

    convo_mod.mine_convos(
        convo_dir=str(src),
        palace_path=str(palace),
        wing="testwing",
        agent="mempalace",
        limit=0,
        dry_run=True,
    )

    assert calls == [], f"convo_miner must not validate in dry-run, got: {calls}"


# ── 6. Helper closes ChromaDB handles before re-opening read-only ───


def test_close_handles_called_before_quick_check(tmp_path, monkeypatch):
    """Windows guard: ChromaDB mmap handles must be released before the read-only re-open."""
    palace = tmp_path / "palace"
    _build_palace_with_drawer(palace)

    order = []

    from mempalace import repair as repair_mod

    real_close = repair_mod._close_chroma_handles
    real_errors = repair_mod.sqlite_integrity_errors

    def _close_spy(palace_path, *args, **kwargs):
        order.append("close")
        return real_close(palace_path, *args, **kwargs)

    def _errors_spy(palace_path, *args, **kwargs):
        order.append("quick_check")
        return real_errors(palace_path, *args, **kwargs)

    monkeypatch.setattr(repair_mod, "_close_chroma_handles", _close_spy)
    monkeypatch.setattr(repair_mod, "sqlite_integrity_errors", _errors_spy)

    _validate_palace_fts5_after_mine(str(palace))

    assert order == ["close", "quick_check"], (
        f"_close_chroma_handles must run before sqlite_integrity_errors, got: {order}"
    )


# ── 7. --mode extract / mine_formats coverage (post-#1555 gap close) ──


def test_cmd_mine_extract_mode_exits_nonzero_with_banner(tmp_path, monkeypatch, capsys):
    """cmd_mine on --mode extract must surface the same recovery banner as
    --mode convos / project when mine_formats raises MineValidationError.
    The third mine entry point landed in develop via #1555 (3.3.6 release)
    after #1548 was written, so without this wire-up the extract path
    would exit 0 on a corrupted FTS5 palace.
    """
    from mempalace import format_miner as format_mod

    palace = str(tmp_path / "palace")
    src = tmp_path / "docs"
    src.mkdir()

    def _raise(*_, **__):
        raise MineValidationError(palace, ["malformed inverted index for FTS5 table"])

    monkeypatch.setattr(format_mod, "mine_formats", _raise)

    with pytest.raises(SystemExit) as exit_info:
        cli.cmd_mine(_mine_args(palace, str(src), mode="extract"))

    assert exit_info.value.code == 1
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "SQLite-layer corruption detected" in combined
    assert "mempalace repair --yes" in combined


def test_mine_formats_dry_run_skips_validator(tmp_path, monkeypatch):
    """`mine_formats(..., dry_run=True)` must not invoke the validator;
    mirrors the project-miner and convo-miner guarantees.
    """
    from mempalace import format_miner as format_mod

    palace = tmp_path / "palace"
    _build_palace_with_drawer(palace)
    src = tmp_path / "docs"
    src.mkdir()

    calls = []

    def _spy(palace_path):
        calls.append(palace_path)

    monkeypatch.setattr(format_mod, "_validate_palace_fts5_after_mine", _spy)

    format_mod.mine_formats(
        format_dir=str(src),
        palace_path=str(palace),
        wing="testwing",
        agent="mempalace",
        limit=0,
        dry_run=True,
    )

    assert calls == [], f"mine_formats must not validate in dry-run, got: {calls}"


def test_mine_formats_keyboard_interrupt_skips_validator(tmp_path, monkeypatch):
    """KeyboardInterrupt mid-mine routes through the outer `except
    KeyboardInterrupt` branch, NOT the `else` branch where validation
    sits, so a Ctrl-C abort must not trigger end-of-mine FTS5 validation.
    The per-file `except Exception` does not catch BaseException, so the
    interrupt propagates up to the outer handler as required.
    """
    from mempalace import format_miner as format_mod

    palace = tmp_path / "palace"
    _build_palace_with_drawer(palace)
    src = tmp_path / "docs"
    src.mkdir()
    fake_doc = src / "stub.docx"
    fake_doc.write_bytes(b"PK\x03\x04stub")

    calls = []

    def _spy(palace_path):
        calls.append(palace_path)

    def _interrupt(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(format_mod, "_validate_palace_fts5_after_mine", _spy)
    monkeypatch.setattr(format_mod, "scan_formats", lambda *_a, **_k: [fake_doc])
    monkeypatch.setattr(format_mod, "extract_text", _interrupt)

    format_mod.mine_formats(
        format_dir=str(src),
        palace_path=str(palace),
        wing="testwing",
        agent="mempalace",
        limit=0,
        dry_run=False,
    )

    assert calls == [], f"mine_formats must not validate on KeyboardInterrupt, got: {calls}"


def test_mine_formats_full_chain_raises_when_fts5_corrupt(tmp_path, monkeypatch):
    """End-to-end: mine_formats must propagate MineValidationError from
    `_validate_palace_fts5_after_mine`. Mirrors
    `test_full_chain_raises_through_mine_impl` for the extract path.
    Empty source dir is sufficient: the for-loop iterates zero times, the
    `else` branch runs, validation fires on the (mock-corrupted) sqlite.

    The validator is monkeypatched to raise directly rather than corrupting
    the real sqlite file -- see test_full_chain_raises_through_mine_impl's
    docstring for why real non-isolated corruption isn't usable here
    (ChromaDB's own Rust bindings panic opening a sufficiently corrupted
    file, before this test's validator would ever run).
    """
    from mempalace import format_miner as format_mod

    palace = tmp_path / "palace"
    _build_palace_with_drawer(palace)

    src = tmp_path / "docs"
    src.mkdir()

    called = []
    real_errors = ["malformed inverted index for FTS5 table main.embedding_fulltext_search"]

    def _validator_spy(path):
        called.append(path)
        raise MineValidationError(path, real_errors)

    monkeypatch.setattr(format_mod, "_validate_palace_fts5_after_mine", _validator_spy)

    with pytest.raises(MineValidationError) as exc_info:
        format_mod.mine_formats(
            format_dir=str(src),
            palace_path=str(palace),
            wing="testwing",
            agent="mempalace",
            limit=0,
            dry_run=False,
        )

    assert called == [str(palace)], f"validator must be the raise-source; spy recorded: {called}"
    assert list(exc_info.value.errors) == real_errors
    assert exc_info.value.palace_path == str(palace)


def test_mine_formats_full_chain_auto_heals_isolated_fts5_corruption(tmp_path):
    """End-to-end for the extract path: isolated FTS5-only corruption must
    be auto-healed by `_validate_palace_fts5_after_mine`, not raise.
    Mirrors `test_full_chain_auto_heals_isolated_fts5_corruption` for the
    project-miner path (#1926/#1928).
    """
    from mempalace import format_miner as format_mod

    palace = tmp_path / "palace"
    _build_palace_with_drawer(palace)
    _corrupt_fts5_segment(palace / "chroma.sqlite3")

    src = tmp_path / "docs"
    src.mkdir()

    # Must not raise: the corruption is healed before mine_formats returns.
    format_mod.mine_formats(
        format_dir=str(src),
        palace_path=str(palace),
        wing="testwing",
        agent="mempalace",
        limit=0,
        dry_run=False,
    )
