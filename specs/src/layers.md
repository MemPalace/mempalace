# Behavior Specification — `layers.py` (4-Layer Memory Stack)

This module implements a 4-layer memory retrieval stack over a palace store. Each
layer loads progressively more content: L0 identity, L1 essential story, L2
on-demand filtered retrieval, L3 unlimited semantic search. Layers read from a
palace collection (the drawers store) and from an on-disk identity text file
(`mempalace/layers.py:L1-L17`).

## External dependencies / contracts

- A collection is obtained via a palace-collection accessor given a palace path and
  a `create=False` flag; this is the read-only handle used by every layer
  (`mempalace/layers.py:L24-L31`, `mempalace/layers.py:L100-L100`).
- Default palace path comes from configuration when not explicitly supplied
  (`mempalace/layers.py:L92-L95`, `mempalace/layers.py:L199-L201`,
  `mempalace/layers.py:L258-L260`, `mempalace/layers.py:L378-L381`).
- Default identity file path is `~/.mempalace/identity.txt` (user home expanded)
  (`mempalace/layers.py:L51-L54`, `mempalace/layers.py:L381-L381`).
- Drawer records carry document text plus a metadata map with keys including
  `wing`, `room`, `source_file`, and optional weight keys `importance`,
  `emotional_weight`, `weight` (`mempalace/layers.py:L131-L144`,
  `mempalace/layers.py:L153-L153`, `mempalace/layers.py:L297-L299`).

## Layer 0 — Identity

`Layer0(identity_path=None)`: if `identity_path` is omitted, defaults to
`~/.mempalace/identity.txt` (`mempalace/layers.py:L51-L54`).

`render() -> str`: returns the identity text. On first call, if the file exists its
contents are read and trimmed of surrounding whitespace; the result is cached for
subsequent calls (`mempalace/layers.py:L57-L70`). If the file does not exist, it
returns the literal default string
`"## L0 — IDENTITY\nNo identity configured. Create ~/.mempalace/identity.txt"`
(`mempalace/layers.py:L65-L68`).

`token_estimate() -> int`: returns the rendered text length divided by 4 (integer
division) as a token estimate (`mempalace/layers.py:L72-L73`).

## Layer 1 — Essential Story

`Layer1(palace_path=None, wing=None)`: stores the palace path (config default if
omitted) and an optional wing filter (`mempalace/layers.py:L92-L95`).

Constants governing output (`mempalace/layers.py:L88-L90`):
- At most 15 drawers (moments) included.
- Total L1 text hard-capped at 3200 characters.
- Scans at most 2000 drawers when generating.

`generate() -> str` behavior:

1. Opens the collection read-only. If opening fails, returns the literal string
   `"## L1 — No palace found. Run: mempalace mine <dir>"`
   (`mempalace/layers.py:L99-L102`).
2. Fetches drawers in batches of 500, requesting documents and metadata, paging by
   offset. If a `wing` filter is set, applies a `{"wing": <wing>}` filter on each
   batch (`mempalace/layers.py:L104-L122`). Pagination stops when: a fetch fails,
   a batch returns no documents, a batch is smaller than 500, or accumulated
   documents reach the 2000 scan cap (`mempalace/layers.py:L112-L124`).
3. If no documents collected, returns `"## L1 — No memories yet."`
   (`mempalace/layers.py:L126-L127`).
4. Scores each drawer with a default importance of 3; the first present value among
   metadata keys `importance`, `emotional_weight`, `weight` is parsed as a float
   and used as the importance (parse failures fall back to 3, and the loop stops at
   the first key that is present regardless of parse success)
   (`mempalace/layers.py:L130-L144`).
5. Sorts by importance descending and keeps the top 15
   (`mempalace/layers.py:L146-L148`).
6. Groups selected drawers by their `room` metadata (default `"general"`)
   (`mempalace/layers.py:L150-L154`).

Output format (`mempalace/layers.py:L156-L184`):
- First line is the literal header `## L1 — ESSENTIAL STORY`.
- Rooms are emitted in sorted (alphabetical) order; each room begins with a blank
  line followed by `[<room>]`.
- Each entry line is `  - <snippet>`, where snippet is the document with newlines
  replaced by spaces, leading/trailing whitespace stripped, and truncated to 197
  characters plus `"..."` if longer than 200 characters.
- If the drawer's `source_file` metadata is set, the entry appends `  (<basename>)`
  using only the file's base name (`mempalace/layers.py:L166-L175`).
- Output is bounded by the 3200-character cap: before appending an entry, if the
  running length plus the entry length would exceed the cap, the line
  `  ... (more in L3 search)` is appended and the result returned immediately
  (`mempalace/layers.py:L177-L179`).
- The running length counter includes room header lines and entry lines
  (`mempalace/layers.py:L159-L182`).

## Layer 2 — On-Demand

`Layer2(palace_path=None)`: config-default palace path if omitted
(`mempalace/layers.py:L199-L201`).

`retrieve(wing=None, room=None, n_results=10) -> str`:

1. Opens the collection read-only; on failure returns `"No palace found."`
   (`mempalace/layers.py:L205-L208`).
2. Builds a where-filter from `wing`/`room` via the shared filter builder; the
   filter is only applied if non-empty (`mempalace/layers.py:L210-L214`).
3. Fetches up to `n_results` drawers (documents + metadata). If the fetch raises,
   returns `"Retrieval error: <error>"` (`mempalace/layers.py:L216-L219`).
4. If no documents found, returns `"No drawers found for <label>."`, where label is
   `wing=<wing>` and/or `room=<room>` depending on which were supplied (joined with
   a space when both) (`mempalace/layers.py:L221-L228`).
5. Output: first line `## L2 — ON-DEMAND (<count> drawers)`; each entry is
   `  [<room>] <snippet>` with room defaulting to `?`, snippet truncated to 297
   chars + `"..."` if longer than 300, and an optional `  (<basename>)` suffix when
   `source_file` is set (`mempalace/layers.py:L230-L244`).

## Layer 3 — Deep Search

`Layer3(palace_path=None)`: config-default palace path if omitted
(`mempalace/layers.py:L258-L260`).

`search(query, wing=None, room=None, n_results=5) -> str`:

1. Opens the collection read-only; on failure returns `"No palace found."`
   (`mempalace/layers.py:L264-L267`).
2. Builds optional where-filter from `wing`/`room` (`mempalace/layers.py:L269-L277`).
3. Runs a semantic query for the given query text requesting up to `n_results`
   matches with documents, metadata, and distances. If the query raises, returns
   `"Search error: <error>"` (`mempalace/layers.py:L271-L282`).
4. If no documents returned, returns `"No results found."`
   (`mempalace/layers.py:L284-L289`).
5. Computes a similarity from each distance using the collection's distance metric,
   rounded to 3 decimals (`mempalace/layers.py:L291-L296`).
6. Output: header `## L3 — SEARCH RESULTS for "<query>"`; for each result indexed
   from 1, two or three lines: `  [<i>] <wing>/<room> (sim=<similarity>)`, then
   `      <snippet>` (snippet stripped, newlines to spaces, truncated to 297 + `...`
   beyond 300), and `      src: <basename>` when `source_file` present. `wing` and
   `room` default to `?` (`mempalace/layers.py:L292-L310`).

`search_raw(query, wing=None, room=None, n_results=5) -> list`: same query path but
returns a list of result dicts instead of text. On collection-open failure or query
failure, returns an empty list (`mempalace/layers.py:L312-L334`). Each dict has keys
`text` (document, empty string if missing), `wing` (default `"unknown"`), `room`
(default `"unknown"`), `source_file` (base name of source path, `?` if missing),
`similarity` (distance-to-similarity rounded to 3), and `metadata` (the full
metadata map) (`mempalace/layers.py:L336-L360`). Results with missing
document/metadata degrade gracefully: the hit still appears with its real distance
and fallback values for missing fields (`mempalace/layers.py:L343-L349`).

## MemoryStack — unified interface

`MemoryStack(palace_path=None, identity_path=None)`: resolves palace path from
config default and identity path to `~/.mempalace/identity.txt` if omitted, then
constructs one of each layer (`mempalace/layers.py:L378-L386`).

`wake_up(wing=None) -> str`: concatenates L0 identity, a blank line, then L1
essential story, joined by newlines. If `wing` is provided, it is applied as the L1
wing filter before generation (`mempalace/layers.py:L388-L407`). This is described
as roughly 600-900 tokens (`mempalace/layers.py:L388-L392`).

`recall(wing=None, room=None, n_results=10) -> str`: delegates to L2 retrieve
(`mempalace/layers.py:L409-L411`).

`search(query, wing=None, room=None, n_results=5) -> str`: delegates to L3 search
(`mempalace/layers.py:L413-L415`).

`status() -> dict`: returns a status map with `palace_path`; nested `L0_identity`
containing the identity `path`, an `exists` boolean (filesystem check on the
identity path), and `tokens` (L0 token estimate); descriptive entries for
`L1_essential`, `L2_on_demand`, `L3_deep_search`; and `total_drawers` equal to the
collection count, or 0 if the collection cannot be opened or counted
(`mempalace/layers.py:L417-L445`).

## CLI (standalone)

When run as a script, the module exposes a command-line interface
(`mempalace/layers.py:L452-L513`).

- With no subcommand, prints usage and exits with code 0
  (`mempalace/layers.py:L455-L467`).
- Argument parsing: any `--key=value` argument becomes a flag `key=value`; any
  non-`--` argument is positional (`mempalace/layers.py:L471-L479`).
- An optional `--palace=<path>` flag overrides the palace path for the stack
  (`mempalace/layers.py:L481-L482`).

Commands (`mempalace/layers.py:L484-L513`):
- `wake-up` / `wakeup`: prints a header `Wake-up text (~<tokens> tokens):`, a line
  of 50 `=` characters, then the wake-up text; token count is text length // 4.
  Honors `--wing` (`mempalace/layers.py:L484-L490`).
- `recall`: prints L2 retrieval text, honoring `--wing` and `--room`
  (`mempalace/layers.py:L492-L496`).
- `search`: joins positional args as the query; if the query is empty prints a usage
  line and exits with code 1; otherwise prints L3 search text, honoring `--wing` and
  `--room` (`mempalace/layers.py:L498-L506`).
- `status`: prints the status dict as indented JSON (`mempalace/layers.py:L508-L510`).
- Any unrecognized command prints usage and exits with code 0
  (`mempalace/layers.py:L512-L513`).

## Side effects

- Reads the identity file from disk (`mempalace/layers.py:L62-L64`) and checks its
  existence (`mempalace/layers.py:L62-L62`, `mempalace/layers.py:L423-L423`).
- Opens and reads the palace collection (read-only, never creating it)
  (`mempalace/layers.py:L100-L100`, `mempalace/layers.py:L206-L206`,
  `mempalace/layers.py:L265-L265`, `mempalace/layers.py:L439-L439`).
- CLI writes to standard output and sets process exit codes (0 on usage/no-command,
  1 on empty search query) (`mempalace/layers.py:L464-L502`).
