"""Detached loopback hub bootstrap for stdio --ensure-hub."""

from __future__ import annotations

import subprocess

import pytest

from mempalace import hub_bootstrap


class _FakeProc:
    def poll(self):
        return None

    returncode = None


class TestWantsEnsureHub:
    def test_flag(self):
        assert hub_bootstrap.wants_ensure_hub(["--ensure-hub"]) is True
        assert hub_bootstrap.wants_ensure_hub([]) is False

    def test_env(self, monkeypatch):
        monkeypatch.setenv(hub_bootstrap.ENSURE_HUB_ENV, "1")
        assert hub_bootstrap.wants_ensure_hub([]) is True
        monkeypatch.setenv(hub_bootstrap.ENSURE_HUB_ENV, "off")
        assert hub_bootstrap.wants_ensure_hub([]) is False


class TestBackendGate:
    def test_pgvector_skips_hub(self):
        assert hub_bootstrap._backend_needs_single_writer_hub("pgvector") is False

    def test_chroma_needs_hub(self):
        assert hub_bootstrap._backend_needs_single_writer_hub("chroma") is True

    def test_blank_defaults_to_chroma(self, monkeypatch):
        monkeypatch.delenv("MEMPALACE_BACKEND", raising=False)
        assert hub_bootstrap._backend_needs_single_writer_hub(None) is True


class TestEnsureHub:
    def test_empty_palace_is_false(self):
        assert hub_bootstrap.ensure_hub("") is False
        assert hub_bootstrap.ensure_hub(None) is False

    def test_alive_hub_short_circuits(self, tmp_path, monkeypatch):
        palace = str(tmp_path / "palace")
        monkeypatch.setattr(hub_bootstrap, "_hub_ready", lambda path: path == palace)
        monkeypatch.setattr(
            hub_bootstrap,
            "_spawn_hub",
            lambda *a, **k: pytest.fail("must not spawn when a hub is already live"),
        )
        assert hub_bootstrap.ensure_hub(palace) is True

    def test_remote_backend_skips_spawn(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            hub_bootstrap,
            "_spawn_hub",
            lambda *a, **k: pytest.fail("pgvector must not start a local hub"),
        )
        assert hub_bootstrap.ensure_hub(str(tmp_path / "palace"), backend="pgvector") is False

    def test_spawn_and_wait(self, tmp_path, monkeypatch):
        palace = str(tmp_path / "palace")
        spawned = {}

        def ready(path):
            return spawned.get("ok") is True and path == palace

        def spawn(path, backend, log_path):
            spawned["path"] = path
            spawned["backend"] = backend
            spawned["log"] = log_path
            spawned["ok"] = True
            return _FakeProc()

        monkeypatch.setattr(hub_bootstrap, "_hub_ready", ready)
        monkeypatch.setattr(hub_bootstrap, "_spawn_hub", spawn)
        monkeypatch.setattr(hub_bootstrap, "_READY_POLL_S", 0)
        assert hub_bootstrap.ensure_hub(palace, backend="chroma") is True
        assert spawned["path"] == palace
        assert spawned["backend"] == "chroma"
        assert spawned["log"].name == "hub.log"

    def test_second_caller_reuses_winner(self, tmp_path, monkeypatch):
        palace = str(tmp_path / "palace")
        state = {"ready": False, "spawns": 0}

        def ready(path):
            return state["ready"]

        def spawn(path, backend, log_path):
            state["spawns"] += 1
            state["ready"] = True
            return _FakeProc()

        monkeypatch.setattr(hub_bootstrap, "_hub_ready", ready)
        monkeypatch.setattr(hub_bootstrap, "_spawn_hub", spawn)
        monkeypatch.setattr(hub_bootstrap, "_READY_POLL_S", 0)
        assert hub_bootstrap.ensure_hub(palace) is True
        assert hub_bootstrap.ensure_hub(palace) is True
        assert state["spawns"] == 1

    def test_dead_child_still_waits_for_sibling_hub(self, tmp_path, monkeypatch):
        palace = str(tmp_path / "palace")
        calls = {"n": 0}

        class _Dead:
            returncode = 2

            def poll(self):
                return 2

        def ready(path):
            calls["n"] += 1
            return calls["n"] >= 3

        monkeypatch.setattr(hub_bootstrap, "_hub_ready", ready)
        monkeypatch.setattr(hub_bootstrap, "_READY_TIMEOUT_S", 1.0)
        monkeypatch.setattr(hub_bootstrap, "_READY_POLL_S", 0)
        assert hub_bootstrap._wait_for_hub(palace, _Dead()) is True
        assert calls["n"] >= 3

    def test_spawn_failure_falls_back(self, tmp_path, monkeypatch):
        monkeypatch.setattr(hub_bootstrap, "_hub_ready", lambda path: False)
        monkeypatch.setattr(hub_bootstrap, "_spawn_hub", lambda *a, **k: None)
        monkeypatch.setattr(hub_bootstrap, "_READY_TIMEOUT_S", 0.0)
        monkeypatch.setattr(hub_bootstrap, "_READY_POLL_S", 0)
        assert hub_bootstrap.ensure_hub(str(tmp_path / "palace")) is False


class TestDetachedKwargs:
    def test_posix(self, monkeypatch):
        monkeypatch.setattr(hub_bootstrap.os, "name", "posix")
        kwargs = hub_bootstrap.detached_popen_kwargs()
        assert kwargs.get("start_new_session") is True
        assert kwargs.get("stdin") is subprocess.DEVNULL
        assert "creationflags" not in kwargs

    def test_windows(self, monkeypatch):
        monkeypatch.setattr(hub_bootstrap.os, "name", "nt")
        monkeypatch.setattr(hub_bootstrap.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
        monkeypatch.setattr(hub_bootstrap.subprocess, "DETACHED_PROCESS", 0x00000008, raising=False)
        monkeypatch.setattr(
            hub_bootstrap.subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200, raising=False
        )
        monkeypatch.setattr(
            hub_bootstrap.subprocess,
            "CREATE_BREAKAWAY_FROM_JOB",
            0x01000000,
            raising=False,
        )
        kwargs = hub_bootstrap.detached_popen_kwargs()
        flags = kwargs.get("creationflags", 0)
        assert flags & 0x08000000
        assert not (flags & 0x00000008)
        assert "start_new_session" not in kwargs
