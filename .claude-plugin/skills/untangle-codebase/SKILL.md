---
name: untangle-codebase
description: "Clean up technical debt in any codebase with a measure-first, one-target-at-a-time loop: baseline duplication, oversized modules, dead code, tangled imports and flaky or slow tests, pick one bounded target, pin its behaviour with tests before touching it, change in small reviewed steps, then re-measure and report the difference. Use when asked to refactor, untangle, deduplicate, tidy or reduce debt, not for feature work or one-line fixes."
---

# Untangle Codebase

A loop for paying down technical debt without breaking behaviour or
sprawling into a rewrite. Every cleanup is justified by a number, scoped to one
target, protected by tests and ends with a before/after report.

## When to use

- The request is "clean up", "refactor", "deduplicate", "split this module",
  "remove dead code" or "this area is a mess".
- Do not use it for features, bug fixes or one-line tidying. A bug fix needs
  its own regression test, not this loop.

## The loop

1. **Measure first.** Record a baseline before reading code for ideas.
2. **Pick one bounded target.** Rank candidates, choose one, write its boundary.
3. **Pin behaviour.** Prove tests cover what the target does before changing it.
4. **Change in small steps.** One reviewable change at a time, tests green each time.
5. **Measure again and report.** Same commands, same scope, show the difference.

Skipping step 1 or 3 is the most common failure: without a baseline you cannot
say whether the cleanup helped, and without pinning you cannot say it was safe.

## 1. Measure first

Run the same commands before and after, and save the raw output. Use the
project's own tools where they exist; the bundled script covers the basics for
any language.

| Signal | How to measure |
| --- | --- |
| Oversized modules | Line counts per file, top 10. |
| Duplication | Repeated blocks of N lines across files, or the project's clone detector. |
| Dead code | Language tooling (unused exports, unreferenced functions, coverage of never-run code). A name appearing once in the whole repo is only a candidate. |
| Tangled imports | Import cycles, and modules with many importers and many imports (high fan-in plus fan-out). |
| Tests | Total runtime, slowest tests, tests that fail intermittently when rerun. |

Bundled helper (standard library only, read-only, never modifies files):

```
python3 scripts/measure.py <repo-root> --top 10 --dup-lines 6
```

It prints the largest files and the most repeated multi-line blocks, as text or
with `--json` for diffing. Its duplicate detection is deliberately crude (exact
lines after whitespace trimming): use it to find candidates, then confirm by
reading. Run it from the skill directory or give its full path. Save its output
as the baseline.

For flaky or slow tests, run the relevant test files several times with the
project's runner and record pass/fail counts and the slowest durations. Do not
run the whole suite repeatedly on a shared machine; measure the area you will touch.

## 2. Pick one bounded target

Choose by value over risk: high measured pain, small blast radius, good test
reach. Then write down, before editing:

- **Target:** the exact files or functions.
- **Boundary:** what is out of scope. Anything you notice outside it goes on a
  follow-up list, not into this change.
- **Goal:** one measurable statement, such as "no block of 6+ identical lines
  shared by these three files" or "module X under 400 lines with the same public API".
- **Done signal:** the commands from step 1 and the numbers they should show.

If the target needs more than a handful of reviewable steps, split it and do
the first slice only.

## 3. Pin behaviour before changing it

Refactoring means behaviour stays identical, so show that it is guarded.

1. Find the tests that exercise the target (coverage for the files, or tests
   that import them). Run them and confirm they pass on the untouched code.
2. Check they would notice a change: temporarily break the target (flip a
   condition, return early) and confirm a test fails, then revert. If nothing
   fails, the behaviour is not pinned.
3. Where it is not pinned, add characterisation tests first: call the current
   code with representative and edge inputs and assert what it returns today,
   even if that looks odd. Commit these before the refactor.
4. If the target cannot be tested at all, say so and stop or narrow the
   target. Do not refactor blind.

## 4. Change in small, reviewed steps

- One kind of change per commit: move, rename, extract, delete or merge
  duplicates. Never mix a refactor with a behaviour change.
- Run the pinned tests after every step. A red step is reverted or fixed
  before the next one, never stacked on.
- Keep public interfaces stable until the end; move callers over, then remove
  the old path in its own commit.
- For deletions, confirm the code is unreferenced by search, including
  dynamic use (reflection, string lookups, config, templates, docs), and keep
  the deletion in its own commit so it reverts cleanly.
- Get each step, or each small group of steps, reviewed before building
  on it. A reviewer sees the diff and the baseline numbers.
- Stop when the goal from step 2 is met. Do not keep tidying.

## 5. Measure again and report

Rerun the step 1 commands with identical arguments and scope. Report:

- Baseline and after numbers side by side, with the commands used.
- The tests run and their results, including the characterisation tests added.
- What changed, in a sentence per commit.
- What did not improve or got worse (a metric that moved the wrong way is
  reported, not hidden).
- The follow-up list of things seen but left alone.

If the numbers did not improve, say so and consider reverting: churn without
measurable gain is not cleanup.

## Worked example

Request: "The two report exporters look copy-pasted. Clean that up."

1. **Measure.** `scripts/measure.py src --dup-lines 6` shows `csv_export.py`
   and `xlsx_export.py` sharing 84 duplicated lines (a row-filtering and
   formatting block); the two files are 410 and 390 lines.
2. **Target.** Only the shared filtering/formatting block in those two files.
   Out of scope: the writers, the CLI, the slow test elsewhere. Goal: the
   block exists once, both exporters keep their public functions unchanged.
3. **Pin.** The exporter tests pass. Breaking the date format in the csv block
   makes one fail, but the xlsx path has no date test, so first add a
   characterisation test for the xlsx date output and commit it.
4. **Steps.** (a) Extract the block into `export_common.py` and call it from
   csv, tests green, reviewed. (b) Switch xlsx to the same helper, tests
   green, reviewed. (c) Delete the now-unused local copies.
5. **Report.** Duplicated lines 84 to 0; the two files 410/390 to 330/310
   plus a 70-line shared module; 14 existing and 2 new tests pass; the
   unrelated slow test and a stale `legacy_export` function noted as follow-ups.

## Guardrails

- Never change behaviour, tests' expectations or public APIs as a side effect.
- Do not rewrite what you cannot measure or test. Prefer the boring option.
- Keep going only while each step is reviewable and green; otherwise stop and
  report where you are.
- Respect the project's own conventions, linter and review process.
