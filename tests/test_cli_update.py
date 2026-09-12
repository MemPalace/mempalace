"""CLI contracts for opt-in update awareness."""

import json
import sys

from mempalace.cli import main
from mempalace.version import __version__


def test_update_configure_requires_explicit_consent_and_persists_it(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "mempalace",
            "update",
            "configure",
            "--enable",
            "--interval-days",
            "14",
            "--installer",
            "uv-tool",
        ],
    )

    main()

    assert json.loads(capsys.readouterr().out) == {
        "enabled": True,
        "interval_days": 14,
        "channel": "stable",
        "installer": "uv-tool",
    }
    stored = json.loads((tmp_path / ".mempalace" / "updates.json").read_text(encoding="utf-8"))
    assert stored["enabled"] is True
    assert stored["interval_days"] == 14
    assert stored["installer"] == "uv-tool"


def test_update_plan_does_not_execute_commands(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["mempalace", "update", "plan"])

    main()

    assert json.loads(capsys.readouterr().out) == {
        "available": False,
        "installed": __version__,
        "actions": [],
    }
