"""Tests for the 'hooks active but daemon off' status warning.

The warning surfaces the at-risk topology from epic #1963: stop-hooks are
actively saving but the daemon isn't running, so concurrent hook saves
fail-fast on ``mine_palace_lock`` and the repeated concurrent-open pressure
against ChromaDB's single-writer HNSW segment contributes to recurring
divergence. ``mempalace status`` is a common command — surfacing the state
there is the cheapest adoption nudge.
"""

import os
import time
from unittest.mock import patch

from mempalace.cli import _warn_if_hooks_active_daemon_off


def _seed_recent_hook_log(tmp_path):
    """Stand up a ~/.mempalace/hook_state/hook.log that looks recently active."""
    state_dir = tmp_path / ".mempalace" / "hook_state"
    state_dir.mkdir(parents=True)
    (state_dir / "hook.log").write_text("[recent hook activity]\n")


def test_no_warning_when_daemon_running(tmp_path, capsys, monkeypatch):
    """Daemon up → hook saves are serialized → no warning."""
    monkeypatch.setenv("HOME", str(tmp_path))
    _seed_recent_hook_log(tmp_path)

    with patch("mempalace.daemon.get_client_if_running", return_value=object()):
        _warn_if_hooks_active_daemon_off(str(tmp_path / "palace"))

    captured = capsys.readouterr()
    assert "daemon is not running" not in captured.out


def test_warning_when_hooks_active_daemon_off(tmp_path, capsys, monkeypatch):
    """Hooks active (recent hook.log) + daemon off → warn + actionable hint."""
    monkeypatch.setenv("HOME", str(tmp_path))
    _seed_recent_hook_log(tmp_path)

    with patch("mempalace.daemon.get_client_if_running", return_value=None):
        _warn_if_hooks_active_daemon_off(str(tmp_path / "palace"))

    captured = capsys.readouterr()
    assert "daemon is not running" in captured.out
    assert "mempalace daemon start" in captured.out


def test_no_warning_when_hooks_never_ran(tmp_path, capsys, monkeypatch):
    """No hook.log at all → hooks never active here → nothing to warn about."""
    monkeypatch.setenv("HOME", str(tmp_path))
    # Intentionally do NOT create ~/.mempalace/hook_state/hook.log.

    with patch("mempalace.daemon.get_client_if_running", return_value=None):
        _warn_if_hooks_active_daemon_off(str(tmp_path / "palace"))

    captured = capsys.readouterr()
    assert "daemon is not running" not in captured.out


def test_no_warning_when_hook_activity_stale(tmp_path, capsys, monkeypatch):
    """hook.log older than the 7-day window → hooks effectively inactive → no warning."""
    monkeypatch.setenv("HOME", str(tmp_path))
    _seed_recent_hook_log(tmp_path)
    log = tmp_path / ".mempalace" / "hook_state" / "hook.log"
    old = time.time() - (30 * 24 * 3600)  # 30 days ago
    os.utime(log, (old, old))

    with patch("mempalace.daemon.get_client_if_running", return_value=None):
        _warn_if_hooks_active_daemon_off(str(tmp_path / "palace"))

    captured = capsys.readouterr()
    assert "daemon is not running" not in captured.out


def test_no_warning_when_daemon_probe_raises(tmp_path, capsys, monkeypatch):
    """Best-effort contract: a daemon probe that raises must not break status."""

    def boom(*_args, **_kwargs):
        raise RuntimeError("probe failed")

    monkeypatch.setenv("HOME", str(tmp_path))
    _seed_recent_hook_log(tmp_path)

    with patch("mempalace.daemon.get_client_if_running", side_effect=boom):
        _warn_if_hooks_active_daemon_off(str(tmp_path / "palace"))

    captured = capsys.readouterr()
    assert "daemon is not running" not in captured.out
