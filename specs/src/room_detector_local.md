# Spec: room_detector_local

Local (no-API, no-network) room detection for a MemPalace project. Defines rooms either by auto-detecting from folder/file structure or by a manual interactive flow, then writes a `mempalace.yaml` config. The module performs only local filesystem reads and writes and console I/O; it never requires an API key or internet (mempalace/room_detector_local.py:L1-L10).

## Room keyword map

A fixed mapping from a folder/filename keyword to a canonical room name is used by all detection paths. The mapping is many-to-one: multiple keywords collapse to the same room name. The canonical room names produced are: `frontend`, `backend`, `documentation`, `design`, `costs`, `meetings`, `team`, `research`, `planning`, `testing`, `scripts`, `configuration` (mempalace/room_detector_local.py:L21-L97). The full keyword→room table is the contract; e.g. `frontend`/`front-end`/`front_end`/`client`/`ui`/`views`/`components`/`pages` → `frontend`; `backend`/`server`/`api`/`routes`/`services`/`controllers`/`models`/`database`/`db` → `backend`; `docs`/`doc`/`documentation`/`wiki`/`readme`/`notes` → `documentation` (mempalace/room_detector_local.py:L23-L97). Keys are matched in their normalized form (lowercased, hyphens already replaced with underscores) by the callers.

## Public surface

### detect_rooms_from_folders(project_dir: str) -> list[dict]

Walks the top-level and one-level-deep subdirectory structure of `project_dir` and returns a list of room dicts (mempalace/room_detector_local.py:L100-L105). The input path is expanded (`~`) and resolved to an absolute path before iteration (mempalace/room_detector_local.py:L106).

A fixed set of directory names is always skipped at every level: `.git`, `node_modules`, `__pycache__`, `.venv`, `venv`, `env`, `dist`, `build`, `.next`, `coverage` (mempalace/room_detector_local.py:L109-L120).

Top-level pass: for each direct child directory not in the skip set, its name is normalized by lowercasing and replacing `-` with `_` (mempalace/room_detector_local.py:L129-L130). If the normalized name is a key in the keyword map, the mapped canonical room name is recorded (first occurrence wins; later duplicates for the same room name are ignored) and associated with the original directory name (mempalace/room_detector_local.py:L131-L134). Otherwise, if the directory name is longer than 2 characters and its first character is alphabetic, the directory itself becomes a room: its name is lowercased with `-` and spaces replaced by `_`, and recorded (first occurrence wins) against the original name (mempalace/room_detector_local.py:L136-L139).

Nested pass: for each top-level non-skipped directory, its immediate children are listed; for each child directory not in the skip set, the normalized child name (lowercase, `-`→`_`) is looked up in the keyword map only, and any mapped room name is recorded (first occurrence wins) against the original child name (mempalace/room_detector_local.py:L142-L169). Unlike the top-level pass, nested directories that do not match the keyword map do NOT become rooms on their own.

Robustness: each `is_dir()` check and each directory listing is guarded; any OS-level error (e.g. on Windows reparse points / untrusted mount points) causes that item (or that directory's contents) to be skipped and logged at debug level rather than aborting (mempalace/room_detector_local.py:L123-L128, L142-L163).

Output: each recorded entry becomes a room dict `{"name": <room_name>, "description": "Files from <original>/", "keywords": [<room_name>, <original_lowercased>]}` (mempalace/room_detector_local.py:L172-L180). If no room named `general` is present, a fallback room `{"name": "general", "description": "Files that don't fit other rooms", "keywords": []}` is appended (mempalace/room_detector_local.py:L182-L190). The returned list therefore always contains at least the `general` room. Ordering follows insertion order of detection (top-level matches first, then top-level fallback-name rooms, then nested matches), with `general` last unless already present (mempalace/room_detector_local.py:L171-L192).

### detect_rooms_from_files(project_dir: str) -> list[dict]

Fallback detector used when folder structure gives no signal. Recursively walks all files under the expanded/resolved `project_dir`, pruning a skip set of directories `.git`, `node_modules`, `__pycache__`, `.venv`, `venv`, `dist`, `build` (note: this set differs from the folder detector's — it lacks `env`, `.next`, `coverage`) (mempalace/room_detector_local.py:L195-L206). For each filename, the name is normalized (lowercase, `-`→`_`, space→`_`) and every keyword from the map that appears as a substring of the normalized name increments a counter for that keyword's room (mempalace/room_detector_local.py:L207-L211).

Rooms are emitted for any room whose substring-match count is at least 2, ordered by count descending; at most 6 rooms are returned (mempalace/room_detector_local.py:L213-L225). Each emitted room dict is `{"name": <room>, "description": "Files related to <room>", "keywords": [<room>]}` (mempalace/room_detector_local.py:L217-L223). If no room reaches the threshold, a single fallback `{"name": "general", "description": "All project files", "keywords": []}` is returned (mempalace/room_detector_local.py:L227-L230).

### print_proposed_structure(project_name, rooms, total_files, source) -> None

Prints a banner to stdout containing a header line "MemPalace Init — Local setup", the wing name (`project_name`), a line stating `(<total_files> files found, rooms detected from <source>)`, and for each room a `ROOM: <name>` line followed by an indented description line (mempalace/room_detector_local.py:L233-L242). Pure console side effect; no return value.

### get_user_approval(rooms: list) -> list[dict]

Interactive console approval flow. Prints the option menu (accept all / edit / add) and reads a single line from stdin, stripped and lowercased (mempalace/room_detector_local.py:L245-L254). If the input is empty, `y`, or `yes`, the rooms list is returned unchanged (mempalace/room_detector_local.py:L256-L257).

If the input is exactly `edit`, the current rooms are listed numbered from 1; the user is prompted for comma-separated room numbers to remove. Each entered token that is a digit is interpreted as a 1-based index and the corresponding rooms are removed (non-digit tokens are ignored; out-of-range indices simply match nothing) (mempalace/room_detector_local.py:L259-L266).

An add loop runs if the original choice was `add`, OR if the user answers `y` to the "Add any missing rooms?" prompt (mempalace/room_detector_local.py:L268). In the loop, each entered room name is stripped, lowercased, and spaces replaced with `_`; an empty name ends the loop. For each non-empty name the user is prompted for a description, and a room dict `{"name": <name>, "description": <desc>, "keywords": [<name>]}` is appended (mempalace/room_detector_local.py:L269-L277). Returns the (possibly mutated) rooms list (mempalace/room_detector_local.py:L279).

### save_config(project_dir, project_name, rooms) -> None

Writes a YAML config file. The on-disk contract is a mapping with two top-level keys in this order: `wing` (set to `project_name`) and `rooms` (a list). Each room entry is normalized to keys `name`, `description`, and `keywords`, where `keywords` defaults to `[<name>]` if absent from the input room dict (mempalace/room_detector_local.py:L282-L293). The file is written to `<resolved project_dir>/mempalace.yaml` in block style (not flow style) with keys kept in insertion order (not alphabetically sorted) (mempalace/room_detector_local.py:L294-L296). After writing, it prints the saved config path and a "Next step" hint suggesting `mempalace mine <project_dir>` (mempalace/room_detector_local.py:L298-L301).

### detect_rooms_local(project_dir: str, yes: bool = False) -> None

Main entry point orchestrating the local setup (mempalace/room_detector_local.py:L304-L305). The project directory is expanded/resolved; the wing name is derived from the resolved directory's base name passed through `normalize_wing_name` (mempalace/room_detector_local.py:L306-L309). If the resolved project path does not exist, it prints `ERROR: Directory not found: <project_dir>` and exits the process with status code 1 (mempalace/room_detector_local.py:L311-L313).

It counts files via `scan_project(project_dir)` (mempalace/room_detector_local.py:L316-L318). Detection strategy, in order: first `detect_rooms_from_folders` with source label "folder structure" (mempalace/room_detector_local.py:L321-L322); if that yields at most one room (i.e. only the `general` fallback), it retries with `detect_rooms_from_files` and source label "filename patterns" (mempalace/room_detector_local.py:L325-L327); if still empty, it uses a single `general` room with source label "fallback (flat project)" (mempalace/room_detector_local.py:L330-L332).

It prints the proposed structure (mempalace/room_detector_local.py:L334). If `yes` is true the detected rooms are accepted without prompting; otherwise the interactive approval flow runs (mempalace/room_detector_local.py:L335-L338). Finally it saves the config for the approved rooms (mempalace/room_detector_local.py:L339).

## Side effects and contracts summary

- Filesystem reads: directory iteration / recursive file walk under the resolved `project_dir` (mempalace/room_detector_local.py:L123-L169, L205-L211).
- Filesystem write: `<project_dir>/mempalace.yaml` in YAML block style, key order `wing` then `rooms`, each room having `name`/`description`/`keywords` (mempalace/room_detector_local.py:L294-L296).
- Console I/O: prompts and banners to stdout; reads from stdin in the approval flow (mempalace/room_detector_local.py:L233-L301).
- Process exit: status 1 when the project directory does not exist (mempalace/room_detector_local.py:L311-L313).
- No network and no API key are used anywhere in this module (mempalace/room_detector_local.py:L1-L10).
- External dependencies invoked: `normalize_wing_name` for wing naming and `scan_project` for the file count (mempalace/room_detector_local.py:L306-L318).
