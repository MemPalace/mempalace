# Spec: `mempalace_mine` MCP tool behavior (test_mcp_mine.py)

This spec captures the observable contract of the `mempalace_mine` MCP tool as asserted by its test suite. The tool wraps the in-process miners (projects / convos / extract) that the CLI uses, runs them synchronously, and returns a structured result, while isolating miner stdout from the JSON-RPC channel (tests/test_mcp_mine.py:L1-L15).

## Registration

The tool is registered under the name `mempalace_mine` in the server's `TOOLS` registry. Its registry entry exposes a `handler` (the `tool_mine` function) and an `input_schema` whose `required` field is exactly the single-element list `["source"]` (tests/test_mcp_mine.py:L36-L42).

## Invocation surface

The handler is invoked as `tool_mine(source=..., mode=..., dry_run=..., wing=...)`. `source` is a filesystem directory path (required). `mode` is a string selecting the miner; observed valid values are `"projects"`, `"convos"`, and `"extract"` (tests/test_mcp_mine.py:L56, L67, L92, L117, L221). `dry_run` is a boolean (default false) (tests/test_mcp_mine.py:L92, L120, L144). `wing` is a string naming the target wing for filing (tests/test_mcp_mine.py:L117, L144).

## Result shape

The result is a structured object (not raw text). On success it contains `success: true`, `mode` echoing the requested mode, `dry_run` echoing the requested dry-run flag, and `output`: a non-empty string holding the miner's captured stdout (tests/test_mcp_mine.py:L92-L96, L117-L120). On failure it contains `success: false` and an `error` string (tests/test_mcp_mine.py:L57-L58, L68-L69, L77-L78). Failures may additionally carry an `error_class` string identifying the error kind (tests/test_mcp_mine.py:L167, L205, L223, L243, L262).

## Guard rails (pre-flight validation)

- No configured palace: when the active config has an empty `palace_path` (collection name `mempalace_drawers`), the tool returns `success: false` with an `error` field rather than attempting to mine (tests/test_mcp_mine.py:L48-L58).
- Invalid mode: an unrecognized `mode` (e.g. `"bogus"`) yields `success: false` with an `error` whose text contains "invalid mode" (case-insensitive) (tests/test_mcp_mine.py:L61-L69).
- Missing source directory: a `source` path that does not exist yields `success: false` with an `error` whose text contains "source" (case-insensitive) (tests/test_mcp_mine.py:L72-L78).

## Dispatch and dry-run

A dry-run projects mine over a source directory containing a real content file returns `success: true`, `mode == "projects"`, `dry_run == true`, and a non-empty `output` string (tests/test_mcp_mine.py:L84-L96).

## Convos mining side effect (on-disk contract)

A real (non-dry-run) convos mine over a directory containing a transcript file files drawers into the palace. Given a transcript with three distinct prompt/answer turns, after the mine the result has `success: true`, `mode == "convos"`, `dry_run == false`, and the persistent palace collection named `mempalace_drawers` (at the config's `palace_path`) contains at least 2 entries (tests/test_mcp_mine.py:L99-L127). The transcript turn delimiter observed is a line beginning with `> ` for the prompt, followed by the answer on the next line, with blank-line-separated turns (tests/test_mcp_mine.py:L110-L115).

## Stdout isolation contract

Miner stdout (which the miners print as progress and a summary) must be captured into the result's `output` field and must not leak to the real file descriptor 1 (the JSON-RPC channel). After a mine, the miner's terminal `Done.` marker appears in `result["output"]` but does NOT appear on the real captured stdout stream (tests/test_mcp_mine.py:L130-L147).

## Output size bounding

Miner output is tail-truncated to a bounded length. When the miner emits a very large summary (e.g. 5000 characters), the result still has `success: true`, sets `output_truncated: true`, and the returned `output` is exactly 4000 characters long (the trailing/tail portion is kept) (tests/test_mcp_mine.py:L170-L187).

## Error classification

The tool maps underlying miner failures to specific `error_class` values:

- Lock held: when the miner raises `MineAlreadyRunning` (palace lock held by another process), the result is `success: false` with `error_class == "LockHeldByOtherProcess"` (tests/test_mcp_mine.py:L150-L167).
- Interrupt: when the miner raises a process-exit signal (`SystemExit`, e.g. exit code 130 from Ctrl-C), the tool catches it (does not let it escape and kill the server) and returns `success: false` with `error_class == "Interrupted"` (tests/test_mcp_mine.py:L227-L243).
- Missing dependency in extract mode: when extract-mode mining raises an import failure for a missing optional package, the result is `success: false` with `error_class == "MissingDependency"` and an `error` string mentioning the install extra `mempalace[extract]` (tests/test_mcp_mine.py:L209-L224).
- Import error outside extract mode: an import failure during a non-extract mode is treated as a genuine bug, NOT a missing dependency. The result is `success: false` with `error_class == "ImportError"` and an `error` containing "mine failed"; it must NOT be labeled `MissingDependency` (tests/test_mcp_mine.py:L190-L206).
- Generic failure: any other unexpected miner exception is surfaced with `success: false`, an `error` containing "mine failed", and `error_class` equal to the originating exception's type name (e.g. `"RuntimeError"`) (tests/test_mcp_mine.py:L246-L262).

## Test fixtures / helpers (observable in this file)

The file defines an internal helper that overrides the server's active config object for a test (`_patch`) and a helper that writes UTF-8 text to a file path (`_write`) (tests/test_mcp_mine.py:L22-L31). Tests rely on shared fixtures `config` (provides `palace_path`) and `tmp_dir` (a temporary directory root) supplied externally (tests/test_mcp_mine.py:L61, L84, L99).
