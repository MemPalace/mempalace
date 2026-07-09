from __future__ import annotations

import os

import pytest

from mempalace import palace


def _redirect_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))


def test_mine_palace_lock_retries_transient_holder_when_wait_is_enabled(
    monkeypatch,
    tmp_path,
):
    _redirect_home(monkeypatch, tmp_path)
    attempts: list[str] = []

    def fake_try_lock(lock_file):
        attempts.append(lock_file.name)
        return len(attempts) >= 2

    monkeypatch.setattr(palace, "_try_lock_palace_lock_file", fake_try_lock)
    monkeypatch.setattr(palace.time, "sleep", lambda _seconds: None)

    with palace.mine_palace_lock(
        str(tmp_path / "palace"),
        wait_seconds=1,
        poll_seconds=0.01,
    ):
        pass

    assert len(attempts) == 2


def test_mine_palace_lock_env_can_enable_bounded_wait(monkeypatch, tmp_path):
    _redirect_home(monkeypatch, tmp_path)
    attempts: list[str] = []

    def fake_try_lock(lock_file):
        attempts.append(lock_file.name)
        return len(attempts) >= 2

    monkeypatch.setattr(palace, "_try_lock_palace_lock_file", fake_try_lock)
    monkeypatch.setattr(palace.time, "sleep", lambda _seconds: None)
    monkeypatch.setenv("MEMPALACE_MINE_PALACE_LOCK_WAIT_SECONDS", "1")
    monkeypatch.setenv("MEMPALACE_MINE_PALACE_LOCK_POLL_SECONDS", "0.01")

    with palace.mine_palace_lock(str(tmp_path / "palace")):
        pass

    assert len(attempts) == 2


def test_mine_palace_lock_default_remains_fail_fast(monkeypatch, tmp_path):
    _redirect_home(monkeypatch, tmp_path)
    attempts: list[str] = []

    def fake_try_lock(lock_file):
        attempts.append(lock_file.name)
        return False

    monkeypatch.setattr(palace, "_try_lock_palace_lock_file", fake_try_lock)
    monkeypatch.setattr(
        palace,
        "_read_lock_holder",
        lambda _lock_file: "PID 12345 synthetic-holder",
    )

    with pytest.raises(palace.MineAlreadyRunning, match="wait for it to finish"):
        with palace.mine_palace_lock(str(tmp_path / "palace")):
            pass

    assert len(attempts) == 1


def test_mine_palace_lock_times_out_for_alive_holder(monkeypatch, tmp_path):
    _redirect_home(monkeypatch, tmp_path)
    attempts: list[str] = []
    monotonic_values = iter([0.0, 0.02])

    def fake_try_lock(lock_file):
        attempts.append(lock_file.name)
        return False

    monkeypatch.setattr(palace, "_try_lock_palace_lock_file", fake_try_lock)
    monkeypatch.setattr(
        palace,
        "_read_lock_holder",
        lambda _lock_file: f"PID {os.getpid()} synthetic-holder",
    )
    monkeypatch.setattr(palace.time, "monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr(palace.time, "sleep", lambda _seconds: None)

    with pytest.raises(palace.MineAlreadyRunning, match="timed out"):
        with palace.mine_palace_lock(
            str(tmp_path / "palace"),
            wait_seconds=0.01,
            poll_seconds=0.01,
        ):
            pass

    assert len(attempts) == 1


def test_dead_holder_detection_is_conservative(monkeypatch):
    monkeypatch.setattr(palace, "_process_is_alive", lambda pid: False)

    assert palace._holder_pid_is_dead("PID 98765432 (dead writer)") is True
    assert palace._holder_pid_is_dead("another writer (identity not recorded)") is False
    assert palace._holder_pid_is_dead(f"PID {os.getpid()} (self)") is False
