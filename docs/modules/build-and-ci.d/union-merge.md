- **Owns:**

  - `.gitattributes` also sets `merge=union` for `docs/modules/tests.md` and `SYSTEM.md`, the append-only docs most changes touch, so a local batch merge keeps both sides of a concurrent append.
