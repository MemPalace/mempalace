"""Public-interface tests for opt-in release awareness."""

import json
import sys
import threading
import time
from datetime import datetime, timezone

from mempalace.update_awareness import (
    cached_update_status,
    check_updates,
    configure_updates,
    prepare_upgrade,
    schedule_update_check,
)


NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)


def test_disabled_automatic_check_never_contacts_release_source(tmp_path):
    calls = []

    result = check_updates(
        config_dir=tmp_path,
        installed_version="3.8.0",
        fetch_latest=lambda: calls.append(True) or "3.9.0",
        now=NOW,
    )

    assert result == {"enabled": False, "installed": "3.8.0", "checked": False}
    assert calls == []


def test_disabled_background_check_never_starts_or_contacts_release_source(tmp_path):
    calls = []

    assert not schedule_update_check(
        config_dir=tmp_path,
        fetch_latest=lambda: calls.append(True) or "3.9.0",
        now=NOW,
    )
    assert calls == []


def test_explicit_check_fetches_and_caches_latest_release_when_disabled(tmp_path):
    result = check_updates(
        config_dir=tmp_path,
        installed_version="3.8.0",
        fetch_latest=lambda: "3.9.0",
        now=NOW,
        force=True,
    )

    assert result == {
        "enabled": False,
        "installed": "3.8.0",
        "latest": "3.9.0",
        "available": True,
        "checked": True,
        "checked_at": "2026-08-28T12:00:00Z",
        "restart_required_after_upgrade": True,
    }
    assert (
        prepare_upgrade(config_dir=tmp_path, installed_version="3.8.0", installer="uv-tool")[
            "requires_user_authorization"
        ]
        is True
    )
    assert cached_update_status(config_dir=tmp_path, installed_version="3.8.0") == {
        "enabled": False,
        "installed": "3.8.0",
    }


def test_opted_in_check_uses_fresh_cache_without_network(tmp_path):
    configure_updates(config_dir=tmp_path, enabled=True, interval_days=7, installer="uv-tool")
    check_updates(
        config_dir=tmp_path,
        installed_version="3.8.0",
        fetch_latest=lambda: "3.9.0",
        now=NOW,
        force=True,
    )
    calls = []

    result = check_updates(
        config_dir=tmp_path,
        installed_version="3.8.0",
        fetch_latest=lambda: calls.append(True) or "4.0.0",
        now=datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc),
    )

    assert result["latest"] == "3.9.0"
    assert result["available"] is True
    assert result["checked"] is False
    assert calls == []


def test_corrupt_cached_release_is_ignored_and_refreshed(tmp_path):
    configure_updates(config_dir=tmp_path, enabled=True, installer="uv-tool")
    (tmp_path / "updates-cache.json").write_text(
        '{"latest": "not-a-version", "checked_at": 123}',
        encoding="utf-8",
    )

    assert cached_update_status(config_dir=tmp_path, installed_version="3.8.0") == {
        "enabled": True,
        "installed": "3.8.0",
        "checked": False,
    }
    refreshed = check_updates(
        config_dir=tmp_path,
        installed_version="3.8.0",
        fetch_latest=lambda: "3.9.0",
        now=NOW,
    )
    assert refreshed["latest"] == "3.9.0"
    assert refreshed["checked"] is True


def test_failed_periodic_check_is_not_retried_until_interval_expires(tmp_path):
    configure_updates(config_dir=tmp_path, enabled=True, installer="uv-tool")
    calls = []

    def fail():
        calls.append(True)
        raise OSError("offline")

    for _ in range(2):
        try:
            check_updates(
                config_dir=tmp_path,
                installed_version="3.8.0",
                fetch_latest=fail,
                now=NOW,
            )
        except OSError:
            pass

    assert calls == [True]


def test_timezone_naive_cached_attempt_is_treated_as_stale(tmp_path):
    configure_updates(config_dir=tmp_path, enabled=True, installer="uv-tool")
    (tmp_path / "updates-cache.json").write_text(
        '{"attempted_at": "2026-08-28T12:00:00"}', encoding="utf-8"
    )

    result = check_updates(
        config_dir=tmp_path,
        installed_version="3.8.0",
        fetch_latest=lambda: "3.9.0",
        now=NOW,
    )

    assert result["checked"] is True


def test_cached_agent_status_and_plan_never_apply_the_upgrade(tmp_path):
    configure_updates(config_dir=tmp_path, enabled=True, installer="uv-tool")
    check_updates(
        config_dir=tmp_path,
        installed_version="3.8.0",
        fetch_latest=lambda: "3.9.0",
        now=NOW,
        force=True,
    )

    assert cached_update_status(config_dir=tmp_path, installed_version="3.8.0")["available"]
    plan = prepare_upgrade(config_dir=tmp_path, installed_version="3.8.0")
    assert plan["requires_user_authorization"] is True
    assert plan["actions"][0] == {
        "kind": "command",
        "argv": ["uv", "tool", "upgrade", "mempalace"],
    }
    assert plan["actions"][1] == {
        "kind": "command",
        "argv": ["npx", "skills", "update", "mempalace", "mempalace-recall", "mempalace-task"],
    }
    assert plan["actions"][2] == {
        "kind": "instruction",
        "text": "restart the MemPalace MCP server or shared hub",
    }
    assert plan["actions"][3] == {
        "kind": "instruction",
        "text": "refresh agent MCP tool lists",
    }
    assert "--yes" not in json.dumps(plan["actions"])


def test_pip_plan_targets_the_interpreter_running_mempalace(tmp_path, monkeypatch):
    runtime = tmp_path / "venv" / "bin" / "python"
    monkeypatch.setattr(sys, "executable", str(runtime))
    configure_updates(config_dir=tmp_path, enabled=True, installer="pip")
    check_updates(
        config_dir=tmp_path,
        installed_version="3.8.0",
        fetch_latest=lambda: "3.9.0",
        now=NOW,
        force=True,
    )

    plan = prepare_upgrade(config_dir=tmp_path, installed_version="3.8.0")

    assert plan["actions"][0] == {
        "kind": "command",
        "argv": [str(runtime), "-m", "pip", "install", "--upgrade", "mempalace"],
    }


def test_opted_in_background_check_publishes_cached_state_for_agents(tmp_path):
    configure_updates(config_dir=tmp_path, enabled=True, installer="uv-tool")
    assert schedule_update_check(config_dir=tmp_path, fetch_latest=lambda: "3.9.0", now=NOW)
    deadline = time.monotonic() + 2
    cache_file = tmp_path / "updates-cache.json"
    while not cache_file.exists() or "3.9.0" not in cache_file.read_text(encoding="utf-8"):
        assert time.monotonic() < deadline
        time.sleep(0.01)

    assert cached_update_status(config_dir=tmp_path, installed_version="3.8.0")["available"]


def test_in_flight_check_cannot_restore_withdrawn_consent(tmp_path):
    entered_fetch = threading.Event()
    release_fetch = threading.Event()

    def fetch_latest():
        entered_fetch.set()
        assert release_fetch.wait(timeout=2)
        return "3.9.0"

    configure_updates(config_dir=tmp_path, enabled=True, installer="uv-tool")
    assert schedule_update_check(config_dir=tmp_path, fetch_latest=fetch_latest, now=NOW)
    assert entered_fetch.wait(timeout=2)

    configure_updates(config_dir=tmp_path, enabled=False)
    release_fetch.set()
    deadline = time.monotonic() + 2
    cache_file = tmp_path / "updates-cache.json"
    while "3.9.0" not in cache_file.read_text(encoding="utf-8"):
        assert time.monotonic() < deadline
        time.sleep(0.01)

    assert cached_update_status(config_dir=tmp_path, installed_version="3.8.0") == {
        "enabled": False,
        "installed": "3.8.0",
    }
