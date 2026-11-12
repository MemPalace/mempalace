- **Tests:**

  - `tests/test_docs_fragments.py` also covers the per-change field fragments in `docs/modules/<module>.d/`: a fragment counts as the module block change and extends its fields, the bad shapes fail the check (unknown module, Paths, empty or loose text, wrong extension), `--print modules` includes them, two changes to one module merge cleanly with fragments and conflict without them, and the repository's `.gitattributes` keeps both sides of an append to `docs/modules/tests.md` and `SYSTEM.md`.
