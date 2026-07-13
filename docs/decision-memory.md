# Authority-aware decision memory PoC

Decision drawers may carry an optional structured envelope while their content
remains verbatim:

- `decision_key`: stable logical identity across versions;
- `authority_uri`: canonical local file path or `file://` URI;
- `authority_version`: `sha256:<hex>` or `mtime_ns:<integer>`;
- `memory_kind`: for example `decision`, `finding`, or `preference`;
- `authority_status`: `current`, `stale`, `unverified`, or `superseded`.

`mempalace_search(verify_authority=true)` compares supported local authority
tokens and includes an authority envelope on every result. Verification is
opt-in because hashing large files has a real read-path cost. Unsupported and
legacy authorities remain `unverified`; they are never assumed current.

`mempalace_supersede_drawer` requires both the predecessor ID and the exact same
non-empty `decision_key`. It files a new verbatim drawer, then marks the old
drawer `superseded` with `superseded_by=<new id>`. Default MCP search hides that
history; `include_superseded=true` exposes it. No semantic-similarity threshold
can supersede a decision implicitly.

Checkpoint items accept the same fields plus `supersedes_id`, so an agent can
save a reviewed decision transition in one call.

This PoC resolves only local files. Production authority adapters could support
Git blobs, GitHub issues, planners, or document systems without changing the
drawer lifecycle contract.
