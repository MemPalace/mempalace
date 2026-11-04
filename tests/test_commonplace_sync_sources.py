"""Memory version selection and structure-only source diagnostics."""

import json
import os
import time

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


def test_memory_versions_ride_window_onboard_skipped(hub):
    h = hub()
    p, token, grant = actors(h)
    root = h.home.parent / "notes"
    root.mkdir()
    sroot = h.home.parent / "native"
    sroot.mkdir()
    (sroot / "session.jsonl").write_bytes(raw(records("S")))
    text = "  synthetic notes café 🦉\r\nKeep  every word.\t"
    note = root / "note.md"
    note.write_text(text, newline="")
    os.utime(note, (time.time() - 120, time.time() - 120))
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
    assert rc == 0  # COUNTED:L15
    reached("L15")
    from commonplace.onboarding import POINTER

    (root / "pointer.md").write_text(POINTER)
    original = "synthetic previously onboarded memory"
    (root / "original.md").write_text(original)
    manifest = h.home / ".commonplace/onboarding.json"
    manifest.write_text(
        json.dumps(
            {
                "version": 1,
                "files": {
                    str(root / "original.md"): {
                        "original_sha256": sha(original.encode()),
                        "pointer_sha256": sha(POINTER.encode()),
                        "backup": str(root / "original.md"),
                    }
                },
            }
        )
    )
    for path in root.iterdir():
        os.utime(path, (time.time() - 120, time.time() - 120))
    assert h.cli(["sync", "run", "--once"]) == 0
    rows = [r for r in h.imports(p) if r["source"] == "native-memory"]
    assert len(rows) == 1
    assert h.read(p, rows[0]["stored_thread_id"]) == text.encode()
    changed = text + "\nnew version"
    note.write_text(changed, newline="")
    os.utime(note, (time.time() - 120, time.time() - 120))
    h.at(901)
    assert h.cli(["sync", "run", "--once"]) == 0
    rows = [r for r in h.imports(p) if r["source"] == "native-memory"]
    assert len(rows) == 2
    assert {h.read(p, row["stored_thread_id"]) for row in rows} == {text.encode(), changed.encode()}
    assert h.cli(["sync", "run", "--once"]) == 0
    assert [r for r in h.imports(p) if r["source"] == "native-memory"] == rows


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
