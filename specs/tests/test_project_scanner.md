# Behavior Spec: `mempalace.project_scanner` (derived from its test suite)

This spec captures the externally observable behavior of the `project_scanner`
module as asserted by `tests/test_project_scanner.py`. Each claim cites the test
that pins the behavior. The test file imports the public surface from
`mempalace.project_scanner` (tests/test_project_scanner.py:L12-L31).

## Public surface

The module exposes these names: data types `PersonInfo` and `ProjectInfo`; the
union-find helper `_UnionFind`; dedup/classification helpers `_dedupe_people`,
`_is_bot`, `_looks_like_real_name`, `_collect_manifest_names`, `_merge_detected`;
manifest parsers `_parse_cargo`, `_parse_gradle`, `_parse_gomod`,
`_parse_package_json`, `_parse_pom`, `_parse_pyproject`; and the high-level
entry points `discover_entities`, `find_git_repos`, `scan`, and `to_detected_dict`
(tests/test_project_scanner.py:L12-L31).

## Manifest parsers (file path in → project name string or absent)

Each parser takes a path to a manifest file and returns the project name as a
string, or returns "absent"/null when the name cannot be determined.

- `_parse_package_json`: given a JSON file, returns the value of the `name`
  field (e.g. `"my-package"`) (tests/test_project_scanner.py:L45-L48). Returns
  absent when the `name` field is missing (tests/test_project_scanner.py:L51-L54)
  and when the file content is not valid JSON (tests/test_project_scanner.py:L57-L60).

- `_parse_pyproject`: given a TOML file, returns the project name from the
  PEP 621 `[project]` table's `name` (e.g. `"my-py-package"`)
  (tests/test_project_scanner.py:L63-L66), and also from the `[tool.poetry]`
  table's `name` (e.g. `"poetry-pkg"`) (tests/test_project_scanner.py:L69-L72).

- `_parse_cargo`: given a Cargo TOML file, returns the `[package]` `name`
  (e.g. `"rust-crate"`) (tests/test_project_scanner.py:L75-L78).

- `_parse_gomod`: given a `go.mod` file, returns the last path segment of the
  `module` declaration, not the full module path. `module github.com/user/my-go-mod`
  yields `"my-go-mod"` (tests/test_project_scanner.py:L81-L84).

- `_parse_pom`: given a Maven `pom.xml`, returns the project's own
  `artifactId`. When both a `<parent>` block and a top-level `<artifactId>`
  exist, the top-level (child) `artifactId` wins over the parent's; e.g. with a
  parent artifact `parent-artifact` and child `child-artifact`, the result is
  `"child-artifact"` (tests/test_project_scanner.py:L87-L102). Returns absent
  when the `artifactId` element is missing
  (tests/test_project_scanner.py:L105-L111) and when the XML is malformed/unclosed
  (tests/test_project_scanner.py:L105-L112). When scanning the parsed XML's root
  children, only children whose tag is a string are considered; a child whose tag
  is a non-string value is skipped and a later `artifactId` string child is
  used (e.g. result `"safe-artifact"`) (tests/test_project_scanner.py:L115-L130).

- `_parse_gradle`: resolves the Gradle project name with the following order.
  Given a `build.gradle`, it reads the sibling settings file (`settings.gradle`)
  and returns its `rootProject.name` (e.g. `"settings-name"`)
  (tests/test_project_scanner.py:L133-L138). It supports the Kotlin DSL setter
  form `rootProject.name.set("...")` in `settings.gradle.kts`, returning the set
  value (e.g. `"kotlin-settings-name"`) (tests/test_project_scanner.py:L141-L145).
  When no settings name is available, it falls back to the name of the directory
  containing the build file (e.g. `"gradle-dir-name"`)
  (tests/test_project_scanner.py:L148-L154).

## Bot filtering: `_is_bot(name, email) -> bool`

Returns true when the committer is a bot. Detected bots include
GitHub Actions (`github-actions[bot]` with a `...@users.noreply.github.com`
address) (tests/test_project_scanner.py:L160-L161), Dependabot
(`dependabot[bot]`, `dependabot@github.com`)
(tests/test_project_scanner.py:L164-L165), and PR bots whose display name ends
in "Bot" (`Comfy Org PR Bot`) (tests/test_project_scanner.py:L168-L169).

Returns false for real humans using a GitHub privacy email of the form
`<id>+<handle>@users.noreply.github.com` (e.g. `Igor Lins e Silva`) — privacy
emails must NOT be filtered (tests/test_project_scanner.py:L172-L175). The "bot"
suffix match requires a word boundary before "bot", so a person whose surname is
`Robot` (`Sarah Robot`) is NOT flagged as a bot
(tests/test_project_scanner.py:L178-L181).

## Real-name heuristic: `_looks_like_real_name(name) -> bool`

Returns true for plausible multi-word human names such as `"Igor Lins e Silva"`
and `"Jane Doe"` (tests/test_project_scanner.py:L184-L186). Returns false for
handles/usernames: a leading-digit token `"666ghj"`, single-token handles
`"comfyanonymous"` and `"bensig"`, the empty string `""`, and an
underscore-joined single token `"no_spaces_handle"`
(tests/test_project_scanner.py:L189-L194). The implication is a real name
requires at least one space (multiple word tokens) and word-like tokens.

## Commit dedup: `_dedupe_people(commits) -> {name: PersonInfo}`

Input is a list of commit tuples `(display_name, email, repo)`. Output is a map
from canonical display name to a `PersonInfo`.

- Commits are merged into one person when they share an email address. Three
  commits — two with display names `"Milla J"`/`"MSL"` sharing one email and a
  third `"Milla J"` with a different email — collapse to a single person keyed
  `"Milla J"` with `total_commits == 3`. The display name `"MSL"` is filtered out
  as a key because it lacks a space, but its commit still counts toward the total
  (tests/test_project_scanner.py:L200-L211).

- Distinct people with distinct names and distinct emails remain separate
  entries (`"Alice Example"` and `"Bob Sample"`)
  (tests/test_project_scanner.py:L214-L221).

- Commits sharing the same display name but two different emails merge into one
  person; that person's `total_commits == 2` and its `emails` set has length 2
  (tests/test_project_scanner.py:L224-L232).

## Union-find: `_UnionFind`

A disjoint-set structure with `find(x)` and `union(a, b)`. `find` on an unseen
element returns the element itself (singleton)
(tests/test_project_scanner.py:L722-L724). After `union("a","b")`, `find("a")`
equals `find("b")` (tests/test_project_scanner.py:L727-L730). Union is
transitive: after `union("a","b")` and `union("b","c")`, `find("a")` equals
`find("c")` (tests/test_project_scanner.py:L733-L737).

## `ProjectInfo`

Construct with at least `name`, `repo_root` (a path), and optional flags
`is_mine`, `has_git`, `manifest`. Exposes a `confidence` value:

- When `is_mine` is true, `confidence == 0.99`
  (tests/test_project_scanner.py:L238-L240).
- When `has_git` is false but a `manifest` (e.g. `"package.json"`) is present,
  `confidence > 0.8` (tests/test_project_scanner.py:L243-L245).

Other observable fields used by callers: `name`, `manifest`, `repo_root`,
`has_git`, `is_mine`, `total_commits`, `user_commits` (see `scan` below).

## `PersonInfo`

Construct with `name`, `total_commits`, and `repos` (a set of repo identifiers),
plus an `emails` set. Provides `to_signal() -> str` describing commit activity,
with correct singular/plural wording:

- 1 commit across 1 repo: `"1 commit across 1 repo"`
  (tests/test_project_scanner.py:L248-L250).
- 5 commits across 2 repos: `"5 commits across 2 repos"`
  (tests/test_project_scanner.py:L251-L252).

## `find_git_repos(root) -> list[path]`

Discovers git repository roots under `root`.

- Detects a repo at the root itself when a `.git` directory exists (root is in
  the result) (tests/test_project_scanner.py:L258-L261).
- Detects a nested repo (a subdirectory containing `.git`)
  (tests/test_project_scanner.py:L264-L269).
- When the root is itself a repo, nested repos deeper in the tree are still
  discovered as separate roots; both root and the deep nested repo appear
  (tests/test_project_scanner.py:L272-L280).
- Recognizes `.git` as a file containing a `gitdir: <path>` marker (worktree /
  submodule style), not only as a directory; both a root and a nested
  file-marker repo are detected (tests/test_project_scanner.py:L283-L290). The
  marker file content format is the literal line `gitdir: <path>\n`
  (tests/test_project_scanner.py:L38-L39).
- An empty directory (no `.git`) yields an empty list
  (tests/test_project_scanner.py:L293-L294).

## `_collect_manifest_names(root) -> list[(file, name, dir)]`

Collects manifest-derived project names under `root`, returning entries each
carrying a manifest file, the parsed name, and the manifest directory. Recursion
stops at a git boundary marked by a `.git` file (gitdir marker): given a root
with a `.git` file marker plus `package.json` named `root-name`, and a nested
directory that is also a git-file-marked repo with its own `package.json`, only
the root name `"root-name"` is collected — the nested repo is not descended into
(tests/test_project_scanner.py:L479-L487).

## `scan(path) -> (projects, people)`

Scans a directory tree and returns a list of `ProjectInfo` and a list of
`PersonInfo`. Behavior requires a git executable for commit attribution; tests
skip when git is unavailable (tests/test_project_scanner.py:L300-L303). Commit
authorship for repos created in tests is driven by git author/committer
name+email environment (tests/test_project_scanner.py:L305-L316,L325-L343).

Project naming and `is_mine`:

- A directory with `package.json` (name `my-app`) plus a git repo yields one
  project named `"my-app"` with `is_mine == true`
  (tests/test_project_scanner.py:L345-L352).
- A directory with `pyproject.toml` (name `pyproj`) plus git yields a project
  named `"pyproj"` (tests/test_project_scanner.py:L354-L358).
- A Maven `pom.xml` (artifact `maven-app`) plus git yields a project named
  `"maven-app"` whose `manifest == "pom.xml"`
  (tests/test_project_scanner.py:L361-L374).

No-git project detection:

- A directory with only Gradle settings/build files (no git) yields one project
  named `"gradle-root"` with `manifest == "settings.gradle.kts"`,
  `has_git == false`, and an empty people list
  (tests/test_project_scanner.py:L377-L386).
- A Gradle subproject without git keeps its own manifest directory: a root with
  `settings.gradle` (name `gradle-root`, `repo_root == root`) and a subdir `app`
  with `build.gradle` yields a separate `app` project whose
  `manifest == "build.gradle"` and `repo_root == app`
  (tests/test_project_scanner.py:L389-L400).
- A manifest-only directory (no git) still produces a project: `package.json`
  named `manifest-only` yields one project with that name, `has_git == false`,
  and empty people (tests/test_project_scanner.py:L469-L476).

Mixed/multi-language repos:

- Inside a single git repo with a root `package.json` (`web-root`), Java/Gradle
  subprojects are surfaced as their own projects with their own `repo_root`:
  `web-root` (manifest `package.json`), `java-service` (manifest `pom.xml`,
  `repo_root == service`), and `worker` (manifest `build.gradle.kts`,
  `repo_root == worker`) (tests/test_project_scanner.py:L403-L426).
- A git repo without a root manifest but containing a Java subproject yields a
  project for the repo itself keyed by the repo directory name with
  `manifest == null` and `repo_root == root`, plus the subproject `java-service`
  with `manifest == "pom.xml"` and `repo_root == service`
  (tests/test_project_scanner.py:L429-L447).

Root manifest priority and fallbacks:

- When multiple manifests exist at the root (`package.json` named
  `package-name` and `pyproject.toml` named `pyproject-name`), the root project
  is named by an explicit priority order: `pyproject.toml` wins, so
  `projects[0].name == "pyproject-name"`. A nested `package.json`
  (`nested-name`) does not override the root choice
  (tests/test_project_scanner.py:L450-L458).
- When a git repo has no manifest, the project name falls back to the repo
  directory name (e.g. `my-repo-name`)
  (tests/test_project_scanner.py:L461-L466).

Commit totals and bot exclusion:

- Bot commits are excluded from contributor totals while still counting toward
  the overall commit count. With one human commit (Jane Doe) and one
  `github-actions[bot]` commit, the project reports `total_commits == 1` and
  `user_commits == 1`, and people list is exactly `["Jane Doe"]`
  (tests/test_project_scanner.py:L490-L504). (Observably, the bot commit is not
  attributed to a person and not counted in user totals.)

Empty / missing inputs:

- An empty directory yields empty projects and empty people
  (tests/test_project_scanner.py:L507-L510).
- A nonexistent path yields empty projects and empty people (no error)
  (tests/test_project_scanner.py:L513-L517).

## `to_detected_dict(projects, people) -> dict`

Produces a "detected entities" dictionary with exactly the keys
`{"people", "projects", "topics", "uncertain"}` — `topics` and `uncertain` are
always present even when empty so callers can rely on the shape
(tests/test_project_scanner.py:L523-L529). Each project entry carries `name` and
`type == "project"`; each person entry carries `name` and `type == "person"`.
With one project `p` and one person `Jane Doe`, `projects[0].name == "p"`,
`projects[0].type == "project"`, `people[0].name == "Jane Doe"`,
`people[0].type == "person"`, and both `topics` and `uncertain` are empty lists
(tests/test_project_scanner.py:L530-L535).

## `_merge_detected(primary, secondary, drop_secondary_uncertain=False) -> dict`

Merges two detected-entity dictionaries (each having `people`, `projects`,
`uncertain`, and entries shaped with `name`, `type`, `confidence`, `frequency`,
`signals`).

- Deduplication is case-insensitive across categories: a secondary `uncertain`
  entry `MemPalace` is deduped against a primary `projects` entry `mempalace`,
  so the merge keeps the single project and drops the uncertain entry (resulting
  `projects` length 1, `uncertain` length 0). The primary entry wins
  (tests/test_project_scanner.py:L541-L571).
- With `drop_secondary_uncertain=True`, all `uncertain` entries from the
  secondary input are discarded (`merged["uncertain"] == []`)
  (tests/test_project_scanner.py:L574-L584).
- Distinct names are preserved: a primary person `Alice Smith` and a secondary
  person `Bob Jones` both survive (people length 2)
  (tests/test_project_scanner.py:L587-L615).

## `discover_entities(path, llm_provider=None, show_progress=..., ...) -> dict`

High-level entity discovery returning a dict with at least `people`, `projects`,
`uncertain` categories (and `topics` per `to_detected_dict`). The `path` is
passed as a string (tests/test_project_scanner.py:L629,L643,L665,L684,L710).

- Prose fallback: when there are no manifests and no git, a regex-based detector
  on prose text (e.g. a `notes.md`) is the only source; a name repeated with
  person-like signals (e.g. `Riley`) appears among the discovered entity names
  (tests/test_project_scanner.py:L621-L632).
- Real-signal preference: when a manifest exists (`package.json` named
  `realproj`) plus git, the manifest name wins and `realproj` appears in
  `projects` even when prose contains noisy repeated candidates
  (tests/test_project_scanner.py:L635-L645).
- LLM refinement contract: an optional `llm_provider` object supplies a
  `classify(system, user, json_mode=True)` method that returns an object with a
  `text` attribute containing JSON of the form
  `{"classifications": [{"name": ..., "label": ...}]}`. When a real signal
  exists, regex-uncertain prose candidates are sent to the provider for
  refinement: exactly one prompt is issued, the candidate name (e.g. `Noise`)
  appears in that prompt's `user` text, and a candidate the LLM labels
  `COMMON_WORD` is excluded from all returned categories
  (tests/test_project_scanner.py:L648-L669).
- Repo roots do not auto-promote LLM-only candidates into projects: when the LLM
  labels a prose candidate `Terraform` as `PROJECT`, the real manifest project
  `realproj` stays in `projects` while `Terraform` is NOT placed in `projects`
  and instead remains in `uncertain`
  (tests/test_project_scanner.py:L672-L688).
- Case-variant collapse: a project named `myproj` from a manifest and a
  CamelCase variant `MyProj` discovered from a Claude Code session `cwd`
  collapse into one project entry (case-insensitive, matching `_merge_detected`
  and `miner.add_to_known_entities` semantics). The first-seen casing wins; with
  the manifest seeded first, the single surviving name lowercases to `myproj`
  (tests/test_project_scanner.py:L691-L716). The session source is a JSONL file
  whose line is a JSON object such as `{"type": "user", "cwd": "/home/u/src/MyProj"}`
  (tests/test_project_scanner.py:L708).

## Side effects / observable contracts

- `scan`/`discover_entities` read the local filesystem and invoke the `git`
  executable to obtain commit authorship; with no git available, commit-based
  behavior is unavailable (tests/test_project_scanner.py:L300-L303,L335-L343).
- Git authorship is read from per-commit author/committer name and email
  (tests/test_project_scanner.py:L305-L316).
- Claude Code session inputs are JSONL files where each line is a JSON object
  containing at least `type` and `cwd` fields, used to derive project cwd names
  (tests/test_project_scanner.py:L708).
