"""Behavioral coverage for the Cursor hook shell scripts.

Mirrors ``tests/test_hooks_shell.py`` + ``tests/test_hooks_bash_compat.py``
in shape so a future contributor recognises the pattern. The three
hooks live at ``hooks/cursor/`` and source ``hooks/cursor/lib/common.sh``.

Covered contracts:

- bash 3.2 compatibility (no ``mapfile`` / ``readarray``; ``sed -n 'Np'``
  used for line extraction; ``bash -n`` clean).
- Per-conversation counter increments atomically across ``stop`` calls
  and emits a ``followup_message`` only on the configured interval.
- ``MEMPAL_DISABLE_HOOK=1`` and ``MEMPALACE_HOOKS_AUTO_SAVE=false`` both
  short-circuit every hook to ``{}``.
- Malformed stdin dumps the payload to a bounded 0600 file and logs a
  warning; the hook still exits 0 with ``{}`` so Cursor proceeds.
- ``loop_count > 0`` short-circuits the save hook (loop-prevention).
- A pending-save marker dropped by ``preCompact`` forces a save
  followup on the very next ``stop`` regardless of the counter.
- ``infer_wing_from_cwd`` handles ``/``, trailing slashes, spaces, and
  empty input.
- The wake hook emits ``additional_context`` referencing the inferred
  wing.
- The precompact hook drops a pending-save marker and emits the
  documented ``user_message`` shape.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOKS_DIR = REPO_ROOT / "hooks" / "cursor"
SAVE_HOOK = HOOKS_DIR / "mempal_save_hook_cursor.sh"
PRECOMPACT_HOOK = HOOKS_DIR / "mempal_precompact_hook_cursor.sh"
WAKE_HOOK = HOOKS_DIR / "mempal_wake_hook_cursor.sh"
COMMON_LIB = HOOKS_DIR / "lib" / "common.sh"

# All three .sh scripts, parametrised together for the source-level and
# universal-behaviour tests. ids= keeps pytest output readable.
ALL_HOOKS = pytest.mark.parametrize(
    "hook",
    [SAVE_HOOK, PRECOMPACT_HOOK, WAKE_HOOK],
    ids=["save_hook", "precompact_hook", "wake_hook"],
)

pytestmark = pytest.mark.skipif(os.name == "nt", reason="bash hook scripts are POSIX-only")


# ── helpers ──────────────────────────────────────────────────────────


def _run_hook(
    hook: Path,
    stdin: str,
    home: Path,
    *,
    extra_env: dict | None = None,
    path_prefix: list[Path] | None = None,
    expected_rc: int = 0,
) -> tuple[str, str]:
    """Invoke a hook with a controlled environment and assert exit code.

    Returns ``(stdout, stderr)``. Forces ``MEMPAL_PYTHON=sys.executable``
    so the hook always finds a Python that can ``import json`` — without
    this, GUI-launched CI on macOS could hit a missing python3 on the
    inherited PATH and produce spurious failures unrelated to the hook
    logic. Forces a clean ``HOME`` so the state directory is sandboxed
    under the test's ``tmp_path``.
    """
    env = {
        "HOME": str(home),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "MEMPAL_PYTHON": sys.executable,
    }
    if path_prefix:
        env["PATH"] = os.pathsep.join(str(p) for p in path_prefix) + os.pathsep + env["PATH"]
    if extra_env:
        env.update(extra_env)
    p = subprocess.run(
        ["bash", str(hook)],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        # Force a permissive umask so the hook's own ``umask 077`` inside
        # parsing subshells is provably the sole reason diagnostic files
        # end up mode 0600. Without this, an ambient restrictive umask
        # on the CI runner would mask a regression that drops the in-hook
        # ``umask`` line. Mirrors tests/test_hooks_bash_compat.py.
        preexec_fn=lambda: os.umask(0o022),
    )
    assert p.returncode == expected_rc, (
        f"{hook.name} exited {p.returncode} (expected {expected_rc}); "
        f"stderr={p.stderr!r}; stdout={p.stdout!r}"
    )
    return p.stdout, p.stderr


def _stop_payload(
    *,
    conv: str = "conv-1",
    loop_count: int = 0,
    transcript: str = "",
) -> str:
    return json.dumps(
        {
            "conversation_id": conv,
            "loop_count": loop_count,
            "status": "completed",
            "model": "claude-sonnet-4-20250514",
            "hook_event_name": "stop",
            "transcript_path": transcript,
            "workspace_roots": ["/Users/test/sampleProj"],
        }
    )


def _precompact_payload(*, conv: str = "conv-1", transcript: str = "") -> str:
    return json.dumps(
        {
            "conversation_id": conv,
            "hook_event_name": "preCompact",
            "trigger": "auto",
            "transcript_path": transcript,
            "workspace_roots": ["/Users/test/sampleProj"],
        }
    )


def _session_start_payload(*, conv: str = "conv-1") -> str:
    return json.dumps(
        {
            "conversation_id": conv,
            "session_id": conv,
            "hook_event_name": "sessionStart",
            "is_background_agent": False,
            "composer_mode": "agent",
            "workspace_roots": ["/Users/test/sampleProj"],
        }
    )


def _state_dir(home: Path) -> Path:
    return home / ".mempalace" / "hook_state"


def _log_text(home: Path) -> str:
    log = _state_dir(home) / "cursor_hook.log"
    return log.read_text() if log.exists() else ""


# ── source-level bash 3.2 compat (matches tests/test_hooks_bash_compat.py) ──


class TestBash32Compat:
    @ALL_HOOKS
    def test_bash_syntax_clean(self, hook):
        p = subprocess.run(
            ["bash", "-n", str(hook)],
            capture_output=True,
            text=True,
        )
        assert p.returncode == 0, f"{hook.name} syntax error: {p.stderr}"

    def test_common_lib_syntax_clean(self):
        p = subprocess.run(
            ["bash", "-n", str(COMMON_LIB)],
            capture_output=True,
            text=True,
        )
        assert p.returncode == 0, f"common.sh syntax error: {p.stderr}"

    @ALL_HOOKS
    def test_no_bash4_array_builtins(self, hook):
        src = "\n".join(
            line for line in hook.read_text().splitlines() if not line.lstrip().startswith("#")
        )
        assert "mapfile" not in src, (
            f"{hook.name} uses mapfile; unavailable on macOS /bin/bash 3.2 (#1440)"
        )
        assert "readarray" not in src, (
            f"{hook.name} uses readarray; unavailable on macOS /bin/bash 3.2 (#1440)"
        )

    def test_common_lib_no_bash4_array_builtins(self):
        src = "\n".join(
            line
            for line in COMMON_LIB.read_text().splitlines()
            if not line.lstrip().startswith("#")
        )
        assert "mapfile" not in src and "readarray" not in src, (
            "common.sh uses bash-4-only array builtins; would break macOS bash 3.2"
        )

    def test_common_lib_uses_sed_n_for_extraction(self):
        # Defense: if a future edit swaps the sed-based parser for
        # mapfile (the bash-4 form), this catches it at source level
        # before any behavioural test runs. ``parse_cursor_stdin`` reads
        # seven values (sentinel + six fields) so we expect at least
        # seven ``sed -n 'Np'`` calls.
        src = "\n".join(
            line
            for line in COMMON_LIB.read_text().splitlines()
            if not line.lstrip().startswith("#")
        )
        assert src.count("sed -n '") >= 7, (
            "common.sh must use sed -n 'Np' for POSIX-portable line extraction"
        )


# ── kill switches ───────────────────────────────────────────────────


class TestKillSwitches:
    @ALL_HOOKS
    @pytest.mark.parametrize("value", ["1", "true", "yes", "on"])
    def test_disable_hook_env_short_circuits(self, hook, value, tmp_path):
        out, _ = _run_hook(
            hook,
            _stop_payload(),
            tmp_path,
            extra_env={"MEMPAL_DISABLE_HOOK": value},
        )
        assert json.loads(out) == {}, f"MEMPAL_DISABLE_HOOK={value} must short-circuit; got {out!r}"
        # No state files should be created when the kill switch fires.
        state = _state_dir(tmp_path)
        assert not (state / "cursor_hook.log").exists() or _log_text(tmp_path) == ""

    @ALL_HOOKS
    @pytest.mark.parametrize("value", ["false", "0", "no", "off"])
    def test_auto_save_env_short_circuits(self, hook, value, tmp_path):
        out, _ = _run_hook(
            hook,
            _stop_payload(),
            tmp_path,
            extra_env={"MEMPALACE_HOOKS_AUTO_SAVE": value},
        )
        assert json.loads(out) == {}, (
            f"MEMPALACE_HOOKS_AUTO_SAVE={value} must short-circuit; got {out!r}"
        )

    @ALL_HOOKS
    def test_config_file_auto_save_false_short_circuits(self, hook, tmp_path):
        cfg_dir = tmp_path / ".mempalace"
        cfg_dir.mkdir(parents=True)
        (cfg_dir / "config.json").write_text(json.dumps({"hooks": {"auto_save": False}}))
        out, _ = _run_hook(hook, _stop_payload(), tmp_path)
        assert json.loads(out) == {}, (
            f"config.json hooks.auto_save=false must short-circuit; got {out!r}"
        )


# ── malformed stdin ─────────────────────────────────────────────────


class TestMalformedStdin:
    @ALL_HOOKS
    def test_malformed_input_does_not_crash(self, hook, tmp_path):
        out, _ = _run_hook(hook, "not-json garbage", tmp_path)
        # Must still produce parseable JSON so Cursor proceeds.
        assert json.loads(out) == {}, f"hook must emit {{}} on malformed input; got {out!r}"

    @ALL_HOOKS
    def test_malformed_input_logs_warning_and_dumps_payload(self, hook, tmp_path):
        _run_hook(hook, "not-json garbage", tmp_path)
        state = _state_dir(tmp_path)
        log = (state / "cursor_hook.log").read_text()
        assert "WARN: input parse failed" in log, (
            f"expected parse-failure warning in log; got: {log!r}"
        )
        dump = state / "cursor_last_input.log"
        assert dump.exists()
        assert "not-json garbage" in dump.read_text()

    @ALL_HOOKS
    def test_dump_is_mode_0600(self, hook, tmp_path):
        _run_hook(hook, "not-json garbage", tmp_path)
        dump = _state_dir(tmp_path) / "cursor_last_input.log"
        mode = stat.S_IMODE(dump.stat().st_mode)
        assert mode == 0o600, f"cursor_last_input.log mode should be 0600, got {oct(mode)}"

    @ALL_HOOKS
    def test_dump_cap_at_4096_bytes(self, hook, tmp_path):
        _run_hook(hook, "x" * 4097, tmp_path)
        dump = _state_dir(tmp_path) / "cursor_last_input.log"
        assert dump.stat().st_size == 4096, (
            f"cap must be exactly 4096 bytes; got {dump.stat().st_size}"
        )

    @ALL_HOOKS
    def test_empty_stdin_does_not_dump(self, hook, tmp_path):
        out, _ = _run_hook(hook, "", tmp_path)
        assert json.loads(out) == {}
        dump = _state_dir(tmp_path) / "cursor_last_input.log"
        assert not dump.exists(), "empty stdin must not produce a dump file"

    @ALL_HOOKS
    def test_successful_parse_leaves_no_python_err_log(self, hook, tmp_path):
        if hook == SAVE_HOOK:
            payload = _stop_payload()
        elif hook == PRECOMPACT_HOOK:
            payload = _precompact_payload()
        else:
            payload = _session_start_payload()
        _run_hook(hook, payload, tmp_path)
        err_log = _state_dir(tmp_path) / "cursor_last_python_err.log"
        assert not err_log.exists(), "successful parse must clean up cursor_last_python_err.log"


# ── save hook: counter + threshold ──────────────────────────────────


class TestSaveHookCounter:
    def test_counter_increments_across_invocations(self, tmp_path):
        for _ in range(3):
            out, _ = _run_hook(SAVE_HOOK, _stop_payload(conv="conv-A"), tmp_path)
            assert json.loads(out) == {}, "below threshold must be a no-op"
        counter_file = _state_dir(tmp_path) / "cursor_conv-A.count"
        assert counter_file.read_text() == "3", (
            f"counter should be 3 after 3 invocations; got {counter_file.read_text()!r}"
        )

    def test_counter_per_conversation_isolated(self, tmp_path):
        _run_hook(SAVE_HOOK, _stop_payload(conv="conv-A"), tmp_path)
        _run_hook(SAVE_HOOK, _stop_payload(conv="conv-A"), tmp_path)
        _run_hook(SAVE_HOOK, _stop_payload(conv="conv-B"), tmp_path)
        assert (_state_dir(tmp_path) / "cursor_conv-A.count").read_text() == "2"
        assert (_state_dir(tmp_path) / "cursor_conv-B.count").read_text() == "1"

    def test_threshold_emits_followup_message(self, tmp_path):
        # Lower the interval to keep the test fast.
        env = {"MEMPAL_SAVE_INTERVAL": "3"}
        for _ in range(2):
            out, _ = _run_hook(SAVE_HOOK, _stop_payload(), tmp_path, extra_env=env)
            assert json.loads(out) == {}
        out, _ = _run_hook(SAVE_HOOK, _stop_payload(), tmp_path, extra_env=env)
        response = json.loads(out)
        assert "followup_message" in response, (
            f"third invocation must emit a followup_message; got {response!r}"
        )
        msg = response["followup_message"]
        # Followup must reference the real MCP tool names (regression
        # guard against future typos that would silently fail).
        assert "mempalace_add_drawer" in msg
        assert "mempalace_check_duplicate" in msg
        assert "mempalace_diary_write" in msg
        assert "cursor-ide" in msg, "diary entries must be tagged agent_name=cursor-ide"

    def test_threshold_followup_references_inferred_wing(self, tmp_path):
        env = {"MEMPAL_SAVE_INTERVAL": "1"}
        # workspace_roots[0] = /Users/test/sampleProj -> wing=sampleproj
        out, _ = _run_hook(SAVE_HOOK, _stop_payload(), tmp_path, extra_env=env)
        msg = json.loads(out)["followup_message"]
        assert "sampleproj" in msg, f"followup should reference inferred wing; got {msg!r}"

    def test_save_interval_zero_is_coerced_to_default(self, tmp_path):
        """Regression for gh-PR review: MEMPAL_SAVE_INTERVAL=0 would
        otherwise crash bash on `$((NEXT % 0))` (division by zero).
        Zero must be coerced to the default interval (15) so the hook
        survives a misconfigured env var without exiting non-zero.
        """
        env = {"MEMPAL_SAVE_INTERVAL": "0"}
        # Three independent invocations: each must succeed (rc=0) and
        # emit {} since the coerced interval (15) is never reached.
        for _ in range(3):
            out, _ = _run_hook(SAVE_HOOK, _stop_payload(), tmp_path, extra_env=env)
            assert json.loads(out) == {}, (
                f"SAVE_INTERVAL=0 must coerce to default and pass through; got {out!r}"
            )


# ── save hook: followup opt-out ─────────────────────────────────────


class TestSaveHookFollowupSilence:
    """The Cursor followup_message is ON by default (it is the
    load-bearing verbatim path because Cursor's transcript is unminable),
    but users can silence it. These tests lock the opt-out contract.
    """

    def test_followup_on_by_default_at_threshold(self, tmp_path):
        """Sanity baseline: with no silence flag, the threshold emits a
        followup. Guards against an accidental default flip."""
        env = {"MEMPAL_SAVE_INTERVAL": "1"}
        out, _ = _run_hook(SAVE_HOOK, _stop_payload(), tmp_path, extra_env=env)
        assert "followup_message" in json.loads(out)

    @pytest.mark.parametrize("value", ["1", "true", "yes", "on"])
    def test_cursor_silent_suppresses_followup(self, value, tmp_path):
        env = {"MEMPAL_SAVE_INTERVAL": "1", "MEMPAL_CURSOR_SILENT": value}
        out, _ = _run_hook(SAVE_HOOK, _stop_payload(), tmp_path, extra_env=env)
        assert json.loads(out) == {}, (
            f"MEMPAL_CURSOR_SILENT={value!r} must suppress the followup; got {out!r}"
        )

    @pytest.mark.parametrize("value", ["false", "0", "no", "off"])
    def test_verbose_false_suppresses_followup(self, value, tmp_path):
        env = {"MEMPAL_SAVE_INTERVAL": "1", "MEMPAL_VERBOSE": value}
        out, _ = _run_hook(SAVE_HOOK, _stop_payload(), tmp_path, extra_env=env)
        assert json.loads(out) == {}, (
            f"MEMPAL_VERBOSE={value!r} must suppress the followup; got {out!r}"
        )

    def test_silenced_followup_still_increments_counter(self, tmp_path):
        """Silence must not disable bookkeeping — the counter still
        advances so cadence is preserved if the user re-enables."""
        env = {"MEMPAL_SAVE_INTERVAL": "5", "MEMPAL_CURSOR_SILENT": "1"}
        for _ in range(2):
            _run_hook(SAVE_HOOK, _stop_payload(conv="conv-S"), tmp_path, extra_env=env)
        counter = _state_dir(tmp_path) / "cursor_conv-S.count"
        assert counter.exists() and counter.read_text().strip() == "2", (
            "silenced followup must still maintain the per-conversation counter"
        )

    def test_silenced_pending_marker_emits_empty(self, tmp_path):
        """A consumed pending marker normally forces a followup; under
        silence it must emit {} but still clear the marker."""
        env = {"MEMPAL_CURSOR_SILENT": "1"}
        pending = _state_dir(tmp_path) / "cursor_conv-P.pending"
        pending.parent.mkdir(parents=True, exist_ok=True)
        pending.touch()
        out, _ = _run_hook(SAVE_HOOK, _stop_payload(conv="conv-P"), tmp_path, extra_env=env)
        assert json.loads(out) == {}
        assert not pending.exists(), "pending marker must be consumed even when silenced"


# ── save hook: loop-prevention ──────────────────────────────────────


class TestSaveHookLoopPrevention:
    def test_loop_count_gt_zero_short_circuits(self, tmp_path):
        out, _ = _run_hook(
            SAVE_HOOK,
            _stop_payload(loop_count=1),
            tmp_path,
            extra_env={"MEMPAL_SAVE_INTERVAL": "1"},
        )
        assert json.loads(out) == {}, (
            "loop_count > 0 must short-circuit even at the trigger interval"
        )
        # No counter file should be written in the short-circuit path.
        assert not (_state_dir(tmp_path) / "cursor_conv-1.count").exists()

    def test_loop_count_zero_does_not_short_circuit(self, tmp_path):
        out, _ = _run_hook(
            SAVE_HOOK,
            _stop_payload(loop_count=0),
            tmp_path,
            extra_env={"MEMPAL_SAVE_INTERVAL": "1"},
        )
        assert "followup_message" in json.loads(out)


# ── save hook: pending-save marker from preCompact ──────────────────


class TestPendingSaveMarker:
    def test_pending_marker_forces_followup_regardless_of_counter(self, tmp_path):
        state = _state_dir(tmp_path)
        state.mkdir(parents=True, exist_ok=True)
        # Drop the marker as if precompact had run.
        (state / "cursor_conv-1.pending").write_text("")
        out, _ = _run_hook(
            SAVE_HOOK,
            _stop_payload(),
            tmp_path,
            # SAVE_INTERVAL=1000 ensures the normal counter path would
            # not trigger; the marker is the only reason a followup
            # gets emitted.
            extra_env={"MEMPAL_SAVE_INTERVAL": "1000"},
        )
        response = json.loads(out)
        assert "followup_message" in response, (
            "pending marker must force a followup even far below threshold"
        )
        # Marker must be consumed on read.
        assert not (state / "cursor_conv-1.pending").exists(), (
            "pending marker must be deleted after consumption"
        )

    def test_pending_marker_is_per_conversation(self, tmp_path):
        state = _state_dir(tmp_path)
        state.mkdir(parents=True, exist_ok=True)
        (state / "cursor_conv-OTHER.pending").write_text("")
        out, _ = _run_hook(
            SAVE_HOOK,
            _stop_payload(conv="conv-1"),
            tmp_path,
            extra_env={"MEMPAL_SAVE_INTERVAL": "1000"},
        )
        # conv-1 has no marker -> counter path -> no trigger -> {}.
        assert json.loads(out) == {}
        # conv-OTHER marker must NOT be consumed by conv-1's invocation.
        assert (state / "cursor_conv-OTHER.pending").exists()


# ── preCompact hook ─────────────────────────────────────────────────


class TestPreCompactHook:
    def test_emits_user_message(self, tmp_path):
        out, _ = _run_hook(PRECOMPACT_HOOK, _precompact_payload(), tmp_path)
        response = json.loads(out)
        # Cursor's preCompact only accepts user_message; never
        # followup_message or decision.
        assert "user_message" in response, f"expected user_message; got {response!r}"
        assert "followup_message" not in response
        assert "decision" not in response

    def test_drops_pending_marker(self, tmp_path):
        _run_hook(PRECOMPACT_HOOK, _precompact_payload(conv="conv-X"), tmp_path)
        marker = _state_dir(tmp_path) / "cursor_conv-X.pending"
        assert marker.exists(), "preCompact must drop a pending-save marker"

    def test_logs_trigger(self, tmp_path):
        _run_hook(PRECOMPACT_HOOK, _precompact_payload(conv="conv-Y"), tmp_path)
        log = _log_text(tmp_path)
        assert "event=preCompact" in log
        assert "conv=conv-Y" in log
        assert "trigger=auto" in log


# ── wake (sessionStart) hook ───────────────────────────────────────


class TestWakeHook:
    def test_emits_additional_context(self, tmp_path):
        out, _ = _run_hook(WAKE_HOOK, _session_start_payload(), tmp_path)
        response = json.loads(out)
        assert "additional_context" in response, (
            f"sessionStart must emit additional_context; got {response!r}"
        )
        ctx = response["additional_context"]
        # Must reference the inferred wing AND the real MCP tools.
        assert "sampleproj" in ctx, f"context should reference inferred wing; got {ctx!r}"
        assert "mempalace_search" in ctx
        assert "mempalace_diary_read" in ctx
        assert "cursor-ide" in ctx

    def test_falls_back_to_env_when_workspace_roots_missing(self, tmp_path):
        # Cursor always provides workspace_roots, but the env-var
        # fallback path needs coverage so a future Cursor schema
        # change cannot silently break the wake hook.
        payload = json.dumps(
            {
                "conversation_id": "conv-Z",
                "session_id": "conv-Z",
                "hook_event_name": "sessionStart",
                "is_background_agent": False,
                "composer_mode": "agent",
            }
        )
        out, _ = _run_hook(
            WAKE_HOOK,
            payload,
            tmp_path,
            extra_env={"CURSOR_PROJECT_DIR": "/Users/test/envFallback"},
        )
        ctx = json.loads(out)["additional_context"]
        assert "envfallback" in ctx, (
            f"env-var fallback workspace should drive wing inference; got {ctx!r}"
        )


# ── infer_wing_from_cwd via direct function call ────────────────────


def _call_infer_wing(arg: str) -> str:
    """Source common.sh in a bash subshell and invoke mempal_infer_wing.

    Returns the function's stdout. Uses bash -c so we never have to
    pollute the test's own shell environment with the common.sh state
    (which mkdir's directories and resolves Python paths).
    """
    script = f'. "{COMMON_LIB}" >/dev/null 2>&1; mempal_infer_wing "$1"'
    # The argument is passed as a positional so it survives any shell
    # quirks around spaces/empty values exactly as the production hook
    # would see them.
    p = subprocess.run(
        ["bash", "-c", script, "_test", arg],
        capture_output=True,
        text=True,
        env={
            "HOME": "/tmp",
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "MEMPAL_PYTHON": sys.executable,
        },
        timeout=10,
    )
    assert p.returncode == 0, f"infer_wing call failed: {p.stderr!r}"
    return p.stdout


class TestInferWing:
    def test_basename_of_normal_path(self):
        assert _call_infer_wing("/Users/me/myproject") == "myproject"

    def test_strips_trailing_slash(self):
        assert _call_infer_wing("/Users/me/myproject/") == "myproject"

    def test_root_path_falls_back(self):
        assert _call_infer_wing("/") == "root"

    def test_empty_input_falls_back(self):
        assert _call_infer_wing("") == "cursor_session"

    def test_spaces_collapsed_to_underscore(self):
        assert _call_infer_wing("/Users/me/my project") == "my_project"

    def test_lowercases_uppercase_basename(self):
        # Cursor on macOS often hands us /Users/<user>/Projects/MyApp.
        # The wing scoping in MemPalace's MCP tools is case-sensitive,
        # so the wake hook and save hook must produce identical wings
        # for the same workspace — lowercasing is the simplest
        # contract.
        assert _call_infer_wing("/Users/me/MyApp") == "myapp"

    def test_windows_style_path(self):
        # Cursor on Windows passes C:\path\to\Project as workspace_root.
        # The hook scripts are POSIX-only (we skip them on Windows) but
        # WSL users may still hit a backslash-bearing path via the
        # CURSOR_PROJECT_DIR env var when Cursor is launched from
        # PowerShell.
        assert _call_infer_wing(r"C:\Users\me\MyProj") == "myproj"


# ── state-file TTL + GC ─────────────────────────────────────────────


def _run_common_snippet(snippet: str, home: Path, *, extra_env: dict | None = None) -> str:
    """Source common.sh and run a bash snippet against a sandboxed HOME.

    Returns stdout. Used to exercise mempal_state_ttl_days /
    mempal_gc_stale_state directly without going through a full hook.
    """
    script = f'. "{COMMON_LIB}" >/dev/null 2>&1; {snippet}'
    env = {
        "HOME": str(home),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "MEMPAL_PYTHON": sys.executable,
    }
    if extra_env:
        env.update(extra_env)
    p = subprocess.run(
        ["bash", "-c", script, "_test"],
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )
    assert p.returncode == 0, f"snippet failed: {p.stderr!r}"
    return p.stdout


def _age_file(path: Path, days: int) -> None:
    old = time.time() - days * 86400
    os.utime(path, (old, old))


class TestStateTtlDays:
    @pytest.mark.parametrize(
        "value,expected",
        [
            ("", "30"),
            ("abc", "30"),
            ("45", "45"),
            ("08", "8"),
            ("007", "7"),
            ("0", "0"),
        ],
    )
    def test_ttl_validation_and_octal_strip(self, value, expected, tmp_path):
        out = _run_common_snippet(
            "mempal_state_ttl_days",
            tmp_path,
            extra_env={"MEMPAL_STATE_TTL_DAYS": value} if value != "" else {},
        )
        assert out.strip() == expected, (
            f"MEMPAL_STATE_TTL_DAYS={value!r} should resolve to {expected!r}; got {out.strip()!r}"
        )

    def test_ttl_default_when_unset(self, tmp_path):
        assert _run_common_snippet("mempal_state_ttl_days", tmp_path).strip() == "30"


class TestStateGc:
    def test_removes_stale_count_and_pending(self, tmp_path):
        sd = _state_dir(tmp_path)
        sd.mkdir(parents=True, exist_ok=True)
        stale_count = sd / "cursor_old.count"
        stale_pending = sd / "cursor_old.pending"
        fresh_count = sd / "cursor_new.count"
        for f in (stale_count, stale_pending, fresh_count):
            f.write_text("1")
        _age_file(stale_count, 40)
        _age_file(stale_pending, 40)
        _run_common_snippet("mempal_gc_stale_state", tmp_path)
        assert not stale_count.exists(), "stale .count older than TTL must be swept"
        assert not stale_pending.exists(), "stale .pending older than TTL must be swept"
        assert fresh_count.exists(), "recent state must be preserved"

    def test_preserves_shared_logs_and_other_editor_state(self, tmp_path):
        sd = _state_dir(tmp_path)
        sd.mkdir(parents=True, exist_ok=True)
        # Shared logs + another editor's state, all aged well past the TTL.
        keep = [
            sd / "cursor_hook.log",
            sd / "cursor_last_input.log",
            sd / "cursor_last_python_err.log",
            sd / "antigravity_save_count_xyz",
            sd / "hook.log",
        ]
        for f in keep:
            f.write_text("x")
            _age_file(f, 99)
        _run_common_snippet("mempal_gc_stale_state", tmp_path)
        for f in keep:
            assert f.exists(), f"GC must never touch {f.name}"

    def test_creates_sweep_marker(self, tmp_path):
        _run_common_snippet("mempal_gc_stale_state", tmp_path)
        assert (_state_dir(tmp_path) / "cursor_last_sweep").exists()

    def test_throttled_within_24h(self, tmp_path):
        sd = _state_dir(tmp_path)
        sd.mkdir(parents=True, exist_ok=True)
        # A fresh sweep marker must suppress a second sweep, so a stale
        # file created afterwards survives until the throttle expires.
        (sd / "cursor_last_sweep").write_text("")
        stale = sd / "cursor_old.count"
        stale.write_text("1")
        _age_file(stale, 40)
        _run_common_snippet("mempal_gc_stale_state", tmp_path)
        assert stale.exists(), "GC must be throttled when last_sweep is recent"

    def test_gc_gated_by_kill_switch(self, tmp_path):
        """A disabled hook must not sweep (or even create the marker)."""
        sd = _state_dir(tmp_path)
        sd.mkdir(parents=True, exist_ok=True)
        stale = sd / "cursor_zombie.count"
        stale.write_text("1")
        _age_file(stale, 40)
        _run_hook(
            SAVE_HOOK,
            _stop_payload(),
            tmp_path,
            extra_env={"MEMPAL_DISABLE_HOOK": "1"},
        )
        assert stale.exists(), "disabled hook must not GC state"
        assert not (sd / "cursor_last_sweep").exists(), (
            "disabled hook must not even create the sweep marker"
        )


# ── logging discipline ─────────────────────────────────────────────


class TestLogging:
    def test_log_uses_iso8601_utc_timestamps(self, tmp_path):
        _run_hook(SAVE_HOOK, _stop_payload(), tmp_path)
        log = _log_text(tmp_path)
        # ISO 8601 with 'Z' suffix means UTC, locale-independent.
        # Regression guard against switching back to %H:%M:%S which
        # loses both the date and the timezone.
        assert "T" in log and "Z]" in log, f"log timestamps must be ISO 8601 UTC; got: {log!r}"
