"""A drawer overwrite must have a complete, acknowledged prior revision."""

import errno
import json
import os
import subprocess
import sys

import pytest

from mempalace import revisions


@pytest.mark.skipif(os.name != "posix", reason="requires a POSIX file-size limit")
def test_real_short_write_does_not_acknowledge_a_torn_revision(tmp_path):
    script = """
import json, resource, signal, sys
from mempalace.revisions import append_drawer_revision, revisions_path
import mempalace.palace
signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
_, hard = resource.getrlimit(resource.RLIMIT_FSIZE)
resource.setrlimit(resource.RLIMIT_FSIZE, (64, hard))
try:
    append_drawer_revision(sys.argv[1], "d1", "prior verbatim text " * 1000)
    result = {"acknowledged": True}
except OSError:
    result = {"acknowledged": False}
result["bytes_written"] = revisions_path(sys.argv[1]).stat().st_size
print(json.dumps(result))
"""
    child = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "palace")],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    assert json.loads(child.stdout.splitlines()[-1]) == {
        "acknowledged": False,
        "bytes_written": 64,
    }


@pytest.mark.parametrize("failure", ["zero", "after_prefix", "fsync"])
def test_failed_revision_keeps_old_drawer_and_can_be_retried(
    monkeypatch, config, kg, palace_path, failure
):
    from mempalace import mcp_server
    from mempalace.palace import get_collection

    col = get_collection(palace_path)
    original = "Original café text.\r\nKeep this exactly."
    col.upsert(
        ids=["revision_failure"],
        documents=[original],
        metadatas=[{"wing": "w", "room": "r"}],
    )
    monkeypatch.setattr(mcp_server, "_config", config)
    monkeypatch.setattr(mcp_server, "_get_kg", lambda: kg)
    real_open, real_write, real_fsync = os.open, os.write, os.fsync
    revision_fd = None
    wrote_prefix = False

    def remember_open(path, flags, *args, **kwargs):
        nonlocal revision_fd
        fd = real_open(path, flags, *args, **kwargs)
        if str(path) == str(revisions.revisions_path(palace_path)):
            revision_fd = fd
        return fd

    def fail_write(fd, data):
        nonlocal wrote_prefix
        if fd == revision_fd and failure != "fsync":
            if failure == "zero":
                return 0
            if not wrote_prefix:
                wrote_prefix = True
                return real_write(fd, data[:13])
            raise OSError(errno.ENOSPC, "injected full revision device")
        return real_write(fd, data)

    def fail_fsync(fd):
        if fd == revision_fd and failure == "fsync":
            raise OSError(errno.EIO, "injected revision fsync failure")
        return real_fsync(fd)

    with monkeypatch.context() as patch:
        patch.setattr(os, "open", remember_open)
        patch.setattr(os, "write", fail_write)
        patch.setattr(os, "fsync", fail_fsync)
        result = mcp_server.tool_update_drawer("revision_failure", content="Replacement.")

    assert result["success"] is False
    assert "could not record prior drawer text" in result["error"]
    assert col.get(ids=["revision_failure"], include=["documents"])["documents"] == [original]

    retried = mcp_server.tool_update_drawer("revision_failure", content="Replacement.")
    assert retried["success"] is True
    assert col.get(ids=["revision_failure"], include=["documents"])["documents"] == ["Replacement."]
    saved = revisions.load_drawer_revisions(palace_path, "revision_failure")[-1]
    assert saved["content"] == original
    assert saved["revision_id"] == retried["revision_id"]


@pytest.mark.parametrize("write_size", [1, 7, 64])
def test_successive_short_writes_preserve_complete_unicode_revision(
    tmp_path, monkeypatch, write_size
):
    real_write = os.write
    monkeypatch.setattr(os, "write", lambda fd, data: real_write(fd, data[:write_size]))
    original = "café 🚀\r\nzero\x00 and stray \udcff"

    revision_id = revisions.append_drawer_revision(str(tmp_path), "d1", original)

    [saved] = revisions.load_drawer_revisions(str(tmp_path))
    assert saved["revision_id"] == revision_id
    assert saved["content"] == original
