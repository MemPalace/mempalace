"""Stdio UTF-8 reconfiguration helper for Windows entry points.

Python on Windows defaults stdio to the system ANSI codepage
(cp1252/cp1251/cp950 depending on locale), which mojibakes UTF-8 input
or output the moment a non-Latin character shows up. Every console
entry point that touches stdio needs to fix this on Windows -- the MCP
server, the CLI, the fact_checker `--stdin` mode -- so the
reconfigure code lives here in one place to keep the per-stream
errors policies aligned across them.

Per-stream errors policy is caller-chosen:

* MCP server uses ``strict`` on stdout/stderr because everything written
  there is server-controlled JSON-RPC; any encode failure is a real bug
  the operator wants loud.
* CLI / fact_checker use ``replace`` on stdout/stderr because they print
  verbatim drawer text that may contain surrogate halves round-tripped
  from filenames -- ``strict`` would crash mid-print.
* All callers use ``surrogateescape`` on stdin so a malformed byte from
  a redirected file or a misbehaving client survives as a lone surrogate
  the consumer's parser surfaces, instead of ``UnicodeDecodeError``
  killing the read loop on the first bad byte.
"""

from __future__ import annotations

import sys
from typing import Callable, Optional


def reconfigure_stdio_utf8_on_windows(
    *,
    stdin_errors: str = "surrogateescape",
    stdout_errors: str = "strict",
    stderr_errors: str = "strict",
    on_failure: Optional[Callable[[str, BaseException], None]] = None,
) -> None:
    """Reconfigure stdio to UTF-8 on Windows. No-op elsewhere.

    Args:
        stdin_errors: errors= policy for stdin.reconfigure().
        stdout_errors: errors= policy for stdout.reconfigure().
        stderr_errors: errors= policy for stderr.reconfigure().
        on_failure: optional ``(stream_name, exc) -> None`` callback for
            streams whose ``reconfigure`` raises (e.g. Jupyter-replaced
            streams that lack the method-shape we expect). Defaults to a
            ``WARNING:`` line on the original sys.stderr.
    """
    if sys.platform != "win32":
        return

    policies = (
        ("stdin", stdin_errors),
        ("stdout", stdout_errors),
        ("stderr", stderr_errors),
    )
    for name, errors in policies:
        stream = getattr(sys, name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors=errors)
        except Exception as exc:  # noqa: BLE001 -- last-resort guard
            if on_failure is not None:
                on_failure(name, exc)
            else:
                print(
                    f"WARNING: Could not reconfigure {name} to UTF-8: {exc}",
                    file=sys.stderr,
                )
