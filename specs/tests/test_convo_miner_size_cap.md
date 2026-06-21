# Spec: test_convo_miner_size_cap

Behavior specification distilled from `tests/test_convo_miner_size_cap.py`. This is a
test module that asserts a single contract on the conversation miner's file-size cap.

## Purpose

This test enforces that the conversation transcript miner does not silently drop
transcripts larger than 10 MB (tests/test_convo_miner_size_cap.py:L1-L10). It mirrors
an equivalent fix in the project file miner and exists because long sessions, exports,
and multi-year dumps routinely exceed 10 MB while the old cap silently skipped them
(tests/test_convo_miner_size_cap.py:L3-L9).

## Subject Under Test

The test imports a single named constant, `MAX_FILE_SIZE`, from the conversation miner
module (tests/test_convo_miner_size_cap.py:L12). The conversation miner module is
therefore required to export a `MAX_FILE_SIZE` value representing the maximum file size,
in bytes, that the miner will accept before skipping a transcript file.

## Public Surface

One test class containing one test case
(tests/test_convo_miner_size_cap.py:L15-L16):

- `TestConvoMinerSizeCap.test_max_file_size_accommodates_long_transcripts` — verifies the
  size cap is large enough to accommodate realistic transcripts.

The test takes no inputs beyond the imported constant and produces no return value; it
either passes or fails an assertion.

## Contract / Invariant Asserted

`MAX_FILE_SIZE` MUST be greater than or equal to `100 * 1024 * 1024` bytes (100 MB)
(tests/test_convo_miner_size_cap.py:L24). The numeric threshold is expressed as
100 multiplied by 1024 multiplied by 1024, i.e. 104857600 bytes
(tests/test_convo_miner_size_cap.py:L24).

The intended rationale, recorded in the assertion message, is that the cap is a sanity
rail against pathological binary inputs rather than a limit on legitimate text, since
downstream chunking makes source size irrelevant to storage or embedding cost
(tests/test_convo_miner_size_cap.py:L17-L22). The message further recommends matching the
project miner's value of 500 MB for cross-miner consistency, though only the 100 MB
minimum is enforced (tests/test_convo_miner_size_cap.py:L29-L30).

## Error / Failure Behavior

If `MAX_FILE_SIZE` is below 100 MB, the assertion fails and emits a diagnostic message
reporting the actual cap in both bytes and whole megabytes, and stating that the
configuration reproduces the same silent-drop bug as the project miner's former 10 MB cap
(tests/test_convo_miner_size_cap.py:L24-L31). The message references the location where
oversized files are skipped (conversation miner, approximately line 289, via a `continue`)
(tests/test_convo_miner_size_cap.py:L27-L28).

## Side Effects

None. The test performs no filesystem, network, process, or environment interaction; it
reads one constant and evaluates one comparison (tests/test_convo_miner_size_cap.py:L12-L31).
