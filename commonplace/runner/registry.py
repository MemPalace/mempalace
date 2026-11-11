"""Owner-managed machine grants and binding attestations."""

import json
import re
import sqlite3

from commonplace import control_db
from commonplace.control_db import ControlError
from commonplace.policy import Policy, canonical_wing, is_private_wing

MACHINE_ID = re.compile(r"mach_[0-9a-f]{16}")
LABEL = re.compile(r"[a-z0-9][a-z0-9._-]{0,39}")
PROJECT = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
TOPIC = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}")
CHAT_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,119}")
PROVIDER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
MODES = ("approval-required", "auto-accept-edits", "auto", "full-access")
WORKER_TOOLS = (
    "task_show",
    "task_take_next",
    "task_update",
    "task_comment",
    "task_heartbeat",
    "task_handoff",
    "ask",
)
GRANT_KEYS = {
    "label",
    "projects",
    "topics",
    "wings",
    "max_runtime_mode",
    "requesters",
    "max_in_flight",
    "max_starts_per_hour",
}
JSON_KEYS = ("projects", "topics", "wings", "requesters", "binding_topics")
RESET = (
    "binding_state='unbound', binding_topics=NULL, binding_provider_instance_id=NULL, "
    "binding_worker_connection_id=NULL, binding_attested_by=NULL, "
    "binding_attested_at=NULL, binding_grant_version=NULL, binding_epoch=binding_epoch+1"
)


class NotFound(ControlError):
    """The machine does not belong to this workspace."""


class Conflict(ControlError):
    """An owner action conflicts with existing registry state."""


def ensure(db):
    with db._lock:
        db._conn.executescript("""
            CREATE TABLE IF NOT EXISTS runner_machines (
                id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL,
                label TEXT NOT NULL,
                machine_connection_id TEXT NOT NULL UNIQUE,
                worker_connection_id TEXT NOT NULL UNIQUE,
                projects TEXT NOT NULL, topics TEXT NOT NULL, wings TEXT NOT NULL,
                max_runtime_mode TEXT NOT NULL DEFAULT 'approval-required'
                    CHECK(max_runtime_mode IN ('approval-required','auto-accept-edits','auto','full-access')),
                requesters TEXT NOT NULL DEFAULT '[]',
                max_in_flight INTEGER NOT NULL DEFAULT 1 CHECK(max_in_flight BETWEEN 1 AND 4),
                max_starts_per_hour INTEGER NOT NULL DEFAULT 6 CHECK(max_starts_per_hour BETWEEN 1 AND 30),
                enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
                grant_version INTEGER NOT NULL DEFAULT 1,
                binding_epoch INTEGER NOT NULL DEFAULT 1,
                binding_state TEXT NOT NULL DEFAULT 'unbound'
                    CHECK(binding_state IN ('unbound','owner_attested')),
                binding_topics TEXT, binding_provider_instance_id TEXT,
                binding_worker_connection_id TEXT, binding_attested_by TEXT,
                binding_attested_at TEXT, binding_grant_version INTEGER,
                enrolled_by TEXT NOT NULL, updated_by TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL, revoked_at TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS runner_machines_label
                ON runner_machines(workspace_id, label) WHERE revoked_at IS NULL;
        """)
        db._conn.execute("BEGIN IMMEDIATE")
        try:
            columns = {row[1] for row in db._conn.execute("PRAGMA table_info(runner_machines)")}
            if "binding_epoch" not in columns:
                db._conn.execute(
                    "ALTER TABLE runner_machines ADD COLUMN binding_epoch INTEGER NOT NULL DEFAULT 1"
                )
            db._conn.execute("COMMIT")
        except BaseException:
            db._conn.execute("ROLLBACK")
            raise


def _owner(owner):
    if not owner.person or owner.role != "owner":
        raise PermissionError("a signed-in workspace owner is required")


def _strings(value, key, limit, pattern=None):
    if not isinstance(value, list) or not 1 <= len(value) <= limit:
        raise ValueError(f"{key} must contain 1 to {limit} entries")
    if any(not isinstance(v, str) or (pattern and not pattern.fullmatch(v)) for v in value):
        raise ValueError(f"invalid {key}")
    return list(dict.fromkeys(value))


def _grant(db, owner, grant, *, initial=False):
    if not isinstance(grant, dict) or set(grant) - (
        GRANT_KEYS | (set() if initial else {"enabled"})
    ):
        raise ValueError("unknown grant key")
    data = dict(grant)
    if initial:
        for key in ("label", "projects", "topics", "wings"):
            if key not in data:
                raise ValueError(f"{key} is required")
    if "label" in data:
        label = data["label"]
        if isinstance(label, str) and MACHINE_ID.fullmatch(label):
            raise ValueError("label must not look like a machine id")
        if not isinstance(label, str) or not LABEL.fullmatch(label):
            raise ValueError("invalid label")
    for key, limit, pattern in (
        ("projects", 32, PROJECT),
        ("topics", 32, TOPIC),
        ("wings", 8, None),
    ):
        if key in data:
            data[key] = _strings(data[key], key, limit, pattern)
    if "wings" in data:
        if any(
            not w.strip()
            or len(w) > 200
            or any(c in w for c in "*?[]")
            or is_private_wing(w)
            or not owner.policy.can_read_wing(w)
            for w in data["wings"]
        ):
            raise ValueError("invalid wings")
        data["wings"] = list(dict.fromkeys(canonical_wing(w) for w in data["wings"]))
    if "max_runtime_mode" in data and data["max_runtime_mode"] not in MODES:
        raise ValueError("invalid max_runtime_mode")
    for key, limit in (("max_in_flight", 4), ("max_starts_per_hour", 30)):
        if key in data and (type(data[key]) is not int or not 1 <= data[key] <= limit):
            raise ValueError(f"{key} must be between 1 and {limit}")
    if "enabled" in data and type(data["enabled"]) is not bool:
        raise ValueError("enabled must be a boolean")
    if "requesters" in data:
        refs = data["requesters"]
        if not isinstance(refs, list) or any(not isinstance(ref, str) for ref in refs):
            raise ValueError("invalid requesters")
        for ref in refs:
            p = db.authenticate_connection(ref)
            linked = db._conn.execute(
                "SELECT 1 FROM runner_connections WHERE connection_id=?", (ref,)
            ).fetchone()
            if (
                p is None
                or p.workspace_id != owner.workspace_id
                or p.auth != "pat"
                or p.policy.trust != "trusted"
                or linked
            ):
                raise ValueError("requesters must be active trusted workspace agents")
        data["requesters"] = list(dict.fromkeys(refs))
    return data


def _worker_policy(wings):
    """Read and write exactly the granted wings; the first is the default write wing."""
    return Policy.build(
        tools=WORKER_TOOLS,
        scopes=("memory:read", "memory:write"),
        read_wings=wings,
        write_wings=wings,
        write_wing=wings[0],
        trust="standard",
    )


def _audit(db, owner, action, machine_id, *, reset=False):
    db.audit(
        action=f"runner.{action}",
        decision="ok",
        workspace_id=owner.workspace_id,
        user_id=owner.user_id,
        connection_id=owner.connection_id,
        auth=owner.auth,
        reason="binding_reset" if reset else "",
        target_ids=(machine_id,),
    )


def _write(db, machine_id, data):
    values = {k: json.dumps(v) if k in JSON_KEYS and v is not None else v for k, v in data.items()}
    try:
        db._conn.execute(
            f"UPDATE runner_machines SET {', '.join(k + '=?' for k in values)} WHERE id=?",
            (*values.values(), machine_id),
        )
    except sqlite3.IntegrityError:
        raise Conflict("an active machine already uses this label") from None


def enroll(db, owner, grant):
    _owner(owner)
    with db._lock, db._tx() as conn:
        data = _grant(db, owner, grant, initial=True)
        mid = control_db._new_id("mach")
        now = control_db._iso(control_db._now())
        credentials = {}
        for kind, policy in (
            (
                "machine",
                Policy.build(profile="slim", tools=[], scopes=[], read_wings=[], trust="standard"),
            ),
            ("worker", _worker_policy(data["wings"])),
        ):
            cid, token = db.create_token(
                workspace_ref=owner.workspace_id,
                user_ref=owner.user_id,
                actor=owner.user_id,
                name=f"runner-{kind}:{mid}",
                client="commonplace-runner",
                policy=policy,
                expires_days=90,
            )
            control_db.link_runner(conn, cid, mid, kind)
            credentials[f"{kind}_connection_id"] = cid
            credentials[f"{kind}_token"] = token
        try:
            conn.execute(
                "INSERT INTO runner_machines(id, workspace_id, label, machine_connection_id, worker_connection_id, projects, topics, wings, enrolled_by, updated_by, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    mid,
                    owner.workspace_id,
                    data.pop("label"),
                    credentials["machine_connection_id"],
                    credentials["worker_connection_id"],
                    json.dumps(data.pop("projects")),
                    json.dumps(data.pop("topics")),
                    json.dumps(data.pop("wings")),
                    owner.user_id,
                    owner.user_id,
                    now,
                    now,
                ),
            )
        except sqlite3.IntegrityError:
            raise Conflict("an active machine already uses this label") from None
        if data:
            _write(db, mid, data)
        _audit(db, owner, "enroll", mid)
        return {"machine_id": mid, **credentials, "binding_state": "unbound"}


def get_machine(db, ws, ref):
    if not isinstance(ref, str):
        return None
    with db._lock:
        key = "id" if MACHINE_ID.fullmatch(ref) else "label"
        active = "" if key == "id" else " AND revoked_at IS NULL"
        return db._conn.execute(
            f"SELECT * FROM runner_machines WHERE workspace_id=? AND {key}=?{active}", (ws, ref)
        ).fetchone()


def machines(db, ws):
    with db._lock:
        return db._conn.execute(
            "SELECT * FROM runner_machines WHERE workspace_id=? ORDER BY created_at, id", (ws,)
        ).fetchall()


def _active(db, owner, machine_id):
    row = get_machine(db, owner.workspace_id, machine_id)
    if row is None:
        raise NotFound("machine not found")
    if row["revoked_at"]:
        raise Conflict("machine is revoked")
    return row


def patch(db, owner, machine_id, grant):
    _owner(owner)
    with db._lock, db._tx():
        return patch_in(db, owner, machine_id, grant)


def patch_in(db, owner, machine_id, grant, *, remove_requester=None):
    """Patch inside the caller's control transaction; removal preserves stale peers."""
    _owner(owner)
    row = (
        _active(db, owner, machine_id)
        if remove_requester is None
        else get_machine(db, owner.workspace_id, machine_id)
    )
    if remove_requester is None:
        data = _grant(db, owner, grant)
    else:
        data = {
            "requesters": [ref for ref in json.loads(row["requesters"]) if ref != remove_requester]
        }
    reset = any(key in data and data[key] != json.loads(row[key]) for key in ("topics", "wings"))
    if "wings" in data:
        db._conn.execute(
            "UPDATE connections SET policy=? WHERE id=?",
            (_worker_policy(data["wings"]).to_json(), row["worker_connection_id"]),
        )
    data.update(
        grant_version=row["grant_version"] + 1,
        updated_by=owner.user_id,
        updated_at=control_db._iso(control_db._now()),
    )
    _write(db, row["id"], data)
    if reset:
        db._conn.execute(f"UPDATE runner_machines SET {RESET} WHERE id=?", (row["id"],))
    _audit(db, owner, "patch", row["id"], reset=reset)
    return get_machine(db, owner.workspace_id, row["id"])


def attest(db, owner, machine_id, binding):
    _owner(owner)
    with db._lock, db._tx():
        row = _active(db, owner, machine_id)
        if not isinstance(binding, dict) or set(binding) != {
            "topics",
            "provider_instance_id",
            "worker_connection_id",
        }:
            raise ValueError("topics, provider_instance_id and worker_connection_id are required")
        topics = _strings(binding["topics"], "topics", 32, TOPIC)
        if set(topics) != set(json.loads(row["topics"])):
            raise ValueError("topics must match the machine grant")
        if binding["worker_connection_id"] != row["worker_connection_id"]:
            raise ValueError("worker_connection_id must match the machine worker")
        provider = binding["provider_instance_id"]
        if not isinstance(provider, str) or not PROVIDER.fullmatch(provider):
            raise ValueError("invalid provider_instance_id")
        if (
            row["binding_state"] == "owner_attested"
            and provider != row["binding_provider_instance_id"]
        ):
            raise Conflict("provider_change_requires_reset")
        now = control_db._iso(control_db._now())
        _write(
            db,
            row["id"],
            dict(
                binding_state="owner_attested",
                binding_topics=topics,
                binding_provider_instance_id=provider,
                binding_worker_connection_id=row["worker_connection_id"],
                binding_attested_by=owner.user_id,
                binding_attested_at=now,
                binding_grant_version=row["grant_version"],
                updated_by=owner.user_id,
                updated_at=now,
            ),
        )
        _audit(db, owner, "attest", row["id"])
        return {
            "binding_state": "owner_attested",
            "verified": False,
            "note": "attested by the owner; not verified by Commonplace",
            "launch_shape": "uncaptured",
        }


def rotate(db, owner, machine_id, kind="worker"):
    _owner(owner)
    if kind not in ("machine", "worker"):
        raise ValueError("kind must be machine or worker")
    with db._lock, db._tx():
        row = _active(db, owner, machine_id)
        token = db.rotate_token(
            row[f"{kind}_connection_id"],
            actor=owner.user_id,
            workspace_ref=owner.workspace_id,
            expires_days=90,
            _runner_owned=True,
        )
        _write(
            db,
            row["id"],
            dict(
                grant_version=row["grant_version"] + 1,
                updated_by=owner.user_id,
                updated_at=control_db._iso(control_db._now()),
            ),
        )
        if kind == "worker":
            db._conn.execute(f"UPDATE runner_machines SET {RESET} WHERE id=?", (row["id"],))
        _audit(db, owner, "rotate", row["id"], reset=kind == "worker")
        return {
            "machine_id": row["id"],
            f"{kind}_token": token,
            "binding_state": "unbound" if kind == "worker" else row["binding_state"],
        }


def revoke(db, owner, machine_id):
    _owner(owner)
    with db._lock, db._tx():
        row = _active(db, owner, machine_id)
        for kind in ("machine", "worker"):
            db.revoke(
                row[f"{kind}_connection_id"],
                actor=owner.user_id,
                workspace_ref=owner.workspace_id,
                _runner_owned=True,
            )
        now = control_db._iso(control_db._now())
        _write(
            db,
            row["id"],
            dict(
                revoked_at=now,
                enabled=0,
                grant_version=row["grant_version"] + 1,
                updated_by=owner.user_id,
                updated_at=now,
            ),
        )
        db._conn.execute(f"UPDATE runner_machines SET {RESET} WHERE id=?", (row["id"],))
        _audit(db, owner, "revoke", row["id"], reset=True)
        return {"ok": True}


def worker_principal(db, machine):
    return db.authenticate_connection(machine["worker_connection_id"])


def seat_trust_in(db, owner, seat_cid, trusted):
    """Change only the trust bit of an active project seat, inside a control txn."""
    _owner(owner)
    row = db.connection(seat_cid, owner.workspace_id)
    principal = db.authenticate_connection(seat_cid)
    seat = db._conn.execute(
        "SELECT 1 FROM project_orchestrators WHERE workspace_id=? AND connection_id=?",
        (owner.workspace_id, seat_cid),
    ).fetchone()
    if principal is None or not seat or getattr(principal, "runner", None) is not None:
        raise Conflict("seat is not active")
    policy = json.loads(row["policy"])
    policy["trust"] = "trusted" if trusted else "standard"
    db._conn.execute("UPDATE connections SET policy=? WHERE id=?", (json.dumps(policy), seat_cid))
