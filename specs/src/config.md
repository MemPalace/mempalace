# Behavior Specification — `mempalace/config.py`

The configuration system resolves settings with the precedence
**environment variables > config file (`~/.mempalace/config.json`) > defaults**
(mempalace/config.py:L1-L5). It also exposes shared input sanitizers for names,
content, and temporal values.

## Constants and observable defaults

- `MAX_NAME_LENGTH` is `128` characters; used by both name and KG-value sanitizers (mempalace/config.py:L19-L19).
- `DEFAULT_PALACE_PATH` is the user home path `~/.mempalace/palace` (mempalace/config.py:L197-L197).
- `DEFAULT_COLLECTION_NAME` is `"mempalace_drawers"` (mempalace/config.py:L198-L198).
- `DEFAULT_BACKEND` is `"chroma"` (mempalace/config.py:L199-L199).
- `DEFAULT_MAX_BACKUPS` is `10` (mempalace/config.py:L205-L205).
- Chunking defaults: `DEFAULT_CHUNK_SIZE=800`, `DEFAULT_CHUNK_OVERLAP=100`, `DEFAULT_MIN_CHUNK_SIZE=50` (mempalace/config.py:L234-L236).
- `DEFAULT_TOPIC_WINGS` is the ordered list `[emotions, consciousness, memory, technical, identity, family, creative]` (mempalace/config.py:L238-L246).
- `DEFAULT_HALL_KEYWORDS` maps each of those seven wing names to a fixed keyword list (e.g. `emotions` → scared/afraid/worried/happy/sad/love/hate/feel/cry/tears) (mempalace/config.py:L248-L304).

## String sanitizers (module functions)

### `strip_lone_surrogates(text) -> str`
Replaces every lone UTF-16 surrogate code point (U+D800–U+DFFF) with the
replacement character U+FFFD, so the resulting string is legal UTF-8
(mempalace/config.py:L26-L31).

### `normalize_wing_name(name) -> str`
Produces a wing slug: lower-cases the input, replaces each space and hyphen
with an underscore, and strips leading/trailing underscores. A path-encoded
dirname like `-home-user-proj` yields `home_user_proj` (mempalace/config.py:L34-L46).

### `sanitize_name(value, field_name="name") -> str`
Validates and returns a trimmed wing/room/entity name. Raises an error
(`ValueError`) when: the value is not a string or is empty/whitespace-only
(message `"{field_name} must be a non-empty string"`); the trimmed value
exceeds 128 characters; it contains `..`, `/`, or `\` (path traversal); it
contains a null byte; or it fails the safe-character pattern
(mempalace/config.py:L49-L74). The safe pattern requires the first and last
characters to be alphanumeric (Unicode word chars excluding underscore), with
interior characters limited to word chars, spaces, `.`, `'`, and `-`, total
length 1–128 (mempalace/config.py:L20-L20, L71-L72). On success returns the
trimmed value (mempalace/config.py:L57-L74).

### `sanitize_kg_value(value, field_name="value") -> str`
More permissive validator for knowledge-graph subject/object names. Raises on
empty/non-string input and on values over 128 characters and on null bytes; it
does **not** enforce the safe-character set or block path characters
(mempalace/config.py:L77-L96). Returns the trimmed value with lone surrogates
stripped (mempalace/config.py:L98-L98).

### `sanitize_iso_temporal(value, field_name="date")`
Validates an ISO-8601 date or canonical UTC datetime. `None` and empty string
pass through unchanged (mempalace/config.py:L154-L155). Non-string non-empty
input raises `"{field_name} must be a string"` (mempalace/config.py:L156-L157).
The value is trimmed, then validated for both regex shape and real-calendar
validity (mempalace/config.py:L159-L167). Accepted forms: `YYYY-MM-DD`;
`YYYY-MM-DDTHH:MM:SSZ`; and `YYYY-MM-DDTHH:MM:SS+00:00` which is normalized so
the returned value ends in `Z` instead of `+00:00`
(mempalace/config.py:L143-L172). Rejected (raise `ValueError` with message
`"{field_name}={value!r} is not a valid ISO-8601 date or UTC datetime
(expected YYYY-MM-DD or YYYY-MM-DDTHH:MM:SSZ)"`): partial dates, naive
datetimes, non-UTC offsets, fractional seconds, space-separated datetimes, and
impossible calendar dates (mempalace/config.py:L113-L135, L163-L167). Month must
be 01–12 and day 01–31 per regex, with further calendar validation rejecting
e.g. invalid month/day combinations (mempalace/config.py:L116-L132).

### `sanitize_iso_date(value, field_name="date")`
Backward-compatible alias that delegates to `sanitize_iso_temporal`; accepts
full dates and canonical UTC datetimes identically (mempalace/config.py:L175-L183).

### `sanitize_content(value, max_length=100_000) -> str`
Validates drawer/diary content. Raises `"content must be a non-empty string"`
for non-string or empty/whitespace input; raises when length exceeds
`max_length` (default 100,000); raises `"content contains null bytes"` for null
bytes. Returns content with lone surrogates stripped
(mempalace/config.py:L186-L194).

## `sqlite_read_uri(db_path) -> str`
Returns a read-only SQLite `file:` URI of the form
`file:<percent-encoded-path>?mode=ro`, percent-encoding the path and normalizing
separators so paths containing spaces or reserved characters open correctly on
every platform (mempalace/config.py:L208-L220).

## `get_configured_collection_name() -> str`
Returns the configured collection name (`MempalaceConfig().collection_name`).
Result is cached so the config file is not re-read on repeated calls
(mempalace/config.py:L223-L226).

## Class `MempalaceConfig`

### Construction
`__init__(config_dir=None)` sets the config directory to `config_dir` if given,
else `~/.mempalace` (mempalace/config.py:L313-L322). It computes
`config.json` and `people_map.json` paths inside that directory
(mempalace/config.py:L323-L324). If `config.json` exists it is loaded as JSON
into the in-memory file config; on JSON-decode or OS read error the file config
falls back to an empty mapping (no crash) (mempalace/config.py:L327-L332).

### Read-only properties (resolution rules)

- `palace_path`: env `MEMPALACE_PALACE_PATH` or `MEMPAL_PALACE_PATH` (first non-empty), expanded (`~`) and absolutized; else config `palace_path`; else `DEFAULT_PALACE_PATH` (mempalace/config.py:L334-L343).
- `tunnel_file`: `tunnels.json` in the parent directory of `palace_path` (mempalace/config.py:L345-L348).
- `hallway_file`: `hallways.json` in the parent directory of `palace_path` (mempalace/config.py:L350-L360).
- `collection_name`: config `collection_name`; else `DEFAULT_COLLECTION_NAME` (mempalace/config.py:L362-L365).
- `backend`: config `backend` (trimmed, lower-cased) first; else env `MEMPALACE_BACKEND` (trimmed, lower-cased); else `"chroma"` (mempalace/config.py:L367-L380).
- `qdrant_url`: env `MEMPALACE_QDRANT_URL` (trimmed); else config `qdrant_url`; else `http://localhost:6333` (mempalace/config.py:L382-L393).
- `qdrant_api_key`: env `MEMPALACE_QDRANT_API_KEY`; else config `qdrant_api_key`; else `None` (mempalace/config.py:L395-L402).
- `qdrant_namespace`: env `MEMPALACE_QDRANT_NAMESPACE` (trimmed); else config `qdrant_namespace` (trimmed); else `None` (mempalace/config.py:L404-L411).
- `qdrant_timeout`: env `MEMPALACE_QDRANT_TIMEOUT` else config `qdrant_timeout` (default 10.0); coerced to float; non-numeric or non-positive values fall back to `10.0` (mempalace/config.py:L413-L422).
- `pgvector_dsn`: env `MEMPALACE_PGVECTOR_DSN` (trimmed); else config `pgvector_dsn`; else `postgresql://localhost:5432/mempalace` (mempalace/config.py:L424-L437).
- `pgvector_namespace`: env `MEMPALACE_PGVECTOR_NAMESPACE` (trimmed); else config `pgvector_namespace` (trimmed); else `None` (mempalace/config.py:L439-L446).
- `people_map`: contents of `people_map.json` if it exists and parses; on decode/OS error or absence, falls back to config `people_map`; else `{}` (mempalace/config.py:L448-L457).
- `hooks_auto_save`: env `MEMPALACE_HOOKS_AUTO_SAVE` if set — true unless the lower-cased value is `false`/`0`/`no`; else config `hooks.auto_save`; else `True` (mempalace/config.py:L459-L470).
- `topic_wings`: config `topic_wings`; else `DEFAULT_TOPIC_WINGS` (mempalace/config.py:L472-L475).
- `hall_keywords`: config `hall_keywords`; else `DEFAULT_HALL_KEYWORDS` (mempalace/config.py:L477-L480).
- `entity_languages`: env `MEMPALACE_ENTITY_LANGUAGES` or `MEMPAL_ENTITY_LANGUAGES`, split on commas with empties dropped (falling back to `["en"]` if all empty); else config `entity_languages` (if a non-empty list); else `["en"]` (mempalace/config.py:L589-L605).
- `embedding_device`: env `MEMPALACE_EMBEDDING_DEVICE` (trimmed, lower-cased); else config `embedding_device`; else `"auto"`. Documented value set: auto/cpu/cuda/coreml/dml (mempalace/config.py:L625-L640).
- `embedding_model`: env `MEMPALACE_EMBEDDING_MODEL` (trimmed, lower-cased); else config `embedding_model`; else `"minilm"`. Documented values: `minilm`, `embeddinggemma` (mempalace/config.py:L642-L661).
- `embedding_threads`: env `MEMPALACE_EMBEDDING_THREADS` else config `embedding_threads`. Unset or `"auto"` (or empty) → half the logical CPU count, minimum 1; a positive integer → that exact value; `0` or negative → `0` (uncapped); non-numeric → half logical CPUs min 1 (mempalace/config.py:L663-L692).
- `topic_tunnel_min_count`: env `MEMPALACE_TOPIC_TUNNEL_MIN_COUNT` if it parses to an int ≥ 1; else config `topic_tunnel_min_count` parsed as int (default 1, non-numeric → 1); final result is clamped to a minimum of 1 (mempalace/config.py:L732-L756).
- `max_backups`: env `MEMPALACE_MAX_BACKUPS` coerced as int ≥ 0 if usable; else config `max_backups` coerced as int ≥ 0 (default 10); negative/non-numeric fall back to `10`; `0` disables pruning (keeps all backups) (mempalace/config.py:L758-L786).
- `hook_silent_save`: config `hooks.silent_save`; else `True` (mempalace/config.py:L788-L791).
- `hook_desktop_toast`: config `hooks.desktop_toast`; else `False` (mempalace/config.py:L793-L796).
- `hook_use_daemon`: env `MEMPALACE_HOOKS_DAEMON` if set — true when lower-cased value is `true`/`1`/`yes`/`on`; else config `hooks.daemon` interpreted as bool, or as the same true-string set if a string, or `value == 1` otherwise; default `False` (mempalace/config.py:L798-L809).

### Chunk-config validation invariants
`chunk_size`, `chunk_overlap`, and `min_chunk_size` properties each derive from a
single validated computation (mempalace/config.py:L520-L565). Raw values are
coerced to integers with documented-default fallback: a bool, empty/garbage
string, non-numeric, JSON null, value below the minimum, or numeric overflow
(e.g. JSON `1e1000`) all yield the default rather than crashing
(mempalace/config.py:L482-L518). Enforced invariants: `chunk_size >= 1`;
`0 <= chunk_overlap < chunk_size` (if overlap ≥ size it is repaired to the
default overlap when that is below `chunk_size`, otherwise to `chunk_size - 1`);
`min_chunk_size <= chunk_size` (if larger it is repaired to the default when the
default fits, otherwise to `chunk_size`). Violations are repaired, never raised
(mempalace/config.py:L520-L550).

`min_chunk_size_explicit` returns the coerced `min_chunk_size` **only** when
`config.json` explicitly defines a usable value (`>= 0` and `<= chunk_size`);
returns `None` when the key is absent/null or unusable. This `None` sentinel
signals "user has not tuned this" to downstream callers
(mempalace/config.py:L567-L587).

### Mutating methods (write side effects)

- `set_entity_languages(languages)`: normalizes (trim, drop empties; default `["en"]` if all empty), stores under `entity_languages`, creates the config dir, writes `config.json` as JSON (indent 2, non-ASCII preserved), best-effort chmod `0o600`; write/chmod errors are swallowed. Returns the normalized list (mempalace/config.py:L607-L623).
- `set_embedding_model(model)`: stores lower-cased trimmed model under `embedding_model`, ensures dir, writes JSON (indent 2), chmods `0o600`, errors swallowed (mempalace/config.py:L694-L712).
- `set_backend(backend)`: validates the lower-cased trimmed backend name via `get_backend_class` (which raises for unknown backends), then stores it under `backend`, writes JSON, chmods `0o600` (mempalace/config.py:L714-L730).
- `set_hook_setting(key, value)`: ensures a `hooks` mapping exists, sets `hooks[key]=value`, writes `config.json` as JSON (indent 2); write errors swallowed (mempalace/config.py:L811-L820).
- `init()`: creates the config directory, best-effort chmod `0o700`. If `config.json` does not already exist, writes a default config containing only `palace_path`, `collection_name`, `topic_wings`, and `hall_keywords` (deliberately **not** the chunk parameters, to preserve "untuned" detection), then chmods the file `0o600`. Returns the config-file path (mempalace/config.py:L822-L852).
- `save_people_map(people_map)`: ensures dir, writes `people_map.json` as JSON (indent 2), chmods `0o600`, returns the file path (mempalace/config.py:L854-L867).

### On-disk contracts
`config.json` is a JSON object holding the keys read by the properties above
(mempalace/config.py:L839-L846). `people_map.json` is a JSON object mapping name
variants to canonical names (mempalace/config.py:L448-L457, L854-L862). Both
files are written with owner-only permissions `0o600` and the config directory
with `0o700` where the platform supports it (mempalace/config.py:L824-L851).
