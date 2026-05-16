"""Tests for the mine-cascade circuit breaker and Windows commit guard
introduced in fix for #1518.
"""

import json
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

import mempalace.hooks_cli as hooks_cli_mod
from mempalace.hooks_cli import (
    _COOLDOWN_LADDER_SEC,
    _commit_pressure_high,
    _in_cooldown,
    _read_failure_state,
    _reap_finished_mines,
    _record_failure,
    _record_success,
    _success_threshold_sec,
)


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    """Point STATE_DIR and friends at a tmp_path so tests don't poison
    the developer's real ``~/.mempalace/hook_state``."""
    state_dir = tmp_path / "hook_state"
    pid_dir = state_dir / "mine_pids"
    fail_file = state_dir / "mine.failures.json"
    monkeypatch.setattr(hooks_cli_mod, "STATE_DIR", state_dir)
    monkeypatch.setattr(hooks_cli_mod, "_MINE_PID_DIR", pid_dir)
    monkeypatch.setattr(hooks_cli_mod, "_MINE_FAIL_FILE", fail_file)
    state_dir.mkdir(parents=True, exist_ok=True)
    pid_dir.mkdir(parents=True, exist_ok=True)
    yield state_dir


# --- failure state read/write ---


def test_read_failure_state_default_when_missing(isolated_state):
    state = _read_failure_state()
    assert state == {"consecutive": 0, "cooldown_until": 0}


def test_read_failure_state_default_when_corrupt(isolated_state):
    hooks_cli_mod._MINE_FAIL_FILE.write_text("{not json")
    state = _read_failure_state()
    assert state == {"consecutive": 0, "cooldown_until": 0}


def test_read_failure_state_default_when_not_dict(isolated_state):
    hooks_cli_mod._MINE_FAIL_FILE.write_text(json.dumps(["unexpected"]))
    state = _read_failure_state()
    assert state == {"consecutive": 0, "cooldown_until": 0}


# --- record_failure increments and ladders ---


def test_record_failure_increments_and_writes_cooldown(isolated_state):
    before = time.time()
    _record_failure()
    state = _read_failure_state()
    assert state["consecutive"] == 1
    assert state["cooldown_until"] >= before + _COOLDOWN_LADDER_SEC[0] - 1


def test_record_failure_climbs_ladder(isolated_state):
    for expected_step in (1, 2, 3):
        _record_failure()
        state = _read_failure_state()
        assert state["consecutive"] == expected_step


def test_record_failure_caps_at_ladder_length(isolated_state):
    for _ in range(len(_COOLDOWN_LADDER_SEC) + 4):
        _record_failure()
    state = _read_failure_state()
    assert state["consecutive"] == len(_COOLDOWN_LADDER_SEC)


# --- record_success resets ---


def test_record_success_removes_failure_file(isolated_state):
    _record_failure()
    assert hooks_cli_mod._MINE_FAIL_FILE.exists()
    _record_success()
    assert not hooks_cli_mod._MINE_FAIL_FILE.exists()


def test_record_success_noop_when_file_missing(isolated_state):
    # Must not raise even if no failures were ever recorded.
    _record_success()


# --- cooldown gating ---


def test_in_cooldown_false_when_no_failures(isolated_state):
    assert _in_cooldown() is False


def test_in_cooldown_true_immediately_after_failure(isolated_state):
    _record_failure()
    assert _in_cooldown() is True


def test_in_cooldown_false_after_window_expires(isolated_state, monkeypatch):
    _record_failure()
    state = _read_failure_state()
    fake_now = state["cooldown_until"] + 1
    monkeypatch.setattr(hooks_cli_mod.time, "time", lambda: fake_now)
    assert _in_cooldown() is False


# --- _reap_finished_mines: PID-classification ---


def _write_slot(pid_dir: Path, name: str, pid: int, age_seconds: float):
    slot = pid_dir / f"mine_{name}.pid"
    slot.write_text(str(pid))
    target_mtime = time.time() - age_seconds
    os.utime(slot, (target_mtime, target_mtime))
    return slot


def test_reap_skips_live_pids(isolated_state):
    pid_dir = hooks_cli_mod._MINE_PID_DIR
    slot = _write_slot(pid_dir, "live", pid=12345, age_seconds=5)
    with patch.object(hooks_cli_mod, "_pid_alive", return_value=True):
        _reap_finished_mines()
    assert slot.exists()
    assert _read_failure_state()["consecutive"] == 0


def test_reap_records_failure_when_dead_and_fast(isolated_state):
    pid_dir = hooks_cli_mod._MINE_PID_DIR
    slot = _write_slot(pid_dir, "fast", pid=99999, age_seconds=2)
    with patch.object(hooks_cli_mod, "_pid_alive", return_value=False):
        _reap_finished_mines()
    assert not slot.exists()
    assert _read_failure_state()["consecutive"] == 1


def test_reap_records_success_when_dead_and_long_running(isolated_state):
    pid_dir = hooks_cli_mod._MINE_PID_DIR
    _record_failure()
    assert hooks_cli_mod._MINE_FAIL_FILE.exists()
    slot = _write_slot(pid_dir, "slow", pid=88888, age_seconds=_success_threshold_sec() + 30)
    with patch.object(hooks_cli_mod, "_pid_alive", return_value=False):
        _reap_finished_mines()
    assert not slot.exists()
    assert not hooks_cli_mod._MINE_FAIL_FILE.exists()


def test_reap_ignores_non_numeric_slot(isolated_state):
    pid_dir = hooks_cli_mod._MINE_PID_DIR
    slot = pid_dir / "mine_garbage.pid"
    slot.write_text("not-a-pid")
    with patch.object(hooks_cli_mod, "_pid_alive", return_value=False):
        _reap_finished_mines()
    assert slot.exists()
    assert _read_failure_state()["consecutive"] == 0


def test_reap_handles_missing_pid_dir(isolated_state):
    # Even if the dir vanished mid-flight, reaping must not raise.
    pid_dir = hooks_cli_mod._MINE_PID_DIR
    for child in pid_dir.iterdir():
        child.unlink()
    pid_dir.rmdir()
    _reap_finished_mines()


# --- success threshold tunable via env ---


def test_success_threshold_uses_env(monkeypatch):
    monkeypatch.setenv("MEMPALACE_HOOK_SUCCESS_THRESHOLD_SEC", "120")
    assert _success_threshold_sec() == 120.0


def test_success_threshold_default_on_bad_env(monkeypatch):
    monkeypatch.setenv("MEMPALACE_HOOK_SUCCESS_THRESHOLD_SEC", "not-a-number")
    assert _success_threshold_sec() == 30.0


# --- Windows commit-charge guard ---


def test_commit_pressure_false_on_non_windows(monkeypatch):
    monkeypatch.setattr(hooks_cli_mod.sys, "platform", "linux")
    assert _commit_pressure_high() is False


def test_commit_pressure_respects_disable_env(monkeypatch):
    monkeypatch.setattr(hooks_cli_mod.sys, "platform", "win32")
    monkeypatch.setenv("MEMPALACE_HOOK_DISABLE_COMMIT_GUARD", "1")
    assert _commit_pressure_high() is False


# --- _maybe_auto_ingest + _mine_sync honor the gates ---


def test_maybe_auto_ingest_short_circuits_in_cooldown(isolated_state, monkeypatch):
    monkeypatch.setenv("MEMPAL_DIR", str(isolated_state))  # any non-empty target
    monkeypatch.setattr(
        hooks_cli_mod, "_get_mine_targets", lambda: [(str(isolated_state), "projects")]
    )
    _record_failure()  # forces cooldown
    spawned = []
    monkeypatch.setattr(hooks_cli_mod, "_spawn_mine", lambda cmd: spawned.append(cmd))
    hooks_cli_mod._maybe_auto_ingest()
    assert spawned == []


def test_maybe_auto_ingest_runs_when_no_failures(isolated_state, monkeypatch):
    monkeypatch.setattr(
        hooks_cli_mod, "_get_mine_targets", lambda: [(str(isolated_state), "projects")]
    )
    monkeypatch.setattr(hooks_cli_mod, "_commit_pressure_high", lambda: False)
    spawned = []
    monkeypatch.setattr(hooks_cli_mod, "_spawn_mine", lambda cmd: spawned.append(cmd))
    hooks_cli_mod._maybe_auto_ingest()
    assert len(spawned) == 1


def test_maybe_auto_ingest_short_circuits_on_commit_pressure(isolated_state, monkeypatch):
    monkeypatch.setattr(
        hooks_cli_mod, "_get_mine_targets", lambda: [(str(isolated_state), "projects")]
    )
    monkeypatch.setattr(hooks_cli_mod, "_commit_pressure_high", lambda: True)
    spawned = []
    monkeypatch.setattr(hooks_cli_mod, "_spawn_mine", lambda cmd: spawned.append(cmd))
    hooks_cli_mod._maybe_auto_ingest()
    assert spawned == []


def test_mine_sync_short_circuits_in_cooldown(isolated_state, monkeypatch):
    monkeypatch.setattr(
        hooks_cli_mod, "_get_mine_targets", lambda: [(str(isolated_state), "projects")]
    )
    _record_failure()
    ran = []
    monkeypatch.setattr(hooks_cli_mod.subprocess, "run", lambda *a, **kw: ran.append((a, kw)))
    hooks_cli_mod._mine_sync()
    assert ran == []


def test_mine_sync_short_circuits_on_commit_pressure(isolated_state, monkeypatch):
    monkeypatch.setattr(
        hooks_cli_mod, "_get_mine_targets", lambda: [(str(isolated_state), "projects")]
    )
    monkeypatch.setattr(hooks_cli_mod, "_commit_pressure_high", lambda: True)
    ran = []
    monkeypatch.setattr(hooks_cli_mod.subprocess, "run", lambda *a, **kw: ran.append((a, kw)))
    hooks_cli_mod._mine_sync()
    assert ran == []
