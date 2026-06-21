# Spec: tests/test_mcp_stdio_protection.py

Regression test module for MCP stdio protection (issue #225). It asserts the externally observable contract that the MCP server, which multiplexes JSON-RPC over stdio, never lets stray output corrupt the JSON-RPC stream on stdout (tests/test_mcp_stdio_protection.py:L1-L11). These tests constrain the behavior of the module `mempalace.mcp_server`; the module under test is consulted only as ground truth for what these tests require.

## Purpose / Context

The MCP protocol carries JSON-RPC over stdio, and stdout must carry only valid JSON-RPC messages. Transitive dependencies may print banners/warnings to stdout (even at the C/file-descriptor level), which corrupts the consumer's JSON parser (tests/test_mcp_stdio_protection.py:L1-L6). The contract enforced here is: during module import, stdout is redirected to stderr at both the language level and the file-descriptor level; before entering the protocol loop, the real stdout is restored (tests/test_mcp_stdio_protection.py:L8-L11).

## Public surface

Three test cases, each runs an isolated child process and asserts on its exit code or captured stdout.

### test_module_import_redirects_stdout_to_stderr

Spawns a fresh child interpreter that, before importing the server module, records the current standard-output handle, then imports the `mempalace.mcp_server` module (tests/test_mcp_stdio_protection.py:L18-L25). After import, the contract requires:
- The process-wide standard-output stream is the same object as the standard-error stream, i.e. all writes to stdout are diverted to stderr (tests/test_mcp_stdio_protection.py:L26-L29).
- The module exposes a saved handle named `_REAL_STDOUT` whose value is the original standard-output handle captured before import (tests/test_mcp_stdio_protection.py:L30-L32).

The child writes a confirmation token to stderr and is expected to exit successfully (tests/test_mcp_stdio_protection.py:L33-L34). The test runs this child with output captured and a 60-second timeout, and asserts the child's exit code is 0; on failure it reports both captured stdout and stderr (tests/test_mcp_stdio_protection.py:L36-L41).

### test_restore_stdout_returns_real_stdout

Spawns a fresh child interpreter that records the original standard-output handle, imports `mempalace.mcp_server`, and confirms stdout has been redirected to stderr (tests/test_mcp_stdio_protection.py:L44-L52). It then invokes the module operation `_restore_stdout()`, after which the contract requires the process-wide standard-output stream to be the original handle again so that the protocol loop can write JSON-RPC responses to the real stdout (tests/test_mcp_stdio_protection.py:L44-L57). The restore operation must be idempotent: calling `_restore_stdout()` a second time leaves stdout as the original handle and does not error (tests/test_mcp_stdio_protection.py:L58). The child writes a confirmation token to stderr and must exit successfully; the parent asserts exit code 0 and reports captured stdout/stderr on failure (tests/test_mcp_stdio_protection.py:L59-L67).

### test_mcp_server_no_stdout_noise_on_clean_exit

Runs the server module as a program (module-invocation entrypoint `mempalace.mcp_server`) with empty standard input, output captured, and a 60-second timeout (tests/test_mcp_stdio_protection.py:L70-L80). The contract: with empty stdin, the input read returns end-of-input, the main loop exits cleanly, and the child must produce exactly zero bytes on stdout before any first JSON-RPC response, because any stdout content would corrupt the JSON-RPC stream in real use (tests/test_mcp_stdio_protection.py:L70-L83).

## Inputs / Outputs / Types

- The first two tests pass a multi-line source program (text) to a child interpreter via the `-c` flag and inspect the integer exit code (tests/test_mcp_stdio_protection.py:L36-L41, L62-L67).
- The third test passes empty bytes (`b""`) as the child's standard input and inspects the captured standard-output bytes, requiring them to equal empty bytes `b""` (tests/test_mcp_stdio_protection.py:L75-L83).
- All child processes are run with output captured and a 60-second timeout (tests/test_mcp_stdio_protection.py:L38-L40, L64-L66, L78-L80).

## Invariants and ordering guarantees

- Ordering: the original stdout handle must be captured before the server module is imported, since the import is what performs the redirection (tests/test_mcp_stdio_protection.py:L24-L26, L50-L52).
- Redirection invariant: after import and before restore, stdout and stderr refer to the same stream (tests/test_mcp_stdio_protection.py:L26-L29, L52).
- Restore invariant: after restore, stdout equals the pre-import original handle (tests/test_mcp_stdio_protection.py:L54-L57).
- Idempotence invariant: a second restore is a no-op that leaves the original handle in place (tests/test_mcp_stdio_protection.py:L58).
- Saved-handle invariant: the module's `_REAL_STDOUT` always holds the original stdout (tests/test_mcp_stdio_protection.py:L30-L32).

## Error and edge-case behavior

- Empty stdin is the edge case for the program run: end-of-input causes a clean loop exit with no stdout emitted (tests/test_mcp_stdio_protection.py:L70-L83).
- Each child is bounded by a 60-second timeout, so a hang is surfaced as a timeout failure rather than blocking indefinitely (tests/test_mcp_stdio_protection.py:L39, L65, L79).
- Failure diagnostics for the first two tests include both captured stdout and stderr of the child (tests/test_mcp_stdio_protection.py:L41, L67).

## Side effects

- Spawns child interpreter processes (tests/test_mcp_stdio_protection.py:L36-L40, L62-L66, L75-L80).
- Imports `mempalace.mcp_server` inside child processes, which performs file-descriptor-level and language-level stdout redirection as a side effect of import (tests/test_mcp_stdio_protection.py:L8-L11, L25, L51).
- No filesystem, network, or environment-variable mutation is performed by the test module itself (tests/test_mcp_stdio_protection.py:L13-L83).

## Externally observable contracts

- Importing the server module must redirect stdout to stderr and preserve the original stdout as `_REAL_STDOUT` (tests/test_mcp_stdio_protection.py:L18-L32).
- A `_restore_stdout()` operation must restore the original stdout and be idempotent (tests/test_mcp_stdio_protection.py:L44-L58).
- Running the server as a program with empty stdin must exit cleanly (exit code observed via successful capture) and emit zero bytes on stdout (tests/test_mcp_stdio_protection.py:L70-L83).
