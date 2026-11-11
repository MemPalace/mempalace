## topology

- **Paths:** `commonplace/topology/`, `docs/topology.md`, `tests/test_topology_store.py`, `tests/test_topology_status.py`
- **Owns:**

  - The topology store behind the Compute map: nodes, links and site routes as structured `(subject, predicate, object)` facts in the workspace's `topology` wing, in the workspace file `topology.sqlite3` (`topology_facts`, `topology_observations`, `topology_conflicts`). `docs/topology.md` is the contract: node kinds, link methods, routes, provenance, expiry, conflicts, the secret filter and the Python read and write signatures.

  - `model.py` is the allow-list: every field and its shape, the observer, lifetimes, subjects and the secret filter. `store.py` holds `TopologyStore` (one workspace's file, opened like `bots.sqlite3`; `open_topology` caches one per workspace on the gateway) and writes and reads: `upsert` (and the node, link and route forms), `retract`, `resolve_conflict`, `get_node`, `get_link`, `get_route`, `read_graph`, `list_conflicts`, `history`.

  - `status.py` derives live status for machines, nodes and both ends of every link from signals the hub already keeps (the runner connections' last use, the chat relay's last poll, started and underway runner jobs) and the optional `status` path report of a link. `read_status(gateway, workspace_id, graph=, now=, config=)` is the one read; `derive` and `machine_status` are the pure core; `StatusConfig` holds the thresholds (arguments or `COMMONPLACE_TOPOLOGY_*` variables). No probing; a machine or path with no signal is `unknown`, never green. Rules, gaps and the signal list: `docs/topology.md`, "Live status".
- **Promises:**

  - Every fact keeps who observed it, when, from what verbatim evidence, and when it expires. A stale value leaves `values` and is still readable with `stale: true`; nothing is deleted.

  - Two observers disagreeing leaves the current value and files the other as a conflict record; only the same observer, an expired value, an owner decision or an authoritative `replace` write changes the current value, and the old one stays in history.

  - Keys, tokens, passwords and private key material are refused on write, in fields, evidence and observer ids. The refusal names the field and the kind of secret, never the value, and stores nothing.

  - The schema is additive and each write is one transaction. The store does not authorize: callers check the `topology` wing with the existing wing grants. A store is one workspace's file, so reads never cross workspaces; the control database holds no topology text.
- **How it fails:**

  - A threshold set to a non-whole or inconsistent number (green not below red) raises `TopologyError`. A value outside the allow-list, a broken route chain or secret-looking text raises `TopologyError` (`SecretRefused` for secrets) before anything is stored. A workspace without `topology.sqlite3` reads as an empty map, and reading does not create the file.
- **Tests:**

  - `tests/test_topology_store.py` covers upsert and read back with provenance and expiry, refreshes, allow-list refusals, stale facts and route hops, conflicts and their resolutions, the authoritative write, retraction, secret refusals, workspace isolation, an old workspace without the file, reopening, a moved workspace directory, a crash inside a write and the wing grant.

  - `tests/test_topology_status.py` walks a machine and a link end through green, orange, red and green on a fake clock, covers a machine that never reported, start in progress, revoked and disabled machines, the newest signal winning, thresholds from arguments and the environment, path report ageing, one of several paths failing, stale link facts, the hub's real signals (connection use, chat poll, runner jobs), the single read with and without a supplied graph and workspace isolation.
