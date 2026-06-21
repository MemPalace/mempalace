# Spec: `instructions_cli`

Behavior specification for the instruction-text output component used by MemPalace CLI commands. This component reads a named instruction document from an on-disk directory and prints its contents (mempalace/instructions_cli.py:L1-L6).

## Constants / Configuration

- The instructions directory is located inside the package, in a subdirectory named `instructions` that sits alongside this source module (mempalace/instructions_cli.py:L11-L11). An implementation in any language must resolve this directory relative to the installed package location, not the current working directory.
- The set of valid instruction names is exactly: `init`, `search`, `mine`, `help`, `status` (mempalace/instructions_cli.py:L13-L13). Only these names are accepted.

## Public Surface

### `run_instructions(name)`

Reads and prints the instruction document for a given name (mempalace/instructions_cli.py:L16-L17).

Input: `name` — a string identifying which instruction document to emit (mempalace/instructions_cli.py:L16-L16).

Output: on success, the full UTF-8 text content of the corresponding instruction file is written to standard output, followed by a trailing newline (the print adds one newline after the file content) (mempalace/instructions_cli.py:L28-L28). The function returns no value.

## Behavior and Ordering

1. Validate the name first. If `name` is not one of the accepted values, two lines are written to standard error: `Unknown instructions: <name>` followed by `Available: <comma-space-joined sorted list of accepted names>`, then the process exits with status code `1` (mempalace/instructions_cli.py:L18-L21). The available-names list is sorted alphabetically before being joined with `", "` (mempalace/instructions_cli.py:L20-L20). Validation happens before any filesystem access.

2. After name validation passes, the target file path is formed by joining the instructions directory with `<name>.md` (mempalace/instructions_cli.py:L23-L23). The contract is that each instruction is stored as a Markdown file named exactly `<name>.md` in the instructions directory.

3. If the target path does not refer to an existing regular file, one line is written to standard error: `Instructions file not found: <full path>` (the absolute/joined path is included in the message), then the process exits with status code `1` (mempalace/instructions_cli.py:L24-L26). This check distinguishes a valid-but-missing file from an invalid name; both exit with code `1` but emit different messages.

4. If the file exists, its contents are read as UTF-8 text and printed to standard output (mempalace/instructions_cli.py:L28-L28).

## Error and Exit-Code Contract

- Unknown name → exit code `1`, two stderr lines (mempalace/instructions_cli.py:L18-L21).
- Known name but missing file on disk → exit code `1`, one stderr line (mempalace/instructions_cli.py:L24-L26).
- Success → no explicit exit (implicit success / exit code `0`), content on stdout (mempalace/instructions_cli.py:L28-L28).

## Side Effects

- Filesystem read only: reads `<package>/instructions/<name>.md` (mempalace/instructions_cli.py:L11-L11, mempalace/instructions_cli.py:L23-L28). No files are written or modified.
- Writes to standard output (success path) and standard error (both error paths) (mempalace/instructions_cli.py:L19-L28).
- May terminate the process via exit code `1` on either error path (mempalace/instructions_cli.py:L21-L26). No network, environment-variable, or subprocess side effects.
