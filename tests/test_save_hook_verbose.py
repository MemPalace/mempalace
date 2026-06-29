"""TDD: save hook must support verbose mode for developers.

Developers want to see diaries and code in chat.
Regular users want silent background saves.
The hook should check a config flag.
"""

import os


class TestSaveHookVerboseMode:
    """Save hook must have a verbose/silent toggle."""

    def test_hook_checks_verbose_flag(self):
        """Hook must read a MEMPAL_VERBOSE or similar flag."""
        hook_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "hooks",
            "mempal_save_hook.sh",
        )
        src = open(hook_path).read()
        has_verbose = "VERBOSE" in src or "verbose" in src or "SILENT" in src or "silent" in src
        assert has_verbose, (
            "Save hook has no verbose/silent toggle. "
            "Developers need to see diaries and code in chat. "
            "Add MEMPAL_VERBOSE flag: when true, hook blocks and asks "
            "agent to write; when false, saves silently."
        )

    def test_verbose_mode_blocks(self):
        """When verbose, hook should use decision: block so agent writes in chat."""
        hook_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "hooks",
            "mempal_save_hook.sh",
        )
        src = open(hook_path).read()
        # There should be TWO decision paths: block (verbose) and allow (silent)
        has_block = '"decision": "block"' in src or "'decision': 'block'" in src
        has_allow = '"decision": "allow"' in src or "'decision': 'allow'" in src
        assert has_block and has_allow, (
            "Hook needs both 'block' (verbose/developer) and 'allow' (silent) paths. "
            f"Has block: {has_block}, has allow: {has_allow}"
        )
