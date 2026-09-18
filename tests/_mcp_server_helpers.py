"""Shared helpers for the MCP server tests (split from test_mcp_server.py)."""

import os

import pytest


def _patch_mcp_server(monkeypatch, config, kg):
    """Patch the mcp_server module globals to use test fixtures."""
    from mempalace import mcp_server

    monkeypatch.setattr(mcp_server, "_config", config)
    # Accept varargs because production ``_get_kg`` now takes an optional
    # canonical_path; ``_call_kg`` passes the captured key through.
    monkeypatch.setattr(mcp_server, "_get_kg", lambda *a, **kw: kg)
    monkeypatch.setattr(mcp_server, "_taxonomy_cache", None)
    monkeypatch.setattr(mcp_server, "_taxonomy_cache_time", 0.0)
    from mempalace.palace_graph import invalidate_graph_cache

    invalidate_graph_cache()


def _keep_server_command_line_state(monkeypatch):
    """Undo, after the test, whatever the server's command-line flags set.

    Running an entry point applies its flags to process-wide state on purpose,
    and a test that runs one must not leave them behind for the rest of the
    session. The flags' environment variables start the test unset.
    """
    from mempalace import mcp_server

    for name in (
        "_args",
        "_READ_ONLY",
        "_palace_flag_given",
        "_STALE_LIBRARY_WATCHED_DISTS",
        "_STARTUP_DIST_STATE",
        "_STARTUP_DIST_VERSIONS",
        "_STARTUP_DIST_ERRORS",
    ):
        monkeypatch.setattr(mcp_server, name, getattr(mcp_server, name))
    for name in ("MEMPALACE_PALACE_PATH", "MEMPALACE_BACKEND", "MEMPALACE_BACKEND_EXPLICIT"):
        # delenv() of an unset variable records nothing, so a value the entry
        # point sets would outlive the test. Setting it first records "unset".
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)


def _unexpected_client_read(*_a, **_k):
    """Tripwire for paths that must stay off the chroma client (and HNSW)."""
    raise AssertionError("chroma collection opened — this path must read sqlite")


def _get_collection(palace_path, create=False):
    """Helper to get collection from test palace.

    Returns (client, collection) so callers can clean up the client
    when they are done.
    """
    import chromadb

    client = chromadb.PersistentClient(path=palace_path)
    if create:
        return (
            client,
            client.get_or_create_collection("mempalace_drawers", metadata={"hnsw:space": "cosine"}),
        )
    return client, client.get_collection("mempalace_drawers")


# os.chmod on Windows only toggles the read-only attribute, so a file dropped to
# 0o000 there stays readable and the fault these cases construct never happens.
# Same reasoning as tests/test_daemon.py's _posix_only_perms.
_posix_only_perms = pytest.mark.skipif(
    os.name == "nt",
    reason="chmod cannot make a file unreadable on Windows (ACL-based permissions)",
)

# Path.symlink_to() raises WinError 1314 on the Windows runners without
# SeCreateSymbolicLinkPrivilege, before any product code runs. Same guard the
# rest of this suite uses (see the symlink tests above and tests/test_sync.py).
_needs_symlinks = pytest.mark.skipif(
    os.name == "nt",
    reason="symlink creation requires admin privileges on Windows runners",
)

# Making os.getcwd() raise is harmless on POSIX, where realpath() of an absolute
# path never calls it. On Windows ntpath.realpath does call it, and coverage.py
# calls realpath on every newly traced file, so a raising getcwd escapes into the
# tracer and ends the whole session with an INTERNALERROR instead of failing one
# test. The behaviour under test is platform-neutral; only the way of provoking
# it is not.
_posix_only_getcwd_patch = pytest.mark.skipif(
    os.name == "nt",
    reason="patching os.getcwd() breaks ntpath.realpath, which coverage.py calls while tracing",
)
