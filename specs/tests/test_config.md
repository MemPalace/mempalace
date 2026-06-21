# Behavior Specification: `tests/test_config.py`

This is a test suite that pins the externally observable contract of the configuration
module (`mempalace.config`) and a small set of cross-module invariants it shares with
`mempalace.miner` and `mempalace.convo_miner`. The behaviors below are the ground-truth
contract that any reimplementation of the configuration layer must satisfy. Each behavior
is expressed as a property that the configuration object or a free function must exhibit.

## Public surface under test

The suite imports and exercises these names from the config module:
`MempalaceConfig`, `normalize_wing_name`, `sanitize_iso_date`, `sanitize_iso_temporal`,
`sanitize_kg_value`, `sanitize_name`, `sqlite_read_uri` (tests/test_config.py:L7-L15). It
also reaches into `mempalace.miner` (`chunk_text`, `CHUNK_SIZE`, `CHUNK_OVERLAP`,
`MIN_CHUNK_SIZE`) and `mempalace.config` constants `DEFAULT_CHUNK_SIZE`,
`DEFAULT_CHUNK_OVERLAP`, `DEFAULT_MIN_CHUNK_SIZE`, plus `mempalace.convo_miner.MIN_CHUNK_SIZE`
(tests/test_config.py:L642-L709).

## Configuration object construction

`MempalaceConfig` is constructed with a `config_dir` parameter pointing at a directory
(tests/test_config.py:L19). Configuration is loaded from an optional file named `config.json`
inside `config_dir` (tests/test_config.py:L26-L30). When no `config.json` exists, documented
defaults apply (tests/test_config.py:L511-L516).

### Default values (no config file)

With an empty config directory: `palace_path` contains the substring `"palace"`,
`collection_name` equals `"mempalace_drawers"`, and `backend` equals `"chroma"`
(tests/test_config.py:L18-L22).

## Backend selection and precedence

The `backend` value is read from `config.json` key `backend` when present
(tests/test_config.py:L33-L39). A value supplied in `config.json` takes precedence over the
`MEMPALACE_BACKEND` environment variable (tests/test_config.py:L33-L39). When `config.json`
has no backend, the `MEMPALACE_BACKEND` environment variable is used
(tests/test_config.py:L42-L46). Backend values are lower-cased on read: both
`"SQLite_Exact"` (env) and an explicit file value resolve to `"sqlite_exact"`
(tests/test_config.py:L42-L46, L33-L39).

`set_backend(name)` persists the chosen backend so a freshly constructed config from the
same directory observes the new value (tests/test_config.py:L73-L78). `set_backend` rejects
an unknown backend name by raising a `KeyError` (tests/test_config.py:L248-L252).

## Qdrant settings precedence

Qdrant settings (`qdrant_url`, `qdrant_api_key`, `qdrant_namespace`, `qdrant_timeout`) may be
supplied by `config.json` keys or by environment variables `MEMPALACE_QDRANT_URL`,
`MEMPALACE_QDRANT_API_KEY`, `MEMPALACE_QDRANT_NAMESPACE`, `MEMPALACE_QDRANT_TIMEOUT`. The
environment variable wins over the file value for each (tests/test_config.py:L49-L70).
`qdrant_timeout` is a numeric (floating-point) value: `"3.5"` from the environment resolves
to `3.5` (tests/test_config.py:L63-L70).

## Embedding device

`embedding_device` defaults to `"auto"` when `MEMPALACE_EMBEDDING_DEVICE` is unset and no
config file value exists (tests/test_config.py:L81-L84). When read from `config.json`, the
value is trimmed of surrounding whitespace and lower-cased: `"  CUDA  "` becomes `"cuda"`
(tests/test_config.py:L87-L93). The `MEMPALACE_EMBEDDING_DEVICE` environment variable
overrides the config file, with the same trim-and-lowercase normalization: `"  CoreML  "`
overriding a file value `"cpu"` becomes `"coreml"` (tests/test_config.py:L96-L102).

## Embedding threads

`embedding_threads` resolution:
- Unset (no env, no file): defaults to half the logical CPU count, integer-floored. With 10
  CPUs the value is 5 (tests/test_config.py:L105-L110).
- File value of the keyword `"auto"`: also resolves to half the CPU count; with 8 CPUs the
  value is 4 (tests/test_config.py:L113-L119).
- A positive integer in `config.json` passes through unchanged (3 → 3)
  (tests/test_config.py:L122-L127).
- A value of `0` is preserved and means uncapped (tests/test_config.py:L130-L135).
- The `MEMPALACE_EMBEDDING_THREADS` environment variable overrides the file value
  (`"6"` overriding file 2 → 6) (tests/test_config.py:L138-L143).
- An unparseable environment value (e.g. `"not-a-number"`) falls back to the auto behavior
  (half the CPU count); with 4 CPUs the value is 2 (tests/test_config.py:L146-L150).

## `sqlite_read_uri(path)`

Returns a connection URI that opens a SQLite database in read-only mode and that correctly
handles paths containing spaces (tests/test_config.py:L153-L166). The returned URI
percent-encodes spaces as `%20` rather than leaving them raw (tests/test_config.py:L165-L166).
A connection opened from the returned URI can read existing data but rejects writes: an
attempted `INSERT` raises an operational error because read-only mode is honored through the
encoded URI (tests/test_config.py:L168-L175).

## Palace path normalization (environment overrides)

The `MEMPALACE_PALACE_PATH` environment variable sets `palace_path`. The value is normalized
by absolute-path resolution combined with user-home (`~`) expansion, matching the `--palace`
CLI code path (tests/test_config.py:L178-L186). Tilde-prefixed paths have the home directory
expanded; e.g. `~/mempalace-test` resolves to an absolute path ending in `mempalace-test`
(tests/test_config.py:L191-L201). Parent-directory (`..`) segments are collapsed, not
preserved literally, so the normalized `palace_path` contains no `..`
(tests/test_config.py:L206-L216). The legacy environment variable `MEMPAL_PALACE_PATH`
receives identical normalization treatment as `MEMPALACE_PALACE_PATH`
(tests/test_config.py:L221-L232).

## `init()` side effect

`init()` writes a `config.json` file into `config_dir` (tests/test_config.py:L237-L241). The
written file does NOT contain a `backend` key (tests/test_config.py:L242-L244), and a config
loaded from a directory initialized this way still reports the default backend `"chroma"`
(tests/test_config.py:L245).

## `normalize_wing_name(name)`

Produces a lower-cased slug where hyphens and spaces are replaced by underscores:
`"mempal-private"` → `"mempal_private"`, `"My Project"` → `"my_project"`,
`"My-Cool App"` → `"my_cool_app"` (tests/test_config.py:L258-L271). An already-clean name
passes through unchanged: `"memorymark"` → `"memorymark"` (tests/test_config.py:L266-L267).
Leading separators are stripped so the slug never begins with `_`:
`"-home-user-linux-book"` → `"home_user_linux_book"` (tests/test_config.py:L274-L277).
Trailing separators are stripped: `"project-"` → `"project"` (tests/test_config.py:L280-L281).

## `sanitize_name(name)`

Returns the name unchanged for valid ASCII and Unicode names, including Latin-extended,
CJK, and Cyrillic: `"hello"`, `"Jānis"`, `"太郎"`, `"Алексей"` all pass through unchanged
(tests/test_config.py:L287-L300). Raises `ValueError` for a name beginning with an underscore
(`"_foo"`), a path-traversal name (`"../etc/passwd"`), and an empty string
(tests/test_config.py:L303-L315).

## `sanitize_kg_value(value)`

Accepts and returns unchanged values containing commas, colons, parentheses, slashes, hash
characters, and Unicode letters:
`"Alice, Bob, and Carol"`, `"role: engineer"`, `"Python (programming)"`, `"owner/repo"`,
`"issue #123"`, `"Jānis Bērziņš"` (tests/test_config.py:L321-L342). Surrounding whitespace is
stripped: `"  hello  "` → `"hello"` (tests/test_config.py:L345-L346). Raises `ValueError` for:
empty string, whitespace-only string, a string containing a null byte (`"hello\x00world"`),
and a value exceeding 128 characters (a 129-character string is rejected)
(tests/test_config.py:L349-L366).

## `sanitize_iso_date(value, field_name=None)`

Enforces full `YYYY-MM-DD` calendar dates and rejects partial or non-canonical forms because
downstream knowledge-graph queries do lexicographic TEXT comparison on date strings
(tests/test_config.py:L372-L376).

Rejects with `ValueError`:
- Year-only `"2026"` (tests/test_config.py:L372-L376)
- Year-month `"2026-03"` (tests/test_config.py:L379-L381)
- Natural language `"March 2026"` (tests/test_config.py:L400-L402)
- Abbreviated month `"Jan 2025"` (tests/test_config.py:L405-L407)
- US slash format `"03/15/2026"` (tests/test_config.py:L410-L412)
- Invalid month `"2026-13"` (tests/test_config.py:L415-L417)
- Invalid day `"2026-02-32"` (tests/test_config.py:L420-L422)
- A non-string input such as the integer `20260315` (tests/test_config.py:L425-L427)

Accepts / passes through:
- A full date `"2026-03-15"` returned unchanged (tests/test_config.py:L384-L385)
- `None` returned as `None` (tests/test_config.py:L388-L389)
- The empty string `""` returned as `""` (tests/test_config.py:L392-L393)
- A date with surrounding whitespace is trimmed: `"  2026-03-15  "` → `"2026-03-15"`
  (tests/test_config.py:L396-L397)
- As a backward-compatible wrapper, a full canonical datetime is also accepted unchanged:
  `"2026-05-06T14:23:00Z"` → `"2026-05-06T14:23:00Z"` (tests/test_config.py:L447-L448)

When an optional second argument (a field name such as `"valid_from"`) is supplied, the raised
error message includes that field name (tests/test_config.py:L430-L432).

## `sanitize_iso_temporal(value, field_name=None)`

Accepts either a full date `"2026-05-06"` (tests/test_config.py:L435-L436) or a canonical UTC
datetime in the form `YYYY-MM-DDTHH:MM:SSZ`: `"2026-05-06T14:23:00Z"` is returned unchanged
(tests/test_config.py:L439-L440). Surrounding whitespace on a datetime is trimmed:
`" 2026-05-06T14:23:00Z "` → `"2026-05-06T14:23:00Z"` (tests/test_config.py:L443-L444). A
`+00:00` zero offset is normalized to the `Z` suffix:
`"2026-05-06T14:23:00+00:00"` → `"2026-05-06T14:23:00Z"` (tests/test_config.py:L491-L492).

Rejects with `ValueError`:
- Datetime missing seconds `"2026-05-06T14:23"` (tests/test_config.py:L451-L453)
- Naive datetime (no timezone) `"2026-05-06T14:23:00"` (tests/test_config.py:L456-L458)
- Fractional seconds `"2026-05-06T14:23:00.123Z"` (tests/test_config.py:L461-L463)
- Non-UTC timezone offset `"2026-05-06T14:23:00+02:00"` (tests/test_config.py:L466-L468)
- Space separator instead of `T` `"2026-05-06 14:23:00"` (tests/test_config.py:L471-L473)
- Invalid hour `"2026-05-06T24:00:00Z"` (tests/test_config.py:L476-L478)
- Invalid calendar date `"2026-02-31"` (tests/test_config.py:L481-L483)

When the optional field-name argument (e.g. `"as_of"`) is supplied, the raised error message
includes that field name (tests/test_config.py:L486-L488).

## Chunk configuration validation

The properties `chunk_size`, `chunk_overlap`, and `min_chunk_size` resolve from `config.json`
keys of the same name, applying repair-not-crash semantics: a bad config value must never hang
or crash ingest; it is repaired to a safe value (tests/test_config.py:L495-L501).

Documented defaults (no config file): `chunk_size == 800`, `chunk_overlap == 100`,
`min_chunk_size == 50` (tests/test_config.py:L511-L516). Valid file values pass through
unchanged (1200/200/80) (tests/test_config.py:L519-L524).

Coercion and fallback rules:
- Numeric strings are coerced to integers: `"1500"` → 1500, `"50"` → 50
  (tests/test_config.py:L527-L531).
- A non-numeric string (e.g. `"not a number"`) falls back to the default 800
  (tests/test_config.py:L534-L536).
- A boolean value falls back to the default 800 (booleans are NOT treated as integers)
  (tests/test_config.py:L539-L544).
- Negative values fall back to defaults: negative `chunk_size` → 800, negative
  `min_chunk_size` → 50 (tests/test_config.py:L547-L551).
- `chunk_size == 0` falls back to the default 800, since zero would loop forever
  (tests/test_config.py:L554-L557).
- `chunk_overlap >= chunk_size` is repaired so the resulting overlap is strictly less than
  `chunk_size`. When the default overlap (100) fits inside the configured size, it is used:
  size 900 / overlap 900 → overlap 100 (tests/test_config.py:L560-L568). When the default does
  not fit (size too small), the overlap is repaired to `chunk_size - 1`: size 50 / overlap 100
  → overlap 49 (tests/test_config.py:L571-L577).
- `min_chunk_size > chunk_size` is repaired: if the default (50) fits inside the configured
  size it is used (size 1000 / min 2000 → 50); otherwise the value is clamped to `chunk_size`
  (size 20 / min 200 → 20) (tests/test_config.py:L580-L588).
- A JSON `Infinity` value (round-tripping to a non-finite float) is unusable: for `chunk_size`
  it falls back to the default 800 (tests/test_config.py:L656-L664).

## `min_chunk_size_explicit` accessor

A separate accessor that distinguishes a user-tuned `min_chunk_size` from an untuned one. It
returns either a validated integer or `None` (sentinel for untuned/unusable), so callers can
apply their own floor without reaching into raw config (tests/test_config.py:L591-L596).

- Unset (no config file): returns `None` (tests/test_config.py:L599-L601).
- Explicit JSON `null`: returns `None` (untuned) (tests/test_config.py:L604-L608).
- A valid value returns the validated integer: 80 → 80 (tests/test_config.py:L611-L613).
- A numeric string is coerced: `"42"` → 42 (tests/test_config.py:L616-L618).
- Unusable values return `None` (not a crash, not a default): the parametrized set
  `"abc"`, `-5`, `True`, `""`, `"  "` all yield `None` (tests/test_config.py:L621-L628).
- A `min_chunk_size` greater than `chunk_size` is treated as unusable and returns `None`
  (chunk_size 100 / min 500 → None) (tests/test_config.py:L631-L635).
- A JSON `Infinity` value returns `None` (tests/test_config.py:L656-L661).

### Convo-miner fallback invariant

For any config value of `min_chunk_size`, the effective value computed as
`explicit if explicit is not None else convo_miner.MIN_CHUNK_SIZE` must yield a usable integer
that is never a boolean. For garbage/unusable inputs (`"not-a-number"`, `-10`, `True`, `{}`,
`[]`) the effective value equals the convo-miner floor `MIN_CHUNK_SIZE`; for a valid tuned
value of 15 it equals 15 (tests/test_config.py:L638-L653).

## `mempalace.miner.chunk_text` direct-caller guards

`chunk_text(content, source, chunk_size=..., chunk_overlap=...)` validates its numeric
arguments and raises a clear `ValueError` rather than looping forever:
- `chunk_size <= 0` (both 0 and -1) raises `ValueError` mentioning `chunk_size`
  (tests/test_config.py:L667-L675).
- `chunk_overlap >= chunk_size` (overlap 100 or 200 with size 100) raises `ValueError`
  mentioning `chunk_overlap` (tests/test_config.py:L678-L684).
- A negative overlap (`-1`) raises `ValueError` mentioning `chunk_overlap`
  (tests/test_config.py:L687-L691).

## Chunk-constant single-source-of-truth invariant

The legacy re-exported constants `CHUNK_SIZE`, `CHUNK_OVERLAP`, `MIN_CHUNK_SIZE` in the miner
module must equal the canonical `DEFAULT_CHUNK_SIZE`, `DEFAULT_CHUNK_OVERLAP`,
`DEFAULT_MIN_CHUNK_SIZE` constants in the config module, and these equal 800, 100, and 50
respectively (tests/test_config.py:L694-L709).

## `hooks.auto_save`

`hooks_auto_save` defaults to `True` (tests/test_config.py:L715-L717). It may be set in
`config.json` under the nested object `hooks.auto_save`; `False` there yields `False`
(tests/test_config.py:L720-L725). The `MEMPALACE_HOOKS_AUTO_SAVE` environment variable
overrides the config; the values `"false"`, `"0"`, and `"no"` all yield `False`
(tests/test_config.py:L728-L752). The environment variable `"true"` overrides a config file
value of `False`, yielding `True` (tests/test_config.py:L755-L765). The environment variable
thus takes precedence over the config file in both directions.

## `hooks.daemon`

`hook_use_daemon` defaults to `False` when `MEMPALACE_HOOKS_DAEMON` is unset
(tests/test_config.py:L768-L771). It reads from `config.json` nested key `hooks.daemon`;
a boolean `True` yields `True` (tests/test_config.py:L774-L779), and a truthy string such as
`"yes"` also yields `True` (tests/test_config.py:L782-L787). The `MEMPALACE_HOOKS_DAEMON`
environment variable overrides the config file; `"yes"` overriding a file value of `False`
yields `True` (tests/test_config.py:L790-L795).

## `max_backups` (backup retention)

`max_backups` defaults to 10 (tests/test_config.py:L801-L804). A non-negative integer in
`config.json` passes through: 3 → 3 (tests/test_config.py:L807-L812). A value of `0` is a valid
explicit "keep everything" and is preserved (tests/test_config.py:L815-L821). The
`MEMPALACE_MAX_BACKUPS` environment variable overrides the config file value (`"7"` over file 3
→ 7) (tests/test_config.py:L824-L829).

Garbage handling (must never crash migrate/repair):
- Garbage config string values `"abc"`, `""`, `"-5"`, `"1.5"`, `"true"` all fall back to the
  default 10 (tests/test_config.py:L832-L839).
- A negative integer in config (`-3`) falls back to the default 10
  (tests/test_config.py:L842-L847).
- A bad environment value (`"garbage"`) falls back to the config file value rather than the
  hard default: file 4 → 4 (tests/test_config.py:L850-L855).
