"""Tests for tools/preprocess_staging.py — verbatim batch staging.

MemPalace stores content verbatim, full stop. These tests pin that
contract: the only permitted transformations are structural filtering
(unprocessable inputs) and line-boundary splitting of oversized files.
No byte of file content may be rewritten.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import preprocess_staging as pp  # noqa: E402


def _run(staging, **kw):
    kw.setdefault("max_lines", 4000)
    return pp.preprocess_directory(str(staging), **kw)


def _staged_bytes(staging, mined_rel, out_dir=None):
    root = Path(out_dir) if out_dir else staging
    return (root / mined_rel).read_bytes()


# ── Filters ─────────────────────────────────────────────────────────────────


def test_skip_dotfiles(tmp_path):
    staging = tmp_path
    (staging / ".hidden").write_bytes(b"secret\n")
    mined, skipped = _run(staging, batch_id="b1")
    assert mined == []
    assert skipped[0][0] == ".hidden"


def test_skip_binary_extensions(tmp_path):
    (tmp_path / "data.pdf").write_bytes(b"%PDF fake\n")
    mined, skipped = _run(tmp_path, batch_id="b1")
    assert mined == []
    assert skipped[0][0] == "data.pdf"


def test_skip_node_modules(tmp_path):
    d = tmp_path / "node_modules" / "pkg"
    d.mkdir(parents=True)
    (d / "index.js").write_bytes(b"module.exports = {}\n")
    mined, skipped = _run(tmp_path, batch_id="b1")
    assert mined == []
    assert skipped[0][0] == "node_modules/pkg/index.js"


def test_skip_mempalace_yaml(tmp_path):
    (tmp_path / "mempalace.yaml").write_bytes(b"wing: x\n")
    mined, skipped = _run(tmp_path, batch_id="b1")
    assert mined == []


def test_processed_dir_is_ordinary_content(tmp_path):
    """A user dir literally named 'processed' is NOT special-cased."""
    d = tmp_path / "processed"
    d.mkdir()
    (d / "notes.md").write_bytes(b"user notes in a dir named processed\n")
    mined, _ = _run(tmp_path, batch_id="b1")
    assert [m[0] for m in mined] == ["processed/notes.md"]


def test_processable_files_not_skipped(tmp_path):
    for name in ("a.md", "b.txt", "c.py", "d.rs"):
        (tmp_path / name).write_bytes(b"content for " + name.encode() + b"\n")
    mined, _ = _run(tmp_path, batch_id="b1")
    assert sorted(m[0] for m in mined) == ["a.md", "b.txt", "c.py", "d.rs"]


# ── Verbatim contract ───────────────────────────────────────────────────────


def test_license_header_preserved_verbatim(tmp_path):
    content = b"# SPDX-License-Identifier: MIT\n# Copyright (c) 2024 X\n\ncode()\n"
    (tmp_path / "a.py").write_bytes(content)
    mined, _ = _run(tmp_path, batch_id="b1")
    out = tmp_path / ".mp_processed" / "b1" / "a.py"
    assert out.read_bytes() == content


def test_system_info_block_preserved_verbatim(tmp_path):
    content = (
        b"<system-information>\nOS: darwin\n</system-information>\n"
        b"<available_skills>\nskill\n</available_skills>\n"
        b"real content\n"
    )
    (tmp_path / "a.md").write_bytes(content)
    _run(tmp_path, batch_id="b1")
    assert (tmp_path / ".mp_processed" / "b1" / "a.md").read_bytes() == content


def test_part_number_lines_preserved(tmp_path):
    content = b"Part 1 of 3\nline\nPart 2 of 3\nmore\n"
    (tmp_path / "a.md").write_bytes(content)
    _run(tmp_path, batch_id="b1")
    assert (tmp_path / ".mp_processed" / "b1" / "a.md").read_bytes() == content


def test_repeated_lines_preserved(tmp_path):
    content = b"same\nsame\nsame\n"
    (tmp_path / "a.md").write_bytes(content)
    _run(tmp_path, batch_id="b1")
    assert (tmp_path / ".mp_processed" / "b1" / "a.md").read_bytes() == content


def test_xml_blocks_preserved(tmp_path):
    content = b"<file_view>\n<line>1|x</line>\n</file_view>\n"
    (tmp_path / "a.md").write_bytes(content)
    _run(tmp_path, batch_id="b1")
    assert (tmp_path / ".mp_processed" / "b1" / "a.md").read_bytes() == content


def test_crlf_bytes_preserved(tmp_path):
    """No newline translation — CRLF input must be CRLF in the staged copy."""
    content = b"line one\r\nline two\r\n"
    (tmp_path / "a.txt").write_bytes(content)
    _run(tmp_path, batch_id="b1")
    assert (tmp_path / ".mp_processed" / "b1" / "a.txt").read_bytes() == content


def test_non_utf8_bytes_preserved(tmp_path):
    """Bytes that are not valid UTF-8 must not be lossy-decoded."""
    content = b"valid\n\xff\xfe invalid utf8 \x80\n"
    (tmp_path / "a.bin.md").write_bytes(content)
    _run(tmp_path, batch_id="b1")
    assert (tmp_path / ".mp_processed" / "b1" / "a.bin.md").read_bytes() == content


def test_output_never_overwrites_source(tmp_path):
    """Staging writes go to .mp_processed — the source file is untouched."""
    content = b"original\n"
    src = tmp_path / "a.md"
    src.write_bytes(content)
    _run(tmp_path, batch_id="b1")
    assert src.read_bytes() == content


# ── Splitting ───────────────────────────────────────────────────────────────


def test_no_split_when_under_limit(tmp_path):
    (tmp_path / "a.md").write_bytes(b"\n".join(b"line" for _ in range(100)))
    mined, _ = _run(tmp_path, batch_id="b1", max_lines=4000)
    assert mined == [("a.md", "a.md")]


def test_split_when_over_limit(tmp_path):
    lines = [f"line{i}".encode() for i in range(4500)]
    (tmp_path / "big.md").write_bytes(b"\n".join(lines))
    mined, _ = _run(tmp_path, batch_id="b1", max_lines=2000)
    mined_rels = sorted(m[1] for m in mined)
    assert mined_rels == [
        "big__part001_of_003.md",
        "big__part002_of_003.md",
        "big__part003_of_003.md",
    ]


def test_split_parts_reassemble_byte_exact(tmp_path):
    """Parts must carry the original bytes — split on boundaries, no edits."""
    content = b"\n".join(f"row {i}".encode() for i in range(5000))
    (tmp_path / "big.txt").write_bytes(content)
    _run(tmp_path, batch_id="b1", max_lines=2000)
    out = tmp_path / ".mp_processed" / "b1"
    parts = sorted(p for p in out.iterdir() if "__part" in p.name)
    reassembled = b"\n".join(p.read_bytes() for p in parts)
    assert reassembled == content


def test_split_preserves_extension(tmp_path):
    (tmp_path / "big.py").write_bytes(b"x = 1\n" * 5000)
    mined, _ = _run(tmp_path, batch_id="b1", max_lines=2000)
    assert all(m[1].endswith(".py") for m in mined)


# ── Directory staging ───────────────────────────────────────────────────────


def test_preserves_subdirectories(tmp_path):
    (tmp_path / "projA").mkdir()
    (tmp_path / "projB").mkdir()
    (tmp_path / "projA" / "notes.md").write_bytes(b"# A\n\ncontent\n")
    (tmp_path / "projB" / "notes.md").write_bytes(b"# B\n\nother\n")
    mined, _ = _run(tmp_path, batch_id="b1")
    assert sorted(m[1] for m in mined) == ["projA/notes.md", "projB/notes.md"]
    out = tmp_path / ".mp_processed" / "b1"
    assert (out / "projA" / "notes.md").read_bytes() == b"# A\n\ncontent\n"


def test_batch_ids_isolate_same_named_files(tmp_path):
    """Two batches staging different 'notes.md' files must not collide."""
    (tmp_path / "notes.md").write_bytes(b"batch one content\n")
    _run(tmp_path, batch_id="b1")
    (tmp_path / "notes.md").write_bytes(b"batch two content\n")
    _run(tmp_path, batch_id="b2")
    b1 = tmp_path / ".mp_processed" / "b1" / "notes.md"
    b2 = tmp_path / ".mp_processed" / "b2" / "notes.md"
    assert b1.read_bytes() == b"batch one content\n"
    assert b2.read_bytes() == b"batch two content\n"


def test_dry_run_writes_nothing(tmp_path):
    (tmp_path / "a.md").write_bytes(b"content\n")
    mined, _ = _run(tmp_path, batch_id="b1", dry_run=True)
    assert mined == [("a.md", "a.md")]
    assert not (tmp_path / ".mp_processed").exists()


def test_empty_file_skipped(tmp_path):
    (tmp_path / "empty.md").write_bytes(b"   \n\n")
    mined, skipped = _run(tmp_path, batch_id="b1")
    assert mined == []
    assert skipped == [("empty.md", "empty")]


# ── Batch-snapshot / work-dir mode ──────────────────────────────────────────


def _snapshot_line(rel, content):
    import hashlib

    sha = hashlib.sha256(content).hexdigest()
    return f"{rel}\x1f{len(content)}\x1f0\x1f{sha}"


def test_work_dir_mode_stages_verified_copies(tmp_path):
    staging = tmp_path / "staging"
    work = tmp_path / "work"
    out = tmp_path / "mine"
    staging.mkdir()
    work.mkdir()
    content = b"claimed bytes\n"
    (work / "a.md").write_bytes(content)
    snap = tmp_path / "snap"
    snap.write_text(_snapshot_line("a.md", content) + "\n")
    mined, _ = _run(
        staging, work_dir=str(work), snapshot=str(snap), out_dir=str(out), batch_id="b1"
    )
    assert mined == [("a.md", "a.md")]
    assert (out / "a.md").read_bytes() == content


def test_work_dir_mode_rejects_hash_mismatch(tmp_path):
    staging = tmp_path / "staging"
    work = tmp_path / "work"
    staging.mkdir()
    work.mkdir()
    (work / "a.md").write_bytes(b"NOT the claimed bytes\n")
    snap = tmp_path / "snap"
    snap.write_text(_snapshot_line("a.md", b"claimed bytes\n") + "\n")
    mined, skipped = _run(
        staging, work_dir=str(work), snapshot=str(snap), out_dir=str(tmp_path / "m")
    )
    assert mined == []
    assert skipped == [("a.md", "hash_mismatch")]


def test_manifests_written(tmp_path):
    (tmp_path / "ok.md").write_bytes(b"fine\n")
    (tmp_path / "bad.jsonl").write_bytes(b"{}\n")
    manifest = tmp_path / "mined.manifest"
    skip = tmp_path / "skipped.manifest"
    _run(
        tmp_path,
        batch_id="b1",
        manifest_path=str(manifest),
        skip_manifest_path=str(skip),
    )
    assert manifest.read_text() == "ok.md\x1fok.md\n"
    assert skip.read_text() == "bad.jsonl\x1ffiltered\n"
