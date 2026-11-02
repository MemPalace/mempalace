"""Offline restore drills and nightly failure propagation; no remote credentials."""

import importlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path

import pytest

from commonplace.backup import BackupError, backup, restore, verify_backup
from commonplace.control_db import ControlDB

ROOT = Path(__file__).resolve().parents[1]
NIGHTLY = ROOT / "nightly-backup.sh"


@pytest.fixture
def fresh_data(tmp_path):
    data = tmp_path / "data"
    connections = []
    with closing(ControlDB(str(data / "control.sqlite3"))) as control:
        workspaces = []
        for index in range(2):
            user = control.create_user(f"fresh{index}@example.test")
            workspace = control.create_workspace(f"workspace-{index}", user, personal=True)
            workspaces.append((user, workspace))
            directory = data / "workspaces" / workspace
            directory.mkdir(parents=True)
            (directory / "verbatim.txt").write_bytes(b"Exact words.\n\x00\xff")
            (directory / "empty-directory").mkdir()
            # All nested databases must be discovered, not a fixed filename list.
            for name in ("boards.sqlite3", "palace/compact.sqlite3", "retired/threads.sqlite3"):
                path = directory / name
                path.parent.mkdir(parents=True, exist_ok=True)
                conn = sqlite3.connect(path, isolation_level=None)
                connections.append(conn)
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA wal_autocheckpoint=0")
                conn.execute("CREATE TABLE memory (text TEXT)")
                conn.execute("INSERT INTO memory VALUES (?)", (f"Workspace {index}: verbatim.\n",))
        # Also exercise a brand-new account/workspace with no worker files yet.
        user = control.create_user("unused@example.test")
        empty = control.create_workspace("not-yet-opened", user)
        workspaces.append((user, empty))
        try:
            yield data, workspaces
        finally:
            for conn in connections:
                conn.close()


def assert_restored(destination, workspaces):
    with closing(ControlDB(str(destination / "control.sqlite3"))) as control:
        assert {row["id"] for row in control.workspaces()} == {w for _, w in workspaces}
        for user, workspace in workspaces:
            assert control.role(workspace, user) == "owner"
    for index, (_, workspace) in enumerate(workspaces[:2]):
        directory = destination / "workspaces" / workspace
        assert (directory / "verbatim.txt").read_bytes() == b"Exact words.\n\x00\xff"
        assert (directory / "empty-directory").is_dir()
        for name in ("boards.sqlite3", "palace/compact.sqlite3", "retired/threads.sqlite3"):
            with closing(sqlite3.connect(directory / name)) as conn:
                assert conn.execute("SELECT text FROM memory").fetchall() == [
                    (f"Workspace {index}: verbatim.\n",)
                ]
                assert conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]


def test_offline_scratch_restore_measures_time_and_recovers_every_workspace(
    fresh_data, tmp_path, capsys
):
    data, workspaces = fresh_data
    snapshot = tmp_path / "snapshot"
    wal_files = list(data.rglob("*-wal"))
    assert len(wal_files) == 7
    assert all(path.stat().st_size > 0 for path in wal_files)
    manifest = backup(data, snapshot)
    assert verify_backup(snapshot) == manifest
    destination = tmp_path / "scratch"
    started = time.monotonic()
    report = restore(snapshot, destination)
    assert 0 < report["elapsed_seconds"] <= time.monotonic() - started
    assert report["files"] == len(manifest["files"])
    assert report["bytes"] == sum(entry["size"] for entry in manifest["files"])
    assert not list(destination.rglob("*-wal"))
    assert not list(destination.rglob("*-shm"))
    assert_restored(destination, workspaces)
    backup(destination, tmp_path / "restored-backup")
    print(json.dumps({"offline_restore": report}, sort_keys=True))
    # Keep timing visible in an ordinary pytest -s evidence log.
    captured = capsys.readouterr()
    with capsys.disabled():
        print(captured.out, end="")


def test_restore_cli_does_not_initialize_an_unrelated_data_directory(fresh_data, tmp_path):
    data, workspaces = fresh_data
    snapshot = tmp_path / "snapshot"
    backup(data, snapshot)
    untouched = tmp_path / "unrelated-data"
    destination = tmp_path / "scratch"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "commonplace",
            "--data",
            str(untouched),
            "restore",
            str(snapshot),
            str(destination),
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["elapsed_seconds"] > 0
    assert not untouched.exists()
    assert_restored(destination, workspaces)


@pytest.mark.parametrize(
    "damage", ["changed", "missing", "extra", "symlink", "unsafe", "duplicate", "version"]
)
def test_restore_refuses_damaged_inventory_without_publishing(fresh_data, tmp_path, damage):
    data, workspaces = fresh_data
    snapshot = tmp_path / "snapshot"
    manifest = backup(data, snapshot)
    file = snapshot / "workspaces" / workspaces[0][1] / "verbatim.txt"
    if damage == "changed":
        file.write_text("replacement")
    elif damage == "missing":
        file.unlink()
    elif damage == "extra":
        (snapshot / "unlisted").write_text("extra")
    elif damage == "symlink":
        file.unlink()
        file.symlink_to(data / file.relative_to(snapshot))
    elif damage == "unsafe":
        manifest["files"][0]["path"] = "../outside"
    elif damage == "duplicate":
        manifest["files"].append(manifest["files"][0])
    elif damage == "version":
        manifest["version"] = 999
    (snapshot / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(BackupError):
        restore(snapshot, tmp_path / "scratch")
    assert not (tmp_path / "scratch").exists()
    assert not list(tmp_path.glob(".scratch.partial-*"))


def test_restore_preserves_existing_destination(fresh_data, tmp_path):
    snapshot = tmp_path / "snapshot"
    backup(fresh_data[0], snapshot)
    destination = tmp_path / "scratch"
    destination.mkdir()
    (destination / "keep").write_text("existing")
    with pytest.raises(BackupError, match="already exists"):
        restore(snapshot, destination)
    assert (destination / "keep").read_text() == "existing"
    with pytest.raises(BackupError, match="outside"):
        restore(snapshot, snapshot / "nested")


def test_restore_copy_failure_cleans_partial(fresh_data, tmp_path, monkeypatch):
    module = importlib.import_module("commonplace.backup")
    snapshot = tmp_path / "snapshot"
    backup(fresh_data[0], snapshot)

    def fail(*_args):
        raise OSError("scratch disk full")

    monkeypatch.setattr(module.shutil, "copyfile", fail)
    with pytest.raises(OSError, match="disk full"):
        restore(snapshot, tmp_path / "scratch")
    assert not (tmp_path / "scratch").exists()
    assert not list(tmp_path.glob(".scratch.partial-*"))


# Simulate only Docker/ownership, remote restic and alert delivery. Snapshot and
# restore use real SQLite and the production backup implementation throughout.
FAKE_COMMAND = r"""
import json, os, pathlib, sys
name, args = pathlib.Path(sys.argv[0]).name, sys.argv[1:]
with open(os.environ["FAKE_COMMANDS"], "a") as log:
    log.write(json.dumps({"command": name, "args": args}) + "\n")
fail = os.environ.get("FAKE_FAIL", "")
if name == "install":
    pathlib.Path(args[-1]).mkdir(parents=True, exist_ok=True)
elif name == "docker":
    if "ps" in args:
        print("test-container")
    elif "exec" in args:
        if fail == "preflight": sys.exit(9)
    elif "stop" in args:
        if fail == "stop": sys.exit(9)
    elif "start" in args:
        if fail == "restart": sys.exit(9)
    elif "run" in args:
        if fail == "snapshot": sys.exit(9)
        from commonplace.backup import backup
        mount = args[args.index("--volume") + 1]
        target = pathlib.Path(mount.removesuffix(":/backup")) / pathlib.Path(args[-1]).name
        backup(os.environ["FAKE_DATA"], target)
    else:
        sys.exit(10)
elif name == "restic":
    if args[0] == "backup":
        from commonplace.backup import restore
        restore(args[-1], os.environ["FAKE_RESTORED"])
        if fail == "upload": sys.exit(3)
    elif args[0] == "forget":
        if fail == "retention": sys.exit(9)
    else:
        sys.exit(10)
elif name == "alert":
    if fail == "alert": sys.exit(9)
else:
    sys.exit(10)
"""


def run_nightly(tmp_path, data, fail="", **overrides):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("docker", "install", "restic", "alert"):
        executable = bin_dir / name
        executable.write_text(f"#!{sys.executable}\n" + FAKE_COMMAND)
        executable.chmod(0o755)
    staging = tmp_path / "staging"
    staging.mkdir()
    commands = tmp_path / "commands.jsonl"
    recovered = tmp_path / "recovered"
    env = {
        "PATH": f"{bin_dir}:{os.defpath}",
        "PYTHONPATH": str(ROOT),
        "FAKE_COMMANDS": str(commands),
        "FAKE_DATA": str(data),
        "FAKE_RESTORED": str(recovered),
        "FAKE_FAIL": fail,
        "RESTIC_REPOSITORY": "sftp:backup@backup.example.test:/backups",
        "COMMONPLACE_BACKUP_ID": "test-deployment",
        "COMMONPLACE_BACKUP_ALERT": str(bin_dir / "alert"),
        **overrides,
    }
    result = subprocess.run(
        ["bash", str(NIGHTLY), str(staging)],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    calls = (
        [json.loads(line) for line in commands.read_text().splitlines()]
        if commands.exists()
        else []
    )
    # No retained plaintext backup after either success or failure.
    assert {path.name for path in staging.iterdir()} <= {".commonplace-nightly.lock"}
    for log in re.findall(r"details: (/tmp/commonplace-(?:nightly|backup)\.[^)]+)", result.stderr):
        log_path = Path(log)
        assert log_path.is_file()
        assert log_path.stat().st_mode & 0o777 == 0o600
        # The hook has its own diagnostic log, referenced from the nightly log.
        for child_log in re.findall(
            r"details: (/tmp/commonplace-backup\.[^)]+)", log_path.read_text()
        ):
            Path(child_log).unlink()
        log_path.unlink()
    return result, calls, recovered


def test_nightly_uploads_restorable_tree_then_retains_thirty_days(fresh_data, tmp_path):
    result, calls, recovered = run_nightly(tmp_path, fresh_data[0])
    assert result.returncode == 0, result.stderr
    assert result.stdout == "commonplace nightly backup completed\n"
    assert_restored(recovered, fresh_data[1])
    uploads = [call["args"] for call in calls if call["command"] == "restic"]
    assert len(uploads) == 2
    assert uploads[0][:6] == ["backup", "--host", "test-deployment", "--tag", "commonplace", "--"]
    assert uploads[1] == [
        "forget",
        "--host",
        "test-deployment",
        "--tag",
        "commonplace",
        "--group-by",
        "host,tags",
        "--keep-within",
        "30d",
        "--prune",
    ]
    start = next(
        i for i, call in enumerate(calls) if call["command"] == "docker" and "start" in call["args"]
    )
    upload = next(i for i, call in enumerate(calls) if call["command"] == "restic")
    assert start < upload
    assert not any(call["command"] == "alert" for call in calls)


@pytest.mark.parametrize(
    "failure", ["preflight", "stop", "snapshot", "restart", "upload", "retention"]
)
def test_nightly_failure_propagates_alerts_and_skips_unsafe_followup(fresh_data, tmp_path, failure):
    result, calls, _ = run_nightly(tmp_path, fresh_data[0], failure)
    assert result.returncode != 0
    assert "nightly backup failed:" in result.stderr
    alerts = [call for call in calls if call["command"] == "alert"]
    assert len(alerts) == 1
    assert alerts[0]["args"][0] == (failure if failure in {"upload", "retention"} else "snapshot")
    restic = [call["args"][0] for call in calls if call["command"] == "restic"]
    assert restic == ({"upload": ["backup"], "retention": ["backup", "forget"]}.get(failure, []))
    if failure in {"stop", "snapshot", "restart"}:
        assert any(call["command"] == "docker" and "start" in call["args"] for call in calls)


@pytest.mark.parametrize("repository", ["/local/repo", "relative", ""])
def test_nightly_refuses_local_repository_before_downtime(fresh_data, tmp_path, repository):
    result, calls, _ = run_nightly(tmp_path, fresh_data[0], RESTIC_REPOSITORY=repository)
    assert result.returncode == 2
    assert [call["command"] for call in calls] == ["alert"]


def test_nightly_requires_working_alert_configuration_before_downtime(fresh_data, tmp_path):
    result, calls, _ = run_nightly(tmp_path, fresh_data[0], COMMONPLACE_BACKUP_ALERT="")
    assert result.returncode == 2
    assert calls == []


def test_nightly_reports_alert_delivery_failure(fresh_data, tmp_path):
    result, calls, _ = run_nightly(tmp_path, fresh_data[0], "alert", RESTIC_REPOSITORY="/local")
    assert result.returncode != 0
    assert "backup alert delivery failed" in result.stderr
    assert [call["command"] for call in calls] == ["alert"]
