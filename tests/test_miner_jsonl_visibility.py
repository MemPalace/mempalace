"""TDD: miner.py must not silently drop .jsonl files.

The project miner (mempalace.miner.scan_project) walks a directory and
keeps only files whose suffix is in READABLE_EXTENSIONS. The whitelist
contains `.json` but NOT `.jsonl`. Every ChatGPT export, Claude Code
transcript, or any other jsonl transcript dumped into a project
directory is silently dropped with no user-visible output.

Two paths to fix this, both tested here:

  1. READABLE_EXTENSIONS must include `.jsonl` so the file is at least
     readable as text (jsonl is line-delimited JSON — each line is
     already valid text for embedding).
  2. OR scan_project must surface skipped .jsonl files to the user so
     they know to use `--mode convos`.

We test (1) — include .jsonl in READABLE_EXTENSIONS. This matches how
`.json` is already handled: the miner doesn't care what the structure
is, it chunks the text.

Written BEFORE the fix.
"""

import tempfile
from pathlib import Path
from unittest.mock import patch

from mempalace.miner import MAX_FILE_SIZE, READABLE_EXTENSIONS, scan_project


class TestJsonlNotSilentlySkipped:
    def test_jsonl_in_readable_extensions(self):
        """`.jsonl` must be in the readable-extensions whitelist.

        `.json` is already there (see mempalace/miner.py:30). `.jsonl`
        is conceptually the same thing — line-delimited JSON — and all
        of Claude Code's transcripts, ChatGPT exports, and similar
        tooling writes `.jsonl`. Excluding it silently drops user data.
        """
        assert ".jsonl" in READABLE_EXTENSIONS, (
            "mempalace/miner.py:READABLE_EXTENSIONS contains `.json` "
            "but NOT `.jsonl`. Every jsonl file in a mined project is "
            "silently skipped at miner.py:722 "
            "(`if filepath.suffix.lower() not in READABLE_EXTENSIONS: "
            "continue`). This causes the 'convos not being saved' bug "
            "reported by users — the hook fires `mempalace mine`, the "
            "miner walks the directory, skips every .jsonl file, exits "
            "cleanly. No warning, no log line, user sees nothing wrong. "
            "Add `.jsonl` to READABLE_EXTENSIONS."
        )

    def test_scan_project_picks_up_jsonl_file(self):
        """scan_project should find .jsonl files in the target dir."""
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            jsonl_path = tmpdir / "transcript.jsonl"
            jsonl_path.write_text(
                '{"role": "user", "content": "hello"}\n'
                '{"role": "assistant", "content": "hi there"}\n'
                '{"role": "user", "content": "how do I install this"}\n'
                '{"role": "assistant", "content": "pip install mempalace"}\n'
            )

            found = scan_project(str(tmpdir))
            found_names = [p.name for p in found]
            assert "transcript.jsonl" in found_names, (
                "scan_project silently dropped transcript.jsonl. "
                f"Returned: {found_names}. Users placing transcript "
                "exports in a project directory expect them to be mined."
            )

    def test_large_jsonl_not_silently_dropped_by_size_cap(self):
        """Long sessions produce >10 MB transcripts. They must still mine.

        The legacy cap was 10 MB, which is smaller than a long Claude Code
        session's transcript. Users hitting the cap lost their entire
        conversation to a silent `if size > MAX: continue` at miner.py:732.
        Raise the cap well above any realistic transcript size.
        """
        # 10 MB cap was silent failure — real Claude Code long sessions
        # exceed this. The cap must accommodate them.
        assert MAX_FILE_SIZE >= 100 * 1024 * 1024, (
            f"MAX_FILE_SIZE is {MAX_FILE_SIZE} bytes "
            f"({MAX_FILE_SIZE / 1024 / 1024:.0f} MB). Long Claude Code "
            "sessions produce transcripts larger than 10 MB and get "
            "silently dropped. Raise to at least 100 MB — chunking "
            "at 800 chars per drawer means source file size doesn't "
            "matter for downstream storage."
        )

    def test_scan_project_picks_up_50mb_jsonl(self):
        """A 50 MB .jsonl must not be filtered out by the size cap.

        We don't actually write 50 MB (slow test). Instead, we mock
        stat().st_size to report a 50 MB file and confirm scan_project
        still includes it.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            big_jsonl = tmpdir / "big_transcript.jsonl"
            # Write a small real file so the existence / extension / text
            # checks pass; then mock its reported size.
            big_jsonl.write_text('{"role": "user", "content": "hi"}\n')
            fake_size = 50 * 1024 * 1024  # 50 MB

            real_stat = Path.stat

            def fake_stat(self, *args, **kwargs):
                result = real_stat(self, *args, **kwargs)
                if self.name == "big_transcript.jsonl":

                    class _FakeStat:
                        st_size = fake_size
                        st_mode = result.st_mode

                    return _FakeStat()
                return result

            with patch.object(Path, "stat", fake_stat):
                found = scan_project(str(tmpdir))

            found_names = [p.name for p in found]
            assert "big_transcript.jsonl" in found_names, (
                f"50 MB .jsonl was dropped by size cap (MAX_FILE_SIZE="
                f"{MAX_FILE_SIZE}). Returned: {found_names}."
            )
