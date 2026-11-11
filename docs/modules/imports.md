## imports

- **Paths:** `commonplace/imports.py`, `commonplace/import_grant.py`, `commonplace/import_parts.py`, `commonplace/import_sources.py`, `commonplace/import_tools.py`, `commonplace/ingest_cadence.py`
- **Owns:**

  - Consented one-time imports of conversations and facts and the shared ingest cadence.

- **Promises:**

  - Imported-history consent: Reader listing uses literal account-prefix pagination and live Recall authorization; unconsented agents receive the missing-resource response.

  - Imports are verbatim, bounded and consented; no capture, model or external call is involved.

- **How it fails:**

  - A refused or interrupted import leaves already committed batches in place and reports where it stopped.

- **Tests:**

  - `tests/test_commonplace_import.py`, `tests/test_commonplace_import_grant.py`, `tests/test_commonplace_import_list.py`.
