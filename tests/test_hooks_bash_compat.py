"""Regression tests for the bash 3.2 compatibility fix (#1440).

The legacy hooks/*.sh scripts run on the user's system. On stock macOS
that is GNU bash 3.2.57 (Apple GPLv3 freeze, 2006). Using bash 4.0-only
builtins like ``mapfile`` silently breaks parsing: every JSON field
falls back to its default, the hook logs ``Session unknown: 0 exchanges``,
and zero drawers are saved.

These tests cover:
1. Source-level shape (mapfile/readarray absent, sed-based extraction).
2. Behavioral parse contract (session_id reaches the log, not 'unknown').
3. The fail-loud guard: fires only when the parser sentinel is missing,
   never on a legitimately empty/unicode/literal-'unknown' session_id.
4. The guard's disk discipline (bounded dump, overwrite-not-append, 0600).
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SAVE_HOOK = REPO_ROOT / "hooks" / "mempal_save_hook.sh"
PRECOMPACT_HOOK = REPO_ROOT / "hooks" / "mempal_precompact_hook.sh"

# Re-used by every parametrize decorator that runs the same test against
# both hooks. ``ids=`` keeps pytest output readable (`...[save_hook]`
# rather than the default `hook0`/`hook1`).
_BOTH_HOOKS = pytest.mark.parametrize(
    "hook",
    [SAVE_HOOK, PRECOMPACT_HOOK],
    ids=["save_hook", "precompact_hook"],
)

pytestmark = pytest.mark.skipif(os.name == "nt", reason="bash hook scripts are POSIX-only")


def _hook_src_no_comments(hook: Path) -> str:
    return "\n".join(
        line for line in hook.read_text().splitlines() if not line.lstrip().startswith("#")
    )


def _run_hook(
    hook: Path,
    stdin: str,
    home: Path,
    *,
    expected_rc: int = 0,
    extra_env: dict | None = None,
) -> tuple[str, str]:
    """Run a hook with a controlled environment and assert its exit code.

    Returns ``(stdout, stderr)``. On unexpected exit the assertion message
    surfaces the captured stderr so CI failures are not silent. The hook's
    primary diagnostic stream is ``$STATE_DIR/hook.log`` plus the two
    sidecar dumps ``last_input.log`` / ``last_python_err.log``; the
    subprocess's stderr is typically empty on the documented exit paths
    and is captured here only to surface unexpected interpreter failures
    (e.g., a bash syntax error on a future edit).

    Forces ``umask 0o022`` in the child so the hook's own ``umask 077``
    inside the parse subshell is provably the sole reason the diagnostic
    files end up at mode 0600 — without this, a permissive ambient umask
    on the CI runner would mask a regression that drops the in-hook
    ``umask`` line.
    """
    env = {
        "HOME": str(home),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    }
    if extra_env:
        env.update(extra_env)
    p = subprocess.run(
        ["bash", str(hook)],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        preexec_fn=lambda: os.umask(0o022),
    )
    assert p.returncode == expected_rc, (
        f"{hook.name} exited {p.returncode} (expected {expected_rc}); "
        f"stderr={p.stderr!r}; stdout={p.stdout!r}"
    )
    return p.stdout, p.stderr


class TestNoBash4OnlyBuiltins:
    """Source-level regression: bash 4.0 array-read builtins are unavailable on macOS bash 3.2."""

    @_BOTH_HOOKS
    def test_no_mapfile(self, hook):
        code = _hook_src_no_comments(hook)
        assert (
            "mapfile" not in code
        ), f"{hook.name} uses mapfile, unavailable on macOS /bin/bash 3.2 (#1440)"
        assert (
            "readarray" not in code
        ), f"{hook.name} uses readarray, unavailable on macOS /bin/bash 3.2 (#1440)"

    @_BOTH_HOOKS
    def test_sed_extraction_present(self, hook):
        # Strip ``#`` line comments before counting so the assertion fails
        # if all live ``sed -n 'Np'`` calls are deleted but the explanatory
        # comments above the parse block (which mention ``sed -n 'Np'``
        # several times in prose) are left behind — otherwise the test
        # would false-pass on a regression that swapped the extraction
        # method back to ``mapfile`` while keeping the old commentary.
        src = _hook_src_no_comments(hook)
        # Each hook reads at least two values via ``sed -n 'Np'`` (sentinel + session_id).
        assert (
            src.count("sed -n '") >= 2
        ), f"{hook.name} must use sed -n 'Np' for POSIX-portable line extraction"

    @_BOTH_HOOKS
    def test_bash_syntax_clean(self, hook):
        p = subprocess.run(
            ["bash", "-n", str(hook)],
            capture_output=True,
            text=True,
        )
        assert p.returncode == 0, f"{hook.name} syntax error: {p.stderr}"


class TestSessionIdExtraction:
    """Hook must parse session_id from valid JSON, not fall back to 'unknown'."""

    def test_save_hook_extracts_session_id(self, tmp_path):
        out, _ = _run_hook(
            SAVE_HOOK,
            json.dumps(
                {"session_id": "abc12345", "stop_hook_active": False, "transcript_path": ""}
            ),
            tmp_path,
        )
        # Stdout must be valid JSON; Claude Code parses it. A regression that
        # leaks debug output here would silently break the harness contract.
        assert json.loads(out) == {}, f"hook stdout must be valid JSON, got: {out!r}"
        log = (tmp_path / ".mempalace" / "hook_state" / "hook.log").read_text()
        assert "Session abc12345:" in log, f"got fallback 'unknown'; log was: {log!r}"
        # Negative cross-check: the sentinel-distinguishes-success-from-failure
        # contract has no value if the guard fires on the happy path too.
        assert "WARN: input parse failed" not in log
        state_dir = tmp_path / ".mempalace" / "hook_state"
        assert not (state_dir / "last_input.log").exists()
        assert not (
            state_dir / "last_python_err.log"
        ).exists(), "successful parse must leave no last_python_err.log behind"

    def test_precompact_hook_extracts_session_id(self, tmp_path):
        out, _ = _run_hook(
            PRECOMPACT_HOOK,
            json.dumps({"session_id": "abc12345", "transcript_path": ""}),
            tmp_path,
        )
        assert json.loads(out) == {}, f"hook stdout must be valid JSON, got: {out!r}"
        log = (tmp_path / ".mempalace" / "hook_state" / "hook.log").read_text()
        assert "PRE-COMPACT triggered for session abc12345" in log
        assert "WARN: input parse failed" not in log
        state_dir = tmp_path / ".mempalace" / "hook_state"
        assert not (state_dir / "last_input.log").exists()
        assert not (
            state_dir / "last_python_err.log"
        ).exists(), "successful parse must leave no last_python_err.log behind"


class TestFailLoudGuard:
    """Non-parseable stdin must dump the input and warn in hook.log so
    future silent failures are loud, and the dump must stay bounded and
    user-private. The guard must NOT fire on legitimately empty inputs
    or on sanitizer-stripped session_ids (#1440)."""

    @_BOTH_HOOKS
    def test_malformed_input_logs_warning_and_dumps_input(self, hook, tmp_path):
        _run_hook(hook, "not-json garbage", tmp_path)
        state_dir = tmp_path / ".mempalace" / "hook_state"
        log = (state_dir / "hook.log").read_text()
        last_input = (state_dir / "last_input.log").read_text()
        assert "WARN: input parse failed (sentinel missing)" in log
        assert "not-json garbage" in last_input

    @_BOTH_HOOKS
    def test_empty_stdin_does_not_dump_or_warn(self, hook, tmp_path):
        """Empty stdin is a legitimate state (e.g. a hook re-fire on Stop
        with no message body). The guard's ``[ -n "$INPUT" ]`` short-circuit
        must hold so nothing is written to last_input.log."""
        _run_hook(hook, "", tmp_path)
        state_dir = tmp_path / ".mempalace" / "hook_state"
        assert not (state_dir / "last_input.log").exists()
        log_path = state_dir / "hook.log"
        if log_path.exists():
            assert "WARN: input parse failed" not in log_path.read_text()

    @_BOTH_HOOKS
    def test_unicode_session_id_does_not_trip_guard(self, hook, tmp_path):
        """A session_id with non-ASCII characters (Cyrillic, CJK, emoji)
        is stripped by the sanitizer to '', defaults to 'unknown'. The
        sentinel still printed, so the guard must skip and NOT spam disk.
        Parametrized over both hooks: each has its own inline Python
        parser and its own sanitizer regex, so a copy-paste regression
        in only one would otherwise be invisible. The precompact parser
        ignores the ``stop_hook_active`` key, so the same payload works
        for both."""
        _run_hook(
            hook,
            json.dumps({"session_id": "сессия", "stop_hook_active": False, "transcript_path": ""}),
            tmp_path,
        )
        state_dir = tmp_path / ".mempalace" / "hook_state"
        assert not (
            state_dir / "last_input.log"
        ).exists(), "unicode-only session_id sanitized to empty must NOT trip the guard"

    @_BOTH_HOOKS
    def test_literal_unknown_session_id_does_not_trip_guard(self, hook, tmp_path):
        """A user who literally passes session_id='unknown' is parsing
        cleanly; the sentinel-based guard must distinguish that from a
        crash and skip the dump. Parametrized over both hooks for the
        same reason as the unicode test: the sentinel logic is duplicated
        between the two parsers."""
        _run_hook(
            hook,
            json.dumps({"session_id": "unknown", "stop_hook_active": False, "transcript_path": ""}),
            tmp_path,
        )
        state_dir = tmp_path / ".mempalace" / "hook_state"
        assert not (
            state_dir / "last_input.log"
        ).exists(), "literal session_id='unknown' must NOT trip the guard"

    @_BOTH_HOOKS
    def test_dump_is_bounded_and_overwritten(self, hook, tmp_path):
        """The dump caps at exactly 4096 bytes and overwrites on each
        failure so a repeating misconfiguration cannot grow the file
        unbounded. Both payloads here are intentionally not-valid-JSON so
        the guard fires on each call — the overwrite contract is what
        is being tested, not the validation logic. Parametrized over
        both hooks because each hook has its own ``head -c 4096 > ...``
        line — a future edit that flips one to ``>>`` would not be caught
        by a save-only test."""
        # 4097 bytes: one over the cap, proves the cutoff fires at exactly
        # 4096 (a regression that silently shrinks the cap to e.g. 1024
        # would slip past a looser ``<= 4096`` check).
        big_payload = "x" * 4097
        _run_hook(hook, big_payload, tmp_path)
        last_input = tmp_path / ".mempalace" / "hook_state" / "last_input.log"
        assert (
            last_input.stat().st_size == 4096
        ), f"cap must be exactly 4096 bytes; got {last_input.stat().st_size}"
        # Second failure with a smaller payload (also not valid JSON, so the
        # guard fires) overwrites the first; the file shrinks instead of
        # accumulating.
        _run_hook(hook, "tiny", tmp_path)
        assert last_input.exists(), "second guard fire must produce a file, not skip the write"
        assert last_input.read_text() == "tiny", "dump must overwrite on each failure, not append"

    @_BOTH_HOOKS
    def test_dump_cap_holds_under_utf8_locale(self, hook, tmp_path):
        """Under a UTF-8 locale, a multibyte payload of 2000 CJK chars =
        6000 bytes would slip a character-counted substring (`${var:0:N}`)
        past the 4096-byte cap. The hook uses ``head -c`` precisely so
        the bound stays byte-based regardless of locale."""
        # ``C.UTF-8`` is available on every mainstream Linux distribution
        # (Debian/Ubuntu, Fedora, RHEL 8+, Alpine via musl) and macOS
        # bash falls back to the byte-based C locale gracefully;
        # ``en_US.UTF-8`` would silently degrade to no-op on minimal CI
        # images (Alpine, distroless) where that locale is not generated.
        # 2000 copies of U+4E2D (3 bytes each in UTF-8) = 6000 bytes.
        big_payload = "中" * 2000
        _run_hook(
            hook,
            big_payload,
            tmp_path,
            extra_env={"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"},
        )
        last_input = tmp_path / ".mempalace" / "hook_state" / "last_input.log"
        size = last_input.stat().st_size
        assert size == 4096, (
            f"UTF-8 payload must still cap at 4096 bytes (got {size}); "
            "regression to ${var:0:N} would let multibyte input bypass the bound"
        )

    @_BOTH_HOOKS
    def test_dump_is_not_world_readable(self, hook, tmp_path):
        """The dump mirrors the raw hook payload (transcript_path reveals
        the user's home + project layout). Permissions must be 600 so
        other users on a shared box cannot read it."""
        _run_hook(hook, "not-json garbage", tmp_path)
        last_input = tmp_path / ".mempalace" / "hook_state" / "last_input.log"
        mode = stat.S_IMODE(last_input.stat().st_mode)
        assert mode == 0o600, f"last_input.log mode should be 0600, got {oct(mode)}"

    @_BOTH_HOOKS
    def test_python_stderr_captured_on_parse_failure(self, hook, tmp_path):
        """When the inline Python parser crashes (malformed JSON, missing
        interpreter, future regression), its stderr must land in
        last_python_err.log so a debugger can distinguish 'bad user
        input' from 'broken interpreter or broken inline script'."""
        _run_hook(hook, "not-json garbage", tmp_path)
        err_log = tmp_path / ".mempalace" / "hook_state" / "last_python_err.log"
        assert err_log.exists(), "Python stderr must be captured on parse failure"
        contents = err_log.read_text()
        # Python's json.load raises JSONDecodeError with a recognizable
        # traceback. Don't pin the exact message (it varies by Python
        # version) but assert at least one canonical marker is present.
        assert (
            "Traceback" in contents or "json" in contents.lower()
        ), f"expected Python traceback or json error, got: {contents!r}"

    @_BOTH_HOOKS
    def test_python_stderr_log_is_not_world_readable_on_failure(self, hook, tmp_path):
        """The stderr capture mirrors the privacy expectation of
        last_input.log: on a populated failure write it must be 0600."""
        _run_hook(hook, "not-json garbage", tmp_path)
        err_log = tmp_path / ".mempalace" / "hook_state" / "last_python_err.log"
        mode = stat.S_IMODE(err_log.stat().st_mode)
        assert mode == 0o600, f"last_python_err.log mode should be 0600 on failure, got {oct(mode)}"
