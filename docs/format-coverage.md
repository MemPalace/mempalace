# Format coverage — `mempalace mine --mode extract`

**Shipped in mempalace 3.3.6 (PR #1555).**
**Module:** `mempalace/format_miner.py`
**CLI entry:** `mempalace/cli.py` — `cmd_mine` dispatcher routes `--mode extract` here.
**Tests:** `tests/test_format_miner.py` — covers all 14 fringe cases plus orchestrator behavior. See the file for the current test inventory (the count drifts as polish PRs land).

---

## What it does

Adds a third miner alongside the existing two:

```
mempalace mine <dir>                  → miner.py        (project files: code, docs, notes)
mempalace mine <dir> --mode convos    → convo_miner.py  (chat exports)
mempalace mine <dir> --mode extract   → format_miner.py (binary office documents)   ← NEW
```

Supported formats and the transformer each routes through:

| Extension | Transformer | Python | License |
|---|---|---|---|
| `.pdf`    | MarkItDown | ≥ 3.10 | MIT |
| `.docx`   | MarkItDown | ≥ 3.10 | MIT |
| `.pptx`   | MarkItDown | ≥ 3.10 | MIT |
| `.xlsx`   | MarkItDown | ≥ 3.10 | MIT |
| `.epub`   | MarkItDown | ≥ 3.10 | MIT |
| `.rtf`    | **striprtf** | ≥ 3.6 | MIT |

**Per-format routing — why `.rtf` doesn't go through MarkItDown.** Verified live on 2026-05-19 against a local mixed-format test directory: MarkItDown 0.1.5 does NOT convert `.rtf`. It returns the raw RTF control-code source unchanged (`{\rtf1\ansi\ansicpg1252...`). For RTF input, format_miner routes to [striprtf](https://github.com/joshy/striprtf) (pure-Python, MIT, ~150 lines, purpose-built for RTF → plain text). The principle stays intact: transform at read time, never modify the source — only the choice of transformer is per-format.

Both libraries are **optional** runtime dependencies. Users who only mine projects or convos don't pay either install footprint. Users on Python 3.9 can still install `striprtf` to extract RTF; MarkItDown requires 3.10+ and silently skips if not present. Per-extension install messages route to the right library:

- Missing MarkItDown (any of pdf/docx/pptx/xlsx/epub) → `SKIP_NO_MARKITDOWN` → log: `pip install markitdown`
- Missing striprtf (any `.rtf`) → `SKIP_NO_STRIPRTF` → log: `pip install striprtf`

---

## Design — read-time conversion, never modifies source files

Same architectural principle as virtual line numbering (the other 3.3.6 feature in this package): **transform at read time, leave storage untouched.**

```
PDF on disk → (read bytes into memory) → MarkItDown → Markdown text → existing chunker → drawers in ChromaDB
```

- The PDF on the user's disk is read but **never modified**.
- No intermediate `.md` files are written to the user's filesystem.
- The conversion lives entirely in memory; once the text reaches the existing chunker, it's handled identically to any other ingestion.
- Closets / drawers / halls all work the same — the format miner produces the same drawer-shape as the other miners.

---

## Why a new miner, not edits to `miner.py`

Three reasons, in order of importance:

1. **Matches the existing miner-per-content-type pattern.** Mempalace already has `miner.py` and `convo_miner.py`. A third miner is a third instance of the same shape — immediately recognizable to anyone reading the codebase.
2. **Smaller review surface.** `format_miner.py` is a pure addition. Reviewer reads one new file in isolation; the stable code paths in `miner.py` and `convo_miner.py` are untouched.
3. **Cleaner failure isolation.** If the format miner has a bug, the existing miners are physically incapable of being affected.

The trade-off vs. integrating into `miner.py` was decided during the 2026-05-19 design pass: **the new-miner pattern wins on architecture and on review surface.**

---

## Fringe cases handled — 13 (original 12 + striprtf parity)

Each case maps to a specific `ExtractionStatus` enum value so callers (and reviewers) can audit exactly which path fired. All thirteen are covered by dedicated tests in `tests/test_format_miner.py`.

| # | Case | Status code | Behavior |
|---|---|---|---|
| 1 | MarkItDown not installed (any non-RTF format) | `SKIP_NO_MARKITDOWN` | Logs `pip install markitdown` instruction; skips file |
| 2 | File too large (> 500 MB default; caller can override) | `SKIP_TOO_LARGE` | Logs size and threshold; skips |
| 3 | iCloud cloud-only file (`.icloud` suffix OR `st_flags` dataless bit) | `SKIP_CLOUD_ONLY` | Detected before any I/O that would trigger materialization; skips |
| 4 | Encrypted / password-protected PDF | `SKIP_ENCRYPTED` | Catches exceptions matching `encrypt`/`decrypt`/`password`/`protected`; skips |
| 5 | Empty file (zero bytes) | `SKIP_EMPTY` | Silent skip |
| 6 | Permission denied | `SKIP_PERMISSION` | Catches `PermissionError`; logs; skips |
| 7 | Broken symlink (target missing) | `SKIP_BROKEN_SYMLINK` | Detected via `is_symlink()` + `not exists()`; skips |
| 8 | Dirty encoding in extracted text | (recovered) | `decode_robust()` falls back UTF-8 → CP1252 → UTF-8-with-replace; never raises |
| 9 | Windows path semantics | (no special status) | `pathlib.Path` throughout; case-insensitive suffix matching; accepts `str` or `Path` input |
| 10 | Transformer internal crash on malformed file | `SKIP_EXTRACTION_ERROR` | Catches generic `Exception` at file boundary; logs filename + exception type + message tail; skips |
| 11 | Network / sync-drive timeout | `SKIP_NETWORK_TIMEOUT` | Catches `TimeoutError`; skips |
| 12 | Unrecognized extension | `SKIP_UNRECOGNIZED` | Cheap suffix-set check; skips |
| 13 | **striprtf not installed (any `.rtf`)** | `SKIP_NO_STRIPRTF` | Logs `pip install striprtf` instruction; skips file. Added 2026-05-19 after a live integration test surfaced MarkItDown 0.1.5's lack of RTF support. |

Plus `SKIP_UNREADABLE` for catch-all `OSError` during stat (not in the original list, added for defensive completeness — see file-stat block in `extract_text`).

---

## Cases NOT solved by this miner (deferred)

Per the spec, these are documented limits, not bugs:

- **Custom PDF parsers for specific document types.** MarkItDown does its best; we accept its limits.
- **OCR on scanned image PDFs.** Separate concern, opt-in feature for a later release.
- **DRM-locked files.** We can't bypass DRM and shouldn't try.
- **Pathological corrupt files.** They're corrupt — `SKIP_EXTRACTION_ERROR` with a clear log line is the responsible outcome.

These limitations are reported per-file via the skip-code log, so the user always knows what didn't make it in and why.

---

## API reference

All public symbols are exported via `__all__`. Two functions and one enum form the contract:

### `extract_text(path, max_file_size=DEFAULT_MAX_FILE_SIZE) -> tuple[Optional[str], ExtractionStatus]`

Convert one file to text. Returns `(text, status)` where `text` is the extracted Markdown for `OK` cases and `None` for every skip. Pure function — source file at `path` is never modified.

Accepts both `Path` and `str` for `path`.

### `scan_formats(directory) -> list[Path]`

Walk `directory` recursively, return supported files sorted by path. Skips hidden / build directories (`.git`, `.venv`, `__pycache__`, etc.) and OS metadata files (`.DS_Store`, `Thumbs.db`, `desktop.ini`).

### `ExtractionStatus` (Enum)

Twelve documented values plus `SKIP_UNREADABLE`. Each is paired with a string value like `"skip:no_markitdown"` for log readability; tests assert on `.name` for stability.

### `decode_robust(raw: bytes) -> str`

Identical to the helper in the terminal-session normalizer. UTF-8 first, CP1252 fallback, UTF-8-with-replace as final safety net. Never raises.

### `is_icloud_dataless(path: Path) -> bool`

Two signals:
1. Literal `.icloud` suffix (iCloud's offloaded-file placeholder convention)
2. macOS `st_flags` dataless bit (`0x40000000`) — best-effort, gracefully degrades on non-macOS

Returning `True` causes `extract_text` to short-circuit to `SKIP_CLOUD_ONLY` before any I/O that would trigger iCloud materialization.

---

## CLI integration

One-line addition to `mempalace/cli.py` in `cmd_mine`:

```python
def cmd_mine(args):
    ...
    try:
        if args.mode == "convos":
            from .convo_miner import mine_convos
            mine_convos(...)
        elif args.mode == "extract":                       # ← NEW
            from .format_miner import mine_formats         # ← NEW
            mine_formats(                                  # ← NEW
                format_dir=args.dir,                       # ← NEW
                palace_path=palace_path,                   # ← NEW
                wing=args.wing,                            # ← NEW
                agent=args.agent,                          # ← NEW
                limit=args.limit,                          # ← NEW
                dry_run=args.dry_run,                      # ← NEW
            )                                              # ← NEW
        else:
            from .miner import mine
            mine(...)
```

And add `"extract"` to the `--mode` choices in the argparse setup:

```python
p_mine.add_argument(
    "--mode",
    choices=["projects", "convos", "extract"],  # ← extract added
    default="projects",
    help="Ingest mode: 'projects' (default), 'convos' for chat exports, 'extract' for office documents",
)
```

That's the entire CLI change. Two argparse edits + one `elif` branch.

---

## Empirical smoke test — local mixed-format directory

Run on a local test directory containing **52 `.rtf` files + 11 `.pdf` files** (a mix of long-form correspondence and research/architecture documents, no synthetic fixtures):

```
Supported formats: ['.docx', '.epub', '.pdf', '.pptx', '.rtf', '.xlsx']
scan_formats: found 63 supported files
```

**Live end-to-end extraction (2026-05-19, Python 3.13, MarkItDown 0.1.5 + striprtf 0.0.27):**

| Format | Files | Result | Chars extracted |
|---|---|---|---|
| RTF (via striprtf) | 52 | **52/52 OK** | 1,350,679 |
| PDF (via MarkItDown) | 11 | 10 OK, 1 SKIP_ENCRYPTED | 1,340,869 |

The single PDF skip is a real password-protected file — Fringe Case 4 (`SKIP_ENCRYPTED`) fired correctly on real data via the exception-message matcher. Designed behavior validated empirically.

Anti-regression assertion: **zero raw `\rtf1` / `\ansi` control codes leak into extracted text**. Without the per-format routing fix, 52 of 52 RTFs would have ingested as unreadable control-code source. With the fix, every RTF yields clean plain text. This is the integration bug that mocked unit tests cannot catch and that motivated the `SKIP_NO_STRIPRTF` status + per-format dispatch.

---

## Backwards compatibility

- **No changes to existing miners.** `miner.py` and `convo_miner.py` are not touched.
- **No new required dependencies.** MarkItDown and striprtf are optional extras (`pip install markitdown striprtf` or `pip install mempalace[extract]` if declared as such in `pyproject.toml`). MarkItDown requires Python ≥ 3.10; striprtf works on 3.6+. Recommended pyproject entries use environment markers so 3.9 users automatically get only striprtf:
  ```toml
  [project.optional-dependencies]
  extract = [
      "striprtf>=0.0.27",
      "markitdown>=0.1.5; python_version >= '3.10'",
  ]
  ```
- **No on-disk format changes.** Drawers / closets / halls have the exact same shape regardless of which miner produced them.
- **No migration step.** Users who already have a palace mined from the other modes can run `mempalace mine --mode extract ~/docs/` and the new drawers land in the same palace, same wing, same room conventions.
- **Optional `--max-file-size` flag** can be added to `mempalace mine` so users with legitimate large files (e.g. scanned books) can raise the cap from the 500 MB default.

---

## Out of scope for 3.3.6

- **Streaming extraction for huge PDFs** (>500 MB). MarkItDown's default behavior is whole-file in memory. For users with multi-GB PDFs, a streaming path via `pypdf` page-by-page is the natural extension. Deferred to 3.3.7 or 3.4.0.
- **Audio / video transcription.** Whisper integration is its own scope.
- **OCR for scanned PDFs.** Tesseract / cloud OCR integration is its own scope.
- **Migration importers** (Notion / Obsidian / Mem0 export formats). Each is a separate adapter; not bundled here.

These are real future features; they don't belong in a 3.3.6 release whose scope is "additive format coverage via MarkItDown."
