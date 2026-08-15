"""Tests for tools/preprocess_staging.py — boilerplate stripping and file splitting."""

import sys
import textwrap
from pathlib import Path

# tools/ is not a package — add to path for import
_TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"
sys.path.insert(0, str(_TOOLS_DIR))

import preprocess_staging as pp  # noqa: E402


# ── should_skip ──────────────────────────────────────────────────────────


def test_skip_dotfiles():
    assert pp.should_skip(Path(".DS_Store")) is True
    assert pp.should_skip(Path(".gitignore")) is True
    assert pp.should_skip(Path(".hidden_file.py")) is True


def test_skip_binary_extensions():
    assert pp.should_skip(Path("image.png")) is True
    assert pp.should_skip(Path("data.db")) is True
    assert pp.should_skip(Path("archive.zip")) is True


def test_skip_node_modules():
    assert pp.should_skip(Path("node_modules/react/index.js")) is True
    assert pp.should_skip(Path("project/target/debug/binary")) is True


def test_skip_mempalace_yaml():
    assert pp.should_skip(Path("mempalace.yaml")) is True
    assert pp.should_skip(Path("mempal.yaml")) is True


def test_processable_files_not_skipped():
    assert pp.should_skip(Path("script.py")) is False
    assert pp.should_skip(Path("README.md")) is False
    assert pp.should_skip(Path("config.json")) is False
    assert pp.should_skip(Path("query.sql")) is False


# ── strip_block_patterns ─────────────────────────────────────────────────


def test_strip_system_info_block():
    text = "Before\n<system_info>secret stuff</system_info>\nAfter"
    result = pp.strip_block_patterns(text)
    assert "secret stuff" not in result
    assert "Before" in result
    assert "After" in result


def test_strip_available_skills_block():
    text = "<available_skills>big list</available_skills>\ncontent"
    result = pp.strip_block_patterns(text)
    assert "big list" not in result
    assert "content" in result


def test_strip_multiple_blocks():
    text = (
        "<system_info>SECRET_A</system_info>"
        "<additional_metadata>SECRET_B</additional_metadata>"
        "real content"
    )
    result = pp.strip_block_patterns(text)
    assert "SECRET_A" not in result
    assert "SECRET_B" not in result
    assert "real content" in result


# ── strip_license_headers ────────────────────────────────────────────────


def test_strip_spdx_license():
    text = textwrap.dedent("""\
        // SPDX-License-Identifier: MIT
        // This is licensed under MIT
        actual_code();
    """)
    result = pp.strip_license_headers(text)
    assert "SPDX" not in result
    assert "actual_code();" in result


def test_strip_apache_block_comment():
    text = textwrap.dedent("""\
        /*
         * Licensed under the Apache License, Version 2.0
         * you may not use this file except in compliance with the License.
         */
        fn main() {}
    """)
    result = pp.strip_license_headers(text)
    assert "Apache License" not in result
    assert "fn main() {}" in result


def test_strip_copyright_line():
    text = "Copyright (c) 2024 Test Corp\nreal_content();"
    result = pp.strip_license_headers(text)
    assert "Copyright" not in result
    assert "real_content();" in result


def test_preserve_non_license_block_comments():
    text = textwrap.dedent("""\
        /*
         * This is a regular comment, not a license.
         */
        int main() { return 0; }
    """)
    result = pp.strip_license_headers(text)
    assert "regular comment" in result
    assert "int main" in result


# ── strip_tool_noise ─────────────────────────────────────────────────────


def test_strip_tool_confirmations():
    text = "Todos have been modified successfully\nreal content\nSuccessfully wrote 42 bytes"
    result = pp.strip_tool_noise(text)
    assert "Todos" not in result
    assert "Successfully wrote" not in result
    assert "real content" in result


def test_strip_file_view_tags():
    text = '<file-view path="/tmp/test.py">\ncontent\n<ref_file file="/tmp/x" />'
    result = pp.strip_tool_noise(text)
    assert "<file-view" not in result
    assert "<ref_file" not in result
    assert "content" in result


# ── dedup_consecutive ────────────────────────────────────────────────────


def test_dedup_consecutive_lines():
    text = "hello\nhello\nhello\nworld\nworld\nfoo"
    result = pp.dedup_consecutive(text)
    lines = result.split("\n")
    assert lines.count("hello") == 1
    assert lines.count("world") == 1
    assert lines.count("foo") == 1


def test_dedup_preserves_non_consecutive():
    text = "hello\nworld\nhello"
    result = pp.dedup_consecutive(text)
    assert result.count("hello") == 2


def test_dedub_preserves_blank_lines():
    text = "\n\n\ncontent"
    result = pp.dedup_consecutive(text)
    # Blank lines are not deduped (only non-empty consecutive dups)
    assert "content" in result


# ── strip_excessive_blank_lines ──────────────────────────────────────────


def test_collapse_blank_lines():
    text = "a\n\n\n\n\n\n\nb"
    result = pp.strip_excessive_blank_lines(text)
    # 5+ blank lines should collapse to max 2
    assert result.count("\n") <= 4  # "a\n\n\nb" = 3 newlines max
    assert "a" in result
    assert "b" in result


def test_preserve_two_blank_lines():
    text = "a\n\n\nb"
    result = pp.strip_excessive_blank_lines(text)
    # Two blank lines should be preserved
    assert "\n\n\n" in result


# ── process_content ──────────────────────────────────────────────────────


def test_process_content_full_pipeline():
    text = textwrap.dedent("""\
        <system_info>hidden</system_info>
        // SPDX-License-Identifier: MIT
        Todos have been modified successfully

        actual_content();
        actual_content();
    """)
    result = pp.process_content(text)
    assert "hidden" not in result
    assert "SPDX" not in result
    assert "Todos" not in result
    # Dedup: only one "actual_content();" line
    assert result.count("actual_content();") == 1


# ── split_file ───────────────────────────────────────────────────────────


def test_no_split_when_under_limit(tmp_path):
    content = "line\n" * 100
    out = pp.split_file(Path("test.py"), content, max_lines=4000, output_dir=tmp_path)
    assert len(out) == 1
    assert out[0].name == "test.py"
    assert out[0].read_text() == content


def test_split_when_over_limit(tmp_path):
    content = "line\n" * 10000
    out = pp.split_file(Path("test.py"), content, max_lines=4000, output_dir=tmp_path)
    assert len(out) == 3  # 10000 / 4000 = 3 parts (4000 + 4000 + 2000)
    assert "part001" in out[0].name
    assert "part002" in out[1].name
    assert "part003" in out[2].name
    # Total lines preserved (trailing newline adds one empty string element)
    total_lines = sum(len(f.read_text().split("\n")) for f in out)
    assert total_lines == 10001  # 10000 "line\n" = 10000 lines + trailing ""


def test_split_preserves_extension(tmp_path):
    content = "x\n" * 5000
    out = pp.split_file(Path("script.rs"), content, max_lines=4000, output_dir=tmp_path)
    assert all(f.suffix == ".rs" for f in out)


# ── preprocess_directory ─────────────────────────────────────────────────


def test_preprocess_directory_strips_and_writes(tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "test.py").write_text(
        "// SPDX-License-Identifier: MIT\n"
        "# Copyright (c) 2024 Test Corp\n"
        "def real_function():\n"
        "    print('hello world')\n"
        "    return True\n",
        encoding="utf-8",
    )

    stats = pp.preprocess_directory(str(staging), max_lines=4000)

    assert stats["processed"] == 1
    assert stats["output_files"] == 1
    processed_file = staging / "processed" / "test.py"
    assert processed_file.exists()
    content = processed_file.read_text()
    assert "SPDX" not in content
    assert "Copyright" not in content
    assert "real_function" in content


def test_preprocess_directory_splits_large_file(tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    # Use unique lines so dedup doesn't collapse them
    (staging / "big.txt").write_text("".join(f"line {i}\n" for i in range(10000)), encoding="utf-8")

    stats = pp.preprocess_directory(str(staging), max_lines=4000)

    assert stats["processed"] == 1
    assert stats["split"] == 1
    assert stats["output_files"] == 3
    processed_dir = staging / "processed"
    files = list(processed_dir.iterdir())
    assert len(files) == 3


def test_preprocess_directory_skips_binaries(tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (staging / "code.py").write_text(
        "def hello_world():\n    print('hello world')\n    return True\n",
        encoding="utf-8",
    )

    stats = pp.preprocess_directory(str(staging), max_lines=4000)

    assert stats["processed"] == 1
    assert stats["skipped"] >= 1
    # Only code.py should be in processed/
    processed = list((staging / "processed").iterdir())
    assert len(processed) == 1
    assert processed[0].name == "code.py"


def test_preprocess_directory_dry_run(tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "test.py").write_text("print('hello')\n", encoding="utf-8")

    stats = pp.preprocess_directory(str(staging), max_lines=4000, dry_run=True)

    assert stats["processed"] == 0  # dry run doesn't write
    assert not (staging / "processed").exists()


def test_preprocess_directory_preserves_mempalace_yaml(tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "mempalace.yaml").write_text("wing: test\n", encoding="utf-8")
    (staging / "code.py").write_text("print('hello')\n", encoding="utf-8")

    pp.preprocess_directory(str(staging), max_lines=4000)

    # mempalace.yaml should NOT be in processed/
    processed = list((staging / "processed").iterdir())
    assert all(f.name != "mempalace.yaml" for f in processed)


def test_preprocess_directory_cleans_processed_tree_between_runs(tmp_path):
    """A failed batch with nested processed/ trees must be fully removed."""
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "code.py").write_text("print('hello')\n", encoding="utf-8")

    # Simulate a previous failed run that created nested processed/processed/.
    old_processed = staging / "processed" / "processed"
    old_processed.mkdir(parents=True)
    (old_processed / "stale.md").write_text("stale\n", encoding="utf-8")

    pp.preprocess_directory(str(staging), max_lines=4000)

    # The old nested tree should be gone.
    assert not (staging / "processed" / "processed" / "stale.md").exists()
    # The new run should only contain the real output.
    processed_files = list((staging / "processed").rglob("*"))
    assert any(f.name == "code.py" for f in processed_files)


def test_preprocess_directory_excludes_processed_descendants(tmp_path):
    """Files inside any processed/ directory must not be re-preprocessed."""
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "real.py").write_text(
        "x = 1\n"
        "y = 2\n"
        "z = 3\n"
        "def main():\n"
        "    print('hello')\n",
        encoding="utf-8",
    )

    pre_existing = staging / "processed" / "leftover"
    pre_existing.mkdir(parents=True)
    (pre_existing / "leftover.py").write_text("y = 2\n", encoding="utf-8")

    stats = pp.preprocess_directory(str(staging), max_lines=4000)

    # The leftover inside processed/ must not be counted or re-output.
    assert stats["processed"] == 1
    assert not (staging / "processed" / "processed").exists()


def test_preprocess_directory_empty_content_skipped(tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "empty.py").write_text("\n\n\n", encoding="utf-8")
    (staging / "real.py").write_text(
        "def real_function():\n    print('hello world')\n    return True\n",
        encoding="utf-8",
    )

    stats = pp.preprocess_directory(str(staging), max_lines=4000)

    assert stats["processed"] == 1
    processed = list((staging / "processed").iterdir())
    assert len(processed) == 1
    assert processed[0].name == "real.py"


def test_preprocess_directory_respects_batch_snapshot(tmp_path):
    """Only files listed in the batch snapshot are preprocessed."""
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "claimed.py").write_text("x = 1\ny = 2\nz = 3\n", encoding="utf-8")
    (staging / "late.py").write_text("a = 1\nb = 2\nc = 3\n", encoding="utf-8")

    snapshot = staging / ".batch_snapshot"
    # Unit-separator-delimited: rel\x1fsize\x1fmtime\x1fsha256
    snapshot.write_text("claimed.py\x1f0\x1f0\x1f0\n", encoding="utf-8")

    stats = pp.preprocess_directory(str(staging), max_lines=4000, batch_snapshot=snapshot)

    assert stats["processed"] == 1
    assert (staging / "processed" / "claimed.py").exists()
    assert not (staging / "processed" / "late.py").exists()
