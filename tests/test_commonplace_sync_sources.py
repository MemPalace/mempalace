"""Memory version selection and structure-only source diagnostics."""

import json
import os

import pytest

from tests.commonplace_sync_fixtures import (
    actors,
    home_tree,
    hub as hub,
    raw,
    reached,
    records,
    reset_reached as reset_reached,
    sha,
)


@pytest.mark.parametrize("start", [0, 12 * 3600 - 60], ids=["noon", "utc-midnight"])
def test_memory_versions_ride_window_onboard_skipped(hub, start):
    h = hub()
    h.at(start)
    p, token, grant = actors(h)
    root = h.home.parent / "notes"
    root.mkdir()
    sroot = h.home.parent / "native"
    sroot.mkdir()
    (sroot / "session.jsonl").write_bytes(raw(records("S")))
    text = "  synthetic notes café 🦉\r\nKeep  every word.\t"
    note = root / "note.md"
    note.write_text(text, newline="")
    # File stability must use the same clock as the sync scanner.
    stable_at = h.now.timestamp() - 120
    os.utime(note, (stable_at, stable_at))
    E = h.env_file(token, grant)
    rc = h.cli(
        [
            "sync",
            "install",
            "--enable",
            "--no-service",
            "--memory",
            f"notes={root}",
            "--source",
            f"claude-code:{sroot}",
            "--env-file",
            E,
            "--hub",
            h.origin,
        ]
    )
    assert rc == 2  # COUNTED:L15
    reached("L15")
    assert not (h.home / ".commonplace/sync/config.json").exists()
    assert not h.fake_log.exists()
    assert [r for r in h.imports(p) if r["source"] == "native-memory"] == []
    assert note.read_bytes() == text.encode()
    assert (sroot / "session.jsonl").read_bytes() == raw(records("S"))


def test_cursor_unsupported_and_probe_structure_only(hub, capsys):
    h = hub()
    _, token, grant = actors(h)
    E = h.env_file(token, grant)
    before = home_tree(h.home)
    rc = h.cli(
        [
            "sync",
            "install",
            "--source",
            "cursor",
            "--enable",
            "--no-service",
            "--no-activate",
            "--env-file",
            E,
        ]
    )
    assert rc == 1  # COUNTED:L16
    reached("L16")
    from commonplace.sync_protocol import CURSOR_MESSAGE, PROBE_KEY_ALLOWLIST

    output = capsys.readouterr()
    assert CURSOR_MESSAGE in output.out + output.err
    assert home_tree(h.home) == before
    assert not h.fake_log.exists()
    path = h.home.parent / "probe.jsonl"
    secret_key, value = "synthetic_unlisted_key", "synthetic_value_never_printed"
    path.write_text(json.dumps({"type": value, secret_key: {"content": value}}) + "\n")
    assert h.cli(["sync", "probe", str(path)]) == 0
    output = capsys.readouterr()
    report = output.out + output.err
    assert value not in report and secret_key not in report
    assert secret_key not in PROBE_KEY_ALLOWLIST
    assert sha(secret_key.encode())[:12] in report or sha(secret_key.encode()) in report
