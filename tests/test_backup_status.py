"""Opt-in last-completed backup receipts do not affect snapshot outcomes."""

import errno
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from commonplace import backup, backup_supervisor, cli

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 7, 12, tzinfo=timezone.utc)
END = datetime(2026, 11, 7, 12, 1, tzinfo=timezone.utc)


def args(tmp_path, **values):
    return SimpleNamespace(
        data=str(tmp_path / "data"),
        dest_dir=str(tmp_path / "snapshot"),
        online=True,
        check_online=False,
        online_cleanup=False,
        deadline=None,
        **values,
    )


def first_receipt(tmp_path, monkeypatch):
    status = tmp_path / "status"
    status.mkdir(mode=0o700)
    monkeypatch.setenv("COMMONPLACE_BACKUP_STATUS_DIR", str(status))
    monkeypatch.setattr(backup_supervisor, "run_online", lambda *a, **k: Path("manifest.json"))
    assert cli.cmd_backup(args(tmp_path)) == 0
    assert (status / "online.json").is_file(), (
        "Successful existing backup CLI must publish its opt-in receipt"
    )
    return status


def fixed_reporter(monkeypatch):
    from commonplace import backup_status

    real = backup_status.Reporter

    def create():
        times = iter((NOW, END))
        return real(utc_now=lambda: next(times))

    monkeypatch.setattr(backup_status, "Reporter", create)
    return backup_status


def check(status, mode, success=True):
    data = json.loads((status / (mode + ".json")).read_text())
    assert set(data) == {
        "schema",
        "run_id",
        "mode",
        "result",
        "started_at",
        "finished_at",
        "stage",
        "scope",
    }
    assert data["schema"] == 1 and data["scope"] == "instance_snapshot"
    assert data["mode"] == mode and data["result"] == ("success" if success else "failure")
    assert data["stage"] == ("complete" if success else "backup_failed")
    assert data["started_at"] == "2026-11-07T12:00:00Z"
    assert data["finished_at"] == "2026-11-07T12:01:00Z"
    return data


def test_cli_records_final_mode_only(tmp_path, monkeypatch):
    status = first_receipt(tmp_path, monkeypatch)
    fixed_reporter(monkeypatch)
    assert cli.cmd_backup(args(tmp_path)) == 0
    check(status, "online")

    def fail(*a, **k):
        raise backup.BackupError("synthetic-private-destination")

    monkeypatch.setattr(backup_supervisor, "run_online", fail)
    with pytest.raises(backup.BackupError):
        cli.cmd_backup(args(tmp_path))
    check(status, "online", False)
    a = args(tmp_path)
    a.online = False
    monkeypatch.setattr(backup, "backup", lambda *a: {})
    assert cli.cmd_backup(a) == 0
    check(status, "offline")
    monkeypatch.setattr(backup, "backup", fail)
    with pytest.raises(backup.BackupError):
        cli.cmd_backup(a)
    check(status, "offline", False)
    previous = (status / "offline.json").read_bytes()
    monkeypatch.setattr(backup_supervisor, "lock_busy", lambda exc: True)
    monkeypatch.setattr(backup_supervisor, "_linux", lambda: True)
    monkeypatch.setattr(backup_supervisor, "_real_data_dir", lambda data: Path(data))
    monkeypatch.setattr(backup_supervisor, "_answering", lambda data: {"ok": True})
    monkeypatch.setattr(backup_supervisor, "is_staging_dest", lambda *a: True)
    monkeypatch.setattr(backup_supervisor, "run_online", lambda *a, **k: Path("manifest.json"))
    assert cli.cmd_backup(a) == 0
    check(status, "online")
    assert (status / "offline.json").read_bytes() == previous
    monkeypatch.setattr(backup_supervisor, "run_online", fail)
    with pytest.raises(backup.BackupError):
        cli.cmd_backup(a)
    check(status, "online", False)
    assert (status / "offline.json").read_bytes() == previous
    before = {p.name: p.read_bytes() for p in status.iterdir()}
    for flag, func in (("check_online", "check_online"), ("online_cleanup", "cleanup")):
        a = args(tmp_path)
        setattr(a, flag, True)
        monkeypatch.setattr(backup_supervisor, func, lambda *a: {})
        assert cli.cmd_backup(a) == 0
    monkeypatch.setattr(backup, "restore", lambda *a: {})
    assert cli.cmd_restore(SimpleNamespace(backup_dir="unused", dest_dir="unused")) == 0
    assert {p.name: p.read_bytes() for p in status.iterdir()} == before


def test_receipt_atomic_privacy_and_backup_independence(tmp_path, monkeypatch, capsys):
    status = first_receipt(tmp_path, monkeypatch)
    module = fixed_reporter(monkeypatch)
    assert cli.cmd_backup(args(tmp_path)) == 0
    check(status, "online")
    path = status / "online.json"
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.stat().st_uid == os.getuid()
    previous = path.read_bytes()
    # An interruption before finish leaves the prior completed receipt.
    module.Reporter()
    assert path.read_bytes() == previous
    replace = os.replace

    def denied(src, dst, *a, **kw):
        if str(dst) == "online.json" or Path(dst).parent == status:
            raise OSError(errno.EACCES, "private-password-and-path")
        return replace(src, dst, *a, **kw)

    monkeypatch.setattr(os, "replace", denied)
    capsys.readouterr()
    assert cli.cmd_backup(args(tmp_path)) == 0
    assert path.read_bytes() == previous

    def fail(*a, **k):
        raise backup.BackupError("original-backup-error")

    monkeypatch.setattr(backup_supervisor, "run_online", fail)
    with pytest.raises(backup.BackupError, match="^original-backup-error$"):
        cli.cmd_backup(args(tmp_path))
    assert path.read_bytes() == previous
    assert sorted(p.name for p in status.iterdir()) == ["online.json"]
    assert capsys.readouterr().err.splitlines() == ["backup status unavailable"] * 2
    monkeypatch.setattr(os, "replace", replace)
    with pytest.raises(backup.BackupError):
        cli.cmd_backup(args(tmp_path))
    check(status, "online", False)
    monkeypatch.delenv("COMMONPLACE_BACKUP_STATUS_DIR")
    with pytest.raises(backup.BackupError):
        cli.cmd_backup(args(tmp_path))
    assert not (tmp_path / "data").exists()


FAKE_DOCKER = """#!/usr/bin/env python3
import json, os, pathlib, sys
args=sys.argv[1:]
with open(os.environ['CALLS'],'a') as f: f.write(json.dumps(args)+'\\n')
if args[0]=='inspect':
    if '.Mounts' in args[4]: print(json.dumps([{'Destination':'/data','Source':os.environ['DATA'],'RW':True}]))
    else: print('synthetic-id synthetic-start true')
    sys.exit(0)
verb=next(x for x in args if x in ('ps','exec'))
if verb=='ps': print('synthetic-id')
if verb=='exec' and '--online' in args:
    dest=pathlib.Path(os.environ['DATA'])/'.backup-staging'/pathlib.Path(args[-1]).name
    dest.mkdir(); (dest/'control.sqlite3').write_bytes(b'synthetic'); (dest/'manifest.json').write_text('{}')
"""


def test_receipt_mount_and_cli_override(tmp_path, monkeypatch):
    _receipt_mount_and_cli_override(tmp_path)


def test_invalid_receipt_alias_rejected(tmp_path):
    _receipt_mount_and_cli_override(tmp_path, invalid_alias=True)


def _receipt_mount_and_cli_override(tmp_path, *, invalid_alias=False):
    example = ROOT / "deploy/compose.backup-status.yml.example"
    assert example.is_file(), "Opt-in dual-alias receipt mounts must be provided"
    # Render only synthetic interpolation; never read operator deployment files.
    rendered = example.read_text().replace(
        "${COMMONPLACE_BACKUP_STATUS_HOST_DIR:?Set COMMONPLACE_BACKUP_STATUS_HOST_DIR to an existing status directory}",
        str(tmp_path / "receipts"),
    )
    config = yaml.safe_load(rendered)["services"]["commonplace"]
    volumes = config["volumes"]
    assert len(volumes) == 2
    assert volumes[0]["source"] == volumes[1]["source"] == str(tmp_path / "receipts")
    assert {v["target"]: v["read_only"] for v in volumes} == {
        "/run/commonplace-backup-status-ro": True,
        "/run/commonplace-backup-status-rw": False,
    }
    assert all(v["type"] == "bind" and v["bind"]["create_host_path"] is False for v in volumes)
    assert config["environment"] == {
        "COMMONPLACE_BACKUP_STATUS_DIR": "/run/commonplace-backup-status-ro"
    }
    assert "COMMONPLACE_BACKUP_STATUS_WRITE_DIR" not in config["environment"]
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (
        ("docker", FAKE_DOCKER),
        (
            "install",
            '#!/bin/sh\nwhile [ "$#" -gt 0 ]; do case "$1" in -d) shift;; -o|-g|-m) shift 2;; *) mkdir -p "$1"; shift;; esac; done\n',
        ),
    ):
        p = bin_dir / name
        p.write_text(body)
        p.chmod(0o755)
    data = tmp_path / "data"
    data.mkdir()
    dest = tmp_path / "backups"
    dest.mkdir()
    calls = tmp_path / "calls"
    env = {
        "PATH": str(bin_dir) + os.pathsep + os.defpath,
        "DATA": str(data),
        "CALLS": str(calls),
        "COMMONPLACE_DEPLOY_DIR": str(tmp_path),
        "COMMONPLACE_BACKUP_RESERVE_BYTES": "0",
        "COMMONPLACE_BACKUP_MARGIN_BYTES": "0",
    }
    for opt_in in (True,) if invalid_alias else (False, True):
        calls.write_text("")
        if opt_in:
            env["COMMONPLACE_BACKUP_STATUS_WRITE_DIR"] = (
                "/run/invalid-status-alias"
                if invalid_alias
                else "/run/commonplace-backup-status-rw"
            )
        result = subprocess.run(
            ["bash", str(ROOT / "backup-hook.sh"), str(dest)],
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
        )
        if invalid_alias:
            assert result.returncode == 2, result.stderr
            assert not calls.exists() or "--online" not in calls.read_text()
            return
        assert result.returncode == 0, result.stderr
        argv = [json.loads(line) for line in calls.read_text().splitlines()]
        compose = [a for a in argv if a[0] == "compose"]
        assert all(any(v in a for v in ("ps", "exec")) for a in compose)
        snapshot = next(a for a in compose if "--online" in a)
        expected = ["-e", "COMMONPLACE_BACKUP_STATUS_DIR=/run/commonplace-backup-status-rw"]
        if opt_in:
            assert "-e" in snapshot, "Snapshot exec must carry the writer alias override"
            at = snapshot.index("-e")
            assert snapshot[at : at + 2] == expected
            assert snapshot[at + 2 : at + 4] == ["commonplace", "commonplace"]
        else:
            assert "-e" not in snapshot
        assert all("-e" not in a for a in compose if "--online" not in a)
