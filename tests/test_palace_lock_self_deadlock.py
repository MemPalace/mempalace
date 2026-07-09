from __future__ import annotations

import os

import pytest

from mempalace import palace


def _redirect_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))


def _palace_lock_path(tmp_path):
    lock_dir = tmp_path / ".mempalace" / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)

    resolved = os.path.realpath(os.path.expanduser(str(tmp_path / "palace")))
    key = palace.hashlib.sha256(os.path.normcase(resolved).encode()).hexdigest()[:16]
    return lock_dir / f"mine_palace_{key}.lock"


def test_holder_pid_parser_handles_current_pid():
    assert palace._holder_pid_from_message("PID 12345 (mempalace)") == 12345
    assert palace._holder_pid_from_message("another writer") is None


@pytest.mark.skipif(
    os.name == "nt",
    reason="Windows cannot reliably unlink an open byte-locked file",
)
def test_self_deadlocked_palace_lock_unlinks_and_retries(monkeypatch, tmp_path):
    _redirect_home(monkeypatch, tmp_path)
    palace_dir = tmp_path / "palace"
    palace_dir.mkdir()

    lock_path = _palace_lock_path(tmp_path)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(fd)

    leaked = open(lock_path, "r+b")
    try:
        assert palace._lock_mine_lock_file(leaked, blocking=False)

        ident = f"{os.getpid()} leaked-mcp-server".encode("utf-8")
        leaked.seek(palace._LOCK_SENTINEL_BYTES)
        leaked.truncate(palace._LOCK_SENTINEL_BYTES + len(ident))
        leaked.write(ident)
        leaked.flush()

        entered = False
        with palace.mine_palace_lock(str(palace_dir)):
            entered = True

        assert entered is True
        assert lock_path.exists()
    finally:
        try:
            palace._unlock_mine_lock_file(leaked)
        except Exception:
            pass
        leaked.close()


def test_self_deadlock_recovery_never_clears_other_pid(tmp_path):
    lock_path = tmp_path / "mine_palace.lock"
    lock_path.write_text("lock", encoding="utf-8")

    assert (
        palace._recover_self_deadlocked_palace_lock(
            str(lock_path),
            "PID 999999 (other writer)",
        )
        is False
    )
    assert lock_path.exists()
