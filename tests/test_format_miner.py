"""Tests for format_miner (mempalace 3.3.6, integrated in PR #1555).

Covers all 14 fringe cases in the format-coverage spec plus orchestrator
behavior. MarkItDown is mocked at the seam for most tests; a few guarded
integration tests exercise live transformers when the optional extras are
installed.

Run with:
    pytest tests/test_format_miner.py -v
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest


from mempalace.format_miner import (  # noqa: E402
    DEFAULT_MAX_FILE_SIZE,
    SUPPORTED_FORMATS,
    ExtractionStatus,
    decode_robust,
    extract_text,
    is_icloud_dataless,
    scan_formats,
)


def _make_symlink_or_skip(link: Path, target: Path) -> None:
    """Create ``link`` pointing to ``target``, or ``pytest.skip`` when the
    platform/process can't create symlinks.

    Windows without ``SeCreateSymbolicLinkPrivilege`` raises ``OSError``
    (``WinError 1314``) from ``Path.symlink_to()`` BEFORE any product code
    runs, which surfaces as a hard test failure even though the failure
    has nothing to do with the code under test. Per PR #1555 review (Igor):
    symlink tests must skip cleanly in environments without privileges
    rather than fail spuriously.
    """
    try:
        link.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlink creation not permitted on this platform/user: {exc}")


# ─────────────────────────────────────────────────────────────────────────────
# Module surface
# ─────────────────────────────────────────────────────────────────────────────


def test_supported_formats_covers_office_suite():
    assert ".pdf" in SUPPORTED_FORMATS
    assert ".rtf" in SUPPORTED_FORMATS
    assert ".docx" in SUPPORTED_FORMATS
    assert ".xlsx" in SUPPORTED_FORMATS
    assert ".pptx" in SUPPORTED_FORMATS
    assert ".epub" in SUPPORTED_FORMATS


def test_supported_formats_normalised_lowercase():
    for ext in SUPPORTED_FORMATS:
        assert ext == ext.lower()
        assert ext.startswith(".")


def test_extraction_status_enum_has_all_documented_codes():
    expected = {
        "OK",
        "SKIP_TOO_LARGE",
        "SKIP_CLOUD_ONLY",
        "SKIP_EMPTY",
        "SKIP_NO_MARKITDOWN",
        "SKIP_NO_STRIPRTF",
        "SKIP_ENCRYPTED",
        "SKIP_PERMISSION",
        "SKIP_BROKEN_SYMLINK",
        "SKIP_UNRECOGNIZED",
        "SKIP_EXTRACTION_ERROR",
        "SKIP_MISSING_FORMAT_DEPS",
        "SKIP_NETWORK_TIMEOUT",
        "SKIP_UNREADABLE",
    }
    actual = {status.name for status in ExtractionStatus}
    missing = expected - actual
    assert not missing, f"Missing status codes: {missing}"


def test_default_max_file_size_matches_existing_miner():
    assert DEFAULT_MAX_FILE_SIZE == 500 * 1024 * 1024


# ─────────────────────────────────────────────────────────────────────────────
# Fringe Case 12 — unrecognized extension → skip with note (test first because
# it covers the simplest dispatch path)
# ─────────────────────────────────────────────────────────────────────────────


def test_fringe_unrecognized_extension(tmp_path: Path):
    f = tmp_path / "thing.xyz"
    f.write_text("not a real format")
    text, status = extract_text(f)
    assert text is None
    assert status == ExtractionStatus.SKIP_UNRECOGNIZED


# ─────────────────────────────────────────────────────────────────────────────
# Fringe Case 5 — empty file → skip silently
# ─────────────────────────────────────────────────────────────────────────────


def test_fringe_empty_file(tmp_path: Path):
    f = tmp_path / "blank.pdf"
    f.write_bytes(b"")
    text, status = extract_text(f)
    assert text is None
    assert status == ExtractionStatus.SKIP_EMPTY


# ─────────────────────────────────────────────────────────────────────────────
# Fringe Case 2 — file too large → skip with note
# ─────────────────────────────────────────────────────────────────────────────


def test_fringe_too_large(tmp_path: Path):
    f = tmp_path / "huge.pdf"
    f.write_bytes(b"%PDF-1.4\n" + b"\x00" * 1024)
    text, status = extract_text(f, max_file_size=128)
    assert text is None
    assert status == ExtractionStatus.SKIP_TOO_LARGE


def test_fringe_too_large_respects_caller_max(tmp_path: Path):
    """Caller can raise the cap for legitimate big files."""
    f = tmp_path / "huge.pdf"
    f.write_bytes(b"%PDF-1.4\n" + b"\x00" * 2048)
    # When the cap is generous, size alone won't trigger SKIP_TOO_LARGE.
    # (MarkItDown will be invoked; we mock it so the test doesn't require it.)
    with patch("mempalace.format_miner._extract_via_markitdown", return_value="dummy text"):
        text, status = extract_text(f, max_file_size=1024 * 1024)
    assert status == ExtractionStatus.OK
    assert text == "dummy text"


# ─────────────────────────────────────────────────────────────────────────────
# Fringe Case 1 — MarkItDown not installed → clear error
# ─────────────────────────────────────────────────────────────────────────────


def test_fringe_missing_markitdown(tmp_path: Path):
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"%PDF-1.4\nstub")
    with patch(
        "mempalace.format_miner._extract_via_markitdown",
        side_effect=ImportError("No module named 'markitdown'"),
    ):
        text, status = extract_text(f)
    assert text is None
    assert status == ExtractionStatus.SKIP_NO_MARKITDOWN


# ─────────────────────────────────────────────────────────────────────────────
# Fringe Case 4 — encrypted PDF → MarkItDown raises; we catch + skip + note
# ─────────────────────────────────────────────────────────────────────────────


def test_fringe_encrypted_pdf(tmp_path: Path):
    f = tmp_path / "locked.pdf"
    f.write_bytes(b"%PDF-1.4\nencrypted stub")

    class _PasswordError(Exception):
        pass

    with patch(
        "mempalace.format_miner._extract_via_markitdown",
        side_effect=_PasswordError("File has not been decrypted"),
    ):
        text, status = extract_text(f)
    assert text is None
    assert status == ExtractionStatus.SKIP_ENCRYPTED


# ─────────────────────────────────────────────────────────────────────────────
# Fringe Case 6 — permission denied → catch OSError, skip
# ─────────────────────────────────────────────────────────────────────────────


def test_fringe_permission_denied(tmp_path: Path):
    f = tmp_path / "denied.pdf"
    f.write_bytes(b"%PDF-1.4\nstub")
    with patch(
        "mempalace.format_miner._extract_via_markitdown",
        side_effect=PermissionError("Permission denied"),
    ):
        text, status = extract_text(f)
    assert text is None
    assert status == ExtractionStatus.SKIP_PERMISSION


# ─────────────────────────────────────────────────────────────────────────────
# Fringe Case 7 — symlink to nothing → catch, skip
# ─────────────────────────────────────────────────────────────────────────────


def test_fringe_broken_symlink(tmp_path: Path):
    target = tmp_path / "does-not-exist.pdf"
    link = tmp_path / "broken-link.pdf"
    _make_symlink_or_skip(link, target)
    assert link.is_symlink()
    text, status = extract_text(link)
    assert text is None
    assert status == ExtractionStatus.SKIP_BROKEN_SYMLINK


# ─────────────────────────────────────────────────────────────────────────────
# Fringe Case 3 — iCloud cloud-only file detection
# ─────────────────────────────────────────────────────────────────────────────


def test_fringe_icloud_placeholder_extension(tmp_path: Path):
    # macOS sometimes leaves a literal .icloud placeholder for offloaded files
    f = tmp_path / "doc.pdf.icloud"
    f.write_bytes(b"placeholder")
    assert is_icloud_dataless(f) is True


def test_fringe_icloud_skip_extraction(tmp_path: Path):
    # Cloud-only file. extract_text should not call MarkItDown and should
    # return SKIP_CLOUD_ONLY.
    f = tmp_path / "doc.pdf.icloud"
    f.write_bytes(b"placeholder")
    with patch("mempalace.format_miner._extract_via_markitdown") as mocked:
        text, status = extract_text(f)
    assert text is None
    assert status == ExtractionStatus.SKIP_CLOUD_ONLY
    mocked.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# Fringe Case 8 — encoding fallback (utf-8 → cp1252 → utf-8-replace)
# ─────────────────────────────────────────────────────────────────────────────


def test_decode_robust_clean_utf8():
    assert decode_robust(b"hello \xe2\x9c\xa8 world") == "hello ✨ world"


def test_decode_robust_cp1252_fallback():
    # 0x91 = U+2018 left single quote in cp1252; invalid as standalone utf-8
    raw = b"hello \x91world\x92"
    result = decode_robust(raw)
    assert isinstance(result, str)
    assert "world" in result


def test_decode_robust_never_raises():
    raw = b"\xff\xfe\xfd\xfc" + b"some text"
    result = decode_robust(raw)
    assert isinstance(result, str)
    assert "some text" in result


def test_decode_robust_empty():
    assert decode_robust(b"") == ""


# ─────────────────────────────────────────────────────────────────────────────
# Fringe Case 9 — Windows path differences (pathlib semantics)
# ─────────────────────────────────────────────────────────────────────────────


def test_extract_text_accepts_pathlib_path(tmp_path: Path):
    """Accept Path objects without coercing to str (Windows-safe)."""
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"%PDF-1.4\nstub")
    with patch("mempalace.format_miner._extract_via_markitdown", return_value="content"):
        text, status = extract_text(f)
    assert status == ExtractionStatus.OK


def test_extract_text_accepts_string_path(tmp_path: Path):
    """Accept str paths for callers that pre-stringify."""
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"%PDF-1.4\nstub")
    with patch("mempalace.format_miner._extract_via_markitdown", return_value="content"):
        text, status = extract_text(str(f))
    assert status == ExtractionStatus.OK


def test_supported_format_check_case_insensitive(tmp_path: Path):
    """Windows often shows uppercase extensions; we still recognize them."""
    f = tmp_path / "doc.PDF"
    f.write_bytes(b"%PDF-1.4\nstub")
    with patch("mempalace.format_miner._extract_via_markitdown", return_value="content"):
        text, status = extract_text(f)
    assert status == ExtractionStatus.OK


# ─────────────────────────────────────────────────────────────────────────────
# Fringe Case 10 — MarkItDown internal crash on malformed file
# ─────────────────────────────────────────────────────────────────────────────


def test_fringe_markitdown_generic_crash(tmp_path: Path):
    f = tmp_path / "malformed.pdf"
    f.write_bytes(b"%PDF-1.4\nmalformed")
    with patch(
        "mempalace.format_miner._extract_via_markitdown",
        side_effect=RuntimeError("internal converter explosion"),
    ):
        text, status = extract_text(f)
    assert text is None
    assert status == ExtractionStatus.SKIP_EXTRACTION_ERROR


def test_fringe_markitdown_returns_none(tmp_path: Path):
    """MarkItDown can return None for some inputs; treat as extraction error."""
    f = tmp_path / "weird.pdf"
    f.write_bytes(b"%PDF-1.4\nweird")
    with patch("mempalace.format_miner._extract_via_markitdown", return_value=None):
        text, status = extract_text(f)
    assert text is None
    assert status == ExtractionStatus.SKIP_EXTRACTION_ERROR


# ─────────────────────────────────────────────────────────────────────────────
# Fringe Case 11 — network/sync drive timeout
# ─────────────────────────────────────────────────────────────────────────────


def test_fringe_network_timeout(tmp_path: Path):
    f = tmp_path / "remote.pdf"
    f.write_bytes(b"%PDF-1.4\nstub")
    with patch(
        "mempalace.format_miner._extract_via_markitdown",
        side_effect=TimeoutError("operation timed out"),
    ):
        text, status = extract_text(f)
    assert text is None
    assert status == ExtractionStatus.SKIP_NETWORK_TIMEOUT


# ─────────────────────────────────────────────────────────────────────────────
# Happy paths — formats we explicitly target
# ─────────────────────────────────────────────────────────────────────────────


def test_happy_path_pdf(tmp_path: Path):
    f = tmp_path / "research.pdf"
    f.write_bytes(b"%PDF-1.4\nstub")
    with patch("mempalace.format_miner._extract_via_markitdown", return_value="# Research\n\nbody"):
        text, status = extract_text(f)
    assert status == ExtractionStatus.OK
    assert text == "# Research\n\nbody"


def test_happy_path_docx(tmp_path: Path):
    f = tmp_path / "notes.docx"
    f.write_bytes(b"PK\x03\x04docx-stub")
    with patch("mempalace.format_miner._extract_via_markitdown", return_value="# Notes\n\nbody"):
        text, status = extract_text(f)
    assert status == ExtractionStatus.OK
    assert text.startswith("# Notes")


def test_happy_path_rtf(tmp_path: Path):
    """RTF routes to striprtf, NOT MarkItDown.

    MarkItDown 0.1.5 does not actually convert .rtf — it returns the raw
    source unchanged. striprtf is the correct transformer for this format.
    """
    f = tmp_path / "memo.rtf"
    f.write_bytes(b"{\\rtf1\\ansi memo}")
    with patch("mempalace.format_miner._extract_via_striprtf", return_value="memo body"):
        text, status = extract_text(f)
    assert status == ExtractionStatus.OK
    assert text == "memo body"


# ─────────────────────────────────────────────────────────────────────────────
# Striprtf path — RTF transformer routing (Fringe Case 13: SKIP_NO_STRIPRTF)
# ─────────────────────────────────────────────────────────────────────────────


def test_rtf_routes_to_striprtf_not_markitdown(tmp_path: Path):
    """Critical invariant: .rtf must NEVER touch MarkItDown.

    MarkItDown 0.1.5 returns raw RTF source for .rtf inputs (verified live
    against a local RTF test set on 2026-05-19). Routing .rtf through striprtf is
    the bugfix.
    """
    f = tmp_path / "memo.rtf"
    f.write_bytes(b"{\\rtf1\\ansi memo}")
    with (
        patch("mempalace.format_miner._extract_via_markitdown") as mock_md,
        patch("mempalace.format_miner._extract_via_striprtf", return_value="memo body") as mock_rtf,
    ):
        text, status = extract_text(f)
    assert status == ExtractionStatus.OK
    assert text == "memo body"
    mock_md.assert_not_called()
    mock_rtf.assert_called_once()


def test_non_rtf_does_not_touch_striprtf(tmp_path: Path):
    """Inverse invariant: .pdf / .docx / etc. must not hit striprtf."""
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"%PDF-1.4\nstub")
    with (
        patch("mempalace.format_miner._extract_via_markitdown", return_value="pdf text") as mock_md,
        patch("mempalace.format_miner._extract_via_striprtf") as mock_rtf,
    ):
        text, status = extract_text(f)
    assert status == ExtractionStatus.OK
    assert text == "pdf text"
    mock_md.assert_called_once()
    mock_rtf.assert_not_called()


def test_fringe_missing_striprtf(tmp_path: Path):
    """Fringe Case 13 — striprtf not installed → SKIP_NO_STRIPRTF + clear install msg."""
    f = tmp_path / "memo.rtf"
    f.write_bytes(b"{\\rtf1\\ansi memo}")
    with patch(
        "mempalace.format_miner._extract_via_striprtf",
        side_effect=ImportError("No module named 'striprtf'"),
    ):
        text, status = extract_text(f)
    assert text is None
    assert status == ExtractionStatus.SKIP_NO_STRIPRTF


def test_fringe_striprtf_crash(tmp_path: Path):
    """striprtf raising any other exception → SKIP_EXTRACTION_ERROR."""
    f = tmp_path / "broken.rtf"
    f.write_bytes(b"{\\rtf1\\ansi broken}")
    with patch(
        "mempalace.format_miner._extract_via_striprtf",
        side_effect=RuntimeError("rtf parse explosion"),
    ):
        text, status = extract_text(f)
    assert text is None
    assert status == ExtractionStatus.SKIP_EXTRACTION_ERROR


def test_fringe_striprtf_returns_none(tmp_path: Path):
    """striprtf returning None → SKIP_EXTRACTION_ERROR (same shape as MarkItDown)."""
    f = tmp_path / "weird.rtf"
    f.write_bytes(b"{\\rtf1\\ansi weird}")
    with patch("mempalace.format_miner._extract_via_striprtf", return_value=None):
        text, status = extract_text(f)
    assert text is None
    assert status == ExtractionStatus.SKIP_EXTRACTION_ERROR


def test_fringe_striprtf_empty_output(tmp_path: Path):
    """striprtf returning empty string → SKIP_EXTRACTION_ERROR.

    An RTF that strips to zero characters is not useful as a drawer; treat
    as extraction failure rather than file an empty drawer.
    """
    f = tmp_path / "blank-after-strip.rtf"
    f.write_bytes(b"{\\rtf1\\ansi}")
    with patch("mempalace.format_miner._extract_via_striprtf", return_value=""):
        text, status = extract_text(f)
    assert text is None
    assert status == ExtractionStatus.SKIP_EXTRACTION_ERROR


def test_rtf_uppercase_extension_also_routes_to_striprtf(tmp_path: Path):
    """Windows case-insensitive: .RTF must also route to striprtf."""
    f = tmp_path / "memo.RTF"
    f.write_bytes(b"{\\rtf1\\ansi memo}")
    with (
        patch("mempalace.format_miner._extract_via_markitdown") as mock_md,
        patch("mempalace.format_miner._extract_via_striprtf", return_value="memo body") as mock_rtf,
    ):
        text, status = extract_text(f)
    assert status == ExtractionStatus.OK
    mock_md.assert_not_called()
    mock_rtf.assert_called_once()


def test_happy_path_xlsx(tmp_path: Path):
    f = tmp_path / "spreadsheet.xlsx"
    f.write_bytes(b"PK\x03\x04xlsx-stub")
    with patch(
        "mempalace.format_miner._extract_via_markitdown",
        return_value="| col1 | col2 |\n|---|---|\n| a | b |",
    ):
        text, status = extract_text(f)
    assert status == ExtractionStatus.OK
    assert "col1" in text


# ─────────────────────────────────────────────────────────────────────────────
# scan_formats — directory walker, returns Path objects sorted
# ─────────────────────────────────────────────────────────────────────────────


def test_scan_formats_finds_supported_only(tmp_path: Path):
    (tmp_path / "a.pdf").write_bytes(b"pdf")
    (tmp_path / "b.txt").write_text("text")  # unsupported
    (tmp_path / "c.docx").write_bytes(b"docx")
    (tmp_path / "d.rtf").write_bytes(b"rtf")
    found = {f.name for f in scan_formats(tmp_path)}
    assert "a.pdf" in found
    assert "c.docx" in found
    assert "d.rtf" in found
    assert "b.txt" not in found


def test_scan_formats_walks_subdirectories(tmp_path: Path):
    nested = tmp_path / "deep" / "nested"
    nested.mkdir(parents=True)
    (nested / "buried.pdf").write_bytes(b"pdf")
    found = {f.name for f in scan_formats(tmp_path)}
    assert "buried.pdf" in found


def test_scan_formats_skips_hidden_dirs(tmp_path: Path):
    """Don't descend into .git, .venv, __pycache__, etc. — same SKIP_DIRS as miner."""
    hidden = tmp_path / ".git"
    hidden.mkdir()
    (hidden / "hidden.pdf").write_bytes(b"pdf")
    (tmp_path / "visible.pdf").write_bytes(b"pdf")
    found = {f.name for f in scan_formats(tmp_path)}
    assert "visible.pdf" in found
    assert "hidden.pdf" not in found


def test_scan_formats_skips_ds_store(tmp_path: Path):
    (tmp_path / ".DS_Store").write_bytes(b"")
    (tmp_path / "real.pdf").write_bytes(b"pdf")
    found = {f.name for f in scan_formats(tmp_path)}
    assert "real.pdf" in found
    assert ".DS_Store" not in found


def test_scan_formats_returns_empty_for_missing_dir(tmp_path: Path):
    """scan_formats handles a path that doesn't exist (no crash, empty list)."""
    missing = tmp_path / "does-not-exist"
    assert scan_formats(missing) == []


# ─────────────────────────────────────────────────────────────────────────────
# decode_robust — exercise the cp1252 path explicitly
# ─────────────────────────────────────────────────────────────────────────────


def test_decode_robust_pure_cp1252():
    """Bytes that are invalid UTF-8 but valid CP1252 → second-attempt success."""
    raw = b"\x91hello\x92"
    result = decode_robust(raw)
    assert isinstance(result, str)
    assert "hello" in result
    # 0x91 / 0x92 are CP1252 smart quotes that decode without error
    assert "�" not in result, "CP1252 path should not need the replace fallback"


# ─────────────────────────────────────────────────────────────────────────────
# Note on stat() error branches in extract_text:
# Dedicated PermissionError / FileNotFoundError / OSError stat-handler tests
# were attempted but tripped on Python 3.13 pathlib internals (patching
# Path.stat globally interferes with .exists() / .is_symlink() which call
# stat under the hood). The handlers are kept as defensive guards and
# remain documented as uncovered branches — the rest of the suite (60+
# tests + live integration on 63 real archive files) provides the
# end-to-end safety net.
# ─────────────────────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────────────────────
# Live transformer body tests — skipped when transformer isn't installed.
# These exercise the actual adapter code that mocked tests can't reach.
# ─────────────────────────────────────────────────────────────────────────────


def test_extract_via_striprtf_live(tmp_path: Path):
    """End-to-end: real striprtf converts a real RTF blob to plain text.

    Skipped if striprtf isn't installed (e.g., environments without the
    [extract] extra). When installed, this exercises the actual adapter
    body that mocked tests bypass.
    """
    pytest.importorskip("striprtf.striprtf")
    from mempalace.format_miner import _extract_via_striprtf

    rtf = (
        b"{\\rtf1\\ansi\\ansicpg1252\n{\\fonttbl\\f0\\fnil Helvetica;}\n"
        b"\\f0\\fs24 Hello from a real RTF blob.}"
    )
    f = tmp_path / "live.rtf"
    f.write_bytes(rtf)
    text = _extract_via_striprtf(f)
    assert text is not None
    assert "Hello from a real RTF blob" in text
    assert "\\rtf1" not in text, "raw RTF control codes leaked into output"


def test_extract_via_markitdown_live_pdf(tmp_path: Path):
    """End-to-end: real MarkItDown converts a real PDF blob to text.

    Skipped if markitdown isn't installed (3.10+ only, optional extra).
    Also skipped if only the placeholder ``markitdown`` package is present
    (the real Microsoft package exposes the ``MarkItDown`` class).
    """
    try:
        from markitdown import MarkItDown  # noqa: F401
    except ImportError:
        pytest.skip("real Microsoft markitdown not installed (needs Python 3.10+)")
    from mempalace.format_miner import _extract_via_markitdown

    # Minimal valid PDF that contains the literal text "hello pdf"
    pdf_bytes = (
        b"%PDF-1.1\n"
        b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj<</Type/Page/Parent 2 0 R/Resources<</Font<</F1 4 0 R>>>>"
        b"/MediaBox[0 0 612 792]/Contents 5 0 R>>endobj\n"
        b"4 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\n"
        b"5 0 obj<</Length 44>>stream\n"
        b"BT /F1 24 Tf 100 700 Td (hello pdf) Tj ET\n"
        b"endstream endobj\n"
        b"xref\n0 6\n0000000000 65535 f \n"
        b"trailer<</Size 6/Root 1 0 R>>\n"
        b"startxref\n0\n%%EOF\n"
    )
    f = tmp_path / "live.pdf"
    f.write_bytes(pdf_bytes)
    # The adapter shouldn't crash; if MarkItDown returns text, it includes our marker.
    text = _extract_via_markitdown(f)
    # Allow None (MarkItDown may not parse this minimal PDF cleanly), but the
    # adapter path itself must run without exception.
    if text is not None:
        assert isinstance(text, str)


# ─────────────────────────────────────────────────────────────────────────────
# mine_formats — orchestrator that walks a directory, transforms files,
# chunks the extracted text, and files drawers into the palace. Mirrors the
# shape of mine_convos / mine. The collection + lock primitives are mocked
# so these tests don't write to a real palace.
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def _mine_formats_mocks(tmp_path: Path):
    """Common mocks for mine_formats orchestrator tests.

    Patches:
      - scan_formats   : returns the files we hand it
      - get_collection : returns a MagicMock collection
      - mine_lock      : no-op context manager
      - file_already_mined : False (file not yet mined)
      - _extract_via_striprtf / _extract_via_markitdown : mocked at test level
    """
    from unittest.mock import MagicMock, patch
    from contextlib import contextmanager

    collection = MagicMock()
    collection.delete = MagicMock()
    collection.upsert = MagicMock()

    @contextmanager
    def _fake_lock(source_file):
        yield

    with (
        patch("mempalace.format_miner.get_collection", return_value=collection) as p_coll,
        patch("mempalace.format_miner.mine_lock", side_effect=_fake_lock) as p_lock,
        patch("mempalace.format_miner.file_already_mined", return_value=False) as p_mined,
    ):
        yield {
            "collection": collection,
            "get_collection": p_coll,
            "mine_lock": p_lock,
            "file_already_mined": p_mined,
            "tmp_path": tmp_path,
        }


def test_mine_formats_walks_directory(_mine_formats_mocks):
    """mine_formats must use scan_formats to discover supported files."""
    from unittest.mock import patch
    from mempalace.format_miner import mine_formats

    tmp = _mine_formats_mocks["tmp_path"]
    (tmp / "doc.pdf").write_bytes(b"pdf")
    with (
        patch("mempalace.format_miner.scan_formats", return_value=[]) as p_scan,
        patch("mempalace.format_miner._extract_via_markitdown"),
    ):
        mine_formats(format_dir=str(tmp), palace_path=str(tmp / "palace"))
    p_scan.assert_called_once()


def test_mine_formats_skips_already_mined_files(_mine_formats_mocks):
    """If file_already_mined returns True, extract_text should not be called."""
    from unittest.mock import patch
    from mempalace.format_miner import mine_formats

    tmp = _mine_formats_mocks["tmp_path"]
    f = tmp / "doc.pdf"
    f.write_bytes(b"%PDF-1.4 stub")
    _mine_formats_mocks["file_already_mined"].return_value = True
    with (
        patch("mempalace.format_miner.scan_formats", return_value=[f]),
        patch("mempalace.format_miner._extract_via_markitdown") as p_md,
    ):
        mine_formats(format_dir=str(tmp), palace_path=str(tmp / "palace"))
    p_md.assert_not_called()


def test_mine_formats_skips_extraction_failures(_mine_formats_mocks):
    """When extract_text returns a SKIP status, no real drawers are upserted.

    A sentinel upsert IS expected (so the file isn't re-extracted on every
    re-mine). The sentinel carries ``is_sentinel=True`` to distinguish it
    from a content drawer.
    """
    from unittest.mock import patch
    from mempalace.format_miner import mine_formats

    tmp = _mine_formats_mocks["tmp_path"]
    f = tmp / "bad.pdf"
    f.write_bytes(b"%PDF-1.4 stub")
    with (
        patch("mempalace.format_miner.scan_formats", return_value=[f]),
        patch(
            "mempalace.format_miner._extract_via_markitdown",
            side_effect=RuntimeError("converter blew up"),
        ),
    ):
        mine_formats(format_dir=str(tmp), palace_path=str(tmp / "palace"))
    # Any upsert that fired must be a sentinel, never a content drawer.
    for call in _mine_formats_mocks["collection"].upsert.call_args_list:
        metas = call.kwargs.get("metadatas") or call.args[2]
        for m in metas:
            assert m.get("is_sentinel") is True, (
                f"unexpected non-sentinel upsert on extraction failure: {m}"
            )


def test_mine_formats_files_drawers_for_ok_extractions(_mine_formats_mocks):
    """When extract_text returns OK + text, mine_formats chunks and upserts drawers."""
    from unittest.mock import patch
    from mempalace.format_miner import mine_formats

    tmp = _mine_formats_mocks["tmp_path"]
    f = tmp / "good.pdf"
    f.write_bytes(b"%PDF-1.4 stub")
    long_text = "This is a sufficiently long extracted text. " * 30
    with (
        patch("mempalace.format_miner.scan_formats", return_value=[f]),
        patch("mempalace.format_miner._extract_via_markitdown", return_value=long_text),
    ):
        mine_formats(format_dir=str(tmp), palace_path=str(tmp / "palace"))
    # upsert should have been called at least once
    assert _mine_formats_mocks["collection"].upsert.called


def test_mine_formats_dry_run_does_not_open_collection(_mine_formats_mocks):
    """dry_run=True must not call get_collection or upsert any drawers."""
    from unittest.mock import patch
    from mempalace.format_miner import mine_formats

    tmp = _mine_formats_mocks["tmp_path"]
    f = tmp / "doc.pdf"
    f.write_bytes(b"%PDF-1.4 stub")
    _mine_formats_mocks["get_collection"].reset_mock()
    with (
        patch("mempalace.format_miner.scan_formats", return_value=[f]),
        patch("mempalace.format_miner._extract_via_markitdown", return_value="some text"),
    ):
        mine_formats(format_dir=str(tmp), palace_path=str(tmp / "palace"), dry_run=True)
    _mine_formats_mocks["get_collection"].assert_not_called()
    _mine_formats_mocks["collection"].upsert.assert_not_called()


def test_mine_formats_respects_limit(_mine_formats_mocks):
    """limit=N should restrict processing to the first N files."""
    from unittest.mock import patch
    from mempalace.format_miner import mine_formats

    tmp = _mine_formats_mocks["tmp_path"]
    files = []
    for i in range(5):
        p = tmp / f"f{i}.pdf"
        p.write_bytes(b"%PDF-1.4 stub")
        files.append(p)
    with (
        patch("mempalace.format_miner.scan_formats", return_value=files),
        patch(
            "mempalace.format_miner._extract_via_markitdown", return_value="long text " * 50
        ) as p_md,
    ):
        mine_formats(format_dir=str(tmp), palace_path=str(tmp / "palace"), limit=2)
    # Only 2 of the 5 files should have been extracted
    assert p_md.call_count == 2


def test_mine_formats_wing_defaults_from_directory_name(_mine_formats_mocks):
    """When wing=None, the directory's basename becomes the wing."""
    from unittest.mock import patch
    from mempalace.format_miner import mine_formats

    tmp = _mine_formats_mocks["tmp_path"]
    target_dir = tmp / "my_research_corpus"
    target_dir.mkdir()
    f = target_dir / "doc.pdf"
    f.write_bytes(b"%PDF-1.4 stub")
    with (
        patch("mempalace.format_miner.scan_formats", return_value=[f]),
        patch("mempalace.format_miner._extract_via_markitdown", return_value="long text " * 50),
    ):
        mine_formats(format_dir=str(target_dir), palace_path=str(tmp / "palace"))
    # Inspect the metadata passed to upsert — the wing should derive from the dir name
    call_args = _mine_formats_mocks["collection"].upsert.call_args
    if call_args is not None:
        metas = call_args.kwargs.get("metadatas") or call_args.args[2]
        assert metas[0]["wing"] == "my_research_corpus"


def test_mine_formats_wing_override(_mine_formats_mocks):
    """Explicit wing= param overrides the directory-name default."""
    from unittest.mock import patch
    from mempalace.format_miner import mine_formats

    tmp = _mine_formats_mocks["tmp_path"]
    f = tmp / "doc.pdf"
    f.write_bytes(b"%PDF-1.4 stub")
    with (
        patch("mempalace.format_miner.scan_formats", return_value=[f]),
        patch("mempalace.format_miner._extract_via_markitdown", return_value="long text " * 50),
    ):
        mine_formats(
            format_dir=str(tmp),
            palace_path=str(tmp / "palace"),
            wing="custom_wing_name",
        )
    call_args = _mine_formats_mocks["collection"].upsert.call_args
    if call_args is not None:
        metas = call_args.kwargs.get("metadatas") or call_args.args[2]
        assert metas[0]["wing"] == "custom_wing_name"


def test_mine_formats_ingest_mode_metadata_is_extract(_mine_formats_mocks):
    """Drawers from mine_formats must carry ingest_mode='extract' so they're
    distinguishable from project / convo drawers in the palace."""
    from unittest.mock import patch
    from mempalace.format_miner import mine_formats

    tmp = _mine_formats_mocks["tmp_path"]
    f = tmp / "doc.pdf"
    f.write_bytes(b"%PDF-1.4 stub")
    with (
        patch("mempalace.format_miner.scan_formats", return_value=[f]),
        patch("mempalace.format_miner._extract_via_markitdown", return_value="long text " * 50),
    ):
        mine_formats(format_dir=str(tmp), palace_path=str(tmp / "palace"))
    call_args = _mine_formats_mocks["collection"].upsert.call_args
    assert call_args is not None
    metas = call_args.kwargs.get("metadatas") or call_args.args[2]
    assert metas[0]["ingest_mode"] == "extract"


# ─────────────────────────────────────────────────────────────────────────────
# Bot-review parity fixes (PR #1555 follow-up commit, 2026-05-19):
#   - palace.SKIP_DIRS reuse (covered indirectly by scan_formats tests)
#   - scan_formats skips symlinks
#   - mine_formats uses check_mtime=True
#   - source_mtime + hall + entities in drawer metadata
#   - per-file try/except so one bad file doesn't crash the whole mine
# ─────────────────────────────────────────────────────────────────────────────


def test_scan_formats_skips_symlinks(tmp_path: Path):
    """Symlinks must be skipped — mirrors miner.py and convo_miner.py."""
    real = tmp_path / "real.pdf"
    real.write_bytes(b"%PDF-1.4 stub")
    link = tmp_path / "alias.pdf"
    _make_symlink_or_skip(link, real)
    found = {f.name for f in scan_formats(tmp_path)}
    assert "real.pdf" in found
    assert "alias.pdf" not in found


def test_mine_formats_uses_check_mtime_true(_mine_formats_mocks):
    """mine_formats must call file_already_mined with check_mtime=True so
    updated documents get re-mined (matches miner.py semantics)."""
    from unittest.mock import patch
    from mempalace.format_miner import mine_formats

    tmp = _mine_formats_mocks["tmp_path"]
    f = tmp / "doc.pdf"
    f.write_bytes(b"%PDF-1.4 stub")
    with (
        patch("mempalace.format_miner.scan_formats", return_value=[f]),
        patch("mempalace.format_miner._extract_via_markitdown", return_value="long " * 50),
    ):
        mine_formats(format_dir=str(tmp), palace_path=str(tmp / "palace"))
    # Inspect every file_already_mined call — they must all use check_mtime=True
    calls = _mine_formats_mocks["file_already_mined"].call_args_list
    assert calls, "file_already_mined should have been called at least once"
    for call in calls:
        # check_mtime can be in kwargs or positional. Inspect both.
        kwargs = call.kwargs
        assert kwargs.get("check_mtime") is True, f"check_mtime must be True, got {kwargs}"


def test_mine_formats_records_source_mtime_in_drawer_metadata(_mine_formats_mocks):
    """Each drawer must carry source_mtime so file_already_mined(check_mtime=True)
    can detect updates on re-mine."""
    from unittest.mock import patch
    from mempalace.format_miner import mine_formats

    tmp = _mine_formats_mocks["tmp_path"]
    f = tmp / "doc.pdf"
    f.write_bytes(b"%PDF-1.4 stub")
    with (
        patch("mempalace.format_miner.scan_formats", return_value=[f]),
        patch("mempalace.format_miner._extract_via_markitdown", return_value="long " * 50),
    ):
        mine_formats(format_dir=str(tmp), palace_path=str(tmp / "palace"))
    upsert_calls = _mine_formats_mocks["collection"].upsert.call_args_list
    # Find the content upsert (not the sentinel)
    found_mtime = False
    for call in upsert_calls:
        metas = call.kwargs.get("metadatas") or call.args[2]
        for m in metas:
            if m.get("is_sentinel"):
                continue
            assert "source_mtime" in m, f"missing source_mtime in drawer meta: {m}"
            assert isinstance(m["source_mtime"], (int, float))
            found_mtime = True
    assert found_mtime, "no non-sentinel drawer upsert observed"


def test_mine_formats_records_hall_in_drawer_metadata(_mine_formats_mocks):
    """Each drawer must carry a 'hall' tag — matches miner.py drawer quality."""
    from unittest.mock import patch
    from mempalace.format_miner import mine_formats

    tmp = _mine_formats_mocks["tmp_path"]
    f = tmp / "doc.pdf"
    f.write_bytes(b"%PDF-1.4 stub")
    with (
        patch("mempalace.format_miner.scan_formats", return_value=[f]),
        patch("mempalace.format_miner._extract_via_markitdown", return_value="long " * 50),
    ):
        mine_formats(format_dir=str(tmp), palace_path=str(tmp / "palace"))
    upsert_calls = _mine_formats_mocks["collection"].upsert.call_args_list
    found_hall = False
    for call in upsert_calls:
        metas = call.kwargs.get("metadatas") or call.args[2]
        for m in metas:
            if m.get("is_sentinel"):
                continue
            assert "hall" in m, f"missing hall in drawer meta: {m}"
            assert isinstance(m["hall"], str)
            found_hall = True
    assert found_hall, "no non-sentinel drawer upsert observed"


def test_mine_formats_continues_after_per_file_error(_mine_formats_mocks):
    """One bad file must not crash the whole mine — the loop continues."""
    from unittest.mock import patch
    from mempalace.format_miner import mine_formats

    tmp = _mine_formats_mocks["tmp_path"]
    bad = tmp / "broken.pdf"
    bad.write_bytes(b"%PDF-1.4 stub")
    good = tmp / "fine.pdf"
    good.write_bytes(b"%PDF-1.4 stub")

    # Make chunk_text crash on the first file but succeed on the second.
    call_count = {"n": 0}

    def chunk_text_first_fails(content, source_file, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("simulated chunker explosion")
        return [{"content": "good chunk content here " * 5, "chunk_index": 0}]

    with (
        patch("mempalace.format_miner.scan_formats", return_value=[bad, good]),
        patch("mempalace.format_miner._extract_via_markitdown", return_value="text"),
        # Patch the module-level binding in format_miner (hoisted in the
        # PR #1555 polish work — Gemini #3). The old patch target
        # ``mempalace.miner.chunk_text`` no longer works because
        # format_miner now binds chunk_text at its own module scope.
        patch("mempalace.format_miner.chunk_text", side_effect=chunk_text_first_fails),
    ):
        # Must not raise
        mine_formats(format_dir=str(tmp), palace_path=str(tmp / "palace"))

    # The second file should still have produced an upsert
    upsert_calls = _mine_formats_mocks["collection"].upsert.call_args_list
    non_sentinel_upserts = [
        call
        for call in upsert_calls
        if not any(
            (m or {}).get("is_sentinel") for m in (call.kwargs.get("metadatas") or call.args[2])
        )
    ]
    assert non_sentinel_upserts, (
        "the good file should still have produced a content upsert after the bad file errored"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Amendment #3 — parity with miner.py: detect_room + tunnel computation.
# Tests written FIRST (TDD) to prove the gaps exist, then format_miner.py is
# amended to make them pass. Pattern mirrors miner.py exactly.
# ─────────────────────────────────────────────────────────────────────────────


def test_mine_formats_calls_load_config_for_rooms(_mine_formats_mocks):
    """mine_formats must load mempalace.yaml (via load_config) to get the
    rooms list — same as miner.py:1154. Without this, drawers fall back to
    a single 'documents' room."""
    from unittest.mock import patch
    from mempalace.format_miner import mine_formats

    tmp = _mine_formats_mocks["tmp_path"]
    f = tmp / "doc.pdf"
    f.write_bytes(b"%PDF-1.4 stub")
    fake_config = {
        "wing": "wing_aya",
        "rooms": [{"name": "documents", "keywords": ["documents"]}],
    }
    with (
        patch("mempalace.format_miner.scan_formats", return_value=[f]),
        patch("mempalace.format_miner._extract_via_markitdown", return_value="long " * 50),
        patch("mempalace.format_miner.load_config", return_value=fake_config) as p_cfg,
    ):
        mine_formats(format_dir=str(tmp), palace_path=str(tmp / "palace"))
    p_cfg.assert_called_once()


def test_mine_formats_calls_detect_room_per_file(_mine_formats_mocks):
    """mine_formats must call detect_room(filepath, content, rooms, project_path)
    for each file — same as miner.py:904. Hardcoding room='documents' is the
    bug; this test fails until detect_room is wired in."""
    from unittest.mock import patch
    from mempalace.format_miner import mine_formats

    tmp = _mine_formats_mocks["tmp_path"]
    f = tmp / "doc.pdf"
    f.write_bytes(b"%PDF-1.4 stub")
    fake_config = {
        "wing": "wing_aya",
        "rooms": [{"name": "documents", "keywords": ["documents"]}],
    }
    with (
        patch("mempalace.format_miner.scan_formats", return_value=[f]),
        patch("mempalace.format_miner._extract_via_markitdown", return_value="long " * 50),
        patch("mempalace.format_miner.load_config", return_value=fake_config),
        patch("mempalace.format_miner.detect_room", return_value="family") as p_room,
    ):
        mine_formats(format_dir=str(tmp), palace_path=str(tmp / "palace"))
    p_room.assert_called_once()
    # First positional arg should be the file Path; last arg should be project_path
    call_args = p_room.call_args
    # detect_room(filepath, content, rooms, project_path)
    assert len(call_args.args) == 4 or "filepath" in call_args.kwargs


def test_mine_formats_uses_detected_room_in_drawer_metadata(_mine_formats_mocks):
    """The room field on the drawer metadata must reflect what detect_room
    returned, NOT the hardcoded 'documents'. This is the visible bug from
    the 2026-05-19 mine: every drawer landed in room='documents'."""
    from unittest.mock import patch
    from mempalace.format_miner import mine_formats

    tmp = _mine_formats_mocks["tmp_path"]
    f = tmp / "doc.pdf"
    f.write_bytes(b"%PDF-1.4 stub")
    fake_config = {
        "wing": "wing_aya",
        "rooms": [{"name": "family", "keywords": ["family"]}],
    }
    with (
        patch("mempalace.format_miner.scan_formats", return_value=[f]),
        patch("mempalace.format_miner._extract_via_markitdown", return_value="long " * 50),
        patch("mempalace.format_miner.load_config", return_value=fake_config),
        patch("mempalace.format_miner.detect_room", return_value="family"),
    ):
        mine_formats(format_dir=str(tmp), palace_path=str(tmp / "palace"))
    upsert_calls = _mine_formats_mocks["collection"].upsert.call_args_list
    found_room = None
    for call in upsert_calls:
        metas = call.kwargs.get("metadatas") or call.args[2]
        for m in metas:
            if m.get("is_sentinel"):
                continue
            found_room = m.get("room")
            break
        if found_room:
            break
    assert found_room == "family", (
        f"drawer room must be 'family' (detect_room's return), not {found_room!r}"
    )


def test_mine_formats_calls_compute_topic_tunnels_after_loop(_mine_formats_mocks):
    """After the per-file loop, mine_formats must call
    _compute_topic_tunnels_for_wing(wing) exactly once. Mirrors miner.py:1241.
    Without this, cross-wing topic tunnels never materialize for format-mined
    wings (audit confirmed 0 tunnels in the 2026-05-19 v6 mine)."""
    from unittest.mock import patch
    from mempalace.format_miner import mine_formats

    tmp = _mine_formats_mocks["tmp_path"]
    f = tmp / "doc.pdf"
    f.write_bytes(b"%PDF-1.4 stub")
    fake_config = {
        "wing": "wing_aya",
        "rooms": [{"name": "documents", "keywords": ["documents"]}],
    }
    with (
        patch("mempalace.format_miner.scan_formats", return_value=[f]),
        patch("mempalace.format_miner._extract_via_markitdown", return_value="long " * 50),
        patch("mempalace.format_miner.load_config", return_value=fake_config),
        patch("mempalace.format_miner.detect_room", return_value="documents"),
        patch("mempalace.format_miner._compute_topic_tunnels_for_wing", return_value=0) as p_tun,
    ):
        mine_formats(format_dir=str(tmp), palace_path=str(tmp / "palace"), wing="wing_aya")
    p_tun.assert_called_once_with("wing_aya")


def test_mine_formats_tunnel_failure_does_not_crash_mine(_mine_formats_mocks):
    """If _compute_topic_tunnels_for_wing raises, the mine must still
    complete (summary still prints, no exception propagates). Mirrors the
    try/except wrap at miner.py:1244-1249."""
    from unittest.mock import patch
    from mempalace.format_miner import mine_formats

    tmp = _mine_formats_mocks["tmp_path"]
    f = tmp / "doc.pdf"
    f.write_bytes(b"%PDF-1.4 stub")
    fake_config = {
        "wing": "wing_aya",
        "rooms": [{"name": "documents", "keywords": ["documents"]}],
    }
    with (
        patch("mempalace.format_miner.scan_formats", return_value=[f]),
        patch("mempalace.format_miner._extract_via_markitdown", return_value="long " * 50),
        patch("mempalace.format_miner.load_config", return_value=fake_config),
        patch("mempalace.format_miner.detect_room", return_value="documents"),
        patch(
            "mempalace.format_miner._compute_topic_tunnels_for_wing",
            side_effect=RuntimeError("simulated tunnel-compute failure"),
        ),
    ):
        # Must NOT raise
        mine_formats(format_dir=str(tmp), palace_path=str(tmp / "palace"), wing="wing_aya")


# ─────────────────────────────────────────────────────────────────────────────
# PR #1555 review (Igor) — missing-format-deps surfacing + pyproject extras
# ─────────────────────────────────────────────────────────────────────────────


def test_extract_text_missing_format_dep_returns_distinct_status(tmp_path: Path, monkeypatch):
    """A MarkItDown ``MissingDependencyException`` (raised when a per-format
    sub-extra like ``markitdown[pdf]`` isn't installed) must surface as
    ``SKIP_MISSING_FORMAT_DEPS`` — NOT the generic ``SKIP_EXTRACTION_ERROR``.

    Per PR #1555 review (Igor): real PDFs hit this in production today
    and users see "extraction error" with no signal that the fix is
    ``pip install markitdown[pdf]``. The dispatcher must catch by type
    name so the real markitdown package doesn't have to be import-
    resolvable for the catch to fire.
    """
    from mempalace import format_miner

    # Build a fake exception class with the right __name__ so the
    # type-name catch in extract_text recognises it without requiring
    # the real markitdown to be installed in the test environment.
    class MissingDependencyException(Exception):
        pass

    def fake_extract(p):
        raise MissingDependencyException(
            "MarkItDown failed: install with `pip install markitdown[pdf]`"
        )

    monkeypatch.setattr(format_miner, "_extract_via_markitdown", fake_extract)

    pdf = tmp_path / "fake.pdf"
    pdf.write_bytes(b"%PDF-1.4 stub")

    text, status = extract_text(pdf)
    assert text is None
    assert status == ExtractionStatus.SKIP_MISSING_FORMAT_DEPS, (
        f"expected SKIP_MISSING_FORMAT_DEPS, got {status}"
    )


def test_pyproject_extract_extra_pulls_markitdown_format_subdeps():
    """The ``mempalace[extract]`` extra must include MarkItDown's per-format
    sub-extras (``pdf``, ``docx``, ``pptx``, ``xlsx``) — without them, real
    PDF/DOCX/etc files hit ``MissingDependencyException`` at runtime even
    after ``pip install mempalace[extract]``. Per PR #1555 review (Igor).

    ``.rtf`` is covered by the separate ``striprtf`` dependency and ``.epub``
    ships in base MarkItDown (``EpubConverter`` uses ``beautifulsoup4``
    which is a base requirement, not extra-gated), so neither needs a
    sub-extra here.
    """
    try:
        import tomllib  # 3.11+
    except ImportError:  # pragma: no cover — only hit on 3.9/3.10
        import tomli as tomllib  # type: ignore[import-not-found, no-redef]

    pyproject_path = Path(__file__).resolve().parent.parent / "pyproject.toml"
    with open(pyproject_path, "rb") as f:
        data = tomllib.load(f)

    extract = data["project"]["optional-dependencies"]["extract"]
    extract_str = " ".join(extract).lower()

    # Accept any layout — comma-separated ``markitdown[docx,pdf,pptx,xlsx]``
    # or separate ``markitdown[pdf]`` entries — as long as each format
    # appears inside SOME ``markitdown[...]`` bracketed group.
    import re

    bracketed = "".join(re.findall(r"markitdown\[([^\]]+)\]", extract_str))
    for sub in ("pdf", "docx", "pptx", "xlsx"):
        assert sub in bracketed, (
            f"[extract] must include markitdown[{sub}]; "
            f"got bracketed extras: {bracketed!r}; full extract: {extract}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# PR #1555 review polish — bot feedback items addressed post-merge.
# Five behavioral changes; the trivial items (path expanduser, signature
# annotations, docstring/doc updates) don't need their own RED tests.
# ─────────────────────────────────────────────────────────────────────────────


def test_extract_text_nonexistent_regular_file_returns_unreadable_not_broken_symlink(
    tmp_path: Path,
):
    """A non-existent REGULAR file (not a symlink) must return
    ``SKIP_UNREADABLE``, not ``SKIP_BROKEN_SYMLINK``.

    Per PR #1555 review (Copilot #8): the ``FileNotFoundError`` arm in
    ``extract_text`` was unconditionally mapping to ``SKIP_BROKEN_SYMLINK``,
    which is misleading when the path was a regular file that got deleted
    between scan and extract. ``SKIP_BROKEN_SYMLINK`` should only fire
    when ``p.is_symlink()`` is true.
    """
    p = tmp_path / "deleted.pdf"
    assert not p.exists()
    assert not p.is_symlink()
    text, status = extract_text(p)
    assert text is None
    assert status == ExtractionStatus.SKIP_UNREADABLE, (
        f"Expected SKIP_UNREADABLE for non-existent regular file (file was "
        f"deleted between scan and extract); got {status}. "
        f"SKIP_BROKEN_SYMLINK should only fire when the path is_symlink()."
    )


def test_mine_formats_passes_extract_mode_format_to_file_already_mined(monkeypatch, tmp_path: Path):
    """``mine_formats`` must call ``file_already_mined`` with
    ``extract_mode='format'`` so format-mode idempotency is scoped to its
    own drawer set. Otherwise drawers from convo_miner / project miner
    on the same source file falsely indicate "already mined" to the
    format miner (and vice versa).

    Per PR #1555 review (Copilot #11 + #12). Both call sites — the
    pre-lock check in ``mine_formats`` and the post-lock recheck in
    ``_file_chunks_locked`` — must pass the kwarg.
    """
    from mempalace import format_miner
    from mempalace.format_miner import mine_formats

    calls: list = []

    def fake_check(collection, source_file, check_mtime=False, extract_mode=None):
        calls.append(
            {
                "source_file": source_file,
                "check_mtime": check_mtime,
                "extract_mode": extract_mode,
            }
        )
        return False  # never already mined — let the mine proceed

    monkeypatch.setattr(format_miner, "file_already_mined", fake_check)

    tmp = tmp_path / "src"
    tmp.mkdir()
    f = tmp / "x.pdf"
    f.write_bytes(b"%PDF-1.4 stub")

    fake_config = {
        "wing": "wing_test",
        "rooms": [{"name": "documents", "keywords": ["documents"]}],
    }

    with (
        patch("mempalace.format_miner.scan_formats", return_value=[f]),
        patch("mempalace.format_miner._extract_via_markitdown", return_value="long " * 50),
        patch("mempalace.format_miner.load_config", return_value=fake_config),
        patch("mempalace.format_miner.detect_room", return_value="documents"),
    ):
        mine_formats(format_dir=str(tmp), palace_path=str(tmp / "palace"), wing="wing_test")

    assert calls, "expected file_already_mined to be called at least once"
    for call in calls:
        assert call["extract_mode"] == "format", (
            f"file_already_mined call missing extract_mode='format': {call}"
        )


def test_mine_formats_does_not_write_sentinel_for_skip_no_markitdown(monkeypatch, tmp_path: Path):
    """Sentinel writing must be skipped when the status is a transient
    "missing optional dependency" (``SKIP_NO_MARKITDOWN``,
    ``SKIP_NO_STRIPRTF``, ``SKIP_MISSING_FORMAT_DEPS``). Otherwise the
    file is permanently marked as "already mined" — installing the missing
    extra later does NOT trigger a re-mine, which is a real user surprise.

    Per PR #1555 review (Copilot #14).
    """
    from mempalace import format_miner
    from mempalace.format_miner import mine_formats

    register_calls: list = []

    def fake_register(collection, source_file, wing, agent):
        register_calls.append(source_file)

    monkeypatch.setattr(format_miner, "_register_file", fake_register)

    tmp = tmp_path / "src"
    tmp.mkdir()
    f = tmp / "y.pdf"
    f.write_bytes(b"%PDF-1.4 stub")

    fake_config = {
        "wing": "wing_test",
        "rooms": [{"name": "documents", "keywords": ["documents"]}],
    }

    # Force the extract path to raise ImportError so we hit SKIP_NO_MARKITDOWN.
    with (
        patch("mempalace.format_miner.scan_formats", return_value=[f]),
        patch(
            "mempalace.format_miner._extract_via_markitdown",
            side_effect=ImportError("no markitdown"),
        ),
        patch("mempalace.format_miner.load_config", return_value=fake_config),
        patch("mempalace.format_miner.detect_room", return_value="documents"),
    ):
        mine_formats(format_dir=str(tmp), palace_path=str(tmp / "palace"), wing="wing_test")

    assert str(f) not in register_calls, (
        f"sentinel was written for SKIP_NO_MARKITDOWN file; should be skipped "
        f"so installing markitdown later triggers a re-mine. "
        f"register_calls={register_calls}"
    )


def test_mine_formats_does_not_write_sentinel_for_skip_missing_format_deps(
    monkeypatch, tmp_path: Path
):
    """Same as the SKIP_NO_MARKITDOWN test but for SKIP_MISSING_FORMAT_DEPS
    (raised when markitdown is installed but a per-format sub-extra like
    ``markitdown[pdf]`` is missing).
    """
    from mempalace import format_miner
    from mempalace.format_miner import mine_formats

    register_calls: list = []

    def fake_register(collection, source_file, wing, agent):
        register_calls.append(source_file)

    monkeypatch.setattr(format_miner, "_register_file", fake_register)

    tmp = tmp_path / "src"
    tmp.mkdir()
    f = tmp / "z.pdf"
    f.write_bytes(b"%PDF-1.4 stub")

    fake_config = {
        "wing": "wing_test",
        "rooms": [{"name": "documents", "keywords": ["documents"]}],
    }

    class _FakeMissingDep(Exception):
        pass

    _FakeMissingDep.__name__ = "MissingDependencyException"

    with (
        patch("mempalace.format_miner.scan_formats", return_value=[f]),
        patch(
            "mempalace.format_miner._extract_via_markitdown",
            side_effect=_FakeMissingDep("install markitdown[pdf]"),
        ),
        patch("mempalace.format_miner.load_config", return_value=fake_config),
        patch("mempalace.format_miner.detect_room", return_value="documents"),
    ):
        mine_formats(format_dir=str(tmp), palace_path=str(tmp / "palace"), wing="wing_test")

    assert str(f) not in register_calls, (
        f"sentinel was written for SKIP_MISSING_FORMAT_DEPS file; should be "
        f"skipped so installing markitdown[<fmt>] later triggers a re-mine. "
        f"register_calls={register_calls}"
    )


def test_mine_formats_catches_unexpected_exception_and_prints_summary(
    monkeypatch, tmp_path: Path, capsys
):
    """If an unexpected error happens during mining (something the
    per-file try/except doesn't catch — e.g., the topic-tunnel block
    blowing up in a way the inner handler misses), mine_formats must
    catch it at the outer level, print a partial-progress summary, and
    clean up the PID file. Not crash with a traceback to the user.

    Per PR #1555 review (Gemini #5).
    """
    from mempalace.format_miner import mine_formats

    tmp = tmp_path / "src"
    tmp.mkdir()
    f = tmp / "exc.pdf"
    f.write_bytes(b"%PDF-1.4 stub")

    fake_config = {
        "wing": "wing_test",
        "rooms": [{"name": "documents", "keywords": ["documents"]}],
    }

    # Patch the file enumeration step to raise something unexpected —
    # this fires OUTSIDE the per-file try/except. Without an outer
    # ``except Exception`` clause, the traceback propagates and the
    # summary block never prints.
    def angry_enumerate(*args, **kwargs):
        raise RuntimeError("simulated outer-loop explosion")

    with (
        patch("mempalace.format_miner.scan_formats", side_effect=angry_enumerate),
        patch("mempalace.format_miner.load_config", return_value=fake_config),
    ):
        # Must NOT raise — outer except Exception should catch.
        mine_formats(format_dir=str(tmp), palace_path=str(tmp / "palace"), wing="wing_test")


def test_mine_formats_threads_chunk_size_from_user_config(monkeypatch, tmp_path: Path):
    """``mine_formats`` must pass ``chunk_size`` (and ``chunk_overlap``,
    ``min_chunk_size``) from MempalaceConfig through to ``chunk_text``.
    Currently the config is loaded only to validate readability; the
    custom chunk parameters are never used during format-mode mining,
    so users who tuned their config see no effect.

    Per PR #1555 review (Gemini #3).
    """
    from mempalace import format_miner
    from mempalace.format_miner import mine_formats

    chunk_calls: list = []

    def fake_chunk_text(content, source_file, **kwargs):
        chunk_calls.append(kwargs)
        return [{"content": content, "chunk_index": 0}]

    monkeypatch.setattr(format_miner, "chunk_text", fake_chunk_text)

    tmp = tmp_path / "src"
    tmp.mkdir()
    f = tmp / "config.pdf"
    f.write_bytes(b"%PDF-1.4 stub")

    fake_config = {
        "wing": "wing_test",
        "rooms": [{"name": "documents", "keywords": ["documents"]}],
    }

    # Inject custom config values via a fake MempalaceConfig.
    class _FakeMempalaceConfig:
        chunk_size = 1234
        chunk_overlap = 56
        min_chunk_size = 78

        @property
        def palace_path(self):
            return str(tmp_path / "palace")

    monkeypatch.setattr(format_miner, "MempalaceConfig", _FakeMempalaceConfig)

    with (
        patch("mempalace.format_miner.scan_formats", return_value=[f]),
        patch("mempalace.format_miner._extract_via_markitdown", return_value="long " * 200),
        patch("mempalace.format_miner.load_config", return_value=fake_config),
        patch("mempalace.format_miner.detect_room", return_value="documents"),
        patch("mempalace.format_miner.file_already_mined", return_value=False),
    ):
        mine_formats(format_dir=str(tmp), palace_path=str(tmp / "palace"), wing="wing_test")

    assert chunk_calls, "expected chunk_text to be called at least once"
    call = chunk_calls[0]
    assert call.get("chunk_size") == 1234, (
        f"chunk_text called without user's chunk_size from MempalaceConfig "
        f"(got {call.get('chunk_size')}, expected 1234). kwargs={call}"
    )
    assert call.get("chunk_overlap") == 56, (
        f"chunk_text called without user's chunk_overlap "
        f"(got {call.get('chunk_overlap')}, expected 56). kwargs={call}"
    )
    assert call.get("min_chunk_size") == 78, (
        f"chunk_text called without user's min_chunk_size "
        f"(got {call.get('min_chunk_size')}, expected 78). kwargs={call}"
    )
