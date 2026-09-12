"""MCP server tests — protocol, dispatch, and session cache."""

import json
import os
import subprocess
import sys
from unittest.mock import MagicMock

import pytest

from _chroma_palace_helper import make_minimal_chroma_sqlite
from _mcp_server_helpers import (
    _get_collection,
    _patch_mcp_server,
)

# ── MCP entry point: PYTHONPATH stripping ────────────────────────────────


_MCP_LEAK_PREFIX = "/__mempalace_mcp_leak_sentinel__"


def test_mcp_main_strips_leaked_pythonpath_from_env():
    """mempalace.mcp_server:main must drop PYTHONPATH from the process env
    so any subprocess this server spawns starts clean. Mirrors the
    sys.path-filter test in test_init.py but for the env half of the
    split fix. See #1423.

    Three assertions cover the full split contract:
    - ENV_MID (after import, before main) is preserved verbatim:
      regression detector for someone moving the env pop back into
      __init__.py.
    - SENTINEL_IN_PATH is False at import time: package-level sys.path
      filter half of the split actually ran.
    - ENV_AFTER (after main) is None: MCP entry-point env strip ran.

    The main loop reads JSON-RPC lines from stdin until EOF; closing
    stdin makes readline() return '' and exits the loop cleanly, which
    lets us observe the post-main env state. Probes go to stderr because
    mcp_server redirects stdout at import time for clean JSON-RPC."""
    expected_env = f"{_MCP_LEAK_PREFIX}/a{os.pathsep}{_MCP_LEAK_PREFIX}/b"
    env = os.environ.copy()
    env["PYTHONPATH"] = expected_env
    code = (
        "import os, sys\n"
        "from mempalace.mcp_server import main\n"
        f"prefix = {_MCP_LEAK_PREFIX!r}\n"
        "sys.stderr.write('ENV_MID: ' + repr(os.environ.get('PYTHONPATH')) + '\\n')\n"
        "sys.stderr.write('SENTINEL_IN_PATH: ' + repr(any(prefix in (p or '') for p in sys.path)) + '\\n')\n"
        "main()\n"
        "sys.stderr.write('ENV_AFTER: ' + repr(os.environ.get('PYTHONPATH')) + '\\n')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        input="",  # empty stdin → readline() returns '' → loop breaks
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    diag = f"rc={result.returncode}; stdout={result.stdout!r}; stderr={result.stderr!r}"
    assert result.returncode == 0, f"subprocess failed: {diag}"
    assert f"ENV_MID: {expected_env!r}" in result.stderr, (
        f"package import unexpectedly stripped env (regression in __init__.py): {diag}"
    )
    assert "SENTINEL_IN_PATH: False" in result.stderr, (
        f"package import did not filter sys.path (regression in __init__.py): {diag}"
    )
    assert "ENV_AFTER: None" in result.stderr, f"MCP server did not strip PYTHONPATH: {diag}"


def test_install_shutdown_signal_handlers_routes_term_to_system_exit():
    """SIGTERM/SIGHUP must raise SystemExit so atexit can release the lease (#2205)."""
    import signal

    from mempalace import mcp_server

    previous = {}
    for name in ("SIGTERM", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        previous[sig] = signal.getsignal(sig)

    try:
        mcp_server._install_shutdown_signal_handlers()
        term = signal.SIGTERM
        handler = signal.getsignal(term)
        assert callable(handler)
        with pytest.raises(SystemExit) as exc_info:
            handler(term, None)
        assert exc_info.value.code == 0

        sighup = getattr(signal, "SIGHUP", None)
        if sighup is not None:
            hup_handler = signal.getsignal(sighup)
            assert callable(hup_handler)
            with pytest.raises(SystemExit) as exc_info:
                hup_handler(sighup, None)
            assert exc_info.value.code == 0
    finally:
        for sig, old in previous.items():
            signal.signal(sig, old)


# ── Cold-start diagnostics (#1495) ──────────────────────────────────────


class TestColdStartDiagnostics:
    """``MEMPALACE_LOG_FILE`` + ``MEMPALACE_EAGER_WARMUP`` (#1495).

    Each test runs ``main()`` in a fresh ``subprocess`` because

    * ``_init_logging`` configures logging only at module import, so each
      case needs a fresh interpreter to observe a pristine root logger and
      configure host logging *before* importing the server,
    * ``ChromaBackend._resolve_embedding_function`` is a class-level
      attribute that test monkeypatching mutates globally,
    * The whole point of the new env vars is process-startup behaviour
      and must be exercised under a real ``main()`` boot path.

    Pattern mirrors ``test_mcp_main_strips_leaked_pythonpath_from_env``.
    ``_run_main`` injects ``extra_code`` as a hard-coded ``-c`` source
    fragment from this file only (no untrusted input flows in); the
    subprocess argv form ``[sys.executable, "-c", code]`` avoids shell
    interpretation entirely.
    """

    @staticmethod
    def _run_main(env_overrides: dict, extra_code: str = "", timeout: int = 30):
        env = {
            k: v
            for k, v in os.environ.items()
            if k not in env_overrides or env_overrides[k] is not None
        }
        for k, v in env_overrides.items():
            if v is None:
                env.pop(k, None)
            else:
                env[k] = v
        code = extra_code + "from mempalace.mcp_server import main\nmain()\n"
        return subprocess.run(
            [sys.executable, "-c", code],
            env=env,
            input="",  # empty stdin → readline() returns '' → loop breaks immediately
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )

    def test_log_file_unset_attaches_only_stream_handler(self, tmp_path):
        marker = tmp_path / "handlers.txt"
        env_overrides = {"MEMPALACE_LOG_FILE": None}
        extra = (
            "import logging, pathlib\n"
            "from mempalace import mcp_server  # noqa: F401 — triggers _init_logging()\n"
            f"pathlib.Path({str(marker)!r}).write_text("
            "','.join(type(h).__name__ for h in logging.getLogger().handlers)"
            ")\n"
            "raise SystemExit(0)\n"
        )
        result = self._run_main(env_overrides, extra_code=extra)
        assert result.returncode == 0, f"stderr={result.stderr!r}"
        assert marker.read_text().split(",") == ["StreamHandler"], marker.read_text()

    def test_log_file_empty_string_attaches_only_stream_handler(self, tmp_path):
        marker = tmp_path / "handlers.txt"
        env_overrides = {"MEMPALACE_LOG_FILE": "   "}  # whitespace counts as unset after .strip()
        extra = (
            "import logging, pathlib\n"
            "from mempalace import mcp_server  # noqa: F401\n"
            f"pathlib.Path({str(marker)!r}).write_text("
            "','.join(type(h).__name__ for h in logging.getLogger().handlers)"
            ")\n"
            "raise SystemExit(0)\n"
        )
        result = self._run_main(env_overrides, extra_code=extra)
        assert result.returncode == 0, f"stderr={result.stderr!r}"
        assert marker.read_text().split(",") == ["StreamHandler"], marker.read_text()

    def test_log_file_set_attaches_file_handler_and_persists_startup_line(self, tmp_path):
        log_path = tmp_path / "mcp.log"
        result = self._run_main({"MEMPALACE_LOG_FILE": str(log_path)})
        assert result.returncode == 0, f"stderr={result.stderr!r}"
        assert log_path.exists(), f"log file missing; stderr={result.stderr!r}"
        body = log_path.read_text(encoding="utf-8")
        assert "MemPalace MCP Server starting" in body, body

    def test_log_file_invalid_path_falls_back_to_stderr_with_warning(self, tmp_path):
        # Unique directory name we can grep for cross-platform without
        # depending on path-separator formatting in the %r warning value.
        missing_dir = "missing_dir_for_1495"
        bad_path = tmp_path / missing_dir / "mcp.log"
        result = self._run_main({"MEMPALACE_LOG_FILE": str(bad_path)})
        assert result.returncode == 0, f"stderr={result.stderr!r}"
        # Invalid path must NOT crash the server, must surface a warning, must
        # NOT create the file (the missing-directory ancestor is the failure).
        # Warning must name MEMPALACE_LOG_FILE so the operator knows the source.
        assert "could not be opened" in result.stderr, result.stderr
        assert "MEMPALACE_LOG_FILE" in result.stderr, result.stderr
        assert missing_dir in result.stderr, result.stderr
        assert not bad_path.exists()

    @staticmethod
    def _make_fake_palace(tmp_path):
        """Create just enough on disk for ``_maybe_eager_warmup_embedder``'s
        fresh-install pre-check to pass (``chroma.sqlite3`` exists).

        Returns the palace dir as a string. The file is empty — production
        code must not read its bytes during pre-check; only its existence
        gates whether warmup proceeds to the chromadb client open.
        """
        palace = tmp_path / "palace"
        palace.mkdir()
        make_minimal_chroma_sqlite(palace)
        return str(palace)

    @staticmethod
    def _spy_get_collection_extra(marker_path, return_expr="None"):
        """Render an ``extra_code`` fragment that monkeypatches ``_get_collection``.

        ``marker_path`` records that the spy fired; ``return_expr`` is a Python
        expression evaluated inside the subprocess for the call's return value
        (e.g. ``"None"`` or ``"_FakeCol()"``).
        """
        return (
            "import pathlib\n"
            "from mempalace import mcp_server\n"
            "def _spy_get_collection(create=False):\n"
            f"    pathlib.Path({str(marker_path)!r}).write_text('called')\n"
            f"    return {return_expr}\n"
            "mcp_server._get_collection = _spy_get_collection\n"
        )

    def test_eager_warmup_off_by_default_does_not_open_collection(self, tmp_path):
        marker = tmp_path / "called.txt"
        palace = self._make_fake_palace(tmp_path)
        result = self._run_main(
            {"MEMPALACE_EAGER_WARMUP": None, "MEMPALACE_PALACE_PATH": palace},
            extra_code=self._spy_get_collection_extra(marker),
        )
        assert result.returncode == 0, f"stderr={result.stderr!r}"
        assert not marker.exists(), "warmup ran despite env var being unset"

    @pytest.mark.parametrize("value", ["0", "false", "no", "off", "FALSE"])
    def test_eager_warmup_explicit_falsy_skips_collection_open_without_warning(
        self, tmp_path, value
    ):
        marker = tmp_path / "called.txt"
        palace = self._make_fake_palace(tmp_path)
        result = self._run_main(
            {"MEMPALACE_EAGER_WARMUP": value, "MEMPALACE_PALACE_PATH": palace},
            extra_code=self._spy_get_collection_extra(marker),
        )
        assert result.returncode == 0, f"stderr={result.stderr!r}"
        assert not marker.exists(), f"warmup ran for explicit-falsy value {value!r}"
        assert "not recognized" not in result.stderr, (
            f"explicit-falsy {value!r} should not log a warning; stderr={result.stderr!r}"
        )

    @pytest.mark.parametrize("value", ["tru", "maybe", "ENABLED", "2"])
    def test_eager_warmup_unrecognized_value_warns_and_skips_collection_open(self, tmp_path, value):
        marker = tmp_path / "called.txt"
        palace = self._make_fake_palace(tmp_path)
        result = self._run_main(
            {"MEMPALACE_EAGER_WARMUP": value, "MEMPALACE_PALACE_PATH": palace},
            extra_code=self._spy_get_collection_extra(marker),
        )
        assert result.returncode == 0, f"stderr={result.stderr!r}"
        assert not marker.exists(), f"warmup ran despite unrecognized value {value!r}"
        assert "not recognized" in result.stderr, result.stderr

    @pytest.mark.parametrize("value", ["1", "true", "YES", "On"])
    def test_eager_warmup_truthy_opens_collection_and_invokes_query(self, tmp_path, value):
        """C1 (#1495): warmup must call ``col.query(...)`` — not just open the collection.

        ChromaDB's ``ONNXMiniLM_L6_V2.__init__`` only imports ``onnxruntime``;
        ``InferenceSession`` and model download happen inside ``__call__``,
        which the chromadb query path drives. Pinning both call sites here
        prevents a regression to a no-op resolver-only warmup (the same
        failure mode silent-failure-hunter flagged in initial review).
        Reporter's #1495 proposal: same path covers HNSW cold-load too.
        """
        open_marker = tmp_path / "open_called.txt"
        query_marker = tmp_path / "query_called.txt"
        palace = self._make_fake_palace(tmp_path)
        extra = (
            "import pathlib\n"
            "from mempalace import mcp_server\n"
            "class _FakeCol:\n"
            "    def query(self, **kwargs):\n"
            f"        pathlib.Path({str(query_marker)!r}).write_text(repr(kwargs))\n"
            "        return {'ids': [[]], 'distances': [[]], 'documents': [[]]}\n"
            "_fake_col = _FakeCol()\n"
            "def _spy_get_collection(create=False):\n"
            f"    pathlib.Path({str(open_marker)!r}).write_text('open')\n"
            "    return _fake_col\n"
            "mcp_server._get_collection = _spy_get_collection\n"
        )
        result = self._run_main(
            {"MEMPALACE_EAGER_WARMUP": value, "MEMPALACE_PALACE_PATH": palace},
            extra_code=extra,
        )
        assert result.returncode == 0, f"stderr={result.stderr!r}"
        assert open_marker.exists(), (
            f"_get_collection not called for {value!r}; stderr={result.stderr!r}"
        )
        assert query_marker.exists(), (
            f"col.query not invoked for {value!r} — warmup is a no-op "
            f"(would let cold-load hit first MCP call); stderr={result.stderr!r}"
        )
        # query was called with the sentinel probe text and n_results=1.
        kwargs_repr = query_marker.read_text()
        assert "__mempalace_warmup_probe__" in kwargs_repr, kwargs_repr
        assert "n_results" in kwargs_repr and "1" in kwargs_repr, kwargs_repr
        # Success path logs embedder + HNSW readiness + palace + device for ops.
        assert "embedder + HNSW ready" in result.stderr, result.stderr
        assert f"palace={palace}" in result.stderr, result.stderr

    def test_eager_warmup_fresh_install_skips_without_creating_palace(self, tmp_path):
        """Real integration test (no monkeypatch): an empty palace dir with no
        ``chroma.sqlite3`` must trigger the pre-check skip path BEFORE any
        chromadb call materializes the palace scaffold on disk.

        This pins three behaviours simultaneously:

        1. ``returncode == 0`` — fresh install does not crash the server.
        2. ``chroma.sqlite3`` is NOT created — warmup respects the
           "no on-disk state before ``mempalace init``" contract from
           CLAUDE.md ("Incremental only"). A regression that drops the
           pre-check would let chromadb's ``PersistentClient(path=...)``
           materialize the palace dir.
        3. ``"nothing to warm"`` lands in stderr — the documented INFO
           message actually fires (the previous test that asserted this
           via a monkeypatched ``_get_collection`` was tautological because
           the real ``_get_collection`` swallows ``NotFoundError`` into
           ``return None`` and silently materializes the palace).
        4. No chromadb retry tracebacks ("attempt N/2 failed") leak into
           stderr — those are the noise this PR exists to reduce.
        """
        palace = tmp_path / "fresh_palace"
        palace.mkdir()
        # Confirm precondition: no chroma.sqlite3 exists before main().
        db_path = palace / "chroma.sqlite3"
        assert not db_path.exists()
        result = self._run_main(
            {"MEMPALACE_EAGER_WARMUP": "1", "MEMPALACE_PALACE_PATH": str(palace)},
        )
        assert result.returncode == 0, f"stderr={result.stderr!r}"
        assert "nothing to warm" in result.stderr, result.stderr
        assert "collection open failed" not in result.stderr, result.stderr
        assert "warmup query failed" not in result.stderr, result.stderr
        assert "embedder + HNSW ready" not in result.stderr, result.stderr
        assert "attempt 1/2 failed" not in result.stderr, result.stderr
        assert "attempt 2/2 failed" not in result.stderr, result.stderr
        # Pin the no-side-effect contract: the warmup MUST NOT create the
        # palace scaffold on disk before the user runs ``mempalace init``.
        assert not db_path.exists(), (
            f"warmup materialized chroma.sqlite3 in a fresh palace dir "
            f"(violates 'Incremental only' from CLAUDE.md); stderr={result.stderr!r}"
        )

    def test_eager_warmup_collection_returning_none_surfaces_warning(self, tmp_path):
        """_get_collection retries internally and returns None on persistent
        failure (mcp_server.py:373). Warmup must not log a misleading
        success line in that case."""
        palace = self._make_fake_palace(tmp_path)
        extra = self._spy_get_collection_extra(tmp_path / "called.txt", return_expr="None")
        result = self._run_main(
            {"MEMPALACE_EAGER_WARMUP": "1", "MEMPALACE_PALACE_PATH": palace},
            extra_code=extra,
        )
        assert result.returncode == 0, f"stderr={result.stderr!r}"
        assert "_get_collection returned None" in result.stderr, result.stderr
        assert "embedder + HNSW ready" not in result.stderr, result.stderr

    def test_eager_warmup_collection_open_failure_logs_and_does_not_block_server(self, tmp_path):
        palace = self._make_fake_palace(tmp_path)
        extra = (
            "from mempalace import mcp_server\n"
            "def _boom(create=False):\n"
            "    raise RuntimeError('synthetic-collection-open-fail-1495')\n"
            "mcp_server._get_collection = _boom\n"
        )
        result = self._run_main(
            {"MEMPALACE_EAGER_WARMUP": "1", "MEMPALACE_PALACE_PATH": palace},
            extra_code=extra,
        )
        assert result.returncode == 0, f"stderr={result.stderr!r}"
        assert "collection open failed" in result.stderr, result.stderr
        assert "synthetic-collection-open-fail-1495" in result.stderr, result.stderr
        # palace + error class included in the diagnostic
        assert f"palace={palace}" in result.stderr, result.stderr
        assert "error=RuntimeError" in result.stderr, result.stderr

    def test_eager_warmup_query_failure_logs_and_persists_to_log_file(self, tmp_path):
        """Query may raise (broken HNSW, network failure during ONNX download,
        runtime decoder error). Server stays up and the diagnostic lands in
        both stderr AND ``MEMPALACE_LOG_FILE`` — the latter is the whole
        point of #1495 for ops debugging the original -32000."""
        palace = self._make_fake_palace(tmp_path)
        log_path = tmp_path / "mcp.log"
        extra = (
            "from mempalace import mcp_server\n"
            "class _BadCol:\n"
            "    def query(self, **kwargs):\n"
            "        raise RuntimeError('synthetic-query-fail-1495')\n"
            "mcp_server._get_collection = lambda create=False: _BadCol()\n"
        )
        result = self._run_main(
            {
                "MEMPALACE_EAGER_WARMUP": "1",
                "MEMPALACE_PALACE_PATH": palace,
                "MEMPALACE_LOG_FILE": str(log_path),
            },
            extra_code=extra,
        )
        assert result.returncode == 0, f"stderr={result.stderr!r}"
        assert "warmup query failed" in result.stderr, result.stderr
        assert "synthetic-query-fail-1495" in result.stderr, result.stderr
        assert f"palace={palace}" in result.stderr, result.stderr
        assert "error=RuntimeError" in result.stderr, result.stderr
        assert log_path.exists(), f"log file not created; stderr={result.stderr!r}"
        body = log_path.read_text(encoding="utf-8")
        assert "warmup query failed" in body, body
        assert "synthetic-query-fail-1495" in body, body

    def test_log_file_path_with_embedded_newline_does_not_crash(self, tmp_path):
        """``MEMPALACE_LOG_FILE`` containing a newline (rare misconfig from
        a YAML/env file copy-paste) must fall through the (OSError, ValueError)
        catch rather than escape as an unhandled exception at import time."""
        # Embedding \n inside a path component triggers ValueError on POSIX
        # ("embedded null byte" raises on OS-level open) or OSError depending
        # on platform — both should land in the fail-soft branch.
        bad_path = str(tmp_path / "with\nnewline" / "mcp.log")
        result = self._run_main({"MEMPALACE_LOG_FILE": bad_path})
        assert result.returncode == 0, f"stderr={result.stderr!r}"
        # Server proceeds with stderr-only and surfaces the env-var-named
        # warning so ops can correlate the misconfig.
        assert "could not be opened" in result.stderr, result.stderr
        assert "MEMPALACE_LOG_FILE" in result.stderr, result.stderr

    def test_log_file_invalid_path_failure_surfaces_before_first_log_record(self, tmp_path):
        """Behavioural pin: ``delay=True`` MUST NOT be used on the FileHandler.

        With ``delay=True`` an invalid path raises inside ``emit()`` at runtime,
        unhandled, defeating the fail-soft contract documented in ``_init_logging``.
        This test pins the eager-open semantics by checking that the warning lands
        BEFORE the ``MemPalace MCP Server starting...`` banner — proving that
        ``FileHandler.__init__`` raised and was caught at module import."""
        bad_path = tmp_path / "regression_pin_dir" / "mcp.log"
        result = self._run_main({"MEMPALACE_LOG_FILE": str(bad_path)})
        assert result.returncode == 0, f"stderr={result.stderr!r}"
        warning_pos = result.stderr.find("could not be opened")
        banner_pos = result.stderr.find("MemPalace MCP Server starting")
        assert warning_pos != -1, f"warning missing; stderr={result.stderr!r}"
        assert banner_pos != -1, f"banner missing; stderr={result.stderr!r}"
        assert warning_pos < banner_pos, (
            f"warning at {warning_pos} must precede banner at {banner_pos} — "
            f"if banner is first, FileHandler was opened lazily (delay=True regression). "
            f"stderr={result.stderr!r}"
        )

    def test_host_root_logger_config_survives_import(self, tmp_path):
        """#1860: importing the server must NOT clobber a host app's root
        logger. ``_init_logging`` previously called
        ``logging.basicConfig(force=True)`` at import, resetting root's
        level, format, and handlers — silently overriding any app that
        configured logging before importing ``mempalace.mcp_server``."""
        marker = tmp_path / "rootstate.txt"
        extra = (
            "import logging, pathlib\n"
            # Host app configures logging BEFORE importing mempalace.
            "logging.basicConfig(level=logging.DEBUG, "
            "format='HOST %(levelname)s %(message)s')\n"
            "_sentinel = logging.NullHandler()\n"
            "logging.getLogger().addHandler(_sentinel)\n"
            "from mempalace import mcp_server  # noqa: F401 — triggers _init_logging()\n"
            "_root = logging.getLogger()\n"
            "_fmt = next((h.formatter._fmt for h in _root.handlers "
            "if h.formatter is not None), None)\n"
            f"pathlib.Path({str(marker)!r}).write_text(\n"
            "    f'level={logging.getLevelName(_root.level)}|'\n"
            "    f'sentinel={_sentinel in _root.handlers}|'\n"
            "    f'nhandlers={len(_root.handlers)}|'\n"
            "    f'fmt={_fmt!r}'\n"
            ")\n"
            "raise SystemExit(0)\n"
        )
        result = self._run_main({"MEMPALACE_LOG_FILE": None}, extra_code=extra)
        assert result.returncode == 0, f"stderr={result.stderr!r}"
        state = marker.read_text()
        # Root logger must remain exactly as the host configured it.
        assert "level=DEBUG" in state, state
        assert "sentinel=True" in state, state
        # MEMPALACE_LOG_FILE unset + host owns root → mempalace adds no handler.
        assert "nhandlers=2" in state, state
        assert "fmt='HOST %(levelname)s %(message)s'" in state, state

    def test_log_file_with_host_root_captures_mempalace_only(self, tmp_path):
        """#1860 + #1495: when a host app owns the root logger and
        MEMPALACE_LOG_FILE is set, the file still captures mempalace's own
        records — including the dotted ``mempalace.*`` family (the cold-load
        path) — but NOT the host's. Proves the additive, mempalace-filtered
        file handler: a naive 'reset root' or 'single dedicated logger' fix
        would either leak host logs into the file or drop the dotted family."""
        log_path = tmp_path / "mcp.log"
        extra = (
            "import logging\n"
            # Host owns root logging before the import.
            "logging.basicConfig(level=logging.DEBUG, format='%(message)s')\n"
            "from mempalace import mcp_server  # noqa: F401 — triggers _init_logging()\n"
            "logging.getLogger('host.app').warning('HOST-ONLY-LINE-xyz')\n"
            "logging.getLogger('mempalace.embedding').info('MEMPALACE-DOTTED-LINE-xyz')\n"
            "logging.getLogger('mempalace_mcp').info('MEMPALACE-FLAT-LINE-xyz')\n"
            "logging.shutdown()\n"
            "raise SystemExit(0)\n"
        )
        result = self._run_main({"MEMPALACE_LOG_FILE": str(log_path)}, extra_code=extra)
        assert result.returncode == 0, f"stderr={result.stderr!r}"
        assert log_path.exists(), f"log file missing; stderr={result.stderr!r}"
        body = log_path.read_text(encoding="utf-8")
        assert "MEMPALACE-DOTTED-LINE-xyz" in body, body
        assert "MEMPALACE-FLAT-LINE-xyz" in body, body
        assert "HOST-ONLY-LINE-xyz" not in body, body
        # Format is "%(message)s" in the embedded path too: the line is the bare
        # message with no "LEVEL:name:" prefix (the file handler sets its own
        # formatter, independent of basicConfig which never runs here).
        assert any(line == "MEMPALACE-FLAT-LINE-xyz" for line in body.splitlines()), body

    def test_embedded_host_warning_root_gates_mempalace_info(self, tmp_path):
        """Documents the intentional embedded-mode level-gating tradeoff: when
        a host owns root at WARNING, mempalace INFO heartbeats do NOT reach
        MEMPALACE_LOG_FILE (the file handler rides on the host-gated root), but
        WARNING/ERROR cold-load failure diagnostics still do. #1860 never
        raises the host's level; #1495's motivating case is a standalone launch
        (root empty -> INFO pinned) and is unaffected."""
        log_path = tmp_path / "mcp.log"
        extra = (
            "import logging\n"
            "logging.basicConfig(level=logging.WARNING, format='%(message)s')\n"
            "from mempalace import mcp_server  # noqa: F401 — triggers _init_logging()\n"
            "logging.getLogger('mempalace_mcp').info('INFO-HEARTBEAT-xyz')\n"
            "logging.getLogger('mempalace_mcp').warning('WARN-DIAG-xyz')\n"
            "logging.shutdown()\n"
            "raise SystemExit(0)\n"
        )
        result = self._run_main({"MEMPALACE_LOG_FILE": str(log_path)}, extra_code=extra)
        assert result.returncode == 0, f"stderr={result.stderr!r}"
        body = log_path.read_text(encoding="utf-8")
        assert "WARN-DIAG-xyz" in body, body
        assert "INFO-HEARTBEAT-xyz" not in body, body

    def test_standalone_log_file_excludes_third_party_records(self, tmp_path):
        """The MEMPALACE_LOG_FILE stream is mempalace-only in standalone mode
        too: third-party library records reaching the root logger are kept out
        of the file by ``_MempalaceLogFilter`` (the file stays a clean
        mempalace diagnostic stream)."""
        log_path = tmp_path / "mcp.log"
        extra = (
            "import logging\n"
            "from mempalace import mcp_server  # noqa: F401 — standalone: root starts empty\n"
            "logging.getLogger('chromadb.fake').warning('THIRDPARTY-LINE-xyz')\n"
            "logging.getLogger('mempalace.embedding').info('MEMPALACE-STD-LINE-xyz')\n"
            "logging.shutdown()\n"
            "raise SystemExit(0)\n"
        )
        result = self._run_main({"MEMPALACE_LOG_FILE": str(log_path)}, extra_code=extra)
        assert result.returncode == 0, f"stderr={result.stderr!r}"
        body = log_path.read_text(encoding="utf-8")
        assert "MEMPALACE-STD-LINE-xyz" in body, body
        assert "THIRDPARTY-LINE-xyz" not in body, body

    def test_reload_does_not_duplicate_file_handler(self, tmp_path):
        """#1885 review: the idempotency guard must survive ``importlib.reload``,
        not only a direct second call. A reload re-executes the module body; the
        guard flag is restored from ``globals()`` so ``_init_logging`` early-exits
        and does not stack a second ``FileHandler`` on root."""
        log_path = tmp_path / "mcp.log"
        marker = tmp_path / "counts.txt"
        extra = (
            "import logging, importlib, pathlib\n"
            "from mempalace import mcp_server\n"
            "def _nfile():\n"
            "    return sum(\n"
            "        isinstance(h, logging.FileHandler)\n"
            "        for h in logging.getLogger().handlers\n"
            "    )\n"
            "_before = _nfile()\n"
            "importlib.reload(mcp_server)\n"
            "_after = _nfile()\n"
            f"pathlib.Path({str(marker)!r}).write_text(f'{{_before}},{{_after}}')\n"
            "raise SystemExit(0)\n"
        )
        result = self._run_main({"MEMPALACE_LOG_FILE": str(log_path)}, extra_code=extra)
        assert result.returncode == 0, f"stderr={result.stderr!r}"
        before, after = marker.read_text().split(",")
        assert before == "1", f"expected one file handler after import, got {before}"
        assert after == "1", f"reload duplicated the file handler: {before}->{after}"


# ── Protocol Layer ──────────────────────────────────────────────────────


class TestHandleRequest:
    def test_initialize(self):
        from mempalace.mcp_server import handle_request

        resp = handle_request({"method": "initialize", "id": 1, "params": {}})
        assert resp["result"]["serverInfo"]["name"] == "mempalace"
        assert resp["id"] == 1

    def test_initialize_negotiates_client_version(self):
        from mempalace.mcp_server import handle_request

        resp = handle_request(
            {
                "method": "initialize",
                "id": 1,
                "params": {"protocolVersion": "2025-11-25"},
            }
        )
        assert resp["result"]["protocolVersion"] == "2025-11-25"

    def test_initialize_negotiates_older_supported_version(self):
        from mempalace.mcp_server import handle_request

        resp = handle_request(
            {
                "method": "initialize",
                "id": 1,
                "params": {"protocolVersion": "2025-03-26"},
            }
        )
        assert resp["result"]["protocolVersion"] == "2025-03-26"

    def test_initialize_unknown_version_falls_back_to_latest(self):
        from mempalace.mcp_server import handle_request

        resp = handle_request(
            {
                "method": "initialize",
                "id": 1,
                "params": {"protocolVersion": "9999-12-31"},
            }
        )
        from mempalace.mcp_server import SUPPORTED_PROTOCOL_VERSIONS

        assert resp["result"]["protocolVersion"] == SUPPORTED_PROTOCOL_VERSIONS[0]

    def test_initialize_missing_version_uses_oldest(self):
        from mempalace.mcp_server import handle_request, SUPPORTED_PROTOCOL_VERSIONS

        resp = handle_request({"method": "initialize", "id": 1, "params": {}})
        assert resp["result"]["protocolVersion"] == SUPPORTED_PROTOCOL_VERSIONS[-1]

    def test_notifications_initialized_returns_none(self):
        from mempalace.mcp_server import handle_request

        resp = handle_request({"method": "notifications/initialized", "id": None, "params": {}})
        assert resp is None

    def test_ping_returns_empty_result(self):
        from mempalace.mcp_server import handle_request

        resp = handle_request({"method": "ping", "id": 11, "params": {}})
        assert resp["id"] == 11
        assert resp["result"] == {}

    def test_tools_list(self):
        from mempalace.mcp_server import handle_request

        resp = handle_request({"method": "tools/list", "id": 2, "params": {}})
        tools = resp["result"]["tools"]
        names = {t["name"] for t in tools}
        assert "mempalace_status" in names
        assert "mempalace_search" in names
        assert "mempalace_add_drawer" in names
        assert "mempalace_kg_add" in names

    def test_no_tool_schema_uses_top_level_combinator(self):
        """Anthropic's Messages API rejects a tool whose input schema has a
        top-level anyOf/oneOf/allOf and drops the entire tools array with a
        400, killing the session (#1711). Cross-tool constraints must be
        enforced at dispatch instead.
        """
        from mempalace.mcp_server import handle_request

        resp = handle_request({"method": "tools/list", "id": 2, "params": {}})
        for tool in resp["result"]["tools"]:
            schema = tool["inputSchema"]
            for keyword in ("anyOf", "oneOf", "allOf"):
                assert keyword not in schema, f"{tool['name']} schema has top-level {keyword}"

    def test_null_arguments_does_not_hang(self, monkeypatch, config, palace_path, seeded_kg):
        """Sending arguments: null should return a result, not hang (#394)."""
        _patch_mcp_server(monkeypatch, config, seeded_kg)
        from mempalace.mcp_server import handle_request

        _client, _col = _get_collection(palace_path, create=True)
        del _client
        resp = handle_request(
            {
                "method": "tools/call",
                "id": 10,
                "params": {"name": "mempalace_status", "arguments": None},
            }
        )
        assert "error" not in resp
        assert resp["result"] is not None

    def test_unknown_tool(self):
        from mempalace.mcp_server import handle_request

        resp = handle_request(
            {
                "method": "tools/call",
                "id": 3,
                "params": {"name": "nonexistent_tool", "arguments": {}},
            }
        )
        assert resp["error"]["code"] == -32601

    def test_tools_call_missing_params(self):
        from mempalace.mcp_server import handle_request

        for bad_params in [None, {}, {"arguments": {}}]:
            resp = handle_request(
                {
                    "method": "tools/call",
                    "id": 15,
                    "params": bad_params,
                }
            )
            assert resp["error"]["code"] == -32602
            assert "Invalid params" in resp["error"]["message"]

    def test_unknown_method(self):
        from mempalace.mcp_server import handle_request

        resp = handle_request({"method": "unknown/method", "id": 4, "params": {}})
        assert resp["error"]["code"] == -32601

    def test_any_notification_returns_none(self):
        """All notifications/* methods should return None (no response)."""
        from mempalace.mcp_server import handle_request

        for method in [
            "notifications/initialized",
            "notifications/cancelled",
            "notifications/progress",
            "notifications/roots/list_changed",
        ]:
            resp = handle_request({"method": method, "params": {}})
            assert resp is None, f"{method} should return None"

    def test_unknown_method_no_id_returns_none(self):
        """Messages without id (notifications) must never get a response."""
        from mempalace.mcp_server import handle_request

        resp = handle_request({"method": "unknown/thing", "params": {}})
        assert resp is None

    def test_malformed_method_none(self):
        """method=None or missing should not crash."""
        from mempalace.mcp_server import handle_request

        # Explicit None
        resp = handle_request({"method": None, "params": {}})
        assert resp is None  # no id → no response

        # Missing method entirely
        resp = handle_request({"params": {}})
        assert resp is None

        # method=None with id → should return error, not crash
        resp = handle_request({"method": None, "id": 99, "params": {}})
        assert resp["error"]["code"] == -32601

    @pytest.mark.parametrize("payload", [None, [], "plain", 42, True])
    def test_handle_request_invalid_payload_returns_jsonrpc_error(self, payload):
        from mempalace.mcp_server import handle_request

        resp = handle_request(payload)
        assert resp == {
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": -32600, "message": "Invalid Request"},
        }

    def test_tools_call_dispatches(self, monkeypatch, config, palace_path, seeded_kg):
        _patch_mcp_server(monkeypatch, config, seeded_kg)
        from mempalace.mcp_server import handle_request

        # Create a collection so status works
        _client, _col = _get_collection(palace_path, create=True)
        del _client

        resp = handle_request(
            {
                "method": "tools/call",
                "id": 5,
                "params": {"name": "mempalace_status", "arguments": {}},
            }
        )
        assert "result" in resp
        content = json.loads(resp["result"]["content"][0]["text"])
        assert "total_drawers" in content


# ── Read Tools ──────────────────────────────────────────────────────────


class TestCacheInvalidation:
    """Tests for _get_collection inode/mtime cache invalidation logic."""

    def test_mtime_change_invalidates_cache(self, monkeypatch, config, palace_path, kg):
        """When mtime changes, the cached collection should be replaced."""
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace import mcp_server

        # Create a real collection so _get_collection succeeds
        _client, _col = _get_collection(palace_path, create=True)
        del _client

        # Prime the cache
        col1 = mcp_server._get_collection()
        assert col1 is not None

        # Simulate an external write changing the mtime
        old_mtime = mcp_server._palace_db_mtime
        monkeypatch.setattr(mcp_server, "_palace_db_mtime", old_mtime - 10.0)

        # _get_collection should detect the mtime drift and reconnect
        col2 = mcp_server._get_collection()
        assert col2 is not None

    def test_inode_change_invalidates_cache(self, monkeypatch, config, palace_path, kg):
        """When inode changes (file replaced), the cached collection should be replaced."""
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace import mcp_server

        _client, _col = _get_collection(palace_path, create=True)
        del _client

        # Prime the cache
        col1 = mcp_server._get_collection()
        assert col1 is not None

        # Simulate a rebuild that changes the inode
        monkeypatch.setattr(mcp_server, "_palace_db_inode", 99999)

        col2 = mcp_server._get_collection()
        assert col2 is not None

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="Windows holds chroma.sqlite3 open while the client is cached, blocking os.remove",
    )
    def test_missing_db_invalidates_cache(self, monkeypatch, config, palace_path, kg):
        """When chroma.sqlite3 disappears, a cached collection should be invalidated."""
        _patch_mcp_server(monkeypatch, config, kg)
        import os
        from mempalace import mcp_server

        _client, _col = _get_collection(palace_path, create=True)
        del _client

        # Prime the cache
        col1 = mcp_server._get_collection()
        assert col1 is not None
        assert mcp_server._collection_cache is not None

        # Delete the DB file to simulate a rebuild in progress
        db_file = os.path.join(palace_path, "chroma.sqlite3")
        if os.path.isfile(db_file):
            os.remove(db_file)

        make_client_calls = []

        def fail_if_make_client_called(path):
            make_client_calls.append(path)
            raise AssertionError("_get_collection(create=False) should not open missing Chroma DB")

        monkeypatch.setattr(mcp_server.ChromaBackend, "make_client", fail_if_make_client_called)

        # Cache should be invalidated; _get_collection returns None
        # because the backend can't open a missing DB without create=True
        assert mcp_server._get_collection() is None
        # The key assertion: the old cached collection was dropped
        assert make_client_calls == []
        assert mcp_server._collection_cache is None
        assert mcp_server._palace_db_inode == 0
        assert mcp_server._palace_db_mtime == 0.0

    def test_reconnect_reports_failure_when_no_palace(self, monkeypatch, config, kg):
        """tool_reconnect should report failure when no collection is available."""
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace import mcp_server

        # Make _get_collection always return None
        monkeypatch.setattr(mcp_server, "_get_collection", lambda create=False: None)

        result = mcp_server.tool_reconnect()
        assert result["success"] is False
        assert "No palace found" in result["message"]
        assert result["drawers"] == 0

    def test_reconnect_reports_success(self, monkeypatch, config, palace_path, kg):
        """tool_reconnect should report success with drawer count."""
        _patch_mcp_server(monkeypatch, config, kg)
        _client, _col = _get_collection(palace_path, create=True)
        del _client
        from mempalace import mcp_server

        result = mcp_server.tool_reconnect()
        assert result["success"] is True
        assert "Reconnected" in result["message"]
        assert isinstance(result["drawers"], int)

    def test_reconnect_closes_shared_backend(self, monkeypatch, config, kg):
        _patch_mcp_server(monkeypatch, config, kg)
        from unittest.mock import MagicMock

        from mempalace import mcp_server, palace

        close_palace = MagicMock()
        monkeypatch.setattr(palace._DEFAULT_BACKEND, "close_palace", close_palace)

        class _FakeCol:
            def count(self):
                return 7

        monkeypatch.setattr(mcp_server, "_get_collection", lambda create=False: _FakeCol())

        result = mcp_server.tool_reconnect()
        assert result["success"] is True
        closed_ref = close_palace.call_args.args[0]
        assert closed_ref.local_path == config.palace_path

    def test_reconnect_closes_selected_non_chroma_backend(
        self, monkeypatch, config, palace_path, kg
    ):
        _patch_mcp_server(monkeypatch, config, kg)
        monkeypatch.setenv("MEMPALACE_BACKEND_EXPLICIT", "sqlite_exact")
        from mempalace import mcp_server, palace

        closed = []

        class _FakeBackend:
            def close_palace(self, path):
                closed.append(path)

        class _FakeCol:
            def count(self):
                return 3

        monkeypatch.setattr(palace, "get_backend_for_palace", lambda _path: _FakeBackend())
        monkeypatch.setattr(mcp_server, "_is_chroma_backend", lambda: False)
        monkeypatch.setattr(mcp_server, "_get_collection", lambda create=False: _FakeCol())

        result = mcp_server.tool_reconnect()

        assert result["success"] is True
        assert result["drawers"] == 3
        assert len(closed) == 1
        assert closed[0].local_path == palace_path

    def test_reconnect_closes_previously_cached_backend(self, monkeypatch, config, palace_path, kg):
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace import backends, mcp_server, palace

        closed = []

        class _SelectedBackend:
            name = "sqlite_exact"

            def close_palace(self, ref):
                closed.append(("selected", ref.local_path))

        class _CachedBackend:
            name = "chroma"

            def close_palace(self, ref):
                closed.append(("cached", ref.local_path))

        class _FakeCol:
            def count(self):
                return 3

        monkeypatch.setattr(palace, "get_backend_for_palace", lambda _path: _SelectedBackend())
        monkeypatch.setattr(backends, "get_backend", lambda _name: _CachedBackend())
        monkeypatch.setattr(mcp_server, "_collection_cache_backend", "chroma")
        monkeypatch.setattr(mcp_server, "_is_chroma_backend", lambda: False)
        monkeypatch.setattr(mcp_server, "_get_collection", lambda create=False: _FakeCol())

        result = mcp_server.tool_reconnect()

        assert result["success"] is True
        assert closed == [("selected", palace_path), ("cached", palace_path)]

    def test_get_collection_create_true_avoids_get_or_create_on_reopen(
        self, monkeypatch, config, palace_path, kg
    ):
        """Regression for the MCP-server half of #1262.

        ChromaDB 1.5.x's Rust bindings SIGSEGV when
        ``client.get_or_create_collection`` is called with metadata that
        differs from the collection's stored metadata. The Stop hook
        path (``tool_diary_write`` -> ``_get_collection(create=True)``)
        was reaching that codepath on every session-end; #1262 fixed
        the equivalent crash class in ``ChromaBackend`` but left this
        site untouched. ``_get_collection(create=True)`` must call
        ``client.get_collection`` first and only fall back to
        ``client.create_collection`` when the collection does not yet
        exist on disk.
        """
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace import mcp_server

        col1 = mcp_server._get_collection(create=True)
        assert col1 is not None

        client = mcp_server._client_cache
        assert client is not None

        # Patch at the class level — chromadb's mtime-change detection
        # may rebuild the client between calls, so an instance-level
        # spy would not survive.
        client_cls = type(client)
        calls: list[tuple] = []

        def _spy(self, *args, **kwargs):
            calls.append((args, kwargs))
            raise AssertionError(
                "get_or_create_collection must not be called on reopen "
                "(SIGSEGV path on metadata mismatch)"
            )

        monkeypatch.setattr(client_cls, "get_or_create_collection", _spy)
        mcp_server._collection_cache = None

        col2 = mcp_server._get_collection(create=True)
        assert col2 is not None
        assert calls == [], f"get_or_create_collection was called: {calls}"

    def test_get_collection_passes_embedding_function(self, monkeypatch, config, palace_path, kg):
        """Regression for #1299.

        ``mcp_server._get_collection`` must pass ``embedding_function=`` into
        both ``client.get_collection`` and ``client.create_collection``,
        mirroring ``ChromaBackend.get_collection``. Without it, ChromaDB 1.x
        falls back to its built-in ``DefaultEmbeddingFunction`` (whose lazy
        ONNX provider selection has SIGSEGV'd on python 3.14 + Apple Silicon),
        and writers/readers can disagree with the miner about which EF is
        bound to the collection. The miner / Stop hook ingest path routes
        through ``ChromaBackend.get_collection`` which does this correctly;
        the MCP server must match.
        """
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace import mcp_server

        client = mcp_server._get_client()
        client_cls = type(client)
        captured: dict[str, list[dict]] = {"get": [], "create": []}
        real_get = client_cls.get_collection
        real_create = client_cls.create_collection

        def _spy_get(self, name, **kwargs):
            captured["get"].append(dict(kwargs))
            return real_get(self, name, **kwargs)

        def _spy_create(self, name, **kwargs):
            captured["create"].append(dict(kwargs))
            return real_create(self, name, **kwargs)

        monkeypatch.setattr(client_cls, "get_collection", _spy_get)
        monkeypatch.setattr(client_cls, "create_collection", _spy_create)
        mcp_server._collection_cache = None

        col = mcp_server._get_collection(create=True)
        assert col is not None

        all_calls = captured["get"] + captured["create"]
        assert all_calls, "expected get_collection or create_collection to be called"
        for kwargs in all_calls:
            assert "embedding_function" in kwargs, (
                f"missing embedding_function= in chromadb call: {kwargs}"
            )
            assert kwargs["embedding_function"] is not None

        # Same expectation on the create=False (cache-miss) reopen path.
        mcp_server._collection_cache = None
        captured["get"].clear()
        captured["create"].clear()
        col2 = mcp_server._get_collection()
        assert col2 is not None
        assert captured["get"], "expected get_collection on cache-miss reopen"
        for kwargs in captured["get"]:
            assert "embedding_function" in kwargs
            assert kwargs["embedding_function"] is not None

    def test_get_collection_retries_once_on_exception(self, monkeypatch, config, palace_path, kg):
        """Regression: a transient failure inside _get_collection must trigger
        one retry after clearing the client/collection caches, not silently
        return None.

        Before this fix, a stale chromadb handle (e.g. the rust bindings
        invalidating after an out-of-band write) would raise inside the
        single ``try`` block, get swallowed by ``except Exception: return
        None``, and every subsequent tool call would hit the same poisoned
        cache returning None. The retry forces ``_get_client()`` to rebuild
        the client (which re-runs ``quarantine_stale_hnsw`` per #1322), so
        the second attempt heals the common stale-handle case.
        """
        _patch_mcp_server(monkeypatch, config, kg)
        _client, _col = _get_collection(palace_path, create=True)
        del _client
        from mempalace import mcp_server

        # Force a cold cache so the first call goes through the open path.
        mcp_server._client_cache = None
        mcp_server._collection_cache = None

        real_get_client = mcp_server._get_client
        attempts = {"count": 0}

        def flaky_get_client():
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise RuntimeError("simulated transient chromadb failure")
            return real_get_client()

        monkeypatch.setattr(mcp_server, "_get_client", flaky_get_client)

        col = mcp_server._get_collection()

        # Both attempts ran and the second succeeded.
        assert attempts["count"] == 2
        assert col is not None

    def test_get_collection_returns_none_after_two_failures(
        self, monkeypatch, config, palace_path, kg
    ):
        """If both attempts fail, return None (matches the prior contract for
        permanent failures — only the transient case is now self-healing)."""
        _patch_mcp_server(monkeypatch, config, kg)
        _client, _col = _get_collection(palace_path, create=True)
        del _client
        from mempalace import mcp_server

        mcp_server._client_cache = None
        mcp_server._collection_cache = None

        attempts = {"count": 0}

        def always_fails():
            attempts["count"] += 1
            raise RuntimeError("permanent chromadb failure")

        monkeypatch.setattr(mcp_server, "_get_client", always_fails)

        col = mcp_server._get_collection()

        assert attempts["count"] == 2
        assert col is None


class TestImportKillSwitchSafety:
    """Importing mcp_server must not recreate ~/.mempalace (#1676).

    The module-level WAL setup used to ``mkdir(parents=True)`` at import,
    recreating ``~/.mempalace`` even after the user removed it as the
    documented kill-switch gesture (``_palace_root_exists()``, #1305),
    silently re-arming the autosave/mining hooks. WAL creation is now
    deferred to the first actual write.
    """

    def test_import_does_not_recreate_palace_root(self, tmp_path):
        """import mempalace.mcp_server must not create ~/.mempalace.

        Runs in a fresh subprocess with HOME pointed at tmp_path so the
        assertion targets a clean filesystem, independent of conftest's
        session-level HOME patch.
        """
        palace_root = tmp_path / ".mempalace"
        env = {k: v for k, v in os.environ.items() if not k.startswith("MEMPAL")}
        env["HOME"] = str(tmp_path)
        env["USERPROFILE"] = str(tmp_path)
        result = subprocess.run(
            [sys.executable, "-c", "import mempalace.mcp_server"],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, f"import failed: {result.stderr}"
        assert not palace_root.exists(), (
            f"importing mcp_server recreated {palace_root} as a side effect, "
            "defeating the _palace_root_exists() kill-switch (#1676)"
        )

    def test_wal_log_creates_dir_lazily_on_first_write(self, tmp_path, monkeypatch):
        """_wal_log creates its directory on first use.

        Proves the deferred setup still works (defers WAL creation to write
        time, does not disable it) and preserves the WAL permission bits.
        """
        from mempalace import wal

        wal_file = tmp_path / "fresh" / "wal" / "write_log.jsonl"
        assert not wal_file.parent.exists()
        monkeypatch.setattr(wal, "_WAL_FILE", wal_file)

        wal._wal_log("test_op", {"safe": "ok"})

        assert wal_file.exists(), "lazy WAL init did not create the log on first write"
        entry = json.loads(wal_file.read_text().strip())
        assert entry["operation"] == "test_op"
        assert entry["params"]["safe"] == "ok"

        # Permission bits the refactor must preserve (POSIX only; Windows
        # ignores chmod and the code swallows NotImplementedError).
        if sys.platform != "win32":
            assert wal_file.stat().st_mode & 0o777 == 0o600
            assert wal_file.parent.stat().st_mode & 0o777 == 0o700


class TestStructuredErrors:
    """Verify that _internal_tool_error and MineAlreadyRunning return
    machine-readable structured data (#1552)."""

    def test_internal_tool_error_without_exc_has_no_data_field(self):
        """Backward-compat: callers that omit exc still get a valid error dict."""
        from mempalace.mcp_server import _internal_tool_error

        try:
            raise ValueError("test error")
        except ValueError:
            resp = _internal_tool_error("req-1", "mempalace_search")

        assert resp["jsonrpc"] == "2.0"
        assert resp["id"] == "req-1"
        err = resp["error"]
        assert err["code"] == -32000
        assert err["message"] == "Internal tool error"
        assert "data" not in err

    def test_internal_tool_error_with_exc_includes_structured_data(self):
        """When exc is supplied, the error body must include data.error_class
        and data.message so callers can distinguish error types (#1552)."""
        from mempalace.mcp_server import _internal_tool_error

        exc = RuntimeError("chromadb cold init wedge")
        try:
            raise exc
        except RuntimeError:
            resp = _internal_tool_error("req-2", "mempalace_add_drawer", exc)

        err = resp["error"]
        assert err["code"] == -32000
        assert "data" in err
        assert err["data"]["error_class"] == "RuntimeError"
        assert "chromadb cold init wedge" in err["data"]["message"]

    def test_internal_tool_error_exception_dispatch_passes_exc(self, monkeypatch):
        """handle_request's Exception branch must pass exc to _internal_tool_error."""
        from mempalace import mcp_server

        captured = {}

        def fake_handler(**kwargs):
            raise OSError("fake disk error")

        fake_tool_entry = {
            "handler": fake_handler,
            "input_schema": {"type": "object", "properties": {}},
        }
        monkeypatch.setattr(
            mcp_server,
            "TOOLS",
            {"mempalace_fake": fake_tool_entry},
        )

        original = mcp_server._internal_tool_error

        def spy_error(req_id, tool_name, exc=None):
            captured["exc"] = exc
            return original(req_id, tool_name, exc)

        monkeypatch.setattr(mcp_server, "_internal_tool_error", spy_error)

        req = {
            "jsonrpc": "2.0",
            "id": "r1",
            "method": "tools/call",
            "params": {"name": "mempalace_fake", "arguments": {}},
        }
        resp = mcp_server.handle_request(req)
        assert resp["error"]["code"] == -32000
        assert isinstance(captured.get("exc"), OSError)
        assert "data" in resp["error"]
        assert resp["error"]["data"]["error_class"] == "OSError"

    def test_tool_sync_mine_already_running_returns_error_class(self, monkeypatch, tmp_path):
        """tool_sync MineAlreadyRunning path returns error_class: LockHeldByOtherProcess."""
        from mempalace import mcp_server
        from mempalace.palace import MineAlreadyRunning

        cfg = MagicMock()
        cfg.palace_path = str(tmp_path / "palace")
        monkeypatch.setattr(mcp_server, "_config", cfg)
        monkeypatch.setattr(mcp_server, "_get_kg", lambda *a, **kw: MagicMock())

        def _raise_locked(*args, **kwargs):
            raise MineAlreadyRunning("pid=12345")

        import mempalace.sync as sync_mod

        monkeypatch.setattr(sync_mod, "sync_palace", _raise_locked, raising=False)

        result = mcp_server.tool_sync()
        assert result["success"] is False
        assert "another mine is in progress" in result["error"]
        assert result.get("error_class") == "LockHeldByOtherProcess"

    def test_tool_diary_write_lease_refusal_returns_error_class(self, monkeypatch):
        """tool_diary_write must mark a peer-held palace lease with error_class,
        like tool_mine/tool_sync already do. The daemon keys its defer-vs-fail
        decision on that marker (#2014); swallowed by the bare `except Exception`
        the refusal was indistinguishable from a genuine write error, so a queued
        diary entry was dead-lettered instead of retried."""
        from mempalace import daemon, mcp_server
        from mempalace.palace import MineAlreadyRunning

        class _LeaseHeldCollection:
            def add(self, **kwargs):
                raise MineAlreadyRunning("palace /p is held by PID 999 (mempalace-mcp)")

        monkeypatch.setattr(
            mcp_server, "_get_collection", lambda create=False: _LeaseHeldCollection()
        )
        monkeypatch.setattr(mcp_server, "_wal_log", lambda *a, **kw: None)

        result = mcp_server.tool_diary_write(agent_name="tester", entry="verbatim", topic="t")
        assert result["success"] is False
        assert "is held by PID 999" in result["error"]
        # Assert against the daemon's constant, not a literal: the two are a
        # wire contract, and drift silently un-fixes #2014 (the daemon would
        # stop recognising the refusal and dead-letter the job again).
        assert result.get("error_class") == daemon.LOCK_REFUSAL_ERROR_CLASS

    def test_mcp_idle_timeout_invalid_env_disables_watchdog(self, monkeypatch):
        """Invalid MEMPALACE_MCP_IDLE_HOURS disables idle auto-exit."""
        from mempalace import mcp_server

        monkeypatch.setenv("MEMPALACE_MCP_IDLE_HOURS", "not-a-float")
        assert mcp_server._mcp_idle_timeout_secs() == 0.0

    def test_cache_thread_safe(self, tmp_path, monkeypatch):
        """Concurrent _get_kg() for the same path yields one instance."""
        import concurrent.futures
        from mempalace import mcp_server

        monkeypatch.setattr(mcp_server, "_kg_by_path", {})
        monkeypatch.setattr(mcp_server, "_palace_flag_given", True)
        monkeypatch.setenv("MEMPALACE_PALACE_PATH", str(tmp_path))

        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(lambda _: mcp_server._get_kg(), range(16)))

        ids = {id(kg) for kg in results}
        assert len(ids) == 1, f"expected 1 unique instance, got {len(ids)}"
        assert len(mcp_server._kg_by_path) == 1

    def test_tool_reconnect_drains_kg_cache(self, monkeypatch):
        """``tool_reconnect`` must close cached KG instances and clear the dict.

        Without this, an external replacement of ``knowledge_graph.sqlite3``
        leaves the server pinned to a stale ``sqlite3.Connection``.
        """
        from mempalace import mcp_server

        class _FakeKG:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        fake_a = _FakeKG()
        fake_b = _FakeKG()
        monkeypatch.setattr(mcp_server, "_kg_by_path", {"/a": fake_a, "/b": fake_b})
        # Bypass real ChromaDB so the test isolates KG-cache behaviour.
        monkeypatch.setattr(mcp_server, "_get_collection", lambda: None)

        mcp_server.tool_reconnect()

        assert fake_a.closed is True
        assert fake_b.closed is True
        assert mcp_server._kg_by_path == {}

    def test_tool_reconnect_swallows_kg_close_errors(self, monkeypatch):
        """A failing ``close()`` on one cached KG must not block cache clearing."""
        from mempalace import mcp_server

        class _BoomKG:
            def close(self):
                raise RuntimeError("boom")

        monkeypatch.setattr(mcp_server, "_kg_by_path", {"/a": _BoomKG()})
        monkeypatch.setattr(mcp_server, "_get_collection", lambda: None)

        mcp_server.tool_reconnect()

        assert mcp_server._kg_by_path == {}

    def test_tool_reconnect_rearms_quarantine_gate(self, monkeypatch):
        """``tool_reconnect`` must clear the per-process quarantine gate so
        HNSW safety checks re-run on the next open (#1573)."""
        from mempalace import mcp_server
        from mempalace.backends.chroma import ChromaBackend

        palace_path = "/test/palace/quarantine_rearm"
        gate = {palace_path}
        monkeypatch.setattr(ChromaBackend, "_quarantined_paths", gate)
        monkeypatch.setattr(mcp_server, "_config", type("C", (), {"palace_path": palace_path})())
        monkeypatch.setattr(mcp_server, "_get_collection", lambda: None)

        mcp_server.tool_reconnect()

        assert palace_path not in gate, (
            "tool_reconnect should clear quarantine gate for the palace path"
        )

    def test_get_client_rearms_quarantine_on_reconnect(self, monkeypatch, config, palace_path, kg):
        """``_get_client`` must clear the quarantine gate before calling
        ``make_client`` so HNSW safety checks re-run on reconnect (#1573)."""
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace import mcp_server
        from mempalace.backends.chroma import ChromaBackend

        _client, _col = _get_collection(palace_path, create=True)
        del _client

        mcp_server._get_collection()

        assert config.palace_path in ChromaBackend._quarantined_paths

        old_mtime = mcp_server._palace_db_mtime
        monkeypatch.setattr(mcp_server, "_palace_db_mtime", old_mtime - 10.0)

        quarantine_calls: list[str] = []
        original_prepare = ChromaBackend._prepare_palace_for_open

        @staticmethod
        def spy_prepare(path):
            quarantine_calls.append(path)
            original_prepare(path)

        monkeypatch.setattr(ChromaBackend, "_prepare_palace_for_open", spy_prepare)

        mcp_server._get_client()

        assert len(quarantine_calls) == 1, (
            "_get_client should call _prepare_palace_for_open on reconnect"
        )

    def test_get_client_resets_chroma_system_cache_on_reconnect(
        self, monkeypatch, config, palace_path, kg
    ):
        """``_get_client`` must clear chromadb's path-keyed System/HNSW cache
        (via ``_force_chroma_cache_reset``) *before* calling ``make_client`` on an
        inode/mtime reconnect. Otherwise chromadb hands back the stale in-memory
        HNSW segment, which persists its outdated index over a peer writer's
        on-disk changes, driving the persisted count backwards (#2002)."""
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace import mcp_server
        from mempalace.backends.chroma import ChromaBackend

        _client, _col = _get_collection(palace_path, create=True)
        del _client

        # Prime the cache.
        mcp_server._get_collection()

        # Simulate a peer writer touching chroma.sqlite3 on disk.
        old_mtime = mcp_server._palace_db_mtime
        monkeypatch.setattr(mcp_server, "_palace_db_mtime", old_mtime - 10.0)

        order: list[str] = []
        real_reset = mcp_server._force_chroma_cache_reset
        real_make = ChromaBackend.make_client

        def spy_reset():
            order.append("reset")
            real_reset()

        @staticmethod
        def spy_make(path):
            order.append("make_client")
            return real_make(path)

        monkeypatch.setattr(mcp_server, "_force_chroma_cache_reset", spy_reset)
        monkeypatch.setattr(ChromaBackend, "make_client", spy_make)

        mcp_server._get_client()

        assert order == ["reset", "make_client"], (
            "_get_client must reset chromadb's system cache BEFORE reopening the "
            "client on a staleness reconnect (#2002)"
        )

    def test_call_kg_retries_after_concurrent_close(self, monkeypatch):
        """A KG closed mid-handler must trigger a one-shot retry with a fresh
        instance — not surface a -32000 to the MCP client."""
        import sqlite3 as _sqlite3

        from mempalace import mcp_server

        path = "/fake/palace/knowledge_graph.sqlite3"
        monkeypatch.setattr(mcp_server, "_resolve_kg_path", lambda: path)

        class _ClosedKG:
            def query_entity(self, entity, **kwargs):
                raise _sqlite3.ProgrammingError("Cannot operate on a closed database")

        class _FreshKG:
            def query_entity(self, entity, **kwargs):
                return [{"entity": entity}]

        cache = {mcp_server._canonicalize_kg_path(path): _ClosedKG()}
        monkeypatch.setattr(mcp_server, "_kg_by_path", cache)

        # Second _get_kg() call (after the cache eviction) constructs a new
        # KG. Patch the constructor so we don't open a real sqlite file.
        monkeypatch.setattr(mcp_server, "KnowledgeGraph", lambda **_: _FreshKG())

        result = mcp_server._call_kg(lambda kg: kg.query_entity("Alice"))
        assert result == [{"entity": "Alice"}]
        # The closed instance must be evicted; the fresh one must be cached.
        assert isinstance(cache[mcp_server._canonicalize_kg_path(path)], _FreshKG)

    def test_call_kg_does_not_retry_on_other_errors(self, monkeypatch):
        """Non-ProgrammingError exceptions must propagate without retry —
        we don't want the retry guard masking real bugs."""
        from mempalace import mcp_server

        path = "/fake/palace/knowledge_graph.sqlite3"
        monkeypatch.setattr(mcp_server, "_resolve_kg_path", lambda: path)

        calls = {"count": 0}

        class _FailingKG:
            def query_entity(self, entity, **kwargs):
                calls["count"] += 1
                raise ValueError("bad input")

        monkeypatch.setattr(
            mcp_server, "_kg_by_path", {mcp_server._canonicalize_kg_path(path): _FailingKG()}
        )
        monkeypatch.setattr(mcp_server, "KnowledgeGraph", lambda **_: _FailingKG())

        with pytest.raises(ValueError, match="bad input"):
            mcp_server._call_kg(lambda kg: kg.query_entity("Alice"))
        assert calls["count"] == 1, "non-ProgrammingError must not trigger retry"

    def test_call_kg_gives_up_after_one_retry(self, monkeypatch):
        """If the second attempt also hits a closed DB, give up rather than
        loop forever — a sustained close-stream is a different bug."""
        import sqlite3 as _sqlite3

        from mempalace import mcp_server

        path = "/fake/palace/knowledge_graph.sqlite3"
        monkeypatch.setattr(mcp_server, "_resolve_kg_path", lambda: path)

        calls = {"count": 0}

        class _AlwaysClosedKG:
            def query_entity(self, entity, **kwargs):
                calls["count"] += 1
                raise _sqlite3.ProgrammingError("closed again")

        cache = {}
        monkeypatch.setattr(mcp_server, "_kg_by_path", cache)
        monkeypatch.setattr(mcp_server, "KnowledgeGraph", lambda **_: _AlwaysClosedKG())

        with pytest.raises(_sqlite3.ProgrammingError):
            mcp_server._call_kg(lambda kg: kg.query_entity("Alice"))
        assert calls["count"] == 2, "expected exactly one retry beyond the initial attempt"

    def test_call_kg_passes_captured_path_through_resolve_drift(self, monkeypatch):
        """``_call_kg`` must thread its captured canonical path through
        ``_get_kg`` so insertion and eviction agree on the cache key even
        when FS or env state would otherwise drift between attempts. The
        end-to-end invariant: after the retry, the closed handle that was
        cached under the captured path is gone (evicted) and the cache no
        longer holds it under the stale key.
        """
        import sqlite3 as _sqlite3
        from mempalace import mcp_server

        class _ClosedKG:
            def query_entity(self, entity, **kwargs):
                raise _sqlite3.ProgrammingError("Cannot operate on a closed database")

        class _FreshKG:
            def query_entity(self, entity, **kwargs):
                return [{"entity": entity}]

        # _resolve_kg_path returns shifting values (env rotation between
        # attempts). _canonicalize_kg_path is identity so paths flow
        # through verbatim.
        resolved_seq = iter(["/path/v1", "/path/v2", "/path/v3"])
        monkeypatch.setattr(mcp_server, "_resolve_kg_path", lambda: next(resolved_seq))
        monkeypatch.setattr(mcp_server, "_canonicalize_kg_path", lambda p: p)

        closed = _ClosedKG()
        cache = {"/path/v1": closed}
        monkeypatch.setattr(mcp_server, "_kg_by_path", cache)

        get_kg_args: list = []

        def spy_get_kg(canonical_path=None):
            get_kg_args.append(canonical_path)
            return cache.get(canonical_path) if canonical_path in cache else _FreshKG()

        monkeypatch.setattr(mcp_server, "_get_kg", spy_get_kg)

        result = mcp_server._call_kg(lambda kg: kg.query_entity("Alice"))

        assert result == [{"entity": "Alice"}]
        # Both _get_kg calls received the captured path "/path/v1" rather
        # than the drifted "/path/v2". Without pass-through, the second
        # call would have used "/path/v2" and the closed handle at
        # "/path/v1" would never have been evicted.
        assert get_kg_args == ["/path/v1", "/path/v1"], (
            f"expected both _get_kg calls to receive captured '/path/v1', "
            f"got {get_kg_args} -- captured-path pass-through broken"
        )
        # Eviction landed under the captured key: the closed handle is
        # gone from the cache. With drift the closed handle would still
        # be at "/path/v1" because eviction would have probed "/path/v2".
        assert "/path/v1" not in cache, (
            f"closed handle leaked under captured key after retry; "
            f"cache state: {[(k, type(v).__name__) for k, v in cache.items()]}"
        )

    def test_call_kg_oserror_at_top_propagates_unmasked(self, monkeypatch):
        """``OSError`` from ``_canonicalize_kg_path`` at the top of
        ``_call_kg`` (e.g. transient Windows realpath hiccup on a stale
        junction) must propagate unchanged. The fix-rationale invariant:
        capturing the canonical path before the retry loop means an FS
        error surfaces cleanly to the dispatcher's exception envelope
        instead of getting raised inside the ``except`` branch where it
        would mask a ``sqlite3.ProgrammingError``.
        """
        from mempalace import mcp_server

        monkeypatch.setattr(mcp_server, "_resolve_kg_path", lambda: "/fake/path")
        monkeypatch.setattr(
            mcp_server,
            "_canonicalize_kg_path",
            lambda p: (_ for _ in ()).throw(OSError("simulated realpath failure")),
        )

        op_calls = {"n": 0}

        def op(kg):
            op_calls["n"] += 1
            return None

        with pytest.raises(OSError, match="simulated realpath failure"):
            mcp_server._call_kg(op)
        assert op_calls["n"] == 0, "op must not run if canonicalize fails at top"

    def test_canonicalize_kg_path_collapses_symlink_alias(self, tmp_path):
        """A symlink layer over the palace directory must collapse to one
        cache key — otherwise two tenants pointing at /srv/A and
        /srv/link-to-A open duplicate sqlite3.Connections over the same
        file."""
        if sys.platform == "win32":
            pytest.skip("symlink creation requires admin privileges on Windows runners")

        from mempalace import mcp_server

        target = tmp_path / "real"
        target.mkdir()
        link = tmp_path / "link"
        link.symlink_to(target)

        real_db = str(target / "knowledge_graph.sqlite3")
        link_db = str(link / "knowledge_graph.sqlite3")

        assert mcp_server._canonicalize_kg_path(real_db) == mcp_server._canonicalize_kg_path(
            link_db
        )

    def test_canonicalize_kg_path_routes_through_normcase(self, monkeypatch):
        """``_canonicalize_kg_path`` must apply ``os.path.normcase`` so the
        cache key collapses Windows drive-letter casing
        (``C:\\palace`` vs ``c:\\palace``). On POSIX runners normcase is a
        no-op, so we patch both ``realpath`` and ``normcase`` with sentinel
        wrappers and assert the helper composes them as
        ``normcase(realpath(p))`` -- swapping the order would leave Windows
        symlinks under the original case, defeating the dedup.
        """
        from mempalace import mcp_server

        def fake_realpath(p: str) -> str:
            return f"<RP:{p}>"

        def fake_normcase(p: str) -> str:
            return f"<NC:{p}>"

        monkeypatch.setattr(os.path, "realpath", fake_realpath)
        monkeypatch.setattr(os.path, "normcase", fake_normcase)

        result = mcp_server._canonicalize_kg_path("/some/Path/KG.sqlite3")

        assert result == "<NC:<RP:/some/Path/KG.sqlite3>>", (
            f"expected normcase(realpath(p)) composition, got {result!r}"
        )

    def test_get_kg_dedupes_symlink_alias_end_to_end(self, tmp_path, monkeypatch):
        """End-to-end: two ``_get_kg()`` calls via different symlink layers
        return the same cached instance and construct only one
        ``KnowledgeGraph``."""
        if sys.platform == "win32":
            pytest.skip("symlink creation requires admin privileges on Windows runners")

        from mempalace import mcp_server

        target = tmp_path / "real"
        target.mkdir()
        link = tmp_path / "link"
        link.symlink_to(target)

        real_db = str(target / "knowledge_graph.sqlite3")
        link_db = str(link / "knowledge_graph.sqlite3")

        constructed: list = []

        class _StubKG:
            def __init__(self, db_path=None):
                constructed.append(db_path)

        monkeypatch.setattr(mcp_server, "_kg_by_path", {})
        monkeypatch.setattr(mcp_server, "KnowledgeGraph", _StubKG)

        paths = iter([real_db, link_db])
        monkeypatch.setattr(mcp_server, "_resolve_kg_path", lambda: next(paths))

        kg1 = mcp_server._get_kg()
        kg2 = mcp_server._get_kg()

        assert kg1 is kg2, "symlink alias must hit the cached KG, not construct a duplicate"
        assert len(constructed) == 1, f"expected 1 KG construction, got {len(constructed)}"
        assert len(mcp_server._kg_by_path) == 1


# ── Param-shape diagnostics on tools/call dispatch (#1351) ──────────────


class TestParamShapeDiagnostics:
    """Dispatch-level TypeError on tools/call should surface as JSON-RPC
    -32602 (Invalid params) with the offending parameter named, instead of
    the opaque -32000 Internal tool error. Handler-internal TypeError and
    non-TypeError exceptions stay generic -32000 (no internals leak).
    """

    def test_missing_required_returns_32602_with_param_name(self):
        from mempalace.mcp_server import handle_request

        resp = handle_request(
            {
                "method": "tools/call",
                "id": 1,
                "params": {
                    "name": "mempalace_diary_write",
                    "arguments": {"agent_name": "test"},
                },
            }
        )
        assert resp["error"]["code"] == -32602
        assert "'entry'" in resp["error"]["message"]
        assert "mempalace_diary_write" in resp["error"]["message"]

    def test_handler_internal_typeerror_stays_generic_32000(self, monkeypatch):
        from mempalace import mcp_server

        def boom(**_kw):
            raise TypeError("unsupported operand type(s) for +: 'int' and 'str'")

        monkeypatch.setitem(mcp_server.TOOLS["mempalace_status"], "handler", boom)

        resp = mcp_server.handle_request(
            {
                "method": "tools/call",
                "id": 2,
                "params": {"name": "mempalace_status", "arguments": {}},
            }
        )
        assert resp["error"]["code"] == -32000
        assert resp["error"]["message"] == "Internal tool error"
        assert "unsupported operand" not in resp["error"]["message"]

    def test_chromadb_exception_stays_generic_32000(self, monkeypatch):
        from mempalace import mcp_server

        def boom(**_kw):
            raise RuntimeError("db schema mismatch at /private/path/chroma.sqlite3")

        monkeypatch.setitem(mcp_server.TOOLS["mempalace_status"], "handler", boom)

        resp = mcp_server.handle_request(
            {
                "method": "tools/call",
                "id": 3,
                "params": {"name": "mempalace_status", "arguments": {}},
            }
        )
        assert resp["error"]["code"] == -32000
        assert resp["error"]["message"] == "Internal tool error"
        assert "db schema" not in resp["error"]["message"]
        assert "/private/path" not in resp["error"]["message"]

    def test_two_missing_required_lists_both_names(self):
        """For 2+ missing args Python emits 'a' and 'b'; the response should
        list both quoted names, not return a syntactically broken string.
        """
        from mempalace.mcp_server import handle_request

        resp = handle_request(
            {
                "method": "tools/call",
                "id": 4,
                "params": {"name": "mempalace_diary_write", "arguments": {}},
            }
        )
        assert resp["error"]["code"] == -32602
        message = resp["error"]["message"]
        assert "parameters" in message
        assert "'agent_name'" in message
        assert "'entry'" in message
        assert " and " not in message.split("for tool")[0]

    def test_diary_write_content_aliases_entry(self, monkeypatch):
        """A content-only diary_write call is remapped to 'entry' before
        dispatch (#1245 alias), so it satisfies the required param and the
        alias key is consumed rather than passed through to the handler.
        """
        from mempalace import mcp_server

        captured = {}

        def capture(**kwargs):
            captured.update(kwargs)
            return {"success": True}

        monkeypatch.setitem(mcp_server.TOOLS["mempalace_diary_write"], "handler", capture)
        resp = mcp_server.handle_request(
            {
                "method": "tools/call",
                "id": 5,
                "params": {
                    "name": "mempalace_diary_write",
                    "arguments": {"agent_name": "test", "content": "hello world"},
                },
            }
        )
        assert "error" not in resp
        assert captured.get("entry") == "hello world"
        assert "content" not in captured

    def test_diary_write_entry_wins_over_content(self, monkeypatch):
        """When both 'entry' and the 'content' alias are supplied, 'entry' wins
        and the alias is dropped.
        """
        from mempalace import mcp_server

        captured = {}

        def capture(**kwargs):
            captured.update(kwargs)
            return {"success": True}

        monkeypatch.setitem(mcp_server.TOOLS["mempalace_diary_write"], "handler", capture)
        resp = mcp_server.handle_request(
            {
                "method": "tools/call",
                "id": 6,
                "params": {
                    "name": "mempalace_diary_write",
                    "arguments": {"agent_name": "t", "entry": "real", "content": "alias"},
                },
            }
        )
        assert "error" not in resp
        assert captured.get("entry") == "real"
        assert "content" not in captured

    def test_diary_write_explicit_empty_entry_not_overridden_by_content(self, monkeypatch):
        """An explicitly supplied (even falsy "") 'entry' wins over 'content' —
        the alias only fills in when 'entry' is absent or null, not merely falsy.
        """
        from mempalace import mcp_server

        captured = {}

        def capture(**kwargs):
            captured.update(kwargs)
            return {"success": True}

        monkeypatch.setitem(mcp_server.TOOLS["mempalace_diary_write"], "handler", capture)
        resp = mcp_server.handle_request(
            {
                "method": "tools/call",
                "id": 7,
                "params": {
                    "name": "mempalace_diary_write",
                    "arguments": {"agent_name": "t", "entry": "", "content": "alias"},
                },
            }
        )
        assert "error" not in resp
        assert captured.get("entry") == ""
        assert "content" not in captured

    def test_handler_internal_signature_shape_stays_generic(self, monkeypatch):
        """A TypeError whose function name does not match the dispatched
        handler — e.g. raised by a helper called inside the handler body —
        must fall through to generic -32000, otherwise we'd leak internal
        helper/parameter names as if they were public tool parameters.
        """
        from mempalace import mcp_server

        def calling_handler(**_kw):
            def helper(req):
                return req

            helper()

        monkeypatch.setitem(mcp_server.TOOLS["mempalace_status"], "handler", calling_handler)

        resp = mcp_server.handle_request(
            {
                "method": "tools/call",
                "id": 5,
                "params": {"name": "mempalace_status", "arguments": {}},
            }
        )
        assert resp["error"]["code"] == -32000
        assert resp["error"]["message"] == "Internal tool error"
        assert "'req'" not in resp["error"]["message"]
        assert "helper" not in resp["error"]["message"]

    def test_unexpected_kw_typeerror_inside_handler_stays_generic(self, monkeypatch):
        """The 'got an unexpected keyword argument' shape is unreachable from
        real dispatch (schema-filter on line 2236 drops unknown kwargs for
        normal handlers; **kwargs handlers per #684 accept anything). If a
        handler raises that shape manually, the qualname mismatch must keep
        it on the generic -32000 path so internal helper names cannot leak.
        """
        from mempalace import mcp_server

        def boom(**_kw):
            raise TypeError("some_helper() got an unexpected keyword argument 'foo'")

        monkeypatch.setitem(mcp_server.TOOLS["mempalace_status"], "handler", boom)

        resp = mcp_server.handle_request(
            {
                "method": "tools/call",
                "id": 6,
                "params": {"name": "mempalace_status", "arguments": {}},
            }
        )
        assert resp["error"]["code"] == -32000
        assert resp["error"]["message"] == "Internal tool error"
        assert "'foo'" not in resp["error"]["message"]
        assert "some_helper" not in resp["error"]["message"]


class TestUnknownParamName:
    """A kwarg not in the tool schema (wrong parameter *name*, e.g. text=
    instead of content=) should surface as JSON-RPC -32602 naming the
    offending kwarg, instead of being silently dropped and resurfacing
    indirectly as a later "Missing required 'X'". Symmetric with the
    missing-required path in TestParamShapeDiagnostics. The internal
    wait_for_previous transport kwarg must never be flagged, and
    **kwargs pass-through handlers must keep accepting unknown kwargs.
    """

    def test_unknown_param_returns_32602_naming_the_wrong_kwarg(self):
        from mempalace.mcp_server import handle_request

        resp = handle_request(
            {
                "method": "tools/call",
                "id": 7,
                "params": {
                    "name": "mempalace_add_drawer",
                    "arguments": {"wing": "w", "room": "r", "text": "hello"},
                },
            }
        )
        assert resp["error"]["code"] == -32602
        message = resp["error"]["message"]
        assert "'text'" in message
        assert "Unknown parameter" in message
        assert "mempalace_add_drawer" in message
        # Names the actual wrong kwarg, not the indirect missing-required symptom.
        assert "Missing required" not in message

    def test_two_unknown_params_list_both_names(self):
        from mempalace.mcp_server import handle_request

        resp = handle_request(
            {
                "method": "tools/call",
                "id": 8,
                "params": {
                    "name": "mempalace_add_drawer",
                    "arguments": {"wing": "w", "room": "r", "text": "a", "bogus": "b"},
                },
            }
        )
        assert resp["error"]["code"] == -32602
        message = resp["error"]["message"]
        assert "parameters" in message
        assert "'text'" in message
        assert "'bogus'" in message

    def test_wait_for_previous_not_flagged_as_unknown(self, monkeypatch):
        """wait_for_previous is an internal transport kwarg in no tool schema;
        it is popped before dispatch and must not trip the unknown-param check
        for a normal (non-**kwargs) handler.
        """
        from mempalace import mcp_server

        def stub(agent_name, entry, topic="general"):
            return {"ok": True, "agent": agent_name}

        monkeypatch.setitem(mcp_server.TOOLS["mempalace_diary_write"], "handler", stub)

        resp = mcp_server.handle_request(
            {
                "method": "tools/call",
                "id": 9,
                "params": {
                    "name": "mempalace_diary_write",
                    "arguments": {
                        "agent_name": "x",
                        "entry": "y",
                        "wait_for_previous": True,
                    },
                },
            }
        )
        assert "error" not in resp
        assert "result" in resp

    def test_kwargs_passthrough_handler_keeps_accepting_unknown(self, monkeypatch):
        """Handlers that explicitly accept **kwargs (per #684) bypass the
        schema filter entirely, so an unknown kwarg must still pass through
        rather than being rejected as -32602.
        """
        from mempalace import mcp_server

        def passthrough(**kwargs):
            return {"ok": True, "got": sorted(kwargs)}

        monkeypatch.setitem(mcp_server.TOOLS["mempalace_status"], "handler", passthrough)

        resp = mcp_server.handle_request(
            {
                "method": "tools/call",
                "id": 10,
                "params": {"name": "mempalace_status", "arguments": {"bogus": 1}},
            }
        )
        assert "error" not in resp
        assert "result" in resp
