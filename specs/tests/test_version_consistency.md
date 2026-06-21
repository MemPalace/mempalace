# Behavior Spec: tests/test_version_consistency.py

This is a test module asserting that the package version is reported consistently across three sources: the project manifest, the package's exported version constant, and the MCP server's initialize response.

## Source of Truth: Expected Version

The canonical expected version is derived from the project manifest file `pyproject.toml`, located one directory above the directory containing this test file (i.e. the project root) (tests/test_version_consistency.py:L8-L13). The file is read as UTF-8 text (tests/test_version_consistency.py:L10).

The version string is extracted by matching, at the start of a line, the pattern `version = "<value>"` — specifically a line beginning with `version`, optional surrounding whitespace around an `=`, then a double-quoted string. The first capture group (the quoted contents, excluding the quotes) is returned as the expected version (tests/test_version_consistency.py:L11-L13). The match is anchored to line starts (multi-line mode), so only a top-level `version = "..."` entry is captured.

If no such line is found in the manifest, the helper fails with the message "Could not find project version in pyproject.toml" (tests/test_version_consistency.py:L12). This is an observable failure contract: the manifest MUST contain a parseable top-level version entry.

## Invariant 1: Package version matches manifest

The package's exported version constant (`__version__` from the `mempalace` package) MUST equal the expected version extracted from the manifest (tests/test_version_consistency.py:L4, L16-L17).

## Invariant 2: MCP initialize reports the package version

When the MCP server's request handler is invoked with a JSON-RPC request object having fields `jsonrpc` = "2.0", `id` = 1, and `method` = "initialize", the returned response MUST contain a nested field `result.serverInfo.version` whose value equals the expected version extracted from the manifest (tests/test_version_consistency.py:L5, L20-L22).

This establishes the externally observable contract that the MCP `initialize` response advertises a `serverInfo.version` matching the project manifest version (tests/test_version_consistency.py:L20-L22).

## Side Effects

The module performs a filesystem read of `pyproject.toml` at the project root each time the version is needed (tests/test_version_consistency.py:L9-L10). It invokes the MCP request handler in-process (tests/test_version_consistency.py:L21); no network or process side effects are part of this test's contract.
