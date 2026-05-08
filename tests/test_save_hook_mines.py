"""TDD: save hook must actually mine conversations without MEMPAL_DIR.

The save hook should auto-discover the conversation transcript and mine it
without the user needing to set MEMPAL_DIR. Currently MEMPAL_DIR defaults
to empty, which means the mining block is skipped and nothing is saved
despite the hook telling the agent "saved in background."

Written BEFORE the fix.
"""

import os
import sys

import pytest


class TestSaveHookAutoMines:
    """The save hook must mine the active transcript automatically."""

    def test_hook_mines_transcript_path(self):
        """The hook receives TRANSCRIPT_PATH from Claude Code.
        It should use that to mine the conversation as --mode convos,
        independently of MEMPAL_DIR (which is for project files only)."""
        hook_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "hooks",
            "mempal_save_hook.sh",
        )
        src = open(hook_path).read()

        # The hook must drive the conversation mine off TRANSCRIPT_PATH,
        # using `dirname` to derive the parent dir, and tagging it with
        # `--mode convos` so the convo miner runs (not the projects miner).
        assert "TRANSCRIPT_PATH" in src, "hook must read transcript_path"
        assert "mempalace mine" in src, "hook must invoke `mempalace mine`"
        assert (
            'dirname "$TRANSCRIPT_PATH"' in src
        ), "hook must mine the transcript's parent directory"
        assert (
            "--mode convos" in src
        ), "transcript mine must use --mode convos, not the projects miner"

    def test_mempal_dir_default_not_empty(self):
        """If MEMPAL_DIR is still used, it should have a sensible default,
        not an empty string that silently disables mining."""
        hook_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "hooks",
            "mempal_save_hook.sh",
        )
        src = open(hook_path).read()

        # Check if MEMPAL_DIR defaults to empty
        has_empty_default = 'MEMPAL_DIR=""' in src

        # If it defaults to empty, mining is silently disabled
        if has_empty_default:
            # There must be an alternative mining path that doesn't need MEMPAL_DIR
            has_alternative = (
                src.count("mempalace mine") > 1
                or "TRANSCRIPT_PATH" in src.split("mempalace mine")[0]
            )
            assert has_alternative, (
                'MEMPAL_DIR defaults to "" which silently disables mining. '
                "Either set a default path or add transcript-based mining."
            )


class TestShellHookTranscriptValidation:
    """Both shell hooks must validate transcript paths before mining them.

    Mirrors mempalace.hooks_cli._validate_transcript_path so unsafe paths
    (no extension, traversal segments) are rejected at the shell layer
    too — added in #1231 review (Copilot #7, #8).
    """

    @staticmethod
    def _hook_src(name: str) -> str:
        path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "hooks", name)
        return open(path).read()

    @staticmethod
    def _strip_comments(src: str) -> str:
        return "\n".join(line for line in src.splitlines() if not line.lstrip().startswith("#"))

    def test_save_hook_defines_and_uses_validator(self):
        src = self._strip_comments(self._hook_src("mempal_save_hook.sh"))
        assert "is_valid_transcript_path() {" in src, "validator function must be defined"
        assert (
            'is_valid_transcript_path "$TRANSCRIPT_PATH"' in src
        ), "validator must be invoked against TRANSCRIPT_PATH before mining"

    def test_precompact_hook_defines_and_uses_validator(self):
        src = self._strip_comments(self._hook_src("mempal_precompact_hook.sh"))
        assert "is_valid_transcript_path() {" in src, "validator function must be defined"
        assert (
            'is_valid_transcript_path "$TRANSCRIPT_PATH"' in src
        ), "validator must be invoked against TRANSCRIPT_PATH before mining"

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="shell hooks are POSIX-only; Windows CI bash maps to wsl.exe with no distro",
    )
    def test_validators_run_via_bash(self, tmp_path):
        """Source the validator out of each hook and exercise it directly."""
        import subprocess

        for name in ("mempal_save_hook.sh", "mempal_precompact_hook.sh"):
            src = self._hook_src(name)
            # Extract just the function definition (first occurrence).
            start = src.index("is_valid_transcript_path() {")
            end = src.index("\n}\n", start) + 2
            func_src = src[start:end]
            script = tmp_path / "v.sh"
            script.write_text(
                f"{func_src}\n" 'is_valid_transcript_path "$1" && echo OK || echo NO\n'
            )

            def run(arg: str) -> str:
                return subprocess.run(
                    ["bash", str(script), arg],
                    capture_output=True,
                    text=True,
                    check=False,
                ).stdout.strip()

            assert run("/tmp/sessions/abc.jsonl") == "OK"
            assert run("/tmp/sessions/abc.json") == "OK"
            assert run("") == "NO"
            assert run("/tmp/notes.txt") == "NO"
            assert run("../etc/passwd.jsonl") == "NO"
            assert run("/tmp/../etc/t.jsonl") == "NO"
