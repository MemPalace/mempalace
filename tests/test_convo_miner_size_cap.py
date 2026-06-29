"""TDD: convo_miner.py must not silently drop transcripts larger than 10 MB.

Mirrors the miner.py fix shipped in the same PR family (see
test_miner_jsonl_visibility.py). Long Claude Code sessions, ChatGPT
exports, and multi-year Slack dumps routinely exceed 10 MB. The cap
silently `continue`s past them at convo_miner.py:~289, same silent-drop
pattern as the project miner's.

Written BEFORE the fix.
"""

from mempalace.convo_miner import MAX_FILE_SIZE


class TestConvoMinerSizeCap:
    def test_max_file_size_accommodates_long_transcripts(self):
        """The cap must be well above any realistic transcript.

        Long sessions and lifetime exports exceed 10 MB. The cap exists
        as a sanity rail against pathological binaries, not as a limit
        on legitimate text — downstream chunking means source size does
        not matter for storage or embedding cost.
        """
        assert MAX_FILE_SIZE >= 100 * 1024 * 1024, (
            f"convo_miner.MAX_FILE_SIZE is {MAX_FILE_SIZE} bytes "
            f"({MAX_FILE_SIZE / 1024 / 1024:.0f} MB). Same silent-drop "
            "bug as miner.py's old 10 MB cap — long transcripts get "
            "filtered out at convo_miner.py:~289 with `continue`. "
            "Raise to at least 100 MB (match miner.py at 500 MB for "
            "consistency across both miners)."
        )
