# Behavior Specification: `general_extractor`

Extracts five categories of "memories" from arbitrary text using pure keyword/regex
heuristics, with no LLM and no dependency on the palace, dialect, or layers modules
(mempalace/general_extractor.py:L1-L20). The only external coupling is a default
chunk-size constant (mempalace/general_extractor.py:L25-L25).

## Memory Types

The system recognizes exactly five memory types, each identified by a distinct set
of markers (mempalace/general_extractor.py:L165-L171):

- `decision` — choices made, "we went with X because Y" (mempalace/general_extractor.py:L5-L6, L32-L54)
- `preference` — "always use X", "never do Y", "I prefer Z" (mempalace/general_extractor.py:L7-L7, L56-L73)
- `milestone` — breakthroughs, things that finally worked (mempalace/general_extractor.py:L8-L8, L75-L110)
- `problem` — what broke, what fixed it, root causes (mempalace/general_extractor.py:L9-L9, L112-L131)
- `emotional` — feelings, vulnerability, relationships (mempalace/general_extractor.py:L10-L10, L133-L163)

Each type's markers are a fixed list of case-insensitive regular expression patterns.
The exact pattern lists are part of the observable contract and must be preserved
verbatim by any reimplementation (mempalace/general_extractor.py:L32-L163). Notable
pattern characteristics: milestone matches version strings like `version <digit>`,
`v<num>.<num>`, and quantitative claims such as `<num>x faster` or
`<num>% improvement` (mempalace/general_extractor.py:L106-L109); emotional matches
text wrapped in asterisks `*...*` (mempalace/general_extractor.py:L162-L162).

## Public Surface

### `extract_memories(text, min_confidence=0.3, chunk_size=DEFAULT_CHUNK_SIZE) -> List[Dict]`

The single public entry point (mempalace/general_extractor.py:L365-L369). `DEFAULT_CHUNK_SIZE`
is `800` (mempalace/config.py:L234-L234).

Inputs:
- `text` (string): the source text, any format (mempalace/general_extractor.py:L374-L374).
- `min_confidence` (float, default `0.3`): minimum confidence threshold in range 0.0–1.0; segments below this are discarded (mempalace/general_extractor.py:L367-L367, L422-L423).
- `chunk_size` (integer, default 800): per-memory character cap; segments longer than this are sliced verbatim into multiple memories sharing the same `memory_type` (mempalace/general_extractor.py:L368-L368, L376-L381).

Output: an ordered list of dictionaries, each with exactly three fields
(mempalace/general_extractor.py:L384-L384, L433-L448):
- `content` (string): the extracted text (a stripped segment, or a verbatim slice of one).
- `memory_type` (string): one of `decision`, `preference`, `milestone`, `problem`, `emotional`.
- `chunk_index` (integer): the index of this memory within the returned list, equal to
  the number of memories already accumulated at the time it was appended (i.e. a
  monotonically increasing 0-based counter spanning the whole output, not reset per
  segment) (mempalace/general_extractor.py:L437-L437, L446-L446).

#### Extraction algorithm and ordering

1. The text is split into segments (mempalace/general_extractor.py:L387-L387). Segments are
   processed in order, so output order follows segment order, then slice order within a
   segment (mempalace/general_extractor.py:L390-L450).
2. Segments whose stripped length is less than 20 characters are skipped
   (mempalace/general_extractor.py:L391-L392).
3. Prose is extracted from the segment (code lines removed) and used for scoring; the
   original segment text is what gets stored (mempalace/general_extractor.py:L394-L394, L425-L425).
4. The prose is scored against all five marker sets; only types with a score greater than
   0 are retained in a score map (mempalace/general_extractor.py:L397-L401). If no type
   scores above 0, the segment is skipped (mempalace/general_extractor.py:L403-L404).
5. A length bonus is added based on the original (un-stripped) segment length: `+2` if
   length > 500, `+1` if length > 200, otherwise `+0` (mempalace/general_extractor.py:L407-L412).
6. The winning type is the one with the highest raw score (ties resolved by the underlying
   max selection over the score map); `max_score = winning_score + length_bonus`
   (mempalace/general_extractor.py:L414-L415).
7. The winning type may be reclassified by disambiguation (see below)
   (mempalace/general_extractor.py:L418-L418).
8. Confidence is `min(1.0, max_score / 5.0)`. If confidence is strictly less than
   `min_confidence`, the segment is discarded (mempalace/general_extractor.py:L421-L423).
9. The segment is stripped and stored. If its length is at most `chunk_size`, it is stored
   as a single memory. Otherwise it is sliced into consecutive `chunk_size`-character
   pieces (offsets 0, chunk_size, 2*chunk_size, …), each emitted as a separate memory with
   the same `memory_type` (mempalace/general_extractor.py:L425-L448). Note the
   chunk-size comparison and slicing use the stripped `content`, while the length bonus in
   step 5 uses the un-stripped segment (mempalace/general_extractor.py:L407-L412, L425-L432).

Invariant: all slices of an oversized segment carry the identical `memory_type` of the
parent segment — sub-slices are never re-classified, to avoid silent data loss when a
marker lives in only one slice (mempalace/general_extractor.py:L426-L431).

#### Scoring contract (`_score_markers`)

Scoring lowercases the text, then for each marker regex finds all matches; the score
increments by the count of matches for that marker (so multiple markers and multiple
occurrences accumulate additively as a float) (mempalace/general_extractor.py:L347-L357).
Returned keywords are the de-duplicated set of matched substrings (or group 0 / the
marker pattern when a match group is empty) (mempalace/general_extractor.py:L356-L357).
Only the numeric score is consumed by `extract_memories`; the keyword list is discarded
there (mempalace/general_extractor.py:L399-L399).

#### Disambiguation (`_disambiguate`)

Corrects misclassifications using sentiment and resolution signals
(mempalace/general_extractor.py:L271-L288):
- If the winning type is `problem` AND the text contains a resolution signal: when the
  score map has `emotional > 0` and sentiment is `positive`, reclassify to `emotional`;
  otherwise reclassify to `milestone` (mempalace/general_extractor.py:L276-L279).
- Else if the winning type is `problem` AND sentiment is `positive`: reclassify to
  `milestone` if `milestone > 0` in the score map, else to `emotional` if `emotional > 0`
  (mempalace/general_extractor.py:L282-L286).
- Otherwise the type is unchanged (mempalace/general_extractor.py:L288-L288).

Sentiment (`_get_sentiment`) tokenizes the text into a lowercased word set and compares
the count of words intersecting a fixed positive-word set vs. a fixed negative-word set:
returns `positive` if positives exceed negatives, `negative` if negatives exceed
positives, else `neutral` (mempalace/general_extractor.py:L242-L251). The positive and
negative word sets are fixed and part of the contract (mempalace/general_extractor.py:L178-L239).

Resolution (`_has_resolution`) returns true if the lowercased text matches any of a fixed
set of resolution patterns (`fixed`, `solved`, `resolved`, `patched`, `got it working`,
`it works`, `nailed it`, `figured (it) out`, `the (fix|answer|solution)`)
(mempalace/general_extractor.py:L254-L268).

#### Prose extraction and code filtering

`_extract_prose` splits the segment into lines, toggles a code-fence state on lines whose
stripped form starts with ``` ` ``` (such fence lines and lines inside fences are dropped),
and otherwise keeps lines that are not classified as code lines. The joined, stripped prose
is returned; if that result is empty, the original text is returned unchanged
(mempalace/general_extractor.py:L325-L339).

`_is_code_line` returns false for blank lines (mempalace/general_extractor.py:L313-L315).
A non-blank line is a code line if its stripped form matches any of a fixed set of patterns
(shell prompts, common shell/VCS/tooling commands, code-fence start, language keywords like
import/def/class/function/const, ALL-CAPS assignments, table pipes, dashed rules, lone
braces/brackets, control-flow keywords, method-call lines, simple assignment-from-attribute
lines) (mempalace/general_extractor.py:L295-L318). Additionally, a line longer than 10
characters whose alphabetic-character ratio is below 0.4 is treated as code
(mempalace/general_extractor.py:L319-L322).

#### Segmentation (`_split_into_segments`)

Splits text into segments for extraction (mempalace/general_extractor.py:L453-L493):
1. The text is scanned line by line for speaker-turn markers using three patterns: a line
   starting with `> ` (quoted turn), a line starting with `Human:`/`User:`/`Q:`
   (case-insensitive), and a line starting with `Assistant:`/`AI:`/`A:`/`Claude:`/`ChatGPT:`
   (case-insensitive) (mempalace/general_extractor.py:L462-L475).
2. If at least 3 turn-marker lines are found, the text is split by turns: a new segment
   begins at each turn-marker line, accumulating subsequent non-marker lines into the
   current segment; the final accumulated segment is flushed at the end
   (mempalace/general_extractor.py:L477-L479, L496-L514).
3. Otherwise it falls back to paragraph splitting on blank-line boundaries (`\n\n`), keeping
   only non-empty stripped paragraphs (mempalace/general_extractor.py:L481-L482).
4. Special case: if paragraph splitting yields at most 1 paragraph but the text has more
   than 20 lines, the text is instead chunked into groups of 25 consecutive lines, joined
   and stripped, dropping empty groups (mempalace/general_extractor.py:L484-L491).

## CLI Contract

When invoked as a standalone program (mempalace/general_extractor.py:L521-L550):
- With fewer than 2 arguments (no file path), it prints a usage message describing that it
  extracts decisions, preferences, milestones, problems, and emotional moments, and exits
  with status code `1` (mempalace/general_extractor.py:L524-L529).
- Otherwise it reads the file at `argv[1]` as UTF-8 with malformed bytes replaced
  (`errors="replace"`) (mempalace/general_extractor.py:L531-L533).
- It runs `extract_memories` with default parameters (mempalace/general_extractor.py:L535-L535).
- It prints a header `Extracted <N> memories:`, then for each type in the fixed order
  `decision, preference, milestone, problem, emotional` prints a line `  <type padded to 12>
  <count>` only when that count is non-zero (mempalace/general_extractor.py:L537-L545).
- After a blank line, it prints a preview of up to the first 10 memories, each as
  `  [<type padded to 10>] <first 80 chars of content, newlines replaced with spaces>...`
  (mempalace/general_extractor.py:L547-L550).

## Edge Cases

- Empty or whitespace-only input yields an empty memory list because all segments fail the
  length or scoring gates (mempalace/general_extractor.py:L391-L404).
- A segment that scores above 0 but whose confidence falls below `min_confidence` is
  silently dropped (mempalace/general_extractor.py:L421-L423).
- All matching is case-insensitive because scoring, sentiment, and resolution lowercase
  their input before matching (mempalace/general_extractor.py:L244-L246, L256-L256, L349-L349).
- No filesystem, network, process, or environment side effects occur in the library
  functions; the only I/O is the CLI file read and stdout prints
  (mempalace/general_extractor.py:L531-L550).
