Design before code. Write the plan into the card body before any code exists. The owner approves the plan; `commonplace orch plan-check` must pass on it, and it stops at the first rule it finds broken.

Required shape (text inside ``` fences is ignored by the check):

1. Headings `## 0.` to `## 7.` are present, in this order: 0 design before code, 1 format or interface, 2 behaviour, 3 authority, 4 durability and failure, 5 docs and checks, 6 units, 7 risks and open questions.
2. Section `## 0.` has the bold items `**a.` to `**f.`, in order, each with at least 40 non-space characters:
   - **a. Numbers.** Measure before you design. Every line with a digit starts with a label after indentation and an optional Markdown list marker (for example `- M: …` or `M …`): `M`/MEASURED (say how and where you measured), `T`/TARGET or `G`/GUESS.
   - **b. Smallest design.** The least that meets the card; say what is left for later.
   - **c. Data.** Where the data lives, who owns it, how it is backed up and restored.
   - **d. Risk.** Money, authority, private data, secrets and rate limits.
   - **e. Failure plan.** One table with exactly this header:
     `| Dependency | Timeout | Bounded retry | Idempotency key | User sees | RED |`
     Every row has six non-empty cells; the RED cell lists test ids such as `R1, R2b`.
   - **f. RED tests.** One table whose header starts `| ID | Fault injected | Assertion`, with unique ids such as `R1` or `R2b`. Every id named in (e) appears here.
3. Section `## 6.` contains one line `R-test files:` that maps every (f) id to exactly one existing test file relative to the repository root, for example `R-test files: tests/test_orch_acceptance.py: R1 R2`. Acceptance test files are allowed. Run the check from the repository root or a subdirectory; outside a Git checkout, paths are relative to the current directory.

Write each RED test so it fails on the current code for the reason the plan names, then make it pass. Keep the plan short: numbers, decisions and the tests that prove them.
