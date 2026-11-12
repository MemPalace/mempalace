## mempalace

- **Paths:** `mempalace/`, `seed/`
- **Owns:**

  - The memory engine: verbatim drawers, mining, search, knowledge graph, storage backends and the MCP server; plus the reviewed seed card list.

- **Promises:**

  - Diary reads accept a server-only `thread_scope` (declared with `x-mempalace-server-only`, so `tools/list` omits it): `probe` lists at most 1000 entry topics from metadata only and names the serving store, and `exclude_topics` with that `source` skips those entries before counting and windowing on the same store (the SQLite fast path or the collection); a store change in either direction or any other scope is refused, an empty exclusion list leaves the SQLite page path unchanged, and an over-limit probe names the limit.

  - Search rows (including unified media/code search) and `check_duplicate` matches carry a drawer's `topic` when it has one, so the gateway can hide diary entries whose topic names an unreadable thread from search, CLI search and duplicate checks.

  - The checkpoint schema caps `items` at 500 (`maxItems`), and the tool refuses larger batches.

  - The fact readers (`knowledge_graph`, `fact_index`, `searcher/facts`) take the read deadline from the stdlib-only `mempalace/deadline.py` and never import `commonplace.rules`, so an import-following test selector does not widen every change into most of the suite; `commonplace.rules.checks` re-uses the same context variable and keeps its own clock.

  - Local CLI and gated gateway CLI use the same pure drawer formatter, preserving headers, match scores, folded-copy labels, body whitespace conventions, empty results and BM25 fallback output. The gateway explicitly selects flat rows even when they carry import-authorization metadata and renders CLI text from gated rows for every principal; the local CLI keeps its metadata mode.

  - Drawer retries retain replay status for readable owners while also returning request-derived fresh-write fields, allowing the gateway to hide replay status for unreadable wings.

  - Graph snapshots use local SQL locus expressions, including native booleans and integers outside int64; descriptors also bind stored sort values.

  - Graph page descriptors carry their source order key; grouped Chroma and exact numeric loci retain SQLite text casts.

  - Graph descriptors preserve literal placeholders and backend-specific hall/date values; page source determines readable tie ordering.

  - Graph inputs are authorized before traversal, ranking and statistics; explicit totals reuse endpoint and label proof.
  - Allocation observation activates only on first local dispatch; sampling construction failures release active state, construction failures latch with counted loss, and RSS status/source use the same read.

  - Storage-serving processes emit only a closed primitive allocation schema to a private two-file local diagnostic ring; forwarding and daemon jobs do not activate it. Contexts, queue, line size and retained files are bounded; observation failure never changes storage limits or restart behavior.
  - Bookkeeping uses bounded facet round trips per wing, narrows partial results to unsupported capabilities, and never caches transient overview errors.

  - Facet-backed overview counts remain available; projected recovery closes/discards and retries once, and fingerprinted refusals retain normal response keys.

  - Overview reads project only locus/bookkeeping keys, preserve metadata-less drawers, and refuse acquisition or aggregate bounds explicitly.

  - Tunnel keysets use stable ids; final lookup ignores traversal statistics and unrelated metadata while rechecking current endpoint associations.

  - Explicit tunnel probes page metadata without content, bind both endpoints and optional current drawer descriptors, and revalidate before follow hydration or recording.

  - The MCP tunnel tools create, list, follow and delete tunnels in the server's configured palace, and `mempalace_graph_stats` counts that palace's explicit tunnels on its client path: the room check and the tunnel file never fall back to the ambient default palace.
  - Logical drawer pages authorize physical identities before grouping, dates, totals and slicing; only visible page text is hydrated.

  - Display-label token scans deduplicate identities without rewriting labels or changing citation parsing.

  - Hallway probes return bounded metadata pages in storage order; final reads revalidate exact descriptors.

  - KG search reuses the local tokenizer, selective terms and 50-seed candidate rules on a detached authorized index; deadline checks stop copy, indexing and scoring work. Scoped search requires its detached snapshot.

  - Authenticated room/status sums use authorized pairs. Status suppresses global capacity and raw probe reasons, reports a fixed disabled-index reason, and omits transport diagnostics and palace/database paths. Status, list_rooms (including consented private_chats imports), list_wings and taxonomy propagate backup-pause retry errors; consented private_chats drawer listings do too. Pause tests cover RPC and internal calls on those routes.

  - Scoped wing and taxonomy counts use bounded metadata-only identity pages, normalize absent/null/question-mark loci to unknown, exclude bookkeeping, and revalidate concrete descriptors in fresh snapshots. Local unscoped readers retain their conventions.
  - Exact non-Chroma physical citation ids remain supported without document hydration.

  - A private read-only metadata capability resolves physical and logical citation ids, including every legacy chunk, without document hydration or optional collection creation.

  - Scoped KG writes recheck provenance snapshots in-lock; source/id deletes use authorized rooms and explicit ids, and missing optional closets count as zero.

  - Direct B2 providers reject malformed metadata with encryption errors; marked-workspace refusals retain an explicit locked diagnostic.

  - Operator-blind token-slot primitives accept strict versioned token/grant envelopes only; opt-in B3 metadata stays locked before any B2 key provider or database open.

  - User content is stored verbatim; ingest is append-only; search returns original words; nothing leaves the machine unless the user configured a provider.

- **How it fails:**

  - Changed locus values or sort values invalidate authorization; unavailable snapshots expose no partial graph.

  - Unknown order keys fail closed; unrelated overview bookkeeping and tunnel compatibility exceptions remain unchanged.
  - Dropped, rotated and unmatched allocation records are incomplete coverage; they cannot exclude a memory cause. Unsupported current-RSS sampling and untouched counters remain explicitly unknown.

  - Incomplete drawer membership proof returns an empty pagination envelope; changed metadata cannot authorize a sibling chunk.

  - Capped raw-token scans retain the existing grammar; checkpoints between string scans and token lookups enforce request deadline failure.

  - Changed hallway associations are excluded without rewriting stored rows; unverified internal scope calls are refused, while local unscoped pages remain compatible.
  - Unavailable metadata still fails closed without applying overview bookkeeping normalization to readable graph rows.

  - Incomplete graph metadata or tunnel proof returns the operation’s empty public envelope without global counts.

  - Incomplete room/status acquisition returns zero visible sums with a count-free disabled-index reason; integrity failure flags remain, without raw failure details or shared principal caches.

  - Incomplete acquisition or invalid descriptors discard all projected output. New or changed metadata cannot inherit an earlier allow descriptor.
  - Non-Chroma logical citations fail closed because the collection API cannot guarantee ordered snapshot enumeration.

  - Missing metadata, changed descriptors and incomplete room proof return no association; read probes never invoke mutation handlers.

  - Backend lock or index damage degrades recall; repair tools rebuild indexes without touching drawers.

  - When a session's cached database disappears during a rebuild, the session resets the palace's shared Chroma System as well as its client so dropped owners cannot leave a pool pointing at the renamed database.


- **Tests:**

  - Exact and Compact numeric/boolean witnesses and locus-less ordering cases compare with unchanged local readers.

  - SQLite, Chroma and Compact ordering guards plus numeric zero/exponent cases compare with local graph readers.
  - `tests/test_worker_observation.py` covers generated call pairs, private-value rejection, actual leaf counters, opt-out, transport lifetime, bounded contexts and cross-process ring rotation.
  - Missing-locus bookkeeping reconciles wing and room totals with fixed global facets; Qdrant emits typed server filters and ordinary facet failures use the canonical refusal.
  - `tests/test_overview_facets_r3.py` checks fake/SQLite missing-key oracle parity and primary facet refusals; adapter tests check typed REST facet bodies.

  - `tests/test_overview_facets_r2.py` pins transient backend refusal, deterministic facet call bounds, lost-fingerprint recount and zero-get unsupported acquisition.

  - `tests/test_overview_review_regressions.py` covers real facet wrappers, aggregate bookkeeping and transient/persistent real Chroma recovery without fake byte capabilities.

  - `tests/test_overview_memory_bounds.py` covers lazy recovery, byte splitting, pair/cell refusal, filtered rooms and raw-cache removal.

  - Logical page SQLite/SDK equality, chunk visibility, owner authority, changed associations and incomplete acquisition are covered in the focused drawer-page tests.

  - Hallway guards cover exact 500/501 metadata page boundaries and readable private-token labels.

  - Hallway snapshot tests cover SQLite and legacy JSON ordering and changed descriptors.
  - Default Chroma and exact-backend witnesses cover placeholders, registry rows, numeric halls and collection fallback.

  - Readable graph controls, hidden bridges/top-N candidates, both statistics paths and bounded projection fault guards exercise the read boundary.

  - `tests/test_card253_s02.py` covers room/status branches, real capacity refresh with hidden/control equality, transport diagnostic suppression, invalid wing types, no-palace/local guards and projection faults.

  - Presence and private read-scope tests exercise SQLite, compact, Chroma, facet and generic metadata paths, unknown values, batching and races.
  - Capped-backend tests place a hidden chunk beyond the first short metadata page.

  - Typed association protocol tests cover optional collection absence, metadata-only paging and snapshot revalidation.

  - The `tests/test_` files for mining, search, palace, knowledge graph, backends and the MCP server.
