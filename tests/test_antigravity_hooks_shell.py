"""End-to-end shell tests for the Antigravity hook scripts.

Invokes the bash scripts directly via subprocess with synthetic stdin
JSON and asserts on their stdout / exit code / state-dir side effects.

The two scripts under test are:

* `hooks/antigravity/mempal_save_hook_antigravity.sh`  — Stop event
* `hooks/antigravity/mempal_wake_hook_antigravity.sh`  — PreInvocation event

Test isolation:

* Each test runs in its own temp dir.
* `MEMPAL_STATE_DIR` is overridden to point at the temp dir, so no
  test ever touches the real `~/.mempalace/hook_state/`.
* `HOME` is overridden to a temp dir as well so the kill-switch
  palace-existence check sees a hermetic state.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOKS_DIR = REPO_ROOT / "hooks" / "antigravity"
SAVE_HOOK = HOOKS_DIR / "mempal_save_hook_antigravity.sh"
WAKE_HOOK = HOOKS_DIR / "mempal_wake_hook_antigravity.sh"
COMMON_LIB = HOOKS_DIR / "lib" / "common.sh"

# Skip the entire module on Windows — bash 3.2+ is required.
pytestmark = pytest.mark.skipif(
    os.name == "nt",
    reason="Antigravity shell hooks require bash; Windows uses a separate code path.",
)


def _run_hook(
    script: Path,
    stdin_json: dict | str,
    state_dir: Path,
    home: Path,
    extra_env: dict[str, str] | None = None,
    timeout: float = 10.0,
) -> subprocess.CompletedProcess:
    """Run a hook script with isolated env and synthetic stdin."""
    if isinstance(stdin_json, dict):
        stdin = json.dumps(stdin_json)
    else:
        stdin = stdin_json
    env = os.environ.copy()
    # Hermetic env: HOME and state dir point at the test temp.
    home.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    env["HOME"] = str(home)
    env["MEMPAL_STATE_DIR"] = str(state_dir)
    # Drop any leftover kill-switch envs from the user's environment so
    # the test exercises the gate it intends to.
    for k in ("MEMPAL_DISABLE_HOOK", "MEMPALACE_HOOKS_AUTO_SAVE", "MEMPAL_SAVE_INTERVAL"):
        env.pop(k, None)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(script)],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
    )


def _ensure_palace(home: Path) -> None:
    """Create $HOME/.mempalace/ so the palace-nuke kill switch passes."""
    (home / ".mempalace").mkdir(parents=True, exist_ok=True)


def _poll_log_contains(log_path: Path, needle: str, timeout: float = 5.0) -> bool:
    """Poll a log file until it contains ``needle`` or the timeout elapses.

    The save hook now writes mine/probe outcome lines from a detached
    background subshell, so the foreground returns before those lines
    are flushed. Callers that assert on background-written log lines
    must poll rather than read once.
    """
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if log_path.is_file():
            body = log_path.read_text(errors="replace")
            if needle in body:
                return True
        time.sleep(0.05)
    return False


def _stop_payload(**overrides) -> dict:
    base = {
        "executionNum": 1,
        "terminationReason": "model_stop",
        "error": "",
        "fullyIdle": True,
        "conversationId": "test-conv-001",
        "workspacePaths": ["/tmp/test-workspace"],
        "transcriptPath": "/tmp/test-transcript.jsonl",
        "artifactDirectoryPath": "/tmp/test-artifacts/",
    }
    base.update(overrides)
    return base


def _wake_payload(**overrides) -> dict:
    base = {
        "invocationNum": 1,
        "initialNumSteps": 0,
        "conversationId": "test-conv-001",
        "workspacePaths": ["/tmp/test-workspace"],
        "transcriptPath": "/tmp/test-transcript.jsonl",
        "artifactDirectoryPath": "/tmp/test-artifacts/",
    }
    base.update(overrides)
    return base


# ── Syntax (bash -n) ──────────────────────────────────────────────────


@pytest.mark.parametrize("script", [SAVE_HOOK, WAKE_HOOK, COMMON_LIB], ids=lambda p: p.name)
def test_bash_n_clean(script: Path) -> None:
    """All shell files parse cleanly under bash 3.2+."""
    result = subprocess.run(
        ["bash", "-n", str(script)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, f"bash -n {script.name} failed:\n{result.stderr}"


# ── Save hook ─────────────────────────────────────────────────────────


def test_save_hook_emits_empty_object_on_kill_switch_env(tmp_path: Path) -> None:
    """MEMPAL_DISABLE_HOOK=1 should silently emit `{}` and exit 0."""
    state = tmp_path / "state"
    home = tmp_path / "home"
    _ensure_palace(home)
    result = _run_hook(
        SAVE_HOOK,
        _stop_payload(),
        state_dir=state,
        home=home,
        extra_env={"MEMPAL_DISABLE_HOOK": "1"},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "{}", result.stdout


def test_save_hook_emits_empty_object_on_auto_save_false(tmp_path: Path) -> None:
    """MEMPALACE_HOOKS_AUTO_SAVE=false short-circuits."""
    state = tmp_path / "state"
    home = tmp_path / "home"
    _ensure_palace(home)
    result = _run_hook(
        SAVE_HOOK,
        _stop_payload(),
        state_dir=state,
        home=home,
        extra_env={"MEMPALACE_HOOKS_AUTO_SAVE": "false"},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "{}"


def test_save_hook_emits_empty_object_when_palace_dir_missing(tmp_path: Path) -> None:
    """Removing $HOME/.mempalace acts as the strongest kill switch."""
    state = tmp_path / "state"
    home = tmp_path / "home"
    home.mkdir()
    # Deliberately do NOT create ~/.mempalace
    result = _run_hook(SAVE_HOOK, _stop_payload(), state_dir=state, home=home)
    assert result.returncode == 0
    assert result.stdout.strip() == "{}"


def test_save_hook_emits_empty_object_when_config_disables(tmp_path: Path) -> None:
    """~/.mempalace/config.json `hooks.auto_save: false` short-circuits."""
    state = tmp_path / "state"
    home = tmp_path / "home"
    _ensure_palace(home)
    (home / ".mempalace" / "config.json").write_text(
        json.dumps({"hooks": {"auto_save": False}}),
        encoding="utf-8",
    )
    result = _run_hook(SAVE_HOOK, _stop_payload(), state_dir=state, home=home)
    assert result.returncode == 0
    assert result.stdout.strip() == "{}"


def test_save_hook_emits_empty_object_when_fully_idle_false(tmp_path: Path) -> None:
    """fullyIdle=False defers the save; nothing should write to state."""
    state = tmp_path / "state"
    home = tmp_path / "home"
    _ensure_palace(home)
    result = _run_hook(
        SAVE_HOOK,
        _stop_payload(fullyIdle=False),
        state_dir=state,
        home=home,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "{}"
    # The counter file must NOT exist — we deferred before incrementing.
    counter = state / "antigravity_save_count_test-conv-001"
    assert not counter.exists(), f"counter advanced despite fullyIdle=false: {counter}"


def test_save_hook_emits_empty_object_on_error_termination(tmp_path: Path) -> None:
    """terminationReason=error skips the save."""
    state = tmp_path / "state"
    home = tmp_path / "home"
    _ensure_palace(home)
    result = _run_hook(
        SAVE_HOOK,
        _stop_payload(terminationReason="error", error="model crashed"),
        state_dir=state,
        home=home,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "{}"


def test_save_hook_emits_empty_object_on_malformed_stdin(tmp_path: Path) -> None:
    """Malformed JSON must not crash the hook — fail-open behaviour."""
    state = tmp_path / "state"
    home = tmp_path / "home"
    _ensure_palace(home)
    result = _run_hook(
        SAVE_HOOK,
        "{not even close to json{",
        state_dir=state,
        home=home,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "{}"


def test_save_hook_emits_empty_object_on_empty_stdin(tmp_path: Path) -> None:
    """Empty stdin must not crash the hook."""
    state = tmp_path / "state"
    home = tmp_path / "home"
    _ensure_palace(home)
    result = _run_hook(SAVE_HOOK, "", state_dir=state, home=home)
    assert result.returncode == 0
    assert result.stdout.strip() == "{}"


def test_save_hook_never_emits_decision_continue(tmp_path: Path) -> None:
    """The save hook must NEVER emit `{"decision":"continue"}`.

    That output would force Antigravity into an infinite agent
    re-execution loop. Hard rule, separately tested.
    """
    state = tmp_path / "state"
    home = tmp_path / "home"
    _ensure_palace(home)
    result = _run_hook(SAVE_HOOK, _stop_payload(), state_dir=state, home=home)
    assert result.returncode == 0
    # Parse the output so we don't false-match on substring of
    # "Continue thread of work" or similar prose.
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        pytest.fail(f"save hook emitted non-JSON: {result.stdout!r}")
    assert payload.get("decision") != "continue", (
        f"save hook emitted decision=continue, which would force an infinite "
        f"agent loop. payload={payload!r}"
    )


def test_save_hook_counter_increments_per_fire(tmp_path: Path) -> None:
    """Counter advances on each Stop fire."""
    state = tmp_path / "state"
    home = tmp_path / "home"
    _ensure_palace(home)
    counter_path = state / "antigravity_save_count_test-conv-001"

    for expected in (1, 2, 3):
        result = _run_hook(
            SAVE_HOOK,
            _stop_payload(),
            state_dir=state,
            home=home,
            extra_env={"MEMPAL_SAVE_INTERVAL": "999"},  # high interval -> never trigger save
        )
        assert result.returncode == 0
        assert result.stdout.strip() == "{}"
        assert counter_path.is_file()
        assert counter_path.read_text().strip() == str(expected)


def test_save_hook_floors_zero_save_interval_to_avoid_div_by_zero(tmp_path: Path) -> None:
    """MEMPAL_SAVE_INTERVAL=0 must be floored, never cause `count % 0`."""
    state = tmp_path / "state"
    home = tmp_path / "home"
    _ensure_palace(home)
    result = _run_hook(
        SAVE_HOOK,
        _stop_payload(),
        state_dir=state,
        home=home,
        extra_env={"MEMPAL_SAVE_INTERVAL": "0"},
    )
    # Must NOT crash with bash arithmetic divide-by-zero.
    assert result.returncode == 0, (
        f"save hook crashed on MEMPAL_SAVE_INTERVAL=0:\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    assert result.stdout.strip() == "{}"


def test_save_hook_floors_negative_save_interval(tmp_path: Path) -> None:
    """Negative MEMPAL_SAVE_INTERVAL falls back to default (no crash)."""
    state = tmp_path / "state"
    home = tmp_path / "home"
    _ensure_palace(home)
    result = _run_hook(
        SAVE_HOOK,
        _stop_payload(),
        state_dir=state,
        home=home,
        extra_env={"MEMPAL_SAVE_INTERVAL": "-5"},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "{}"


@pytest.mark.parametrize("interval", ["08", "09", "008", "0099"])
def test_save_hook_handles_leading_zero_save_interval(tmp_path: Path, interval: str) -> None:
    """MEMPAL_SAVE_INTERVAL with leading zeros must NOT trigger bash octal arithmetic.

    bash arithmetic ($((COUNT % INTERVAL))) parses any token starting
    with `0` as octal. Values like "08" or "09" are not valid octal
    digits and would crash the modulo step with::

        bash: 08: value too great for base (error token is "08")

    mempal_save_interval() in lib/common.sh strips leading zeros before
    returning. Regression test for gemini-code-assist review on PR
    #1633.
    """
    state = tmp_path / "state"
    home = tmp_path / "home"
    _ensure_palace(home)
    result = _run_hook(
        SAVE_HOOK,
        _stop_payload(),
        state_dir=state,
        home=home,
        extra_env={"MEMPAL_SAVE_INTERVAL": interval},
    )
    assert result.returncode == 0, (
        f"save hook crashed on MEMPAL_SAVE_INTERVAL={interval!r}:\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    assert result.stdout.strip() == "{}"
    # Stderr must not contain the octal "value too great for base" error.
    assert "value too great for base" not in result.stderr, (
        f"bash octal parse error leaked through for MEMPAL_SAVE_INTERVAL={interval!r}: "
        f"{result.stderr!r}"
    )


def test_common_sh_parser_omits_sentinel_on_malformed_json(tmp_path: Path) -> None:
    """`mempal_parse_stdin` must NOT print the success sentinel on parse failure.

    The bash callers detect parse failure by checking whether line 1
    of the parser output is exactly ``__MEMPAL_PARSE_OK__``. If
    json.load is wrapped in try/except (and falls back to data={}),
    the sentinel still gets printed and the bash defense-in-depth
    branch never engages. Regression test for gemini-code-assist
    review on PR #1633.
    """
    state = tmp_path / "state"
    home = tmp_path / "home"
    _ensure_palace(home)
    state.mkdir(parents=True, exist_ok=True)
    # Source the lib and call mempal_parse_stdin with malformed JSON.
    cmd = f". {COMMON_LIB}; mempal_parse_stdin '{{not even close to json{{'"
    result = subprocess.run(
        ["bash", "-c", cmd],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "HOME": str(home),
            "MEMPAL_STATE_DIR": str(state),
        },
        timeout=10,
    )
    # The function itself shouldn't error (the inner Python crashes,
    # but the subshell catches it). Stdout must NOT contain the sentinel.
    assert "__MEMPAL_PARSE_OK__" not in result.stdout, (
        f"parser printed success sentinel on bad JSON, defeating "
        f"bash-side error detection: stdout={result.stdout!r}"
    )


def test_save_hook_missing_mempalace_python_module_does_not_crash(tmp_path: Path) -> None:
    """When the resolved Python interpreter cannot run `-m mempalace`, fail open.

    The save hook now invokes mempalace via ``"$MEMPAL_PYTHON_BIN"
    -m mempalace mine ...`` rather than the bare ``mempalace`` console
    script. If MEMPAL_PYTHON points at an interpreter that doesn't
    have the package installed, the hook must log the failure and
    still emit ``{}`` — never crash, never block the user.
    """
    state = tmp_path / "state"
    home = tmp_path / "home"
    _ensure_palace(home)
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    # Point MEMPAL_PYTHON at a stub interpreter that has no mempalace
    # package installed — `python -m mempalace --version` will fail.
    stub = tmp_path / "stub_python"
    stub.write_text(
        "#!/bin/sh\n"
        "# Minimal python stub: rejects every -m invocation so the\n"
        '# hook hits the "module unrunnable" branch.\n'
        'case "$*" in\n'
        '    *"-m mempalace"*) exit 1 ;;\n'
        '    *) exec /usr/bin/env python3 "$@" ;;\n'
        "esac\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    result = _run_hook(
        SAVE_HOOK,
        _stop_payload(transcriptPath=str(transcript)),
        state_dir=state,
        home=home,
        extra_env={
            "MEMPAL_PYTHON": str(stub),
            "MEMPAL_SAVE_INTERVAL": "1",
        },
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "{}"
    # The "is not runnable" line is written by the detached background
    # subshell now (the probe moved off the foreground), so poll for it.
    log = state / "antigravity_hook.log"
    assert _poll_log_contains(log, "is not runnable via"), (
        f"expected the 'mempalace not runnable via $MEMPAL_PYTHON_BIN' log line; "
        f"got:\n{log.read_text(errors='replace') if log.is_file() else '<no log>'}"
    )


def test_save_hook_uses_python_module_invocation(tmp_path: Path) -> None:
    """The save hook source MUST invoke mempalace via `-m mempalace`.

    Locks in the gemini-code-assist fix so a future edit doesn't
    silently regress to the bare ``mempalace`` console-script call,
    which fails when the user's PATH doesn't expose the venv bin.
    """
    body = SAVE_HOOK.read_text(encoding="utf-8")
    assert '"$MEMPAL_PYTHON_BIN" -m mempalace' in body, (
        "save hook should invoke mempalace via $MEMPAL_PYTHON_BIN -m mempalace, "
        "not the bare `mempalace` console script. The bare invocation breaks "
        "when the venv's bin/ isn't on the hook's PATH."
    )
    # Also verify the bare invocation is gone (defense-in-depth).
    # Allow `mempalace` to appear in comments / strings, but not as
    # the start of a `nohup ... mempalace mine` command.
    assert "nohup mempalace " not in body, (
        "bare `nohup mempalace ...` invocation found; should be "
        '`nohup "$MEMPAL_PYTHON_BIN" -m mempalace ...`'
    )


def test_save_hook_backgrounds_probe_mine_and_cleanup_in_one_subshell(tmp_path: Path) -> None:
    """Probe + mine + marker cleanup must live in ONE detached subshell.

    igorls' PR #1633 review: the foreground `mempalace --version`
    probe pays the full chromadb/onnx cold-start import (the `mine`
    subparser imports `mempalace.miner` before argparse handles
    `--version`), which blows the save budget. Moving the probe into
    the background subshell — together with the mine and the marker
    cleanup — keeps the foreground instant.

    Folding cleanup into the same subshell also retires the previous
    `kill -0 $MINE_PID` watcher: the `rm -f "$PENDING_FILE"` runs
    sequentially after the mine in the shell that owns it, so there is
    no sibling-PID `wait`/`kill -0` hazard anymore. This test locks in
    that structure.
    """
    body = SAVE_HOOK.read_text(encoding="utf-8")
    # The buggy sibling `wait` must stay gone.
    assert 'wait "$MINE_PID"' not in body, (
        'buggy `wait "$MINE_PID"` reappeared. POSIX wait cannot watch a sibling pid.'
    )
    # The kill -0 polling watcher is no longer needed and should be gone.
    assert "kill -0" not in body, (
        "the `kill -0` watcher should have been retired when probe+mine+cleanup "
        "were folded into a single background subshell."
    )
    # Probe still happens (just inside the subshell now) and uses -m.
    assert '"$MEMPAL_PYTHON_BIN" -m mempalace --version' in body, (
        "the runnability probe must still run via $MEMPAL_PYTHON_BIN -m mempalace"
    )
    # The whole block is backgrounded: a subshell close followed by the
    # detach redirection + `&`.
    assert ") >/dev/null 2>&1 < /dev/null &" in body, (
        "probe+mine+cleanup must be wrapped in a detached `( ... ) ... &` subshell"
    )
    # MINE_PID capture is no longer used (no separate watcher).
    assert "MINE_PID=$!" not in body, (
        "MINE_PID capture is vestigial now that there is no separate watcher"
    )


def test_save_hook_returns_before_slow_version_probe(tmp_path: Path) -> None:
    """The hook must return immediately even if `--version` is slow.

    Proves the probe was moved off the foreground. We point
    MEMPAL_PYTHON at a stub that sleeps for 3s on any `-m mempalace`
    invocation. If the probe still ran in the foreground the hook
    would block ~3s; with the probe backgrounded it returns in well
    under a second.
    """
    import time

    state = tmp_path / "state"
    home = tmp_path / "home"
    _ensure_palace(home)
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    stub = tmp_path / "slow_python"
    stub.write_text(
        "#!/bin/sh\n"
        "# Stub python: any -m mempalace call sleeps 3s, simulating a\n"
        "# heavy cold-start import. Everything else proxies to python3.\n"
        'case "$*" in\n'
        '    *"-m mempalace"*) sleep 3; exit 0 ;;\n'
        '    *) exec /usr/bin/env python3 "$@" ;;\n'
        "esac\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    start = time.monotonic()
    result = _run_hook(
        SAVE_HOOK,
        _stop_payload(transcriptPath=str(transcript)),
        state_dir=state,
        home=home,
        extra_env={"MEMPAL_PYTHON": str(stub), "MEMPAL_SAVE_INTERVAL": "1"},
        timeout=10.0,
    )
    elapsed = time.monotonic() - start
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "{}"
    assert elapsed < 2.0, (
        f"save hook took {elapsed:.2f}s with a 3s --version probe; the probe "
        f"is still running in the foreground instead of the background subshell."
    )


def test_save_hook_counter_write_is_atomic(tmp_path: Path) -> None:
    """Counter is written via mempal_write_counter_atomic (temp + mv).

    igorls' PR #1633 review: the counter was written with a plain
    `printf > file` (truncate-then-write) while the comment claimed it
    was atomic. We verify (a) the source uses the atomic helper, not a
    bare redirect into the counter file, (b) the counter still
    increments correctly across fires, and (c) no temp file is left
    behind.
    """
    body = SAVE_HOOK.read_text(encoding="utf-8")
    assert 'mempal_write_counter_atomic "$COUNTER_FILE" "$COUNT"' in body, (
        "save hook must write the counter via mempal_write_counter_atomic"
    )
    assert 'printf \'%s\' "$COUNT" > "$COUNTER_FILE"' not in body, (
        "non-atomic `printf > $COUNTER_FILE` should have been replaced"
    )

    state = tmp_path / "state"
    home = tmp_path / "home"
    _ensure_palace(home)
    counter_path = state / "antigravity_save_count_test-conv-001"
    for expected in (1, 2, 3):
        result = _run_hook(
            SAVE_HOOK,
            _stop_payload(),
            state_dir=state,
            home=home,
            extra_env={"MEMPAL_SAVE_INTERVAL": "999"},
        )
        assert result.returncode == 0
        assert counter_path.read_text().strip() == str(expected)
    # No leftover temp files from the atomic write.
    temps = [
        p.name
        for p in state.iterdir()
        if ".XXXXXX" in p.name or p.name.startswith("antigravity_save_count_test-conv-001.")
    ]
    assert not temps, f"atomic counter write left temp files behind: {temps}"


def test_save_hook_helper_uses_temp_and_mv(tmp_path: Path) -> None:
    """mempal_write_counter_atomic must use a temp file + mv (not a bare redirect)."""
    body = COMMON_LIB.read_text(encoding="utf-8")
    assert "mempal_write_counter_atomic()" in body
    assert "mktemp" in body, "atomic counter helper should create a temp file via mktemp"
    assert "mv -f" in body, "atomic counter helper should promote the temp with mv -f"


def test_wake_hook_uses_sys_executable_module_invocation(tmp_path: Path) -> None:
    """The wake hook's inner Python must invoke mempalace via sys.executable -m.

    Same rationale as the save hook fix: the bare ``mempalace``
    console script fails when the venv's bin/ isn't on the hook's
    PATH. Using ``[sys.executable, '-m', 'mempalace', ...]`` binds
    the call to the same interpreter that resolved MEMPAL_PYTHON.
    """
    body = WAKE_HOOK.read_text(encoding="utf-8")
    assert "sys.executable, '-m', 'mempalace'" in body, (
        "wake hook should invoke mempalace via [sys.executable, '-m', 'mempalace', ...], "
        "not ['mempalace', ...]. The bare invocation breaks when the venv's bin/ "
        "isn't on the hook's PATH."
    )
    assert "['mempalace', 'wake-up'" not in body, (
        "bare ['mempalace', 'wake-up', ...] invocation found in wake hook"
    )


def test_save_hook_rejects_traversal_in_transcript_path(tmp_path: Path) -> None:
    """A `..` segment in transcriptPath must be rejected."""
    state = tmp_path / "state"
    home = tmp_path / "home"
    _ensure_palace(home)
    # Set interval to 1 so the modulo gate would normally fire on the
    # first Stop, then prove the path validator stops the spawn.
    result = _run_hook(
        SAVE_HOOK,
        _stop_payload(transcriptPath="/legit/../etc/passwd"),
        state_dir=state,
        home=home,
        extra_env={"MEMPAL_SAVE_INTERVAL": "1"},
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "{}"
    log = state / "antigravity_hook.log"
    assert log.is_file()
    log_body = log.read_text()
    assert "invalid transcriptPath rejected" in log_body or "does not exist" in log_body


def test_save_hook_rejects_non_jsonl_transcript_path(tmp_path: Path) -> None:
    """A transcriptPath ending in something other than .json[l] is rejected."""
    state = tmp_path / "state"
    home = tmp_path / "home"
    _ensure_palace(home)
    result = _run_hook(
        SAVE_HOOK,
        _stop_payload(transcriptPath="/tmp/transcript.txt"),
        state_dir=state,
        home=home,
        extra_env={"MEMPAL_SAVE_INTERVAL": "1"},
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "{}"


def test_save_hook_state_files_are_namespaced_antigravity(tmp_path: Path) -> None:
    """Every state file the save hook touches starts with `antigravity_`.

    The shared state directory is also home to Claude Code, Codex, and
    (in the future) Cursor hook state. Namespacing prevents collisions.
    """
    state = tmp_path / "state"
    home = tmp_path / "home"
    _ensure_palace(home)
    result = _run_hook(
        SAVE_HOOK,
        _stop_payload(),
        state_dir=state,
        home=home,
        extra_env={"MEMPAL_SAVE_INTERVAL": "999"},
    )
    assert result.returncode == 0
    leaks = [p.name for p in state.iterdir() if not p.name.startswith("antigravity_")]
    assert not leaks, f"save hook created non-antigravity-namespaced state files: {leaks}"


def test_save_hook_pending_marker_blocks_concurrent_save(tmp_path: Path) -> None:
    """A fresh pending marker should cause the next save to skip."""
    state = tmp_path / "state"
    home = tmp_path / "home"
    _ensure_palace(home)
    pending = state / "antigravity_pending_test-conv-001"
    state.mkdir(parents=True, exist_ok=True)
    pending.touch()
    # Force the modulo gate to fire by setting interval=1.
    result = _run_hook(
        SAVE_HOOK,
        _stop_payload(),
        state_dir=state,
        home=home,
        extra_env={"MEMPAL_SAVE_INTERVAL": "1"},
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "{}"
    log_body = (state / "antigravity_hook.log").read_text(errors="replace")
    assert "pending save still in flight" in log_body


# ── Wake hook ─────────────────────────────────────────────────────────


def test_wake_hook_emits_empty_object_on_kill_switch(tmp_path: Path) -> None:
    """MEMPAL_DISABLE_HOOK=1 silences the wake hook."""
    state = tmp_path / "state"
    home = tmp_path / "home"
    _ensure_palace(home)
    result = _run_hook(
        WAKE_HOOK,
        _wake_payload(),
        state_dir=state,
        home=home,
        extra_env={"MEMPAL_DISABLE_HOOK": "1"},
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "{}"


@pytest.mark.parametrize("invocation", [0, 2, 5, 100])
def test_wake_hook_emits_empty_when_invocation_num_not_one(tmp_path: Path, invocation: int) -> None:
    """Only invocationNum == 1 triggers injection."""
    state = tmp_path / "state"
    home = tmp_path / "home"
    _ensure_palace(home)
    result = _run_hook(
        WAKE_HOOK,
        _wake_payload(invocationNum=invocation),
        state_dir=state,
        home=home,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "{}", (
        f"wake hook injected at invocationNum={invocation}: {result.stdout!r}"
    )


def test_wake_hook_loop_guard_prevents_repeat_injection(tmp_path: Path) -> None:
    """A second fire for the same conversationId must skip via the mkdir guard."""
    state = tmp_path / "state"
    home = tmp_path / "home"
    _ensure_palace(home)
    # Pre-create the woke marker dir.
    woke = state / "antigravity_woke_test-conv-001"
    state.mkdir(parents=True, exist_ok=True)
    woke.mkdir()
    result = _run_hook(
        WAKE_HOOK,
        _wake_payload(),
        state_dir=state,
        home=home,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "{}"
    log_body = (state / "antigravity_hook.log").read_text(errors="replace")
    assert "already woke this conversation" in log_body


def test_wake_hook_never_emits_decision_field(tmp_path: Path) -> None:
    """The wake hook must never emit a `decision` key (that field is Stop-only)."""
    state = tmp_path / "state"
    home = tmp_path / "home"
    _ensure_palace(home)
    result = _run_hook(
        WAKE_HOOK,
        _wake_payload(),
        state_dir=state,
        home=home,
    )
    assert result.returncode == 0
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        pytest.fail(f"wake hook emitted non-JSON: {result.stdout!r}")
    assert "decision" not in payload, f"wake hook emitted a decision field: {payload!r}"


def test_wake_hook_emits_empty_when_mempalace_missing(tmp_path: Path) -> None:
    """When mempalace can't be run, the wake hook degrades to `{}`.

    Antigravity's hook framework should never see a stack trace from
    a missing CLI — emit `{}` and let the conversation start without
    injection.

    Note: the wake hook binds the wake-up call to
    ``[sys.executable, '-m', 'mempalace', ...]`` (the interpreter that
    resolved MEMPAL_PYTHON), NOT a bare ``mempalace`` on PATH. So we
    force MEMPAL_PYTHON="" (resolution falls back to ``python3`` on the
    minimal PATH below) and strip PATH to a system python3 that has no
    mempalace package installed. The inner ``python3 -m mempalace``
    then fails and the hook must emit `{}`. We also override
    MEMPAL_PYTHON explicitly so a value exported in the developer's
    shell can't leak in and point at an interpreter that *does* have
    mempalace.
    """
    state = tmp_path / "state"
    home = tmp_path / "home"
    _ensure_palace(home)
    # Strip PATH down to just the bash + python essentials, dropping
    # any directory that might have a `mempalace` binary, and clear
    # MEMPAL_PYTHON so resolution falls back to this minimal PATH.
    minimal_path = "/usr/bin:/bin"
    result = _run_hook(
        WAKE_HOOK,
        _wake_payload(),
        state_dir=state,
        home=home,
        extra_env={"PATH": minimal_path, "MEMPAL_PYTHON": ""},
    )
    assert result.returncode == 0, (
        f"wake hook crashed when mempalace is missing:\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    assert result.stdout.strip() == "{}"


def test_wake_hook_state_files_are_namespaced_antigravity(tmp_path: Path) -> None:
    """Wake hook state files are also namespaced."""
    state = tmp_path / "state"
    home = tmp_path / "home"
    _ensure_palace(home)
    result = _run_hook(WAKE_HOOK, _wake_payload(), state_dir=state, home=home)
    assert result.returncode == 0
    leaks = [p.name for p in state.iterdir() if not p.name.startswith("antigravity_")]
    assert not leaks, leaks


# ── Wing inference ────────────────────────────────────────────────────


def test_wing_inference_picks_first_workspace_path(tmp_path: Path) -> None:
    """Wing is derived from workspacePaths[0]'s leaf directory.

    Antigravity sends an array; the first element is canonical.
    """
    state = tmp_path / "state"
    home = tmp_path / "home"
    _ensure_palace(home)
    # Set interval=1 and a real existing transcript so the save path
    # logs the inferred wing.
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    workspace = tmp_path / "myproj-with-dashes"
    workspace.mkdir()
    result = _run_hook(
        SAVE_HOOK,
        _stop_payload(
            transcriptPath=str(transcript),
            workspacePaths=[str(workspace), "/some/other/workspace"],
        ),
        state_dir=state,
        home=home,
        extra_env={"MEMPAL_SAVE_INTERVAL": "1"},
    )
    assert result.returncode == 0
    log_body = (state / "antigravity_hook.log").read_text(errors="replace")
    # Hyphens become underscores; lowercase.
    assert "wing=wing_myproj_with_dashes" in log_body, log_body


def test_wing_inference_defaults_to_sessions_when_workspace_empty(tmp_path: Path) -> None:
    """An empty workspacePaths array yields wing_sessions."""
    state = tmp_path / "state"
    home = tmp_path / "home"
    _ensure_palace(home)
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    result = _run_hook(
        SAVE_HOOK,
        _stop_payload(
            transcriptPath=str(transcript),
            workspacePaths=[],
        ),
        state_dir=state,
        home=home,
        extra_env={"MEMPAL_SAVE_INTERVAL": "1"},
    )
    assert result.returncode == 0
    log_body = (state / "antigravity_hook.log").read_text(errors="replace")
    assert "wing=wing_sessions" in log_body


# ── Performance budget (soft) ─────────────────────────────────────────


@pytest.mark.skipif(sys.platform == "win32", reason="not relevant on Windows code path")
def test_save_hook_returns_quickly_under_kill_switch(tmp_path: Path) -> None:
    """Under the kill switch the hook should return well under 1s.

    The integration brief budgets hooks at <500ms. We allow a generous
    1500ms here because CI machines can be slow on cold-cache subprocess
    spawn. The point of the test is to fail loudly if a future edit
    introduces a synchronous mempalace import or DB connection.
    """
    import time

    state = tmp_path / "state"
    home = tmp_path / "home"
    _ensure_palace(home)
    start = time.monotonic()
    result = _run_hook(
        SAVE_HOOK,
        _stop_payload(),
        state_dir=state,
        home=home,
        extra_env={"MEMPAL_DISABLE_HOOK": "1"},
    )
    elapsed = time.monotonic() - start
    assert result.returncode == 0
    assert result.stdout.strip() == "{}"
    assert elapsed < 1.5, (
        f"save hook under kill switch took {elapsed:.3f}s; expected < 1.5s. "
        "A regression here usually means a synchronous import / DB connection "
        "is happening before the kill-switch short-circuit."
    )


# ── State-file GC (PR #1633 hygiene) ──────────────────────────────────


def _backdate(path: Path, days: int) -> None:
    """Set a path's atime/mtime ``days`` days into the past."""
    import time

    past = time.time() - days * 86400
    os.utime(path, (past, past))


def test_gc_removes_stale_state_files(tmp_path: Path) -> None:
    """Stale per-conversation state (older than the TTL) is swept.

    igorls' PR #1633 review flagged unbounded growth of
    antigravity_save_count_*, antigravity_pending_*, and
    antigravity_woke_* artifacts. mempal_gc_stale_state removes those
    older than MEMPAL_STATE_TTL_DAYS (default 30).
    """
    state = tmp_path / "state"
    home = tmp_path / "home"
    _ensure_palace(home)
    state.mkdir(parents=True, exist_ok=True)

    # Stale artifacts (40 days old) — all three shapes.
    stale_count = state / "antigravity_save_count_old-conv"
    stale_pending = state / "antigravity_pending_old-conv"
    stale_woke = state / "antigravity_woke_old-conv"
    stale_count.write_text("7", encoding="utf-8")
    stale_pending.write_text("", encoding="utf-8")
    stale_woke.mkdir()
    for p in (stale_count, stale_pending, stale_woke):
        _backdate(p, 40)

    # Fresh artifacts must survive.
    fresh_count = state / "antigravity_save_count_new-conv"
    fresh_count.write_text("1", encoding="utf-8")

    # Protected files must never be touched even when stale.
    log = state / "antigravity_hook.log"
    log.write_text("log line\n", encoding="utf-8")
    _backdate(log, 99)

    cmd = f". {COMMON_LIB}; mempal_gc_stale_state"
    result = subprocess.run(
        ["bash", "-c", cmd],
        capture_output=True,
        text=True,
        env={**os.environ, "HOME": str(home), "MEMPAL_STATE_DIR": str(state)},
        timeout=10,
    )
    assert result.returncode == 0, result.stderr

    assert not stale_count.exists(), "stale counter file not swept"
    assert not stale_pending.exists(), "stale pending marker not swept"
    assert not stale_woke.exists(), "stale woke marker dir not swept"
    assert fresh_count.exists(), "fresh counter file was wrongly swept"
    assert log.exists(), "protected hook.log was swept (name glob too broad)"


def test_gc_is_throttled_to_once_per_day(tmp_path: Path) -> None:
    """A fresh antigravity_last_sweep marker (<24h) skips the sweep."""
    state = tmp_path / "state"
    home = tmp_path / "home"
    _ensure_palace(home)
    state.mkdir(parents=True, exist_ok=True)

    # Fresh sweep marker — GC should bail before touching anything.
    marker = state / "antigravity_last_sweep"
    marker.write_text("", encoding="utf-8")

    stale_count = state / "antigravity_save_count_old-conv"
    stale_count.write_text("7", encoding="utf-8")
    _backdate(stale_count, 40)

    cmd = f". {COMMON_LIB}; mempal_gc_stale_state"
    result = subprocess.run(
        ["bash", "-c", cmd],
        capture_output=True,
        text=True,
        env={**os.environ, "HOME": str(home), "MEMPAL_STATE_DIR": str(state)},
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    # Throttled: stale file should still be present.
    assert stale_count.exists(), (
        "GC ran despite a fresh antigravity_last_sweep marker; throttle failed"
    )


def test_gc_runs_when_marker_is_stale(tmp_path: Path) -> None:
    """A stale antigravity_last_sweep marker (>24h) lets the sweep run."""
    state = tmp_path / "state"
    home = tmp_path / "home"
    _ensure_palace(home)
    state.mkdir(parents=True, exist_ok=True)

    marker = state / "antigravity_last_sweep"
    marker.write_text("", encoding="utf-8")
    _backdate(marker, 2)  # 2 days old -> stale

    stale_count = state / "antigravity_save_count_old-conv"
    stale_count.write_text("7", encoding="utf-8")
    _backdate(stale_count, 40)

    cmd = f". {COMMON_LIB}; mempal_gc_stale_state"
    result = subprocess.run(
        ["bash", "-c", cmd],
        capture_output=True,
        text=True,
        env={**os.environ, "HOME": str(home), "MEMPAL_STATE_DIR": str(state)},
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert not stale_count.exists(), "stale counter not swept despite stale marker"


def test_state_ttl_days_floors_and_strips(tmp_path: Path) -> None:
    """mempal_state_ttl_days validates input like mempal_save_interval."""
    home = tmp_path / "home"
    _ensure_palace(home)

    def ttl(value: str | None) -> str:
        env = {**os.environ, "HOME": str(home)}
        if value is None:
            env.pop("MEMPAL_STATE_TTL_DAYS", None)
        else:
            env["MEMPAL_STATE_TTL_DAYS"] = value
        out = subprocess.run(
            ["bash", "-c", f". {COMMON_LIB}; mempal_state_ttl_days"],
            capture_output=True,
            text=True,
            env=env,
            timeout=10,
        )
        return out.stdout.strip()

    assert ttl(None) == "30", "default TTL should be 30"
    assert ttl("") == "30", "empty TTL falls back to 30"
    assert ttl("abc") == "30", "garbage TTL falls back to 30"
    assert ttl("7") == "7"
    # Leading zeros must be stripped so `find -mtime +N` never sees octal-ish tokens.
    assert ttl("007") == "7"
    assert ttl("0") == "0"


def test_save_hook_calls_gc(tmp_path: Path) -> None:
    """The save hook wires mempal_gc_stale_state in and creates the sweep marker."""
    body = SAVE_HOOK.read_text(encoding="utf-8")
    assert "mempal_gc_stale_state" in body, "save hook must call mempal_gc_stale_state"

    state = tmp_path / "state"
    home = tmp_path / "home"
    _ensure_palace(home)
    result = _run_hook(
        SAVE_HOOK,
        _stop_payload(),
        state_dir=state,
        home=home,
        extra_env={"MEMPAL_SAVE_INTERVAL": "999"},
    )
    assert result.returncode == 0
    assert (state / "antigravity_last_sweep").exists(), (
        "save hook should have created the antigravity_last_sweep throttle marker"
    )


def test_gc_does_not_run_under_kill_switch(tmp_path: Path) -> None:
    """When the kill switch trips, the save hook returns before GC runs."""
    state = tmp_path / "state"
    home = tmp_path / "home"
    _ensure_palace(home)
    result = _run_hook(
        SAVE_HOOK,
        _stop_payload(),
        state_dir=state,
        home=home,
        extra_env={"MEMPAL_DISABLE_HOOK": "1"},
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "{}"
    # GC runs after the kill-switch check, so no sweep marker is written.
    assert not (state / "antigravity_last_sweep").exists(), (
        "GC ran despite the kill switch being tripped"
    )


# ── Python interpreter resolution (mempal_resolve_python) ─────────────
#
# The hooks run `"$MEMPAL_PYTHON_BIN" -m mempalace`, so MEMPAL_PYTHON_BIN
# must resolve to an interpreter that owns the mempalace package. The
# common install path — `uv tool install mempalace` / `pipx install` —
# puts the console scripts on PATH inside an ISOLATED env whose
# interpreter is NOT system python3. The resolver derives that
# interpreter from the console-script shebang so mining works without
# the user having to set MEMPAL_PYTHON. Regression coverage for the
# silent-skip bug a real user hit on PR #1633.


def _resolve_python(env: dict[str, str]) -> str:
    """Source common.sh under ``env`` and return the resolved MEMPAL_PYTHON_BIN."""
    out = subprocess.run(
        ["bash", "-c", f'. {COMMON_LIB}; printf "%s" "$MEMPAL_PYTHON_BIN"'],
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def _make_fake_python(path: Path) -> Path:
    """Create an executable file whose basename looks like a Python interpreter."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\necho fake-python\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _make_console_script(path: Path, shebang_interp: str) -> Path:
    """Create a fake mempalace console script with the given shebang interpreter."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!{shebang_interp}\nprint('hi')\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def test_resolve_python_derives_interpreter_from_console_script_shebang(
    tmp_path: Path,
) -> None:
    """With MEMPAL_PYTHON unset, the resolver reads the mempalace-mcp shebang.

    Simulates a `uv tool install` layout: the console script is on PATH
    but its interpreter is an isolated Python, NOT the system python3
    that PATH would otherwise resolve.
    """
    home = tmp_path / "home"
    _ensure_palace(home)
    interp = _make_fake_python(tmp_path / "pyhome" / "python3.12")
    bindir = tmp_path / "bin"
    _make_console_script(bindir / "mempalace-mcp", str(interp))

    env = {**os.environ, "HOME": str(home), "PATH": f"{bindir}:/usr/bin:/bin"}
    env.pop("MEMPAL_PYTHON", None)

    assert _resolve_python(env) == str(interp), (
        "resolver should derive the interpreter from the mempalace-mcp "
        "console-script shebang when MEMPAL_PYTHON is unset"
    )


def test_resolve_python_prefers_mcp_script_over_path_python3(tmp_path: Path) -> None:
    """The shebang-derived interpreter must win over a system python3 on PATH.

    This is the crux of the fix: a system python3 is present (and would
    be picked by the old resolver) but lacks the package, while the
    console script's interpreter owns it.
    """
    home = tmp_path / "home"
    _ensure_palace(home)
    interp = _make_fake_python(tmp_path / "pyhome" / "python3.12")
    bindir = tmp_path / "bin"
    _make_console_script(bindir / "mempalace-mcp", str(interp))
    # A decoy python3 earlier on PATH must be ignored in favour of the shebang.
    _make_fake_python(bindir / "python3")

    env = {**os.environ, "HOME": str(home), "PATH": f"{bindir}:/usr/bin:/bin"}
    env.pop("MEMPAL_PYTHON", None)

    assert _resolve_python(env) == str(interp), (
        "shebang-derived interpreter must take precedence over a python3 on PATH"
    )


def test_resolve_python_override_beats_shebang(tmp_path: Path) -> None:
    """An explicit MEMPAL_PYTHON override always wins over shebang derivation."""
    home = tmp_path / "home"
    _ensure_palace(home)
    override = _make_fake_python(tmp_path / "override" / "python3")
    interp = _make_fake_python(tmp_path / "pyhome" / "python3.12")
    bindir = tmp_path / "bin"
    _make_console_script(bindir / "mempalace-mcp", str(interp))

    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{bindir}:/usr/bin:/bin",
        "MEMPAL_PYTHON": str(override),
    }
    assert _resolve_python(env) == str(override), (
        "MEMPAL_PYTHON override must take precedence over the console-script shebang"
    )


def test_resolve_python_rejects_env_style_shebang(tmp_path: Path) -> None:
    """A `#!/usr/bin/env python3` wrapper must be skipped, not used verbatim.

    The first shebang token would be `/usr/bin/env`, which is not a
    Python interpreter. The resolver must reject it (basename guard) and
    fall through to python3 on PATH rather than trying to run
    `/usr/bin/env -m mempalace`.
    """
    home = tmp_path / "home"
    _ensure_palace(home)
    bindir = tmp_path / "bin"
    _make_console_script(bindir / "mempalace-mcp", "/usr/bin/env python3")

    env = {**os.environ, "HOME": str(home), "PATH": f"{bindir}:/usr/bin:/bin"}
    env.pop("MEMPAL_PYTHON", None)

    resolved = _resolve_python(env)
    assert resolved != "/usr/bin/env", "resolver must not return /usr/bin/env"
    assert os.path.basename(resolved).startswith("python"), (
        f"resolver should fall back to a python3 on PATH; got {resolved!r}"
    )


def test_resolve_python_skips_shebang_interp_that_is_not_executable(
    tmp_path: Path,
) -> None:
    """A shebang pointing at a missing/non-executable interpreter is skipped.

    Guards against a stale console script whose interpreter was deleted:
    the resolver must fall through to python3 rather than returning a
    dead path.
    """
    home = tmp_path / "home"
    _ensure_palace(home)
    bindir = tmp_path / "bin"
    missing = tmp_path / "pyhome" / "python3.12"  # never created -> not -x
    _make_console_script(bindir / "mempalace-mcp", str(missing))

    env = {**os.environ, "HOME": str(home), "PATH": f"{bindir}:/usr/bin:/bin"}
    env.pop("MEMPAL_PYTHON", None)

    resolved = _resolve_python(env)
    assert resolved != str(missing), "resolver returned a non-executable shebang interp"
    assert os.path.basename(resolved).startswith("python"), (
        f"resolver should fall back to python3 on PATH; got {resolved!r}"
    )


def test_resolve_python_falls_back_to_path_python3_without_console_scripts(
    tmp_path: Path,
) -> None:
    """With no mempalace console scripts on PATH, resolve to python3 (prior behaviour)."""
    home = tmp_path / "home"
    _ensure_palace(home)

    env = {**os.environ, "HOME": str(home), "PATH": "/usr/bin:/bin"}
    env.pop("MEMPAL_PYTHON", None)

    resolved = _resolve_python(env)
    assert os.path.basename(resolved).startswith("python"), (
        f"resolver should fall back to python3 on PATH; got {resolved!r}"
    )
