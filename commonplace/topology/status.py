"""Live status of machines and link ends, derived from signals runners already send.

Nothing here probes a machine. ``collect_signals`` reads what the hub already keeps (the
runner connections' last use, the chat relay's last poll, the runner job rows);
``machine_status`` and ``derive`` are pure functions over those signals and the topology
graph, so a fake clock walks them through every state. The rule set is in
``docs/topology.md`` under "Live status".
"""

import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Mapping

from . import model, store
from .model import TopologyError

UP, DEGRADED, DOWN, UNKNOWN = "up", "degraded", "down", "unknown"
COLORS = {UP: "green", DEGRADED: "orange", DOWN: "red", UNKNOWN: "grey"}
_SEVERITY = {UP: 0, DEGRADED: 1, DOWN: 2}

SIGNALS = ("machine_connection", "worker_connection", "chat_poll", "start_success")
# A job in state ``starting`` that the hub has not swept within this long is uncertain, not underway.
_STARTING_GRACE_S = 120


@dataclass(frozen=True)
class StatusConfig:
    """The thresholds of the rule set, in whole seconds.

    ``green_within_s`` / ``red_after_s`` bound a machine's newest signal; ``path_fresh_s`` /
    ``path_unknown_after_s`` bound a link's path report. The defaults suit a runner polling
    at most every 60 seconds; a machine that polls every 300 seconds needs
    ``green_within_s`` of at least twice that."""

    green_within_s: int = 180
    red_after_s: int = 900
    path_fresh_s: int = 900
    path_unknown_after_s: int = 3600

    def __post_init__(self):
        for name in self.as_dict():
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise TopologyError(f"no: {name} must be a whole number of seconds, 1 or more")
        if self.green_within_s >= self.red_after_s:
            raise TopologyError("no: green_within_s must be below red_after_s")
        if self.path_fresh_s >= self.path_unknown_after_s:
            raise TopologyError("no: path_fresh_s must be below path_unknown_after_s")

    def as_dict(self) -> dict:
        return {
            "green_within_s": self.green_within_s,
            "red_after_s": self.red_after_s,
            "path_fresh_s": self.path_fresh_s,
            "path_unknown_after_s": self.path_unknown_after_s,
        }

    @classmethod
    def from_mapping(cls, values: Mapping) -> "StatusConfig":
        unknown = set(values) - set(cls().as_dict())
        if unknown:
            raise TopologyError("no: unknown status threshold " + ", ".join(sorted(unknown)))
        return cls(**values)

    @classmethod
    def from_env(cls, environ: Mapping | None = None) -> "StatusConfig":
        environ = os.environ if environ is None else environ
        values = {}
        for name, variable in ENV.items():
            raw = environ.get(variable, "").strip()
            if raw:
                if not raw.isdecimal():
                    raise TopologyError(f"no: {variable} must be a whole number of seconds")
                values[name] = int(raw)
        return cls(**values)


ENV = {
    "green_within_s": "COMMONPLACE_TOPOLOGY_GREEN_S",
    "red_after_s": "COMMONPLACE_TOPOLOGY_RED_S",
    "path_fresh_s": "COMMONPLACE_TOPOLOGY_PATH_FRESH_S",
    "path_unknown_after_s": "COMMONPLACE_TOPOLOGY_PATH_STALE_S",
}


@dataclass(frozen=True)
class MachineSignals:
    """What the hub knows about one enrolled runner machine.

    ``seen`` maps a signal name (see ``SIGNALS``) to the time it last fired; a signal that
    never fired is absent. ``start_in_progress`` is true while a start is leased or starting."""

    machine_id: str
    enabled: bool = True
    revoked: bool = False
    seen: Mapping[str, datetime] = field(default_factory=dict)
    start_in_progress: bool = False


def _record(status, reason, *, at=None, source=None, now=None) -> dict:
    age = None if at is None else max(0, int((now - at).total_seconds()))
    return {
        "status": status,
        "color": COLORS[status],
        "reason": reason,
        "last_signal_at": None if at is None else model.stamp(at),
        "last_signal_source": source,
        "age_s": age,
    }


def machine_status(signals: MachineSignals, *, now: datetime, config: StatusConfig) -> dict:
    """One machine's status record; the rule table is in ``docs/topology.md``."""
    now = model.clock(now)
    seen = {name: at for name, at in signals.seen.items() if at is not None}
    source = max(seen, key=lambda name: seen[name], default=None)
    at = None if source is None else seen[source]
    if signals.revoked:
        return _record(DOWN, "revoked", at=at, source=source, now=now)
    if not signals.enabled:
        return _record(DOWN, "disabled", at=at, source=source, now=now)
    if at is None:
        return _record(UNKNOWN, "never_reported", now=now)
    age = (now - at).total_seconds()
    if age > config.red_after_s:
        return _record(DOWN, "no_heartbeat", at=at, source=source, now=now)
    if age > config.green_within_s:
        return _record(DEGRADED, "late_heartbeat", at=at, source=source, now=now)
    if signals.start_in_progress:
        return _record(DEGRADED, "start_in_progress", at=at, source=source, now=now)
    return _record(UP, "heartbeat", at=at, source=source, now=now)


def _path_status(link: dict, now: datetime, config: StatusConfig) -> dict:
    """A link's path status from its newest ``status`` report, stale or not."""
    report = link["fields"].get("status")
    if report is None:
        return _record(UNKNOWN, "no_report", now=now)
    reported = report["value"]
    at = model.instant(report["observed_at"])
    kwargs = {"at": at, "source": "path_report", "now": now}
    if reported not in _SEVERITY:
        return _record(UNKNOWN, "reported_unknown", **kwargs)
    age = (now - at).total_seconds()
    if age <= config.path_fresh_s:
        return _record(reported, "reported", **kwargs)
    if age <= config.path_unknown_after_s:
        return _record(DEGRADED if reported == UP else reported, "late_report", **kwargs)
    return _record(UNKNOWN, "report_stale", **kwargs)


def _worse(*records: dict) -> dict:
    """The worst known record (down over degraded over up); unknown only when all are unknown.

    Among unknown records the one that saw a signal (a stale report) explains best."""
    known = [r for r in records if r["status"] in _SEVERITY]
    if not known:
        return next((r for r in records if r["last_signal_at"]), records[0])
    return max(known, key=lambda r: _SEVERITY[r["status"]])


def _end(node: dict, path: dict) -> dict:
    chosen = _worse(node, path)
    return {**chosen, "basis": "path" if chosen is path and chosen is not node else "node"}


def derive(graph: dict, signals: Mapping[str, MachineSignals], *, now: datetime, config) -> dict:
    """Status of every machine, node and link end of ``graph``; pure over its arguments."""
    now = model.clock(now)
    machines = {mid: machine_status(sig, now=now, config=config) for mid, sig in signals.items()}
    nodes = {}
    for record in graph["nodes"]:
        runner = record["fields"].get("runner_id", {}).get("value")
        if runner is None:
            nodes[record["key"]] = _record(UNKNOWN, "no_signals", now=now)
        elif runner not in machines:
            nodes[record["key"]] = _record(UNKNOWN, "machine_not_found", now=now)
        else:
            nodes[record["key"]] = dict(machines[runner])
    paths = {link["id"]: _path_status(link, now, config) for link in graph["links"]}
    by_pair = {}
    for link in graph["links"]:
        by_pair.setdefault(frozenset((link["from"], link["to"])), []).append(link["id"])
    links = {}
    unknown_node = _record(UNKNOWN, "no_signals", now=now)
    for link in graph["links"]:
        path = paths[link["id"]]
        if path["status"] == DOWN and any(
            paths[other]["status"] == UP
            for other in by_pair[frozenset((link["from"], link["to"]))]
            if other != link["id"]
        ):
            path = {
                **path,
                "status": DEGRADED,
                "color": COLORS[DEGRADED],
                "reason": "alternate_path_up",
            }
        ends = {side: _end(nodes.get(link[side], unknown_node), path) for side in ("from", "to")}
        links[link["id"]] = {**_worse(ends["from"], ends["to"]), "ends": ends}
    return {
        "as_of": model.stamp(now),
        "config": config.as_dict(),
        "machines": machines,
        "nodes": nodes,
        "links": links,
    }


def _table(conn, name) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


def _instant(value):
    return None if not value else model.instant(value)


def collect_signals(gateway, workspace_id: str, *, now: datetime) -> dict[str, MachineSignals]:
    """Every enrolled runner machine of the workspace with the signals the hub keeps for it."""
    from commonplace.runner import jobs

    now = model.clock(now)
    db = gateway.db
    with db._lock:
        if not _table(db._conn, "runner_machines"):
            return {}
        rows = db._conn.execute(
            "SELECT m.id,m.enabled,m.revoked_at,mc.last_used_at AS machine_at,"
            "wc.last_used_at AS worker_at FROM runner_machines m "
            "LEFT JOIN connections mc ON mc.id=m.machine_connection_id AND mc.workspace_id=m.workspace_id "
            "LEFT JOIN connections wc ON wc.id=m.worker_connection_id AND wc.workspace_id=m.workspace_id "
            "WHERE m.workspace_id=? ORDER BY m.created_at,m.id",
            (workspace_id,),
        ).fetchall()
        chat = (
            dict(
                db._conn.execute(
                    "SELECT machine_id,seen_at FROM chat_runners_seen WHERE workspace_id=?",
                    (workspace_id,),
                ).fetchall()
            )
            if _table(db._conn, "chat_runners_seen")
            else {}
        )
    started, busy = {}, set()
    if rows:
        lease_limit = model.stamp(now)
        starting_limit = model.stamp(now - timedelta(seconds=_STARTING_GRACE_S))
        with jobs.open_store(gateway, workspace_id).connection() as conn:
            for job in conn.execute(
                "SELECT machine_id,state,updated_at,lease_until,starting_at FROM runner_jobs "
                "WHERE state IN ('started','leased','starting')"
            ):
                if job["state"] == "started":
                    started[job["machine_id"]] = max(
                        started.get(job["machine_id"], ""), job["updated_at"]
                    )
                elif (job["state"] == "leased" and (job["lease_until"] or "") > lease_limit) or (
                    job["state"] == "starting" and (job["starting_at"] or "") > starting_limit
                ):
                    busy.add(job["machine_id"])
    result = {}
    for row in rows:
        seen = {
            "machine_connection": _instant(row["machine_at"]),
            "worker_connection": _instant(row["worker_at"]),
            "chat_poll": _instant(chat.get(row["id"])),
            "start_success": _instant(started.get(row["id"])),
        }
        result[row["id"]] = MachineSignals(
            machine_id=row["id"],
            enabled=bool(row["enabled"]),
            revoked=bool(row["revoked_at"]),
            seen={name: at for name, at in seen.items() if at is not None},
            start_in_progress=row["id"] in busy,
        )
    return result


def read_status(
    gateway,
    workspace_id: str,
    *,
    graph: dict | None = None,
    now: datetime | None = None,
    config: StatusConfig | None = None,
) -> dict:
    """The one read: status of every machine, node and link end of the workspace's map.

    ``graph`` is a ``store.read_graph`` result the caller already holds; ``None`` reads it."""
    now = model.clock(now)
    config = StatusConfig.from_env() if config is None else config
    if graph is None:
        graph = store.read_graph(store.open_topology(gateway, workspace_id), now=now)
    return derive(graph, collect_signals(gateway, workspace_id, now=now), now=now, config=config)


__all__ = [
    "COLORS",
    "ENV",
    "MachineSignals",
    "SIGNALS",
    "StatusConfig",
    "collect_signals",
    "derive",
    "machine_status",
    "read_status",
]
