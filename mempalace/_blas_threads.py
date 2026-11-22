"""BLAS/OpenMP thread caps for long-lived MemPalace server processes.

OpenBLAS starts one worker thread per logical core, with its own buffer, the
first time numpy loads (about 800 MB private commit on a 32-thread Windows
host), even in a process that never runs a large matrix product. An MCP
server, the HTTP hub or the Antigravity sidecar bridge sits idle most of its
life, so those processes cap the pools to one thread before numpy, chromadb
or torch can load.

The cap is applied by the server entry points only. Importing ``mempalace``
as a library, and CLI compute paths such as ``mempalace mine`` or a
``sqlite_exact`` scan, keep the interpreter's normal thread pools: a cap set
at package import made CPU embedding run on one torch thread and slowed
exact scans, and it leaked into host applications that import mempalace.

Every variable is set with ``setdefault``, so a value the operator exported
always wins.
"""

from __future__ import annotations

import os
import sys
from collections.abc import MutableMapping

SERVER_THREAD_CAP_VARS = ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS")

# Interpreter options whose value is the next argv entry.
_OPTIONS_WITH_VALUE = frozenset({"-W", "-X", "--check-hash-based-pycs"})


def cap_server_blas_threads(env: MutableMapping[str, str] | None = None) -> None:
    """Default the BLAS/OpenMP pools of a long-lived server process to one thread.

    Call it before anything imports numpy, chromadb or torch: the libraries
    read these variables once, when they load. ``env`` defaults to
    ``os.environ``; pass a child environment to cap a process about to be
    spawned.
    """
    target = os.environ if env is None else env
    for var in SERVER_THREAD_CAP_VARS:
        target.setdefault(var, "1")


def launched_as_module(module: str) -> bool:
    """True when this interpreter was started to run ``module`` as ``__main__``.

    Covers ``python -m <module>`` and the cwd-safe launcher used for child
    interpreters (``mempalace._proc.safe_module_argv``). It is checked while
    the package is still being imported, before ``__main__`` runs, which is
    the only point early enough for a package whose ``__init__`` loads numpy.
    """
    argv = list(getattr(sys, "orig_argv", None) or ())
    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg == "-m":
            return i + 1 < len(argv) and argv[i + 1] == module
        if arg == f"-m{module}":
            return True
        if arg == "-c":
            if i + 1 >= len(argv):
                return False
            from ._proc import _SAFE_MODULE_LAUNCHER

            return argv[i + 1] == _SAFE_MODULE_LAUNCHER.format(module=module)
        if arg in _OPTIONS_WITH_VALUE:
            i += 2
            continue
        if arg == "-" or not arg.startswith("-"):
            # A script path or stdin: everything after it belongs to the script.
            return False
        i += 1
    return False
