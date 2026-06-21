# Behavior Spec: `mempalace.room_detector_local` (from `tests/test_room_detector_local.py`)

This spec is distilled from the test file, which exercises the public surface of the
`room_detector_local` module. The module detects a proposed "room" structure for a
project from its folder/file layout, lets a user review it, and writes a config file.
Public surface under test: `FOLDER_ROOM_MAP`, `detect_rooms_from_files`,
`detect_rooms_from_folders`, `detect_rooms_local`, `get_user_approval`,
`print_proposed_structure`, `save_config` (tests/test_room_detector_local.py:L5-L13).

## `FOLDER_ROOM_MAP` (folder-name → room-name lookup)

A constant mapping table from raw folder names to canonical room names. It MUST map
`frontend`→`frontend`, `backend`→`backend`, `docs`→`documentation`, `tests`→`testing`,
`config`→`configuration` (tests/test_room_detector_local.py:L19-L24). It MUST also
recognize alternative folder names, mapping `front-end`→`frontend`, `back-end`→`backend`,
`server`→`backend`, `client`→`frontend`, `api`→`backend`
(tests/test_room_detector_local.py:L27-L32).

## `detect_rooms_from_folders(path: str) -> list[room]`

Takes a project directory path (as a string) and returns a list of room objects. Each
room object is a mapping with at least `name`, `description`, and `keywords` fields
(tests/test_room_detector_local.py:L159-L174).

For a standard layout containing `frontend`, `backend`, and `docs` subfolders, the
returned room `name`s MUST include `frontend`, `backend`, and `documentation` (the
canonical names from `FOLDER_ROOM_MAP`) (tests/test_room_detector_local.py:L38-L46).

The result ALWAYS includes a room named `general`, even for an empty directory; the
result is never empty (length ≥ 1) (tests/test_room_detector_local.py:L49-L59).

A room derived from a known folder carries a non-empty `description` that references the
originating folder name (e.g. a `docs` folder yields a `documentation` room whose
`description` contains the substring `docs`) (tests/test_room_detector_local.py:L159-L165).
A room also carries a `keywords` field whose value is a non-empty list for known folders
(e.g. the `frontend` room has at least one keyword) (tests/test_room_detector_local.py:L168-L174).

Folders are scanned at the top level and one level deep (nested). When the top level
contains a folder like `src` holding subfolders `components` and `routes`, at least one
of `frontend` or `backend` MUST appear in the detected room names — i.e. nested
subfolders are inspected one level down and mapped via `FOLDER_ROOM_MAP`
(tests/test_room_detector_local.py:L148-L156).

Excluded folders: entries named `.git` and `node_modules` MUST NOT produce rooms; they
are skipped during scanning (tests/test_room_detector_local.py:L138-L145).

Custom (unknown) folder names that do not match `FOLDER_ROOM_MAP` are either added as a
room using the folder name verbatim or absorbed such that the result still contains at
least `general` (e.g. a `mylib` folder yields `mylib` or the fallback `general`)
(tests/test_room_detector_local.py:L177-L182).

### OSError / reparse-point resilience (observable contract)

The scanner MUST NOT crash when filesystem traversal raises `OSError` (e.g. Windows
WinError 448 on untrusted mount points / reparse-point junctions). Three distinct guard
points are required:

1. Top-level pass — if `is_dir()` on a top-level entry raises `OSError`, that entry is
   skipped and the remaining valid folders are still detected (e.g. an
   `untrusted_junction` that raises on `is_dir` is skipped while `frontend` is still
   returned) (tests/test_room_detector_local.py:L62-L88).
2. Nested pass — if `is_dir()` on a nested entry raises `OSError`, that entry is skipped
   and sibling nested folders are still detected (e.g. under `skills/`, a `bad_junction`
   raising on `is_dir` is skipped while `skills/docs` still yields `documentation`)
   (tests/test_room_detector_local.py:L90-L111).
3. Nested `iterdir()` — if listing a directory's children raises `OSError` (even when its
   `is_dir()` succeeds), that directory is skipped without crashing while other
   accessible directories (e.g. `docs`→`documentation`) are still detected
   (tests/test_room_detector_local.py:L114-L135).

## `detect_rooms_from_files(path: str) -> list[room]`

Takes a directory path and returns rooms inferred from file names rather than folders.
File names containing room keywords are matched (e.g. `test_auth.py`, `test_login.py`,
`test_api.py` produce `testing` or fall back to `general`)
(tests/test_room_detector_local.py:L188-L194).

For an empty directory the result is non-empty (length ≥ 1) and contains a room named
`general` (tests/test_room_detector_local.py:L197-L200).

The number of returned rooms is capped at 6. Even when files spanning eight or more
distinct keyword groups (`test`, `doc`, `api`, `config`, `frontend`, `backend`,
`design`, `meeting`) are present, the result length MUST be ≤ 6
(tests/test_room_detector_local.py:L203-L209).

## `save_config(path: str, wing: str, rooms: list[room]) -> None`

Writes a configuration file named `mempalace.yaml` into the given directory
(tests/test_room_detector_local.py:L220-L222). Side effect: creates that file on disk.

The file is valid YAML with this on-disk structure: a top-level `wing` field equal to the
project/wing name passed in, and a top-level `rooms` field that is a list of room objects
preserving order and count; each room object retains its `name` (e.g.
`{"wing": "test_proj", "rooms": [{"name": "general", ...}]}` for a single-room input)
(tests/test_room_detector_local.py:L229-L238). The serialized text contains the wing name
and each room name as substrings (e.g. `myproject`, `frontend`, `backend`)
(tests/test_room_detector_local.py:L215-L226).

## `print_proposed_structure(wing: str, rooms: list[room], file_count: int, source: str) -> None`

Side effect: writes a human-readable summary to standard output. The output MUST contain
the wing name, each room `name`, the file count rendered as `"<N> files"`, and the
source-description string (e.g. for `("myapp", rooms, 42, "folder structure")` the output
contains `myapp`, `frontend`, `42 files`, and `folder structure`)
(tests/test_room_detector_local.py:L244-L254).

## `get_user_approval(rooms: list[room]) -> list[room]`

Interactive review loop reading commands from standard input and returning the (possibly
modified) list of rooms.

- Accept-all: an empty input line accepts the proposal unchanged; the returned list
  equals the input list (tests/test_room_detector_local.py:L260-L264).
- Edit/remove: the command sequence `edit`, `1`, `n` removes room number 1 (1-indexed).
  Given rooms `[frontend, backend]`, this yields a single-room result whose remaining room
  is `backend` (tests/test_room_detector_local.py:L267-L276).
- Add: the command sequence `add`, then a room name, then a description, then an empty
  line, appends a new room with the given name. Given `[general]` and inputs
  `add`, `custom_room`, `My custom room`, ``, the result names include `custom_room`
  (tests/test_room_detector_local.py:L279-L292).

## `detect_rooms_local(path: str, yes: bool) -> None`

Top-level orchestration entry point. Side effect: writes `mempalace.yaml` into `path`
(tests/test_room_detector_local.py:L298-L305).

It depends on a `mempalace.miner` module exposing `scan_project(...)` returning a list of
files; this dependency is resolved at call time (it is patchable/injectable in
`sys.modules`) (tests/test_room_detector_local.py:L301-L304).

Behavior modes:

- `yes=True` (non-interactive): detects rooms from the folder layout and writes the config
  without prompting (e.g. a project with a `docs/readme.md` produces `mempalace.yaml`)
  (tests/test_room_detector_local.py:L298-L305).
- Fallback to files: when folder detection yields only the `general` room, detection falls
  back to file-pattern detection before writing the config; the config is still written
  (e.g. a directory of `test_file_*.py` files produces `mempalace.yaml`)
  (tests/test_room_detector_local.py:L308-L316).
- Missing directory: when `path` does not exist, the function terminates the process via
  process exit (`SystemExit`) rather than returning normally
  (tests/test_room_detector_local.py:L319-L324).
- `yes=False` (interactive): the proposed structure is passed through `get_user_approval`,
  and the approved rooms are written to `mempalace.yaml`
  (tests/test_room_detector_local.py:L327-L340).
