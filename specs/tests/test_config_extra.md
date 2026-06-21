# Behavior Spec: `tests/test_config_extra.py`

Test suite asserting configuration-loading contracts of the `MempalaceConfig`
component. Each test constructs a config object rooted at an isolated temporary
directory (`config_dir`) and verifies an observable behavior. The spec below
describes the behaviors the configuration component MUST exhibit for these tests
to pass; the tests themselves are the executable contract.

## Construction Contract

`MempalaceConfig` is constructed with a `config_dir` parameter: a filesystem
directory path (string) that holds the configuration files (`config.json`,
`people_map.json`) for that instance (tests/test_config_extra.py:L12, L18, L24,
L29, L34, L40, L46, L55, L69, L82). The directory may be empty or contain
malformed files; construction MUST NOT fail in any of the cases below.

## Config File Loading (`config.json`)

When `config.json` contains invalid/unparseable JSON content (e.g. the literal
text `not json`), the component MUST fall back to defaults rather than error: a
constructed instance still exposes a non-empty default `palace_path`
(tests/test_config_extra.py:L9-L13).

When `config.json` contains valid JSON with a `collection_name` key, the
component exposes that value verbatim via `collection_name` (e.g. a stored value
of `custom_col` yields `collection_name == "custom_col"`)
(tests/test_config_extra.py:L78-L83).

## People Map Loading (`people_map.json`)

The component exposes a `people_map` attribute: a mapping (dictionary) from alias
strings to canonical-name strings.

- When `people_map.json` exists and contains valid JSON object mapping, e.g.
  `{"bob": "Robert"}`, `people_map` equals that exact mapping
  (tests/test_config_extra.py:L16-L19).
- When `people_map.json` contains invalid JSON (e.g. the literal `bad`),
  `people_map` falls back to an empty mapping `{}`
  (tests/test_config_extra.py:L22-L25).
- When `people_map.json` is absent from the config directory, `people_map` is an
  empty mapping `{}` (tests/test_config_extra.py:L28-L30).

## Default Topic Wings and Hall Keywords

`topic_wings` is a list and, by default (no config files present), MUST include
the entry `"emotions"` (tests/test_config_extra.py:L33-L36).

`hall_keywords` is a dictionary (mapping) and, by default, MUST include the key
`"technical"` (tests/test_config_extra.py:L39-L42).

## Initialization (`init()`) — Idempotence

Calling `init()` materializes a `config.json` file inside `config_dir` whose
parsed JSON contains a `palace_path` key (tests/test_config_extra.py:L45-L51).
Calling `init()` a second time MUST NOT overwrite or corrupt the existing config
file; after two calls the `config.json` still parses and still contains
`palace_path` (tests/test_config_extra.py:L47-L51). `init()` is therefore
idempotent and append/preserve-only with respect to existing config.

## Saving the People Map (`save_people_map`)

`save_people_map(mapping)` accepts a mapping (alias -> canonical name) and
persists it to disk. It returns a filesystem path object that:

- Points to an existing file after the call (`result.exists()` is true)
  (tests/test_config_extra.py:L54-L57).
- Contains the written mapping as JSON; reading and parsing the returned path
  yields back the supplied entries verbatim (e.g. input
  `{"alice": "Alice Smith"}` yields parsed `data["alice"] == "Alice Smith"`)
  (tests/test_config_extra.py:L56-L60).

## Environment Variable Override (legacy `MEMPAL_PALACE_PATH`)

The palace path may be supplied via environment variables. The current variable
is `MEMPALACE_PALACE_PATH`; the legacy variable `MEMPAL_PALACE_PATH` MUST also be
honored (tests/test_config_extra.py:L63-L73). When the current variable is unset
and the legacy variable holds a raw path (e.g. `/legacy/path`), the exposed
`palace_path` equals the path normalized by absolute-path resolution and
home-directory (`~`) expansion of that raw value (tests/test_config_extra.py:L65-L73).
This normalization is portable: on POSIX it is a no-op for an absolute path; on
Windows it prepends the current drive letter (tests/test_config_extra.py:L70-L73).

## Side Effects / Environment Contract

These tests mutate process environment variables (`MEMPALACE_PALACE_PATH`,
`MEMPAL_PALACE_PATH`) and restore them afterward, indicating the component reads
those variables at construction time (tests/test_config_extra.py:L65-L75). All
file side effects (`config.json`, `people_map.json`) occur within the supplied
`config_dir` (tests/test_config_extra.py:L11, L17, L23, L49, L56-L59).
