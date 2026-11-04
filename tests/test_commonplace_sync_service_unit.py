"""User services render deterministically and only call fake service managers."""

import os
import plistlib
import stat
import subprocess
from pathlib import Path

import pytest

from commonplace import sync_service


class FakeRunner:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, *, check):
        assert check is True
        assert argv[0] in {"systemctl", "launchctl"}
        self.calls.append(argv)
        return subprocess.CompletedProcess(argv, 0)


@pytest.fixture
def home(tmp_path, monkeypatch):
    directory = tmp_path / "home"
    directory.mkdir()
    monkeypatch.setenv("HOME", str(directory))
    monkeypatch.setattr(sync_service.sys, "executable", "/opt/python/bin/python")
    monkeypatch.setattr(sync_service.os, "getuid", lambda: 1234)
    return directory


def test_systemd_golden():
    path, text = sync_service.render(
        "systemd", python="/opt/python/bin/python", env_file="/config/sync.env"
    )
    assert path == Path(".config/systemd/user/commonplace-sync.service")
    assert text == (
        "[Unit]\n"
        "Description=Commonplace live sync\n\n"
        "[Service]\n"
        "EnvironmentFile=/config/sync.env\n"
        'ExecStart="/opt/python/bin/python" -m commonplace sync run\n'
        "Restart=on-failure\n"
        "UMask=0077\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def test_launchd_golden():
    path, text = sync_service.render(
        "launchd", python="/opt/python/bin/python", env_file="/config/sync.env"
    )
    assert path == Path("Library/LaunchAgents/dev.commonplace.sync.plist")
    expected = {
        "Label": "dev.commonplace.sync",
        "ProgramArguments": [
            "/bin/sh",
            "-c",
            'set -a; . "$1" || exit; set +a; shift; exec "$@"',
            "commonplace-sync",
            "/config/sync.env",
            "/opt/python/bin/python",
            "-m",
            "commonplace",
            "sync",
            "run",
        ],
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "Umask": 63,
    }
    assert text == plistlib.dumps(expected, sort_keys=False).decode("utf-8")
    assert plistlib.loads(text.encode()) == expected


@pytest.mark.parametrize("kind", ["systemd", "launchd"])
@pytest.mark.parametrize("activate", [False, True])
def test_install_uses_only_injected_runner(home, kind, activate):
    env_file = home / "credentials.env"
    env_file.write_text("COMMONPLACE_IMPORT_TOKEN=synthetic-test-value\n")
    env_file.chmod(0o600)
    runner = FakeRunner()
    path = sync_service.install(
        kind, home=home, env_file=env_file, activate=activate, runner=runner
    )
    relative, expected = sync_service.render(
        kind, python="/opt/python/bin/python", env_file=env_file
    )
    assert path == home / relative
    assert path.read_text() == expected
    assert "synthetic-test-value" not in expected
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not list(path.parent.glob(f".{path.name}.*"))
    if not activate:
        assert runner.calls == []
    elif kind == "systemd":
        assert runner.calls == [
            ["systemctl", "--user", "daemon-reload"],
            ["systemctl", "--user", "enable", "--now", "commonplace-sync.service"],
        ]
    else:
        assert runner.calls == [["launchctl", "bootstrap", "gui/1234", str(path)]]


@pytest.mark.parametrize("kinds", [(), ("systemd",), ("launchd",), ("systemd", "launchd")])
@pytest.mark.parametrize("activate", [False, True])
def test_uninstall_removes_only_present_services(home, kinds, activate):
    paths = {
        kind: sync_service.install(
            kind, home=home, env_file=home / "sync.env", activate=False, runner=FakeRunner()
        )
        for kind in kinds
    }
    keep = home / "keep.txt"
    keep.write_text("keep")
    runner = FakeRunner()
    sync_service.uninstall(home=home, activate=activate, runner=runner)
    assert all(not path.exists() for path in paths.values())
    assert keep.read_text() == "keep"
    expected = []
    if activate and "systemd" in kinds:
        expected.extend(
            [
                ["systemctl", "--user", "disable", "--now", "commonplace-sync.service"],
                ["systemctl", "--user", "daemon-reload"],
            ]
        )
    if activate and "launchd" in kinds:
        expected.append(["launchctl", "bootout", "gui/1234", str(paths["launchd"])])
    assert runner.calls == expected
    sync_service.uninstall(home=home, activate=activate, runner=FakeRunner())


@pytest.mark.parametrize("kind", ["systemd", "launchd"])
def test_uninstall_failure_preserves_service_for_retry(home, kind):
    path = sync_service.install(
        kind, home=home, env_file=home / "sync.env", activate=False, runner=FakeRunner()
    )

    def failed(argv, *, check):
        raise subprocess.CalledProcessError(1, argv)

    with pytest.raises(subprocess.CalledProcessError):
        sync_service.uninstall(home=home, activate=True, runner=failed)
    assert path.is_file()


@pytest.mark.parametrize("kind", ["systemd", "launchd"])
def test_render_does_not_read_env_file(home, kind):
    env_file = home / "missing.env"
    _, text = sync_service.render(kind, python="/opt/python", env_file=env_file)
    assert str(env_file) in text
    assert not env_file.exists()


def test_systemd_escapes_paths():
    _, text = sync_service.render(
        "systemd", python='/opt/a %/$b/"python', env_file="/config/a %/sync.env"
    )
    assert 'EnvironmentFile="/config/a %%/sync.env"' in text
    assert 'ExecStart="/opt/a %%/$$b/\\"python" -m commonplace sync run' in text


def test_launchd_paths_are_literal_arguments(home):
    env_file = home / 'a "quoted" $file.env'
    _, text = sync_service.render("launchd", python="/opt/a $python", env_file=env_file)
    argv = plistlib.loads(text.encode())["ProgramArguments"]
    assert argv[4:6] == [str(env_file), "/opt/a $python"]
    assert str(env_file) not in argv[2]
    assert "/opt/a $python" not in argv[2]


@pytest.mark.parametrize("platform,kind", [("linux", "systemd"), ("darwin", "launchd")])
def test_auto_selects_platform(monkeypatch, platform, kind):
    monkeypatch.setattr(sync_service.sys, "platform", platform)
    assert sync_service.render("auto", python="/opt/python", env_file="/config/sync.env") == (
        sync_service.render(kind, python="/opt/python", env_file="/config/sync.env")
    )


@pytest.mark.parametrize("kind", ["unknown", "windows"])
def test_unsupported_kind_writes_nothing(home, kind):
    with pytest.raises(ValueError):
        sync_service.install(
            kind, home=home, env_file=home / "sync.env", activate=False, runner=FakeRunner()
        )
    assert list(home.iterdir()) == []


@pytest.mark.parametrize("path", ["relative", "/config/line\nbreak", "/config/nul\x00"])
@pytest.mark.parametrize("argument", ["python", "env_file"])
def test_invalid_paths_are_refused(path, argument):
    arguments = {"python": "/opt/python", "env_file": "/config/sync.env", argument: path}
    with pytest.raises(ValueError):
        sync_service.render("systemd", **arguments)


def test_replace_failure_preserves_existing_file_and_cleans_temporary(home, monkeypatch):
    path = sync_service.install(
        "systemd", home=home, env_file=home / "old.env", activate=False, runner=FakeRunner()
    )
    before = path.read_bytes()

    def failed(source, destination):
        raise OSError("synthetic replace failure")

    monkeypatch.setattr(os, "replace", failed)
    with pytest.raises(OSError):
        sync_service.install(
            "systemd", home=home, env_file=home / "new.env", activate=False, runner=FakeRunner()
        )
    assert path.read_bytes() == before
    assert list(path.parent.iterdir()) == [path]


@pytest.mark.parametrize("kind,other", [("systemd", "launchd"), ("launchd", "systemd")])
def test_per_kind_uninstall_skips_missing_and_leaves_other_service(home, kind, other):
    other_path = sync_service.install(
        other, home=home, env_file=home / "sync.env", activate=False, runner=FakeRunner()
    )
    runner = FakeRunner()
    sync_service.uninstall(kind, home=home, activate=True, runner=runner)
    assert runner.calls == []
    assert other_path.exists()
    path = sync_service.install(
        kind, home=home, env_file=home / "sync.env", activate=False, runner=FakeRunner()
    )
    sync_service.uninstall(kind, home=home, activate=True, runner=runner)
    assert not path.exists()
    assert other_path.exists()
    assert runner.calls[0][0] == ("systemctl" if kind == "systemd" else "launchctl")


def test_launchd_wrapper_loads_environment_and_preserves_arguments(home):
    env_file = home / 'quoted "$env.env'
    env_file.write_text("COMMONPLACE_IMPORT_TOKEN='synthetic value'\n")
    env_file.chmod(0o600)
    python = home / 'fake "$python'
    python.write_text('#!/bin/sh\nprintf "%s\\n" "$COMMONPLACE_IMPORT_TOKEN" "$@"\n')
    python.chmod(0o700)
    _, text = sync_service.render("launchd", python=python, env_file=env_file)
    argv = plistlib.loads(text.encode())["ProgramArguments"]
    result = subprocess.run(
        argv, check=True, capture_output=True, text=True, env={"HOME": str(home)}
    )
    assert result.stdout.splitlines() == ["synthetic value", "-m", "commonplace", "sync", "run"]


def test_launchd_wrapper_does_not_run_when_env_file_is_missing(home):
    marker = home / "unexpected-execution"
    python = home / "fake-python"
    python.write_text(f'#!/bin/sh\ntouch "{marker}"\n')
    python.chmod(0o700)
    _, text = sync_service.render("launchd", python=python, env_file=home / "missing.env")
    argv = plistlib.loads(text.encode())["ProgramArguments"]
    result = subprocess.run(argv, capture_output=True, env={"HOME": str(home)})
    assert result.returncode != 0
    assert not marker.exists()
