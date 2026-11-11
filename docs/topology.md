# Topology store

The single source of truth for the Compute map: machines, how they reach each other, and how a website is routed to the app that serves it. Everything is a structured fact in the workspace's `topology` wing. The map, the agent tools and the live status all read from this store; nothing else holds a second copy.

Code: `commonplace/topology/model.py` (what may be stored) and `commonplace/topology/store.py` (how it is stored and read). Tests: `tests/test_topology_store.py`.

## Facts

One fact is `(subject, predicate, object)` in one workspace, wing `topology`:

| Subject | Predicates | Example |
| --- | --- | --- |
| `node:<key>` | `kind`, `name`, `role`, `hostname`, `aliases`, `tailscale_name`, `runner_id`, `wings`, `hardware`, `os`, `resources` | `node:web1 os "Linux 6.8"` |
| `link:<from>><to>:<method>` | `method`, `status`, `via`, `alias`, `port` | `link:laptop>web1:tailscale status "up"` |
| `route:<site>` | `hops` | `route:example.test hops ["example.test>cdn:dns", "cdn>web1:reverse_proxy"]` |

The object is canonical JSON. A key is lowercase letters, digits, dots, dashes and underscores (up to 63 characters), so a domain name is a valid key for a website node.

Facts are stored per field, not per record. Two observers can therefore each contribute fields to the same node, and a disagreement about one field does not touch the others.

The facts live in the workspace file `topology.sqlite3` (tables `topology_facts`, `topology_observations`, `topology_conflicts`), next to the other workspace stores. The file is therefore encrypted with the workspace when workspace encryption is on, is covered by backups, and holds one workspace only. The hub's control database holds no topology text. The facts are not rows of the memory knowledge graph: graph facts point at a chat range and carry person-approval semantics, while a topology fact points at an observer and its evidence, expires, and can be contested. The wing name is the access rule (see Access).

### Node kinds

`pc`, `mac`, `phone`, `vm`, `server`, `router` (router or edge), `cloud` (cloud or CDN, including a DNS provider), `website`, `container` (container or app).

A node's first write must include `kind`. Node fields:

| Field | Value |
| --- | --- |
| `kind` | one of the kinds above |
| `name`, `role`, `hardware`, `os` | one line of text, up to 200 characters |
| `hostname`, `tailscale_name` | a hostname or address the user chose to record |
| `aliases` | list of connection aliases (for example an ssh alias); stored sorted, without duplicates |
| `runner_id` | the Compute runner machine id (`mach_…`) when the node is a runner |
| `wings` | list of wing names the machine works in |
| `resources` | object with only `cpu`, `gpu` (text) and `cores`, `memory_gb`, `storage_gb` (numbers) |

### Link kinds and methods

A link says that `from` reaches `to` by one method. Two nodes may be linked by several methods at once; each is its own link.

Methods: `direct`, `jump_host`, `tailscale`, `relay`, `dns`, `proxy`, `reverse_proxy`, `hosts` (a machine hosts a container or app).

| Field | Value |
| --- | --- |
| `method` | the link's identity; always equal to the method in its id, written automatically |
| `status` | observed status: `up`, `degraded`, `down` or `unknown` |
| `via` | key of the node in between (the jump host or the relay) |
| `alias` | the name to use on this hop (ssh alias, Tailscale name) |
| `port` | 1 to 65535 |

### Site routes

A route is the ordered chain of link ids from a website node to where it is served, for example DNS provider, proxy or CDN, reverse proxy, host, container. The first hop starts at the site and each hop starts where the previous one ended; a broken chain is refused. A route may name a link that has no facts yet. Reads resolve every hop against the link facts and say `known: false` for such a hop rather than inventing one.

## Provenance

Every write names an `Observer(kind, id)` and verbatim `evidence`:

- `kind` is `agent` (id: the connection id), `runner` (id: the machine id) or `owner` (id: the user id). The caller takes it from the authenticated principal, never from tool arguments.
- `evidence` is the text the observer saw (command output, a config line), 1 to 4000 characters, stored exactly as given.

Each observation is kept with `observer`, `evidence`, `first_observed_at`, `observed_at`, `expires_at` and `seen_count`. The same observer repeating the same evidence updates that observation's times and count; anything else adds a row. Observations are never deleted.

## Expiry

Each observation expires. The default lifetime is 30 days, and 15 minutes for a link's `status`; a write may pass `ttl_seconds` between 60 seconds and 365 days. A fact's `expires_at` is the latest expiry among its observations.

An expired fact is stale. It fades instead of lying:

- a record's `values` holds only fresh values; a stale `status` is simply absent there, and a route hop reports `status: "unknown"`;
- `fields` still returns the stale value with `stale: true`, its times and its provenance, so a reader can say "last seen up three days ago";
- a record whose identity field (`kind`, `method`, `hops`) is stale has `stale: true`;
- a fresh observation of a different value replaces a stale value without a conflict.

Nothing is deleted when it expires.

## Conflicts

A write of a different value for a fact that has a current value goes one of two ways:

- **Superseded.** The old value is expired, or no other observer has a fresh observation of it (the observer is correcting itself). The new value becomes current; the old one stays in history with `superseded_by`.
- **Conflict.** Another observer still vouches for the current value. The current value stays. The new value is stored as a contender and a conflict record links the two, each with its own provenance. Reads show the contender under the field's `conflicts` and in the graph's `conflicts` list. The same disagreement reported again adds an observation to the same record.

A conflict is closed by `resolve_conflict` (accepted or rejected, by the owner), by the contender's only observer later agreeing with the current value (withdrawn), or by an authoritative write or a retraction (replaced). A rejected value reported again opens a new conflict.

`replace=True` is the authoritative write for a change the owner approved: it supersedes the current value whoever observed it and closes the open conflicts on that fact.

Fact states: `current`, `contender`, `superseded`, `rejected`, `withdrawn`, `retracted`. Conflict states: `open`, `accepted`, `rejected`, `withdrawn`, `replaced`.

## The secret filter

The store holds aliases, hostnames, runner ids and wings. It never holds keys, tokens, passwords or private key material. Three rules enforce that on every write, including `retract` and the `facts_for` check that proposals use:

1. **Allow-list.** Only the fields named above exist. Any other field is refused; a field named like a secret (`password`, `api_key`, `ssh_private_key`, …) is refused as a secret.
2. **Shapes.** Short fields have strict shapes (a key, a hostname, an alias, a machine id, a port, a choice from a list), which leaves no room for a credential.
3. **Text scan.** Every text value, the evidence and the observer id are scanned with the hub's existing secret detectors (`commonplace.privacy.scan`, `commonplace.redact.redact`) plus a rule for spelled-out secrets ("the password is …"). A hit refuses the whole write.

A refusal raises `SecretRefused` with a message such as:

```
no: evidence looks like it holds a secret (private_key); topology stores aliases, hostnames, runner ids and wings, never keys, tokens, passwords or private key material
```

The message names the field and the kind of secret, never the value. Evidence is not cleaned and stored: verbatim evidence either passes or the write is refused, and a refused write stores nothing. Naming a key file by path or alias (`IdentityFile ~/.ssh/id_ed25519`) is allowed; its contents are not.

## Access

The store does not authorize; the caller does, with the existing wing rules, before calling it:

- read: `policy.can_read_wing(TOPOLOGY_WING)`;
- write: `policy.can_write_wing(TOPOLOGY_WING)`.

The owner reads every wing, so the topology is owner-readable by default. An agent sees it when its connection's wing grants include `topology`. A store is one workspace's file, so no read returns another workspace's facts.

## Python API

```python
from commonplace.topology import TOPOLOGY_WING, Observer, SecretRefused, TopologyError, model, store
```

`topo` is the workspace's `TopologyStore`:

```python
topo = store.open_topology(gateway, workspace_id)   # in the hub: one cached store per workspace
topo = store.TopologyStore(workspace_dir)           # in tests: a directory, or a callable returning it
```

`now` is an optional timezone-aware `datetime` (tests pass a fake clock). All refusals raise `TopologyError` (a `ValueError`); `SecretRefused` and `store.NotFound` are subclasses. Messages start with `no:` and are safe to show.

### Writes

```python
store.upsert(topo, items, *, observer, evidence,
             ttl_seconds=None, replace=False, now=None) -> list[dict]
store.upsert_node(topo, key, fields, **write) -> list[dict]
store.upsert_link(topo, source, target, method, fields=None, **write) -> list[dict]
store.upsert_route(topo, site, hops, **write) -> list[dict]
store.retract(topo, subject, *, observer, evidence, predicate=None, now=None) -> int
store.resolve_conflict(topo, conflict_id, *, accept, actor, now=None) -> dict
model.facts_for(items) -> list[tuple[str, str, object]]
```

`items` is a list of `{"node": key, "fields": {...}}`, `{"link": [from, to, method], "fields": {...}}` (fields optional) and `{"route": site, "hops": [link ids]}`. One call is one transaction: everything is validated first, and a refusal or a crash stores nothing.

`upsert` returns one entry per fact: `subject`, `predicate`, `fact_id` and `action`, which is `added`, `refreshed`, `superseded` (with `previous`) or `conflict` (with `conflict_id` and `current`).

`retract` takes a subject (`node:<key>`, `link:<id>`, `route:<site>`), or one field of it, off the map and returns how many current facts it retracted. The facts stay in history and the retraction is recorded as an observation. Retracting a node does not retract the links and routes that mention it.

`facts_for` validates items, secret filter included, without storing anything.

### Reads

```python
store.get_node(topo, key, *, now=None) -> dict | None
store.get_link(topo, source, target, method, *, now=None) -> dict | None
store.get_route(topo, site, *, now=None) -> dict | None
store.read_graph(topo, *, now=None) -> dict
store.list_conflicts(topo, *, open_only=True, now=None) -> list[dict]
store.history(topo, subject, *, predicate=None, now=None) -> list[dict]
```

A record:

```python
{
    "subject": "node:web1", "wing": "topology",
    "key": "web1",                    # link: "id", "from", "to", "method"; route: "site", "hops"
    "values": {"kind": "server"},     # fresh values only
    "stale": False,                   # the identity field has expired
    "conflicted": False,              # some field has an open conflict
    "fields": {
        "kind": {
            "fact_id": "tf_…", "value": "server", "state": "current",
            "observed_at": "2026-01-01T10:00:00Z", "expires_at": "2026-01-31T10:00:00Z",
            "stale": False,
            "provenance": [{"observer": {"kind": "runner", "id": "mach_…"}, "evidence": "…",
                            "first_observed_at": "…", "observed_at": "…", "expires_at": "…",
                            "stale": False, "seen_count": 1}],
            "conflicts": [],          # contenders: the same shape plus "conflict_id"
        },
    },
}
```

A route's `hops` is a list of `{"link", "from", "to", "method", "known", "stale", "status"}`.

`read_graph` returns `{"wing", "nodes", "links", "routes", "conflicts"}`. A conflict is `{"conflict_id", "subject", "predicate", "state", "created_at", "resolved_by", "resolved_at", "current", "contender"}`, where both values have the field shape above. `history` returns every value a subject held, newest first, each with `predicate`, `state` and `superseded_by`.

## Live status

Live status of machines and of the two ends of every link, derived from signals that runners already send. There is no probing daemon: nothing in this section opens a connection to a machine.

Code: `commonplace/topology/status.py`. Tests: `tests/test_topology_status.py`.

### The read

The one read the read tools (`topology_get`) and the canvas call:

```python
from commonplace.topology import status

status.read_status(gateway, workspace_id, *, graph=None, now=None, config=None) -> dict
```

`graph` is a `store.read_graph` result the caller already holds (so the map and its status describe the same moment); when `None` the function reads the workspace's graph itself. `now` is an optional timezone-aware `datetime` (tests pass a fake clock). `config` is a `status.StatusConfig`; `None` means `StatusConfig.from_env()`. The caller authorizes (`policy.can_read_wing(TOPOLOGY_WING)`) before calling; the read opens no workspace other than `workspace_id` and creates no topology file.

The pure core, which tests and other callers can use with their own signals, takes no gateway:

```python
status.derive(graph, signals, *, now, config) -> dict      # signals: dict[machine_id, MachineSignals]
status.machine_status(signals, *, now, config) -> dict     # one MachineSignals -> one status record
status.collect_signals(gateway, workspace_id, *, now) -> dict[str, MachineSignals]   # the only reader of the hub's tables
```

The result:

```python
{
    "as_of": "2026-01-01T10:00:00Z",
    "config": {"green_within_s": 180, "red_after_s": 900, "path_fresh_s": 900, "path_unknown_after_s": 3600},
    "machines": {"mach_…": STATUS},      # every enrolled runner machine, with or without a node
    "nodes":    {"web1": STATUS},        # every node of the graph
    "links":    {"laptop>web1:tailscale": {**STATUS, "ends": {"from": STATUS, "to": STATUS}}},
}
```

A `STATUS` record:

```python
{"status": "up" | "degraded" | "down" | "unknown",
 "color": "green" | "orange" | "red" | "grey",     # grey is unknown, never green
 "reason": "<code>",
 "last_signal_at": "2026-01-01T09:59:12Z" | None,  # machines and nodes; for a link end: the path report
 "last_signal_source": "<signal name>" | None,
 "age_s": 48 | None}
```

A link's record is a `STATUS` plus `ends`; each end is a `STATUS` plus `basis` (`node` when the end's own node decided it, `path` when the path report did). `last_signal_at` and `age_s` of a link end are those of the record that decided it.

The status words are the ones a link's `status` fact already uses, so a link's reported status and its derived status compare directly.

### Rules

One rule set, applied in this order. Times are ages at `now`; a signal dated in the future counts as age 0.

**Machine** (an enrolled runner machine; a node is its machine when its `runner_id` names it):

| Condition | Status | Reason |
| --- | --- | --- |
| machine revoked | down | `revoked` |
| machine disabled | down | `disabled` |
| no signal ever | unknown | `never_reported` |
| newest signal older than `red_after_s` | down | `no_heartbeat` |
| newest signal older than `green_within_s` | degraded | `late_heartbeat` |
| a start is leased or starting | degraded | `start_in_progress` |
| otherwise | up | `heartbeat` |

The newest signal is the latest of the signals below. A machine that never produced one is unknown, not green, and a card start never makes an unknown machine green.

**Link path.** A runner (or an agent) may report a path with `upsert_link(..., {"status": ...})`; that is the optional path report. The report's age is `now - observed_at` of the link's newest `status` observation, stale or not:

| Report | Age | Path status | Reason |
| --- | --- | --- | --- |
| none | | unknown | `no_report` |
| any | up to `path_fresh_s` | as reported | `reported` |
| `up` | up to `path_unknown_after_s` | degraded | `late_report` |
| `down` or `degraded` | up to `path_unknown_after_s` | as reported | `late_report` |
| any | older | unknown | `report_stale` |

A path that is `down` while another link between the same two nodes (any direction, any other method) is `up` is `degraded` with reason `alternate_path_up`: the target is still reachable, one path is failing.

**Link end.** An end shows the worse of its node's status and the path's status, ignoring `unknown`; when both are unknown the end is unknown, with the reason of the one that saw a signal (a stale report) when there is one. Worse means `down` over `degraded` over `up`. A node that is not a runner machine has no signals of its own, so its end shows the path report alone.

**Link.** The link's own status is the worse of its two ends by the same rule.

### Signals that exist today

Each signal is read from state the hub already keeps; none is new.

| Signal | Source | What it proves |
| --- | --- | --- |
| `machine_connection` | `connections.last_used_at` of the machine's connection (`runner_machines.machine_connection_id`), touched by the hub on every authenticated runner request, at most once a minute | the runner reached the hub |
| `worker_connection` | `connections.last_used_at` of the machine's worker connection (`runner_machines.worker_connection_id`) | an agent started on the machine is working |
| `chat_poll` | `chat_runners_seen.seen_at` | the runner's chat relay polled |
| `start_success` | newest `runner_jobs.updated_at` in state `started` for the machine | the machine launched a card start |
| `start_in_progress` | any `runner_jobs` row of the machine in state `leased` or `starting` | a start is underway (orange) |

`runner_machines.enabled` and `revoked_at` give the disabled and revoked rows of the rule table.

### What does not exist, and the gap

- **No per-path reachability signal from runners.** Nothing on the hub carries a runner's view of a direct, jump host, Tailscale or relay path. The only path signal is the optional `status` fact of a link written through the store; until runners write it, every link's path is `unknown` / `no_report` and a link end shows its node alone. The Compute stack (not on develop) may add such a report; it must write the same `status` fact so this code needs no change.
- **No heartbeat for a non-runner node** (router, cloud, website, container): they stay `unknown` unless a path report names them.
- **No failed-start signal in the rules.** A failed or uncertain start does not change a machine's status; a machine that fails starts but keeps polling is `up`.
- **The thresholds are per hub, not per machine.** A machine configured with `poll_seconds` near 300 needs `green_within_s` of at least twice that.

### Configuration

Thresholds are configuration, not constants. `StatusConfig` holds them; build it with `StatusConfig(green_within_s=…)`, `StatusConfig.from_mapping({...})` or `StatusConfig.from_env()`.

| Field | Environment variable | Default | Meaning |
| --- | --- | --- | --- |
| `green_within_s` | `COMMONPLACE_TOPOLOGY_GREEN_S` | 180 | a signal this young is green |
| `red_after_s` | `COMMONPLACE_TOPOLOGY_RED_S` | 900 | no signal for longer is red |
| `path_fresh_s` | `COMMONPLACE_TOPOLOGY_PATH_FRESH_S` | 900 | a path report this young is trusted as reported |
| `path_unknown_after_s` | `COMMONPLACE_TOPOLOGY_PATH_STALE_S` | 3600 | a path report older than this is unknown |

Each is a whole number of seconds; `green_within_s` must be below `red_after_s` and `path_fresh_s` below `path_unknown_after_s`, or the config is refused with a `TopologyError`.

## Existing workspaces

The store is additive: one new file per workspace with three tables, created by the first write. No existing file or table changes. A workspace from before this feature has no `topology.sqlite3`; it reads as an empty map (`nodes`, `links`, `routes` and `conflicts` all empty), `get_*` return `None`, and reading does not create the file.
