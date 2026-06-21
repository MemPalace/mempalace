# Behavior Specification — `onboarding.py`

MemPalace first-run interactive setup. It collects mode, people, projects, wings, and embedding-model preference from the user, seeds the entity registry, and writes two bootstrap files describing the user's world (mempalace/onboarding.py:L1-L17).

## Module-level constants

### `DEFAULT_WINGS`
A fixed mapping from mode name to an ordered list of default wing names (mempalace/onboarding.py:L28-L51):
- `"work"` → `projects, clients, team, decisions, research` (mempalace/onboarding.py:L29-L35)
- `"personal"` → `family, health, creative, reflections, relationships` (mempalace/onboarding.py:L36-L42)
- `"combo"` → `family, work, health, creative, projects, reflections` (mempalace/onboarding.py:L43-L50)

These are the three valid modes; every wing/people/project flow keys off one of these strings.

## Interactive prompt primitives

`_ask(prompt, default=None)` reads one line of user input. If a default is provided it is shown in brackets and substituted when the user enters an empty (whitespace-stripped) string; otherwise it returns the stripped input (mempalace/onboarding.py:L69-L73).

`_yn(prompt, default="y")` reads a yes/no line, lowercased and stripped. Empty input returns the boolean of the default (true when default is `"y"`). Any non-empty input is true only when it starts with `"y"` (mempalace/onboarding.py:L76-L80).

## Step 1 — Embedding model selection: `_ask_embedding_model() -> str`

Prompts whether to use the multilingual embedding model, defaulting to yes. Returns `"embeddinggemma"` when the user accepts (default), and `"minilm"` when declined (mempalace/onboarding.py:L88-L110). The observable contract is the two literal return strings; multilingual is the recommended default because cross-lingual recall degrades badly under the English-only model (mempalace/onboarding.py:L89-L96).

## Step 1 — Mode selection: `_ask_mode() -> str`

Prints a welcome and three numbered options, then loops reading input until a valid choice is given. Input `"1"` returns `"work"`, `"2"` returns `"personal"`, `"3"` returns `"combo"`; any other input reprints the prompt and loops (mempalace/onboarding.py:L113-L137). The function does not terminate until one of the three values is returned.

## Step 2 — People collection: `_ask_people(mode) -> (list, dict)`

Returns a tuple `(people_list, aliases_dict)` (mempalace/onboarding.py:L145-L192).

People are dicts with keys `name` (string), `relationship` (string), and `context` (string, `"personal"` or `"work"`). Aliases map nickname string → full-name string (mempalace/onboarding.py:L147-L148, L171, L190).

When mode is `"personal"` or `"combo"`, a personal-people loop runs first (mempalace/onboarding.py:L150-L171). When mode is `"work"` or `"combo"`, a work-people loop runs after (mempalace/onboarding.py:L173-L190). For `"combo"`, personal entries are appended before work entries, fixing list ordering.

Each loop reads one entry per iteration; entering `"done"` (case-insensitive) or an empty string ends that loop (mempalace/onboarding.py:L160-L162, L183-L184). Each entry is split on the first comma into name and (relationship/role); the part before the comma is the name, the part after (if any) is the relationship/role; absent comma yields an empty relationship (mempalace/onboarding.py:L163-L165, L186-L188). Entries with an empty name are discarded (mempalace/onboarding.py:L166, L189).

In the personal loop only, after a valid name the user is additionally prompted for a nickname; a non-empty nickname is recorded as `aliases[nickname] = name` (mempalace/onboarding.py:L167-L170). The work loop does not prompt for nicknames (mempalace/onboarding.py:L186-L190).

## Step 3 — Projects collection: `_ask_projects(mode) -> list`

For mode `"personal"` returns an empty list without prompting (mempalace/onboarding.py:L201-L202). For other modes, loops reading project names; `"done"` (case-insensitive) or empty input ends the loop; empty-after-strip names are skipped; returns the ordered list of project name strings (mempalace/onboarding.py:L204-L218).

## Step 4 — Wings: `_ask_wings(mode) -> list`

Looks up the default wing list for the mode and offers it. If the user enters a non-empty comma-separated string, returns that string split on commas with each element stripped and empty elements removed; otherwise returns the default list unchanged (mempalace/onboarding.py:L226-L240).

## Step 5 — Auto-detection: `_auto_detect(directory, known_people) -> list`

Scans a directory for additional candidate entities. Builds a set of lowercased known names, scans files via the detection helpers, and returns detected people whose lowercased name is not already known AND whose `confidence` is at least `0.7` (mempalace/onboarding.py:L248-L262). Any exception during scanning/detection is swallowed and results in an empty list (mempalace/onboarding.py:L252-L264). When no scannable files are found it returns an empty list (mempalace/onboarding.py:L254-L255). Returned candidate dicts carry at least `name`, `confidence` (a fraction), and `signals` (a list) (mempalace/onboarding.py:L257-L262, L425-L429).

## Step 6 — Ambiguity warning: `_warn_ambiguous(people) -> list`

Returns the list of people names (original casing) whose lowercased form appears in a known set of common English words (mempalace/onboarding.py:L272-L283). This is informational only; it does not remove any entry.

## Bootstrap file generation: `_generate_aaak_bootstrap(people, projects, wings, mode, config_dir=None)`

Determines the output directory: the given `config_dir` if provided, otherwise `~/.mempalace`. The directory is created if missing (parents included) (mempalace/onboarding.py:L298-L299).

### Entity codes
Each person is assigned an AAAK code: the first 3 characters of the name, uppercased. On collision (the candidate code already assigned to another person), the code is recomputed as the first 4 characters uppercased (mempalace/onboarding.py:L301-L309).

### File `aaak_entities.md`
Written (UTF-8) to `<dir>/aaak_entities.md` (mempalace/onboarding.py:L340). On-disk format (newline-joined) (mempalace/onboarding.py:L312-L340):
- Header lines: `# AAAK Entity Registry`, an auto-generated comment, a blank line, and `## People` (mempalace/onboarding.py:L312-L317).
- One line per person: `  CODE=Name (relationship)` when a relationship exists, else `  CODE=Name` (mempalace/onboarding.py:L318-L322).
- If projects exist: a blank line, `## Projects`, then one line per project `  CODE=Project` where the project code is the first 4 characters uppercased (mempalace/onboarding.py:L324-L328).
- A trailing `## AAAK Quick Reference` block with three fixed reference lines describing symbols and structure (mempalace/onboarding.py:L330-L338).

### File `critical_facts.md`
Written (UTF-8) to `<dir>/critical_facts.md` (mempalace/onboarding.py:L387). On-disk format (newline-joined) (mempalace/onboarding.py:L343-L387):
- Header line `# Critical Facts (bootstrap — will be enriched after mining)` followed by a blank line (mempalace/onboarding.py:L343-L346).
- Partition people by `context`: those with `context == "personal"` and those with `context == "work"` (mempalace/onboarding.py:L348-L349).
- If any personal people: section `## People (personal)`, one bullet per person `- **Name** (CODE) — relationship` (omitting `— relationship` when empty), then a blank line (mempalace/onboarding.py:L351-L359).
- If any work people: section `## People (work)` with the same bullet format and a trailing blank line (mempalace/onboarding.py:L361-L369).
- If any projects: section `## Projects` with bullets `- **Project**` and a trailing blank line (mempalace/onboarding.py:L371-L375).
- A `## Palace` section with `Wings: <comma-joined wings>`, `Mode: <mode>`, a blank line, and a note that the file will be enriched after mining (mempalace/onboarding.py:L377-L385).

## Main flow: `run_onboarding(directory=".", config_dir=None, auto_detect=True) -> EntityRegistry`

Executes the steps in fixed order and returns the seeded registry (mempalace/onboarding.py:L390-L482).

Ordering and side effects:
1. Ask mode (mempalace/onboarding.py:L400).
2. Ask embedding model, then persist the choice to config (`set_embedding_model`) so future runs are not re-prompted (mempalace/onboarding.py:L402-L407).
3. Ask people (mempalace/onboarding.py:L410).
4. Ask projects (mempalace/onboarding.py:L413).
5. Ask wings (mempalace/onboarding.py:L416).
6. If `auto_detect` is true and the user agrees to scan, prompt for a directory (defaulting to `directory`), run auto-detection, and present each candidate with its name, confidence as a percentage, and first signal (mempalace/onboarding.py:L419-L430). If the user agrees to add candidates, each is offered individually with `(p)erson, (s)kip`; answering `p` prompts for a relationship/role and a context, then appends the person to the people list (mempalace/onboarding.py:L431-L449). Context resolution: forced `"personal"` in personal mode, forced `"work"` in work mode, and in combo mode taken from a `(p)ersonal or (w)ork` prompt where `w`→`work` and `p`→`personal` (mempalace/onboarding.py:L436-L448).
7. Compute and display ambiguous names, if any (mempalace/onboarding.py:L452-L462).
8. Load the entity registry for `config_dir` and seed it with `mode`, `people`, `projects`, and `aliases` (mempalace/onboarding.py:L465-L466).
9. Generate the two bootstrap files (mempalace/onboarding.py:L469).
10. Print a completion summary including registry summary, wings, registry save path, and the two bootstrap file paths, then return the registry (mempalace/onboarding.py:L471-L482).

## Non-interactive setup: `quick_setup(mode, people, projects=None, aliases=None, config_dir=None, embedding_model=None) -> EntityRegistry`

Programmatic equivalent with no prompts. Loads the registry for `config_dir` and seeds it with the given mode, people, `projects or []`, and `aliases or {}` (mempalace/onboarding.py:L490-L514). `people` must be a list of dicts with keys `name`, `relationship`, `context` (mempalace/onboarding.py:L502). When `embedding_model` is non-null, it is persisted to config via `set_embedding_model`; when omitted, config is left untouched (and the hard default `"minilm"` governs) (mempalace/onboarding.py:L503-L518). Returns the seeded registry (mempalace/onboarding.py:L519). Note this differs from `run_onboarding`, which does not generate bootstrap files.

## CLI entry point

When executed as a script, the first command-line argument is used as the directory to scan (defaulting to `"."`), and `run_onboarding(directory=...)` is invoked (mempalace/onboarding.py:L526-L530). Documented invocations are `python3 -m mempalace.onboarding` and `mempalace init` (mempalace/onboarding.py:L14-L17).
