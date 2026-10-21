"""Stdio tests for the encoding-repair CLI (scripts/).

The tool ships as a script, not a package module; load it directly, the
same way tests/test_backfill_authored_at.py does.
"""

import importlib.util
import io
import subprocess
import sys
import textwrap
from pathlib import Path
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / "scripts" / "mempalace_repair_encoding.py"
_spec = importlib.util.spec_from_file_location("mempalace_repair_encoding", _SCRIPT)
repair_cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(repair_cli)


class _ReconfigurableStringIO(io.StringIO):
    def __init__(self, initial_value=""):
        super().__init__(initial_value)
        self.reconfigure_calls = []

    def reconfigure(self, **kwargs):
        self.reconfigure_calls.append(kwargs)


def test_reconfigures_stdio_to_utf8_on_windows():
    """This entry point must apply the same Windows stdio fix as the others.

    ``mempalace/_stdio.py`` states the rule: every console entry point that
    touches stdio needs it. This one prints verbatim drawer text, so it needs
    it more than most -- the characters it exists to repair are exactly the
    ones the legacy console codepage cannot encode.
    """
    stdin = _ReconfigurableStringIO()
    stdout = _ReconfigurableStringIO()
    stderr = _ReconfigurableStringIO()
    with (
        patch.object(sys, "platform", "win32"),
        patch.object(sys, "stdin", stdin),
        patch.object(sys, "stdout", stdout),
        patch.object(sys, "stderr", stderr),
    ):
        repair_cli._reconfigure_stdio_utf8_on_windows()

    # Mirrors cli.py and fact_checker.py: stdout/stderr use ``replace`` because
    # this tool prints verbatim drawer content that may carry surrogate halves,
    # where ``strict`` would crash mid-preview and abandon the run.
    assert stdin.reconfigure_calls == [{"encoding": "utf-8", "errors": "surrogateescape"}]
    assert stdout.reconfigure_calls == [{"encoding": "utf-8", "errors": "replace"}]
    assert stderr.reconfigure_calls == [{"encoding": "utf-8", "errors": "replace"}]


def test_change_preview_survives_a_legacy_console_codepage():
    """Printing a mojibake preview must not kill the run on a GBK console.

    Reproduces the real failure rather than asserting a call happened: a
    child process is given a legacy codepage for stdout, then asked to print
    the same before/after preview the tool emits for every proposed change.
    Without the reconfigure this raises UnicodeEncodeError on U+00C3 -- the
    very character the repair targets -- and the whole repair run aborts.
    """
    # sys.platform is forced to win32 because the shared helper is a no-op
    # elsewhere; the same patch the cli/fact_checker stdio tests use. stdout is
    # given a legacy codepage so the encode actually has to succeed.
    program = textwrap.dedent(f"""
        import importlib.util, sys
        spec = importlib.util.spec_from_file_location("m", {str(_SCRIPT)!r})
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        # Fake the platform only AFTER importing: mempalace.palace and friends
        # branch on sys.platform at import time and would take the Windows
        # path (msvcrt) on this host.
        sys.platform = "win32"
        m._reconfigure_stdio_utf8_on_windows()
        m._print_change("drawer-1", "CoraÃ§Ã£o", "Coração", preview_chars=80)
        print("SURVIVED")
    """)
    result = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        env={**dict(__import__("os").environ), "PYTHONIOENCODING": "cp936"},
        cwd=str(_REPO_ROOT),
    )
    assert result.returncode == 0, f"tool crashed printing its own preview:\n{result.stderr}"
    assert "SURVIVED" in result.stdout
