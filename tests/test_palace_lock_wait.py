from __future__ import annotations

import os

import pytest

from mempalace import palace


def _redirect_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))


def test_mine_palace_lock_retries_transient_holder(monkeypatch, tmp_path):
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


def test_mine_palace_lock_times_out_for_alive_holder(monkeypatch, tmp_path):
    _redirect_home(monkeypatch, tmp_path)
    palace_dir = tmp_path / "palace"
    palace_dir.mkdir()

    _resolved, _key, lock_path = palace._palace_lock_parts(str(palace_dir))
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(fd)

    leaked = open(lock_path, "r+b")
    try:
        assert palace._try_lock_palace_lock_file(leaked)
        ident = f"{os.getpid()} synthetic-holder".encode("utf-8")
        leaked.seek(palace._LOCK_SENTINEL_BYTES)
        leaked.truncate(palace._LOCK_SENTINEL_BYTES + len(ident))
        leaked.write(ident)
        leaked.flush()

        monkeypatch.setenv("MEMPALACE_MINE_PALACE_LOCK_WAIT_SECONDS", "0")

        with pytest.raises(palace.MineAlreadyRunning, match="timed out"):
            with palace.mine_palace_lock(str(palace_dir)):
                pass
    finally:
        try:
            palace._unlock_palace_lock_file(leaked)
        except Exception:
            pass
        leaked.close()


def test_dead_holder_detection_is_conservative(monkeypatch):
    monkeypatch.setattr(palace, "_process_is_alive", lambda pid: False)

    assert palace._holder_pid_is_dead("PID 98765432 (dead writer)") is True
    assert palace._holder_pid_is_dead("another writer (identity not recorded)") is False
    assert palace._holder_pid_is_dead(f"PID {os.getpid()} (self)") is False
