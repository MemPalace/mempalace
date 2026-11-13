"""Verify the cross-platform privacy boundary before publishing state."""

import os
from types import SimpleNamespace

import pytest

from mempalace import private_state


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission assertion")
def test_owner_only_modes(tmp_path):
    directory = tmp_path / "state"
    directory.mkdir(mode=0o755)
    private_state.restrict_private(directory, directory=True)
    filename = directory / "data"
    filename.write_text("private")
    private_state.restrict_private(filename)
    assert directory.stat().st_mode & 0o777 == 0o700
    assert filename.stat().st_mode & 0o777 == 0o600


def test_windows_acl_is_explicit_and_verified(tmp_path, monkeypatch):
    identity = "HOST\\user"
    calls = []
    path = tmp_path / "state"
    monkeypatch.setattr(private_state, "_WINDOWS", True)

    def run(args, **kwargs):
        calls.append(args)
        if args == ["whoami"]:
            return SimpleNamespace(stdout=identity + "\n")
        return SimpleNamespace(stdout=f"{path} {identity}:(OI)(CI)(F)\n")

    monkeypatch.setattr(private_state.subprocess, "run", run)
    private_state.restrict_private(path, directory=True)
    assert ["icacls", str(path), "/inheritance:r"] in calls
    assert ["icacls", str(path), "/grant:r", identity + ":(OI)(CI)F"] in calls


@pytest.mark.parametrize(
    "identity, acl", [("", ""), ("HOST\\user", "OTHER:(F)"), ("HOST\\user", "HOST\\user:(I)(F)")]
)
def test_windows_permission_failure_is_not_silently_ignored(tmp_path, monkeypatch, identity, acl):
    monkeypatch.setattr(private_state, "_WINDOWS", True)
    monkeypatch.setattr(
        private_state.subprocess,
        "run",
        lambda args, **kwargs: SimpleNamespace(stdout=identity if args == ["whoami"] else acl),
    )
    with pytest.raises(PermissionError):
        private_state.restrict_private(tmp_path / "state")
