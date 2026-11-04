"""User-service rendering is explicit and never activates during acceptance."""

import plistlib

from tests.commonplace_sync_fixtures import (
    actors,
    home_tree,
    hub as hub,
    reached,
    reset_reached as reset_reached,
)


def test_install_enable_writes_user_service_without_activation(hub):
    h = hub()
    _, token, grant = actors(h)
    root = h.home.parent / "native"
    root.mkdir()
    E = h.env_file(token, grant)
    rc = h.cli(
        [
            "sync",
            "install",
            "--enable",
            "--source",
            f"claude-code:{root}",
            "--env-file",
            E,
            "--service",
            "systemd",
            "--no-activate",
            "--hub",
            h.origin,
        ]
    )
    assert rc == 0  # COUNTED:V1
    reached("V1")
    unit = h.home / ".config/systemd/user/commonplace-sync.service"
    text = unit.read_text()
    assert f"EnvironmentFile={E}" in text
    assert " -m commonplace sync run" in text
    assert "Restart=on-failure" in text and "WantedBy=default.target" in text
    assert token not in text and grant not in text
    assert not h.fake_log.exists()
    assert (
        h.cli(
            [
                "sync",
                "install",
                "--enable",
                "--source",
                f"claude-code:{root}",
                "--env-file",
                E,
                "--service",
                "launchd",
                "--no-activate",
                "--hub",
                h.origin,
            ]
        )
        == 0
    )
    plist = h.home / "Library/LaunchAgents/dev.commonplace.sync.plist"
    document = plistlib.loads(plist.read_bytes())
    assert document["Label"] == "dev.commonplace.sync"
    assert document["ProgramArguments"][-4:] == ["-m", "commonplace", "sync", "run"]
    assert E in plist.read_text()
    assert token.encode() not in plist.read_bytes() and grant.encode() not in plist.read_bytes()
    assert not h.fake_log.exists()
    before = home_tree(h.home)
    assert h.cli(["sync", "uninstall"]) == 0
    assert home_tree(h.home) == before
    assert h.cli(["sync", "uninstall", "--enable", "--no-activate"]) == 0
    assert not unit.exists() and not plist.exists()
    assert not h.fake_log.exists()
