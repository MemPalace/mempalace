# project_scanner — Behavior Specification

Detects projects and people from build manifests and git history under a directory tree. Primary entity signal for `mempalace init`; the regex prose detector is a fallback (mempalace/project_scanner.py:L1-L16).

## Constants & Limits

- Directory names always skipped during any walk: `.git`, `node_modules`, `__pycache__`, `.venv`, `venv`, `env`, `dist`, `build`, `.next`, `coverage`, `.terraform`, `vendor`, `target`, `.mempalace`, `.cache`, `.pytest_cache`, `.mypy_cache`, `.ruff_cache`. Additionally, any directory whose name begins with `.` is skipped (mempalace/project_scanner.py:L38-L57, L365).
- Maximum walk depth is 6 levels below root; root is depth 0 (mempalace/project_scanner.py:L59, L363-L371).
- At most 1000 commits are read per repository (mempalace/project_scanner.py:L60, L293-L299).
- Git subprocess calls time out after 10 seconds by default; identity-config reads use a 2-second timeout (mempalace/project_scanner.py:L61, L251-L259, L267-L268, L274-L288).

## Data Shapes

### ProjectInfo
Fields: `name` (string), `repo_root` (path), `manifest` (string or null), `has_git` (bool, default false), `total_commits` (int, default 0), `user_commits` (int, default 0), `is_mine` (bool, default false) (mempalace/project_scanner.py:L67-L75).

Confidence is derived: 0.99 if `is_mine`; otherwise 0.7 if `has_git` and `total_commits > 0`; otherwise 0.85 (manifest-only, no git) (mempalace/project_scanner.py:L77-L83).

Signal string is a comma-joined list: includes the manifest filename when set; when `has_git`, appends one of: `"{user_commits} of your commits"` (mine with user commits), `"{user_commits}/{total_commits} yours"` (has user commits but not mine), or `"{total_commits} commits (none by you)"`. Empty result falls back to literal `"repo"` (mempalace/project_scanner.py:L85-L96).

### PersonInfo
Fields: `name` (string), `total_commits` (int, default 0), `emails` (set of strings), `repos` (set of strings) (mempalace/project_scanner.py:L99-L104).

Confidence: 0.99 if `total_commits >= 100` OR distinct-repo count `>= 3`; else 0.85 if `total_commits >= 20`; else 0.65 (mempalace/project_scanner.py:L106-L112).

Signal string: `"{total_commits} commit[s] across {repo_count} repo[s]"` with correct singular/plural on each count (mempalace/project_scanner.py:L114-L116).

## Manifest Detection

Recognized manifest filenames, each parsed to a project name:
- `package.json` → top-level `name` string (must be non-empty); invalid JSON or read error yields no name (mempalace/project_scanner.py:L122-L128, L228-L233).
- `pyproject.toml` → `project.name`, falling back to `tool.poetry.name`; both must be non-empty strings (mempalace/project_scanner.py:L141-L147).
- `Cargo.toml` → `package.name` (mempalace/project_scanner.py:L150-L153).
- `go.mod` → the path after `module ` on the first matching line, taking the last `/`-segment (mempalace/project_scanner.py:L156-L165).
- `pom.xml` → the first child element with local name `artifactId` (XML namespaces stripped), trimmed text (mempalace/project_scanner.py:L168-L181).
- Gradle (`settings.gradle`, `settings.gradle.kts`, `build.gradle`, `build.gradle.kts`) → `rootProject.name = "..."` or `rootProject.name.set("...")` from settings file (preferred when parsing a `build.gradle*` file) or from the file itself; if neither matches, falls back to the parent directory name (mempalace/project_scanner.py:L184-L213).

TOML parsing returns empty/no result when a TOML reader is unavailable or the file fails to parse (mempalace/project_scanner.py:L131-L138).

Manifest priority order (lower = higher priority): pyproject.toml(0), package.json(1), Cargo.toml(2), go.mod(3), pom.xml(4), settings.gradle(5), settings.gradle.kts(6), build.gradle(7), build.gradle.kts(8); unknown manifests sort after all known ones (mempalace/project_scanner.py:L215-L227).

Java-family manifests (`pom.xml` and the four Gradle files) are treated specially during scan to add sub-module projects (mempalace/project_scanner.py:L239-L245, L571-L587).

Within a repo, manifests are collected via a walk that does not descend into nested git repos, and sorted by: shallowest path first, then manifest priority, then lexicographic posix path for deterministic tie-breaking (mempalace/project_scanner.py:L379-L391, L410-L427).

## Git Helpers

A git command runs as `git -C <cwd> <args>`, returning stdout on exit code 0 and empty string otherwise or on any subprocess error (mempalace/project_scanner.py:L251-L262).

User identity is read first from the first repo's `git config user.name`/`user.email`; if both empty, falls back to global git config. Any failure yields empty strings (mempalace/project_scanner.py:L265-L290, L517-L522).

Authors are read from `git log --max-count=1000 --format=%aN|%aE`, parsing each `name|email` line into trimmed pairs (mempalace/project_scanner.py:L293-L305).

## Bot & Real-Name Filtering

A commit author is treated as a bot if its lowercased name matches any name pattern (e.g. `[bot]`, `dependabot`, `renovate`, `github-actions`, `-bot$`, `bot$` word-boundary, `snyk`, `semantic-release`, `pre-commit-ci`, etc.) OR its lowercased email matches `bot@`, `-bot@`, or `[bot]@`. GitHub privacy emails `@users.noreply.github.com` are deliberately NOT filtered (mempalace/project_scanner.py:L311-L343).

A name "looks like a real name" only if it contains a space, has at least two whitespace-separated parts, and both first and last parts start with an uppercase letter. Lowercase handles, single tokens, and digit-only handles are rejected (mempalace/project_scanner.py:L346-L357).

## Repo Discovery

`find_git_repos(root, max_depth=6)` resolves root, includes root itself if it has a `.git` marker (directory or file), then walks for nested repos; on finding a nested repo it records it and stops descending into it. Returns a list of repo root paths (mempalace/project_scanner.py:L374-L407).

## People Deduplication

Two commits belong to the same person if they share a name OR an email, resolved via union-find over `("name", name)` and `("email", email)` keys (mempalace/project_scanner.py:L433-L466).

For each identity component, the display name is the most-frequent name variant that looks like a real name, falling back to the most-frequent variant overall. Components whose chosen display does not look like a real name are dropped entirely. Distinct components that resolve to the same display name are merged (summing commits, unioning emails and repos) (mempalace/project_scanner.py:L456-L506).

## scan(root) → (projects, people)

`scan(root)` accepts a path-like root. Root is expanded and resolved; if it is not a directory, returns two empty lists (mempalace/project_scanner.py:L509-L513).

For each discovered repo: collects manifests, choosing the manifest located at the repo root as the project's manifest+name; if no root manifest exists, the project name defaults to the repo directory name with null manifest (mempalace/project_scanner.py:L527-L534).

Per repo, non-bot authors are counted: `total_commits` = number of non-bot commit lines; `user_commits` = count of non-bot commits whose name equals the current user name OR whose email equals the current user email (mempalace/project_scanner.py:L536-L545).

A project is flagged `is_mine` only if `user_commits > 0` AND one of: the user's name is among the top-5 authors by commit count; OR the user's share of commits is `>= 10%`; OR `user_commits >= 20` (mempalace/project_scanner.py:L547-L556).

When two repos yield the same project name, the one with more `user_commits` wins (mempalace/project_scanner.py:L567-L569).

Extra (non-root) Java-family manifests within a repo add additional sub-projects, reusing the repo's commit stats and `is_mine`, but only if no existing project with that name already has a manifest or comes from a different repo (mempalace/project_scanner.py:L571-L587).

If no git repos are found anywhere, manifests under the root are still collected and registered as `has_git=false` projects (skipping names already present) (mempalace/project_scanner.py:L591-L602).

Output ordering: projects sorted by (`is_mine` true first, then descending `user_commits`, then descending `total_commits`, then name ascending); people sorted by descending `total_commits` (mempalace/project_scanner.py:L604-L610).

## to_detected_dict(projects, people) → dict

Converts scan results into the entity-detector dict shape. Default caps: 15 projects, 15 people (mempalace/project_scanner.py:L616-L621).

Each project entry: `{name, type:"project", confidence:round(confidence,2), frequency:(user_commits or total_commits), signals:[signal_string]}`. Each person entry: `{name, type:"person", confidence:round(confidence,2), frequency:total_commits, signals:[signal_string]}`. The returned dict has keys `people`, `projects`, `topics:[]`, `uncertain:[]` (mempalace/project_scanner.py:L622-L648).

## discover_entities(...) → dict

Top-level discovery combining sources in preference order. Returns the same dict shape as the entity detector (mempalace/project_scanner.py:L677-L716).

1. Runs `scan` on the directory for manifest + git signals (mempalace/project_scanner.py:L716).
2. If the directory is a Claude Code conversations root, extracts per-project entries from session `cwd` metadata and merges them by case-insensitive name, preferring entries with more `user_commits`; re-sorts using the same project ordering as `scan` (mempalace/project_scanner.py:L718-L740).
3. Converts the real signal via `to_detected_dict` using the supplied caps (mempalace/project_scanner.py:L742).
4. Runs the prose regex detector on up to `prose_file_cap` (default 10) files; merges those into the real signal. On name conflict, primary (real-signal) entries win; dedup is case-insensitive (mempalace/project_scanner.py:L654-L674, L744-L763).
5. When real manifest/git signal exists and no LLM provider is supplied, the secondary detector's `uncertain` bucket is dropped entirely (mempalace/project_scanner.py:L654-L674, L755-L763).
6. If `llm_provider` is supplied, runs a blocking-interactive LLM refinement pass over a collected corpus; project promotions are allowed only when there was no real signal; progress (cancelled / reclassified / dropped / batch-error counts) prints to stderr; Ctrl-C returns partial results (mempalace/project_scanner.py:L765-L792).
7. If `corpus_origin` is supplied (the shape written to `<palace>/.mempalace/origin.json`), a persona-reclassification filter is applied last; with no corpus_origin the output is unchanged (mempalace/project_scanner.py:L794-L802).

## CLI

When run as a script, takes an optional first argument as the scan target (default `.`), runs `scan`, and prints a `=== PROJECTS (n) ===` block (up to 30, each line marked `★` when `is_mine`, with name, `conf=X.XX`, and signal) followed by a `=== PEOPLE (n) ===` block (up to 30, with name, confidence, and signal) (mempalace/project_scanner.py:L808-L820).
