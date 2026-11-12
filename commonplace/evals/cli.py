"""``commonplace eval``: owner-only local archive, list, show, read, purge, preflight.

Kept light on import: the parser is built for every CLI command, so the
scanner, the stores and continuity load only inside an eval command.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import secrets
import sys
import unicodedata
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from pathlib import Path

from . import (
    EVAL_CHUNK_BYTES,
    EVAL_RUN_MAX_BYTES,
    EVAL_WORKSPACE_QUOTA,
    RUN_ID_PATTERN,
    SCHEMA,
    STORED_ID_PATTERN,
    EvalError,
    key_digest,
)

OBSERVATION = (
    "committed rows of one stored thread at one read snapshot; not evidence of a "
    "verified job, input, completion or billing"
)
HELP = """\
Owner-only local eval archives of stored threads (slice 1). Every command needs
--workspace W (a workspace ID) and acts as --by USER or the first admin, who must
be a live owner of W. Nothing here contacts a network, a model or a process.

Eligible: handoff transcripts and ACL imports in the `threads` wing. Live
captures, grant imports, private chats and continuity threads refuse with
eval_source_gap: unsupported_in_slice1.

Keys: use a random key, e.g. `python3 -c 'import secrets;print(secrets.token_urlsafe(32))'`.
Never a title, path, credential or task text. Only a digest of the key is stored;
anyone who can read runs.sqlite3 or its backup can confirm a guessed key. A purged
run's key stays retired for 30 days.

Keep run IDs. `eval purge RUN --confirm RUN` works even when the source is gone.
Purge does not erase retired store generations, backups, free pages or WAL; a
restore of an older generation can bring runs back.

Artifacts read from stdin are stored as given; plaintext you redirected from a
file stays outside the workspace's encryption and purge.

Storage: runs count against the workspace's plan storage. With plans enabled an
archive needs roughly twice the eval DB's size in headroom. The bound holds only
against writers that measure under the thread-store lock; single-part imports,
palace and drawer writers and handoff filing can still exceed the plan limit. Limits:
16 MiB per run, 256 KiB chunks and pages, 1 GiB per workspace.

After a scanner change, list/show/read rescan whole runs (read: on every page).

Permissions: evals/ must be 0700 and runs.sqlite3* 0600, owned by you. Fix with
`chmod 0700 <workspace>/evals` and `chmod 0600 <workspace>/evals/runs.sqlite3*`.
"""
_MANIFEST_SLACK = 8192


def add_parsers(sub) -> None:
    parser = sub.add_parser(
        "eval",
        help="owner-only local eval archives of stored threads",
        description=HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    actions = parser.add_subparsers(dest="eval_action", required=True)

    def action(name, help_text):
        item = actions.add_parser(name, help=help_text)
        item.add_argument("--workspace", required=True, metavar="W")
        if name != "preflight":
            item.add_argument("--by")
        return item

    item = action("archive", "store an immutable observation of one stored thread")
    item.add_argument("--thread", required=True, metavar="STORED_ID")
    item.add_argument("--key", required=True, metavar="K")
    item.add_argument("--artifacts-stdin", action="store_true")
    action("list", "list authorized runs and limits")
    item = action("show", "print one run's manifest")
    item.add_argument("run", metavar="RUN")
    item = action("read", "read up to 256 KiB of one run object as base64")
    item.add_argument("run", metavar="RUN")
    item.add_argument("--object", required=True, metavar="OBJ")
    item.add_argument("--offset", type=int, default=0, metavar="N")
    item = action("purge", "delete one run by ID after an exact confirmation")
    item.add_argument("run", metavar="RUN")
    item.add_argument("--confirm", metavar="RUN")
    action("preflight", "stat-only report of eval paths, modes and encryption")
    parser.set_defaults(func=cmd_eval)


def cmd_eval(args) -> int:
    if not sys.platform.startswith("linux"):
        raise EvalError("eval_platform")
    handler = _HANDLERS[args.eval_action]
    try:
        return handler(args)
    except EvalError:
        raise
    except Exception as exc:
        raise _mapped(exc) from None


def _mapped(exc) -> EvalError:
    from mempalace.sqlite_crypto import EncryptionError

    if isinstance(exc, EncryptionError):
        return EvalError("eval_encryption")
    if _busy(exc):
        return EvalError("eval_busy")
    return EvalError("eval_store_error")


def _busy(exc) -> bool:
    text = str(exc).lower()
    return "locked" in text or "busy" in text


def _print(value) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _data(args) -> Path:
    from ..cli import _data_dir

    return _data_dir(args)


def _directory(data, workspace_id) -> Path:
    from ..workspace_store import workspace_path

    try:
        return workspace_path(data, workspace_id)
    except ValueError:
        raise EvalError("eval_workspace") from None


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _iso(now=None) -> str:
    return datetime.now(timezone.utc).isoformat() if now is None else now


# ── artifacts ──
def _no_duplicates(pairs):
    keys = [key for key, _ in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate key")
    return dict(pairs)


def _reject_constant(_):
    raise ValueError("non-finite number")


def _name_ok(name) -> bool:
    return bool(
        isinstance(name, str)
        and 0 < len(name) <= 255
        and name not in (".", "..")
        and not name.startswith("~")
        and "/" not in name
        and "\\" not in name
        and not any(unicodedata.category(c).startswith("C") for c in name)
    )


def read_artifacts(stream):
    """Read at most cap+1 bytes; refuse above the cap before parsing."""
    data = stream.read(EVAL_RUN_MAX_BYTES + 1)
    if len(data) > EVAL_RUN_MAX_BYTES:
        raise EvalError("eval_too_large")
    try:
        value = json.loads(
            data.decode("utf-8"), object_pairs_hook=_no_duplicates, parse_constant=_reject_constant
        )
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise EvalError("eval_artifacts_invalid") from None
    if not isinstance(value, list):
        raise EvalError("eval_artifacts_invalid")
    items, names = [], set()
    for item in value:
        if (
            not isinstance(item, dict)
            or set(item) != {"name", "text"}
            or not isinstance(item["text"], str)
        ):
            raise EvalError("eval_artifacts_invalid")
        if not _name_ok(item["name"]) or item["name"] in names:
            raise EvalError("eval_artifact_name")
        try:
            encoded = item["text"].encode("utf-8")
        except UnicodeEncodeError:
            raise EvalError("eval_artifacts_invalid") from None
        names.add(item["name"])
        items.append((item["name"], item["text"], encoded))
    return items


# ── checks shared by every response ──
def _check(store, control, principal, stored_id):
    from . import source

    try:
        return source.check(store, control, principal, stored_id)
    except EvalError:
        raise
    except Exception as exc:
        # A failed lookup is neither "unsupported" nor "absent": refuse the
        # whole response so no run silently disappears from a listing.
        raise EvalError("eval_busy" if _busy(exc) else "eval_lookup_failed") from None


def _recheck(data, directory, workspace_id, by, stored_id):
    """Re-run owner, rule, read policy and probe on fresh snapshots."""
    from . import source

    with source.control_snapshot(data) as control:
        principal = source.owner_principal(control, workspace_id, by)
        with source.thread_snapshot(directory) as store:
            stored, code = _check(store, control, principal, stored_id)
    if code:
        raise EvalError(code)
    return stored


@contextmanager
def _reading(args):
    """Owner check, then control, thread-store and eval DB read snapshots."""
    from . import source, store

    data = _data(args)
    directory = _directory(data, args.workspace)
    store.check_permissions(data, directory)
    with ExitStack() as stack:
        control = stack.enter_context(source.control_snapshot(data))
        principal = source.owner_principal(control, args.workspace, args.by)
        conn = store.open_read(data, directory)
        if conn is not None:
            stack.callback(conn.close)
            conn.execute("BEGIN")
        threads = stack.enter_context(source.thread_snapshot(directory))

        def visible(row):
            stored, code = _check(threads, control, principal, row["stored_id"])
            return code is None and stored.source_thread_id == row["source_thread_id"]

        yield conn, visible


def _flags(conn, row, scanner):
    """Stored codes, plus a read-only current-version rescan when versions differ."""
    from . import flags, store

    codes = json.loads(row["scanner_codes"])
    if row["scanner_version"] == scanner.version:
        return codes
    run_id = row["run_id"]
    rows = json.loads(store.object_bytes(conn, run_id, "rows").decode("utf-8"))
    artifacts = [
        (item["name"], store.object_bytes(conn, run_id, f"artifact/{index}").decode("utf-8"))
        for index, item in enumerate(store.artifact_list(row))
    ]
    manifest = json.loads(store.object_bytes(conn, run_id, "manifest").decode("utf-8"))
    return sorted(set(codes) | set(scanner.codes(flags.run_strings(rows, artifacts, manifest))))


# ── commands ──
def _archive(args) -> int:
    from . import flags, source, store

    workspace_id = args.workspace
    digest = key_digest(workspace_id, args.key)
    if STORED_ID_PATTERN.fullmatch(args.thread) is None:
        raise EvalError("eval_not_found")
    data = _data(args)
    directory = _directory(data, workspace_id)
    store.check_permissions(data, directory)
    artifacts = read_artifacts(sys.stdin.buffer) if args.artifacts_stdin else None
    scanner = flags.load()
    supplied = artifacts or []
    artifact_bytes = sum(len(encoded) for _, _, encoded in supplied)
    estimate = _MANIFEST_SLACK + sum(len(name.encode()) + 160 for name, _, _ in supplied)
    with source.control_snapshot(data) as control:
        principal = source.owner_principal(control, workspace_id, args.by)
        limit = source.storage_limit(control, workspace_id)
        with source.thread_snapshot(directory) as threads:
            stored, code = _check(threads, control, principal, args.thread)
            if code:
                raise EvalError(code)
            budget = EVAL_RUN_MAX_BYTES - artifact_bytes - estimate - len(stored.source_thread_id)
            messages, rows = source.snapshot_rows(threads, stored, budget)
    snapshot = source.as_lists(messages)
    described = [
        {"name": name, "bytes": len(encoded), "sha256": _sha(encoded)}
        for name, _, encoded in supplied
    ]
    created_at = _iso()
    manifest = {
        "schema": SCHEMA,
        "observation": OBSERVATION,
        "workspace_id": workspace_id,
        "stored_id": stored.id,
        "source_thread_id": stored.source_thread_id,
        "wing": stored.wing,
        "created_at": stored.created_at,
        "message_count": stored.message_count,
        "source_sha256": stored.sha256,
        "rows_sha256": _sha(rows),
        "rows_bytes": len(rows),
        "artifacts": described,
        "artifact_origin": "caller stdin" if artifacts is not None else "none",
        "archived_at": created_at,
    }
    codes = scanner.codes(flags.run_strings(snapshot, [(n, t) for n, t, _ in supplied], manifest))
    manifest["scanner_codes"] = codes
    manifest["scanner_version"] = scanner.version
    manifest_bytes = json.dumps(
        manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    total = len(rows) + artifact_bytes + len(manifest_bytes)
    if total > EVAL_RUN_MAX_BYTES:
        raise EvalError("eval_too_large")
    content_id = _sha(
        json.dumps(
            [
                manifest["rows_sha256"],
                [[d["name"], d["sha256"]] for d in described],
                stored.id,
                stored.message_count,
            ],
            ensure_ascii=False,
        ).encode("utf-8")
    )
    run = {
        "run_id": "run_" + secrets.token_hex(12),
        "workspace_id": workspace_id,
        "key_digest": digest,
        "content_id": content_id,
        "stored_id": stored.id,
        "source_thread_id": stored.source_thread_id,
        "message_count": stored.message_count,
        "source_sha256": stored.sha256,
        "rows_sha256": manifest["rows_sha256"],
        "manifest_sha256": _sha(manifest_bytes),
        "rows_bytes": len(rows),
        "manifest_bytes": len(manifest_bytes),
        "artifact_bytes": artifact_bytes,
        "total_bytes": total,
        "artifacts": json.dumps(described, ensure_ascii=False),
        "scanner_codes": json.dumps(codes),
        "scanner_version": scanner.version,
        "created_at": created_at,
    }
    objects = {"manifest": manifest_bytes, "rows": rows}
    objects.update({f"artifact/{i}": encoded for i, (_, _, encoded) in enumerate(supplied)})

    def verify(conn):
        _recheck(data, directory, workspace_id, args.by, stored.id)
        stored_rows = store.object_bytes(conn, run["run_id"], "rows")
        copied = json.loads(stored_rows.decode("utf-8"))
        if (
            copied != snapshot
            or _sha(stored_rows) != run["rows_sha256"]
            or any(
                store.object_bytes(conn, run["run_id"], name) != value
                for name, value in objects.items()
                if name != "rows"
            )
        ):
            raise EvalError("eval_fidelity")

    store.checkpoint("before_lock")
    conn, _ = store.open_write(data, directory, create=True)
    _, db = store.paths(directory)

    def storage(conn):
        from .. import plans

        bound = store.physical_bound(conn, db)
        if plans.StorageMeter._walk(directory) - store.file_bytes(db) + bound > limit:
            raise EvalError("eval_storage")

    try:
        with ExitStack() as stack:
            if limit is not None:
                stack.enter_context(source.thread_lock(directory))
                store.checkpoint("locked")
            run_id, created, reused = store.commit_run(
                conn, run, objects, verify=verify, storage=storage if limit is not None else None
            )
    except EvalError:
        raise
    except Exception as exc:
        raise _mapped(exc) from None
    finally:
        conn.close()
    _print({"schema": SCHEMA, "run_id": run_id, "created_at": created, "reused": reused})
    return 0


def _summary(row, codes):
    return {
        "run_id": row["run_id"],
        "stored_id": row["stored_id"],
        "created_at": row["created_at"],
        "bytes": {
            "manifest": row["manifest_bytes"],
            "rows": row["rows_bytes"],
            "artifacts": row["artifact_bytes"],
            "total": row["total_bytes"],
        },
        "digests": {
            "source_sha256": row["source_sha256"],
            "rows_sha256": row["rows_sha256"],
            "manifest_sha256": row["manifest_sha256"],
        },
        "flags": codes,
    }


def _list(args) -> int:
    from . import flags, store

    scanner = flags.load()
    listed = []
    with _reading(args) as (conn, visible):
        rows = store.runs(conn, args.workspace) if conn is not None else []
        for row in rows:
            if visible(row):
                listed.append(_summary(row, _flags(conn, row, scanner)))
    _print(
        {
            "schema": SCHEMA,
            "runs": listed,
            "limits": {
                "quota_bytes": EVAL_WORKSPACE_QUOTA,
                "used_bytes": sum(item["bytes"]["total"] for item in listed),
                "run_max_bytes": EVAL_RUN_MAX_BYTES,
                "chunk_bytes": EVAL_CHUNK_BYTES,
            },
        }
    )
    return 0


def _authorized_run(conn, visible, args):
    from . import store

    if RUN_ID_PATTERN.fullmatch(args.run) is None or conn is None:
        raise EvalError("eval_not_found")
    row = store.run(conn, args.run, args.workspace)
    if row is None or not visible(row):
        raise EvalError("eval_not_found")
    return row


def _show(args) -> int:
    from . import flags, store

    scanner = flags.load()
    with _reading(args) as (conn, visible):
        row = _authorized_run(conn, visible, args)
        manifest = json.loads(store.object_bytes(conn, row["run_id"], "manifest").decode("utf-8"))
        codes = _flags(conn, row, scanner)
    _print(
        {
            "schema": SCHEMA,
            "run_id": row["run_id"],
            "manifest": manifest,
            "flags": codes,
            "scanner_version_current": scanner.version,
        }
    )
    return 0


def _read(args) -> int:
    from . import flags, store

    scanner = flags.load()
    with _reading(args) as (conn, visible):
        row = _authorized_run(conn, visible, args)
        described = store.artifact_list(row)
        if args.object == "manifest":
            total, digest = row["manifest_bytes"], row["manifest_sha256"]
        elif args.object == "rows":
            total, digest = row["rows_bytes"], row["rows_sha256"]
        elif args.object in {f"artifact/{i}" for i in range(len(described))}:
            item = described[int(args.object[len("artifact/") :])]
            total, digest = item["bytes"], item["sha256"]
        else:
            raise EvalError("eval_object_invalid")
        if args.offset < 0 or args.offset > total:
            raise EvalError("eval_offset_invalid")
        page = store.object_range(conn, row["run_id"], args.object, args.offset, EVAL_CHUNK_BYTES)
        codes = _flags(conn, row, scanner)
    end = args.offset + len(page)
    _print(
        {
            "schema": SCHEMA,
            "object": args.object,
            "offset": args.offset,
            "next": end if end < total else None,
            "total": total,
            "sha256": digest,
            "data_base64": base64.b64encode(page).decode("ascii"),
            "flags": codes,
        }
    )
    return 0


def _generations(data, workspace_id):
    """Validated retired generation copies of W; any read error fails closed."""
    from ..store_switch import RETENTION_SECONDS
    from ..workspace_store import POINTER, read_json, validate_pointer, workspace_root

    try:
        root = workspace_root(data, workspace_id)
        if not (root / POINTER).exists():
            return []
        pointer = read_json(root / POINTER)
        validate_pointer(root, pointer)
        return [
            (item["name"], float(item["retired_at"]), RETENTION_SECONDS)
            for item in pointer.get("retained", [])
        ]
    except (OSError, ValueError, KeyError, TypeError):
        raise EvalError("eval_history") from None


def _purge(args) -> int:
    from . import source, store

    if RUN_ID_PATTERN.fullmatch(args.run) is None:
        raise EvalError("eval_run_invalid")
    data = _data(args)
    directory = _directory(data, args.workspace)
    with source.control_snapshot(data) as control:
        source.owner_principal(control, args.workspace, args.by)
    retained = _generations(data, args.workspace)
    print("Purge removes the run from the active generation only.")
    if not retained:
        print("Retired generation copies: none.")
    for name, retired_at, retention in retained:
        when = datetime.fromtimestamp(retired_at, timezone.utc).isoformat()
        print(
            f"Retired generation copy {name}: retired_at {when}, prune-eligible after "
            f"{retention // 86400} days; restoring it may restore purged runs."
        )
    if args.confirm is None:
        raise EvalError("eval_confirm_required")
    if args.confirm != args.run:
        raise EvalError("eval_confirm_mismatch")
    opened = store.open_write(data, directory, create=False)
    if opened is not None:
        conn, capability = opened
        try:
            store.purge(conn, capability, args.run, args.workspace)
        finally:
            conn.close()
    print("eval purge: acknowledged")
    return 0


def _stat(path):
    entry = {"path": str(path), "exists": False}
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return entry
    entry.update(
        exists=True,
        symlink=os.path.islink(path),
        mode=oct(info.st_mode & 0o7777),
        uid=info.st_uid,
        owned=info.st_uid == os.getuid(),
    )
    return entry


def _preflight(args) -> int:
    from mempalace import sqlite_crypto

    from . import store

    data = _data(args)
    directory = _directory(data, args.workspace)
    evals, db = store.paths(directory)
    targets = [data, directory, evals, db, *(Path(str(db) + s) for s in store.SIDECARS)]
    try:
        classification = "encrypted" if sqlite_crypto.encryption_settings(db) else "plain"
    except Exception:
        classification = "invalid_settings"
    try:
        store.check_permissions(data, directory)
        permissions = "ok"
    except EvalError as exc:
        permissions = str(exc)
    _print(
        {
            "schema": SCHEMA,
            "workspace_id": args.workspace,
            "paths": [_stat(path) for path in targets],
            "classification": classification,
            "permissions": permissions,
        }
    )
    return 0


_HANDLERS = {
    "archive": _archive,
    "list": _list,
    "show": _show,
    "read": _read,
    "purge": _purge,
    "preflight": _preflight,
}
