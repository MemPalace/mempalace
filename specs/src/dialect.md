# Behavior Spec: `mempalace/dialect.py` — AAAK Dialect Encoder

## Purpose & Format Contract

This module implements **AAAK Dialect**, a lossy structured-summary format. The original text **cannot** be reconstructed from AAAK output; it is a summary layer that points to verbatim drawers, not lossless compression (mempalace/dialect.py:L12-L15). The encoded format consists of these line shapes, all pipe-delimited (mempalace/dialect.py:L20-L24):

- **Header:** `FILE_NUM|PRIMARY_ENTITY|DATE|TITLE`
- **Zettel:** `ZID:ENTITIES|topic_keywords|"key_quote"|WEIGHT|EMOTIONS|FLAGS`
- **Tunnel:** `T:ZID<->ZID|label`
- **Arc:** `ARC:emotion->emotion->emotion`

## Constant Tables (observable mapping contracts)

`EMOTION_CODES` maps long emotion names to short codes (e.g. `vulnerability`/`vulnerable`→`vul`, `joy`→`joy`, `anger`→`rage`, `devotion`→`love`, `hopelessness`→`despair`, `brutal_honesty`→`raw`, etc.) (mempalace/dialect.py:L53-L94). `_EMOTION_SIGNALS` maps plain-text keywords to emotion codes for detection (e.g. `decided`→`determ`, `worried`→`anx`, `love`→`love`, `hate`→`rage`, `disappoint`→`grief`) (mempalace/dialect.py:L97-L120). `_FLAG_SIGNALS` maps keywords to flags: `DECISION` (decided/chose/switched/migrated/replaced/"instead of"/because), `ORIGIN` (founded/created/started/born/launched/"first time"), `CORE` (core/fundamental/essential/principle/belief/always/"never forget"), `PIVOT` (turning point/changed everything/realized/breakthrough/epiphany), `TECHNICAL` (api/database/architecture/deploy/infrastructure/algorithm/framework/server/config) (mempalace/dialect.py:L123-L158). `_STOP_WORDS` is a fixed set of filler words excluded from topic extraction (mempalace/dialect.py:L163-L297).

## Class `Dialect`

### Construction

`__init__(entities=None, skip_names=None, lang=None)`: `entities` maps full names to short codes; each name is stored under both its original and lowercased form (mempalace/dialect.py:L334-L338). `skip_names` is stored lowercased (mempalace/dialect.py:L339). On construction it loads language-specific AAAK instruction text and regex patterns from the i18n subsystem; if `lang` is given it loads that language, otherwise it uses the current language; it records `self.lang`, `self.aaak_instruction`, and `self.lang_regex` (mempalace/dialect.py:L341-L348). If `entities` is None, entities are auto-coded from first 3 characters at encode time (mempalace/dialect.py:L326-L333).

`from_config(config_path)` (classmethod): reads a JSON config file with keys `entities` (default `{}`), `skip_names` (default `[]`), and `lang` (default `"en"`), and constructs a `Dialect` from them (mempalace/dialect.py:L350-L366).

`save_config(config_path)`: writes a JSON file `{"entities": <canonical>, "skip_names": <list>}` with 2-space indentation. The canonical entity map deduplicates by code, preferring the non-lowercase name variant; each code appears at most once (mempalace/dialect.py:L368-L385).

### Entity / Emotion / Flag primitives

`encode_entity(name) -> code | None`: returns None if any skip-name substring is found (case-insensitive) in the name (mempalace/dialect.py:L391-L392). Otherwise resolves in priority order: exact name match, lowercased name match, then any known key found as a substring of the name (case-insensitive); failing all, auto-codes as the first 3 characters uppercased (mempalace/dialect.py:L393-L401).

`encode_emotions(emotions) -> str`: maps each emotion via `EMOTION_CODES` (falling back to first 4 chars of the name), deduplicates preserving order, and joins the first 3 with `+` (mempalace/dialect.py:L403-L410).

`get_flags(zettel) -> str`: emits `ORIGIN` if `origin_moment` truthy; `SENSITIVE` if `sensitivity` (uppercased) starts with `MAXIMUM`; `CORE` if notes contains "foundational pillar" or "core"; `GENESIS` if notes or `origin_label` contains "genesis"; `PIVOT` if notes contains "pivot". Flags are joined with `+`, or empty string if none (mempalace/dialect.py:L412-L426).

### Plain-text detection helpers

`_detect_emotions(text)`: case-insensitive substring scan against `_EMOTION_SIGNALS`, deduplicated by code, capped at 3 (mempalace/dialect.py:L430-L439). `_detect_flags(text)`: same scan against `_FLAG_SIGNALS`, deduplicated by flag, capped at 3 (mempalace/dialect.py:L441-L450).

`_extract_topics(text, max_topics=3)`: tokenizes words matching `[a-zA-Z][a-zA-Z_-]{2,}`, lowercases, drops stop words and words shorter than 3 chars, counts frequency. Words whose original form is capitalized get +2 weight; words containing `_`, `-`, or an interior uppercase letter (CamelCase) get +2. Returns up to `max_topics` words ranked by descending weight (mempalace/dialect.py:L452-L477).

`_extract_key_sentence(text)`: splits on `.!?` and newlines, keeps fragments longer than 10 chars after stripping; returns `""` if none. Scores each: +2 per decision word present (decided/because/instead/prefer/switched/chose/realized/important/key/critical/discovered/learned/conclusion/solution/reason/why/breakthrough/insight), +1 if under 80 chars, +1 if under 40 chars, -2 if over 150 chars. Highest score wins; result truncated to 52 chars + `"..."` if over 55 chars (mempalace/dialect.py:L479-L530).

`_detect_entities_in_text(text)`: first returns codes for any known non-lowercase entity name found (case-insensitive) in the text; if any found, returns those. Otherwise falls back to capitalized words (cleaned of non-alpha, length ≥2, first char upper, rest lower, not at index 0, not a stop word), auto-coded as first-3-chars-upper, deduplicated, capped at 3 (mempalace/dialect.py:L532-L559).

### `compress(text, metadata=None) -> str`

Summarizes plain text. Entity part is `+`-joined first-3 detected codes, or `???` if none (mempalace/dialect.py:L580-L581). Topics joined with `_` (first 3), or `misc` if none (mempalace/dialect.py:L583-L584). Key sentence wrapped in double quotes if present (mempalace/dialect.py:L586-L587). Emotions and flags joined with `+` (mempalace/dialect.py:L589-L593).

If `metadata` has `source_file` or `wing`, a header line is emitted first: `wing|room|date|<source stem>`, with `?` substituted for missing fields (mempalace/dialect.py:L595-L611). The content line is `0:<entities>|<topics>` followed by optional quote, emotion, and flag parts (only appended when non-empty), pipe-joined. Lines are newline-joined (mempalace/dialect.py:L613-L624).

### Zettel-based encoding

`extract_key_quote(zettel)`: concatenates content, origin_label, notes. Collects quotes via three regex passes — double-quoted spans of 8-55 chars, single-quoted spans of 8-55 chars, and "says/said/articulates/reveals/admits/confesses/asks:"-introduced fragments of 10-55 chars (case-insensitive) (mempalace/dialect.py:L628-L644). Deduplicates (length ≥8), then scores: +2 if starts uppercase or with `"I "`, +2 per emotional word (love/fear/remember/soul/...), +1 if longer than 20 chars, -2 if starts with `The `/`This `/`She `. Returns highest-scoring quote (mempalace/dialect.py:L646-L697). Fallback: if title contains ` - `, returns the post-dash portion truncated to 45 chars; otherwise `""` (mempalace/dialect.py:L699-L701).

`encode_zettel(zettel)`: ZID is the last `-`-delimited segment of `zettel["id"]` (mempalace/dialect.py:L705). People are encoded via `encode_entity`, None-filtered, defaulting to `["???"]`, then sorted-deduped and `+`-joined (mempalace/dialect.py:L707-L711). Topics joined with `_` (first 2) or `misc` (mempalace/dialect.py:L713-L714). Output line: `<zid>:<entities>|<topics>` then optional quote, then `weight` (default 0.5), then optional emotions and flags — pipe-joined (mempalace/dialect.py:L716-L732).

`encode_tunnel(tunnel)`: returns `T:<from-last-seg><->_<to-last-seg>|<label>` where label is truncated before `:` or to 30 chars (mempalace/dialect.py:L734-L740).

`encode_file(zettel_json)`: emits header `FILE_NUM|PRIMARY|DATE|TITLE` where file_num is the pre-`-` segment of `source_file` (or `000`), date is first zettel's `date_context` (or `unknown`), primary is the sorted union of encoded people (first 3, or `???`), title derived from source minus `.txt` and pre-dash segment (mempalace/dialect.py:L742-L761). Then optional `ARC:<arc>` line if `emotional_arc` present (mempalace/dialect.py:L763-L765), one line per zettel (mempalace/dialect.py:L767-L768), one line per tunnel (mempalace/dialect.py:L770-L771), all newline-joined. **Ordering invariant:** header, then arc, then zettels, then tunnels (mempalace/dialect.py:L761-L773).

### File-based compression (side effects: filesystem)

`compress_file(zettel_json_path, output_path=None)`: reads JSON from path, encodes via `encode_file`, optionally writes result to `output_path`, returns the string (mempalace/dialect.py:L777-L785).

`compress_all(zettel_dir, output_path=None)`: iterates files in `zettel_dir` in sorted name order, processes each `.json` file, joining each encoded block followed by a `---` separator line; newline-joined; optionally writes to `output_path`; returns combined string (mempalace/dialect.py:L787-L802).

### `generate_layer1(...)` (side effects: filesystem read/write)

Builds a Layer-1 wake-up file from all `.json` files in `zettel_dir` (sorted) (mempalace/dialect.py:L806-L824). A zettel is "essential" if its `emotional_weight >= weight_threshold` (default 0.85), or `origin_moment` is true, or it carries any of flags `ORIGIN`/`CORE`/`GENESIS` (mempalace/dialect.py:L833-L842). All tunnels across files are collected (mempalace/dialect.py:L844-L852). Essentials are sorted by descending emotional weight, then grouped by the date key (text before first comma of `date_context`) (mempalace/dialect.py:L854-L861).

Output structure (newline-joined): a `## LAYER 1 -- ESSENTIAL STORY` heading, a `## Auto-generated ... Updated <today's date>.` line, blank line (mempalace/dialect.py:L863-L866). Optional `=<section>=` blocks from `identity_sections` (mempalace/dialect.py:L868-L872). Then, per date in sorted order, a `=MOMENTS[<date>]=` header followed by per-zettel lines: `<entities>|<hint>|"<quote>"|SENSITIVE|<weight>|<flags>` where hint is post-`-` title portion (≤30 chars) or joined first-2 topics, quote omitted if equal to hint/title, SENSITIVE added when `sensitivity` set and not already flagged, weight always present, flags appended if any (mempalace/dialect.py:L874-L908). Finally an optional `=TUNNELS=` block listing up to 8 tunnel labels (truncated before `:` or to 40 chars) (mempalace/dialect.py:L910-L916). Optionally written to `output_path`; returns result string (mempalace/dialect.py:L918-L924).

### `decode(dialect_text) -> dict`

Parses AAAK back into a structured summary (not original text). Returns `{"header": {}, "arc": "", "zettels": [], "tunnels": []}`. Lines starting `ARC:` populate `arc` (text after the 4-char prefix); lines starting `T:` append to `tunnels`; lines containing `|` where the first pipe-segment contains `:` append to `zettels`; any other `|`-containing line populates `header` with keys `file/entities/date/title` from the first four pipe segments (missing → `""`) (mempalace/dialect.py:L928-L949).

### Stats

`count_tokens(text)` (static): estimates tokens as `max(1, int(word_count * 1.3))` (mempalace/dialect.py:L953-L965). `compression_stats(original_text, compressed)`: returns dict with `original_tokens_est`, `summary_tokens_est`, `size_ratio` (= orig/max(comp,1) rounded to 1 decimal), `original_chars`, `summary_chars`, and a `note` stating estimates are approximate and AAAK is lossy (mempalace/dialect.py:L967-L983).

## CLI Contract (`python dialect.py ...`)

Invoked as a script (mempalace/dialect.py:L987). With no args, prints usage and exits with code 1 (mempalace/dialect.py:L990-L1006). A `--config <path>` flag (parsed and removed from args anywhere in the argument list) loads entity mappings from JSON (mempalace/dialect.py:L1008-L1020). Subcommands (mempalace/dialect.py:L1022-L1091):

- `--init`: writes example config to `entities.json` and prints confirmation (mempalace/dialect.py:L1022-L1035).
- `--file <zettel.json>`: prints estimated token count then the encoded file output (mempalace/dialect.py:L1037-L1042).
- `--all <dir>`: writes `<dir>/COMPRESSED_MEMORY.aaak`, prints path, total token estimate, and output (mempalace/dialect.py:L1044-L1052).
- `--stats <zettel.json>`: prints JSON vs AAAK token estimates, ratio, and the encoded output (mempalace/dialect.py:L1054-L1066).
- `--layer1 <dir>`: writes `<dir>/LAYER1.aaak`, prints path, token total, and output (mempalace/dialect.py:L1068-L1076).
- Any other args: treated as text to `compress`, prints original/AAAK token+char estimates, ratio, and the compressed result (mempalace/dialect.py:L1078-L1091).

## Edge Cases & Invariants

Empty/missing entities always render as `???` (mempalace/dialect.py:L581,L710,L757,L883). Empty topics render as `misc` (mempalace/dialect.py:L584,L714). Optional parts (quote/emotion/flag) are omitted entirely when empty rather than emitted as empty fields (mempalace/dialect.py:L615-L620,L724-L730). Quotes always wrapped in double quotes when present (mempalace/dialect.py:L587,L717,L900). Encoding output never contains the original verbatim text in full — only extracted fragments (mempalace/dialect.py:L12-L15,L562-L567).
