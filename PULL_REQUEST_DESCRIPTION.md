## What does this PR do?

Resolves merge conflicts from #2332 and adds native parsing and normalization support for Google Antigravity and Gemini IDE conversation session transcripts (`~/.gemini/antigravity-ide/brain/.../transcript.jsonl`).

### Summary of Changes

1. `mempalace/normalize.py`:
   - Adds `_try_antigravity_jsonl(content: str) -> Optional[str]`.
   - Parses `USER_INPUT` steps, extracts user queries from `<USER_REQUEST>` blocks, and strips IDE runtime metadata (`<ADDITIONAL_METADATA>`, `<USER_SETTINGS_CHANGE>`).
   - Parses `PLANNER_RESPONSE` steps, merges multi-turn assistant outputs, and summarizes tool calls (`[Action: ...]`) when content is empty.
   - Formats into standard transcript pairs via `_messages_to_transcript`.
   - Wires `_try_antigravity_jsonl` into `_try_normalize_json_split` dispatch using `_attempt`.
2. `mempalace/convo_miner.py`:
   - Extends `_extract_authored_at` to check `created_at` in addition to `timestamp` so ISO timestamps from Antigravity logs are preserved accurately.
   - Skips `chunks/` directories and duplicate `transcript_full.jsonl` files during conversation scanning to prevent duplicate drawer creation.
3. `tests/test_normalize.py`:
   - Adds unit and integration tests covering basic parsing, metadata stripping, tool call summarization, multi-turn assistant merging, and normalization dispatch.
4. `tests/test_convo_miner_unit.py`:
   - Adds unit tests verifying `created_at` timestamp extraction, skipping `chunks/` directories, and skipping duplicate `transcript_full.jsonl` files.

## How to test

1. Run normalization unit tests:
   ```bash
   pytest tests/test_normalize.py -v
   ```

2. Run convo miner unit tests:
   ```bash
   pytest tests/test_convo_miner_unit.py -v
   ```

3. Run linter and formatting:
   ```bash
   ruff check mempalace/normalize.py mempalace/convo_miner.py tests/test_normalize.py tests/test_convo_miner_unit.py
   ruff format --check mempalace/normalize.py mempalace/convo_miner.py tests/test_normalize.py tests/test_convo_miner_unit.py
   ```

## Checklist
- [x] Tests pass (`python -m pytest tests/ -v`)
- [x] No hardcoded paths
- [x] Linter passes (`ruff check .`)
