# Behavior Spec: `_stdio.py` — Stdio UTF-8 Reconfiguration Helper

## Purpose

This module provides a single shared routine that forces the process's standard I/O streams (stdin, stdout, stderr) to use UTF-8 encoding on Windows, so that non-Latin / non-ASCII UTF-8 text is not corrupted ("mojibaked") by the platform's default ANSI codepage. On all non-Windows platforms the routine does nothing (mempalace/_stdio.py:L1-L9, mempalace/_stdio.py:L49-L50).

## Public Surface

A single public function:

`reconfigure_stdio_utf8_on_windows(*, stdin_errors, stdout_errors, stderr_errors, on_failure) -> None` (mempalace/_stdio.py:L31-L37).

### Parameters (all keyword-only, all optional)

- `stdin_errors`: string error-handling policy applied when reconfiguring stdin. Default value is `"surrogateescape"` (mempalace/_stdio.py:L33). The default ensures malformed bytes from a redirected file or misbehaving client survive as lone surrogates rather than aborting the read with a decode error (mempalace/_stdio.py:L19-L22).
- `stdout_errors`: string error-handling policy applied when reconfiguring stdout. Default value is `"strict"` (mempalace/_stdio.py:L34).
- `stderr_errors`: string error-handling policy applied when reconfiguring stderr. Default value is `"strict"` (mempalace/_stdio.py:L35).
- `on_failure`: optional callback invoked as `on_failure(stream_name, exception)` for any stream whose reconfiguration raises an error. If not provided (i.e. `None`), a default failure behavior is used instead (mempalace/_stdio.py:L36-L47).

### Return value

Returns nothing / no value (mempalace/_stdio.py:L37-L38).

## Behavior

### Platform gating

If the current platform is not Windows (`win32`), the function returns immediately and performs no reconfiguration and no side effects (mempalace/_stdio.py:L49-L50).

### Reconfiguration order and processing

On Windows, the function processes exactly three streams in this fixed order: stdin first, then stdout, then stderr. Each stream is paired with its caller-chosen error policy (mempalace/_stdio.py:L52-L57).

For each stream, in order:
1. The stream object is looked up by name on the standard I/O namespace; if the named stream is absent it is treated as missing (mempalace/_stdio.py:L58).
2. The stream's reconfigure capability is looked up; if the stream does not support reconfiguration, that stream is skipped entirely (no error, no callback) and processing continues to the next stream (mempalace/_stdio.py:L59-L61).
3. Otherwise the stream is reconfigured to encoding UTF-8 using that stream's error policy (mempalace/_stdio.py:L62-L63).

### Error handling per stream

If reconfiguring a given stream raises any exception, the failure is isolated to that stream and does not stop processing of the remaining streams (mempalace/_stdio.py:L62-L71). On such a failure:

- If an `on_failure` callback was supplied, it is invoked with the stream name and the raised exception (mempalace/_stdio.py:L65-L66).
- If no callback was supplied, a warning line is written to the standard error stream in the exact form `WARNING: Could not reconfigure {name} to UTF-8: {exc}`, where `{name}` is the stream name (one of `stdin`, `stdout`, `stderr`) and `{exc}` is the textual rendering of the exception (mempalace/_stdio.py:L67-L71).

## Caller-policy contract (documented intent)

The per-stream error policy is intentionally caller-chosen so callers can align behavior across entry points (mempalace/_stdio.py:L11-L22):
- A server emitting only self-controlled JSON-RPC is expected to use `strict` on stdout/stderr so any encode failure surfaces loudly as a bug (mempalace/_stdio.py:L13-L15).
- A CLI or tool that prints verbatim text possibly containing round-tripped surrogate halves is expected to use `replace` on stdout/stderr to avoid crashing mid-print (mempalace/_stdio.py:L16-L18).
- All callers are expected to use `surrogateescape` on stdin so a single malformed byte does not kill the read loop (mempalace/_stdio.py:L19-L22).

## Invariants and Edge Cases

- Idempotent in effect: calling on a non-Windows platform is always a no-op (mempalace/_stdio.py:L49-L50).
- Missing or non-reconfigurable streams are silently skipped without raising or invoking the failure callback (mempalace/_stdio.py:L58-L61).
- A reconfiguration failure on one stream never prevents the remaining streams from being attempted (loop continues over all three) (mempalace/_stdio.py:L57-L71).
- The only side effects are: (a) reconfiguring the three standard streams to UTF-8 on Windows, and (b) on a failure with no callback, writing one warning line per failing stream to standard error (mempalace/_stdio.py:L62-L71). The function performs no filesystem, network, or environment access.
