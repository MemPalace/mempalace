"""``commonplace orch ...``: orchestrator configs, restore acknowledgement and the checks.

Config commands act on the workspace's ``runner.sqlite3`` through ``commonplace.orch.store``
with a local principal (``auth="local"``, the data-dir owner, see ``SYSTEM.md``). The user
named by ``--by`` (default: the first admin) must own the workspace.

- ``submit FILE`` proposes a config (``.commonplace/orchestrator.toml`` from a branch);
  ``--source-ref`` is recorded, not verified. Prints the version and its sha256.
- ``list``, ``render [VERSION]`` (default: the accepted live version), ``diff VA VB``.
- ``accept VERSION --sha256 HEX`` accepts the proposal whose text has that sha256 and
  prints the diff; ``reject VERSION``; ``exp retire NAME`` closes ``exp:NAME``.
- ``restore-status``; ``restore-ack --text "effects after <T> may be unknown"``.
- ``plan-check FILE`` and ``docs-check`` need no data directory (``plan_check``,
  ``docs_check``).

Planner ``connections`` are checked against the workspace's live (not revoked) connection
names at submit and accept. A configuring accept and a restore acknowledgement arm the
trigger cursor of each board they reset at once (``_arm``), like the MCP and REST surfaces;
if that arm does not happen (a crash, a stale ``gen``), the runner's next sweep arms it.
"""

from __future__ import annotations

import json
import re

from . import docs_check, plan_check

REPO_ONLY = frozenset({"plan-check", "docs-check"})
_VERSION = re.compile(r"v?([1-9][0-9]{0,8})")


def _main():
    from commonplace import cli

    return cli


def _version(value: str) -> int:
    import argparse

    match = _VERSION.fullmatch(value or "")
    if not match:
        raise argparse.ArgumentTypeError("version must be like v3 or 3")
    return int(match.group(1))


def _context(args, *, owner: bool):
    """(principal, store, connection names) for ``args.workspace``."""
    from commonplace.control_db import ControlError, Principal
    from commonplace.policy import Policy
    from commonplace.workspace_store import workspace_path

    from . import store

    cli = _main()
    db = cli._db(args)
    try:
        workspace = db.workspace(args.workspace)
        if workspace is None:
            raise ControlError("no such workspace")
        user = db.user(cli._actor(db, args))
        if owner:
            db.require_owner(workspace["id"], user["id"])
        names = [row["name"] for row in db.connections(workspace["id"]) if not row["revoked_at"]]
    finally:
        db.close()
    principal = Principal(
        user_id=user["id"],
        user_email=user["email"],
        workspace_id=workspace["id"],
        connection_id="",
        connection="cli",
        client="commonplace",
        auth="local",
        role="owner",
        policy=Policy.build(
            profile="full",
            scopes=["memory:read", "memory:write", "admin"],
            read_wings=["*"],
            trust="trusted",
        ),
    )
    directory = workspace_path(cli._data_dir(args), workspace["id"])
    return principal, store.Store(directory), names


def _print(data) -> None:
    print(json.dumps(data, sort_keys=True))


def cmd_submit(args) -> int:
    principal, st, names = _context(args, owner=True)
    with open(args.file, "rb") as handle:
        text = handle.read(_max_bytes() + 1)
    board = args.board or _project(text)
    version = st.propose(
        principal,
        board,
        text,
        connections=names,
        channel=args.channel,
        sha256=args.sha256,
        source_ref=args.source_ref,
    )
    _print(version.public())
    return 0


def _max_bytes() -> int:
    from . import schema

    return schema.MAX_CONFIG_BYTES


def _project(text: bytes) -> str:
    """Board slug default: the config's ``project`` (it must equal the board slug)."""
    from . import schema, store

    try:
        return schema.parse(text).project
    except schema.ConfigError as exc:
        raise store._config_error(exc) from None


def cmd_list(args) -> int:
    _principal, st, _names = _context(args, owner=False)
    rows = st.versions(args.board, channel=args.channel, status=args.status)
    _print([row.public() for row in rows])
    return 0


def cmd_render(args) -> int:
    from . import schema, store

    _principal, st, _names = _context(args, owner=False)
    if args.version is None:
        version = st.accepted(args.board, args.channel)
        if version is None:
            raise store.OrchError("not_found", "no accepted version on that channel")
    else:
        version = st.get(args.board, args.version)
    try:
        config = schema.parse(version.body)
    except schema.ConfigError as exc:
        raise store._config_error(exc) from None
    print(schema.render(config), end="")
    return 0


def cmd_diff(args) -> int:
    from . import schema

    _principal, st, _names = _context(args, owner=False)
    print(schema.format_diff(st.diff(args.board, args.old, args.new)))
    return 0


def _arm(st, board: str) -> bool:
    """Arm ``board``'s NULL trigger cursor at the current ``MAX(task_events.id)`` (0 if none).

    The protocol of ``triggers.arm_cursor`` on the store's directory (the CLI has no
    gateway): read ``gen``, then the MAX, then ``UPDATE ... WHERE gen=? AND cursor IS NULL``.
    Returns False when there is nothing to arm or the arm was stale (``gen`` moved)."""
    from commonplace.boards.store import BoardStore

    from . import store

    with st.connection() as conn:
        pair = store.cursor_in(conn, board)
        if pair is None or pair[0] is not None:
            return False
        gen = pair[1]
        with BoardStore(st.directory).connection() as boards:
            top = boards.execute("SELECT COALESCE(MAX(id), 0) FROM task_events").fetchone()[0]
        changed = conn.execute(
            "UPDATE orch_cursor SET cursor=? WHERE board=? AND gen=? AND cursor IS NULL",
            (top, board, gen),
        ).rowcount
    return changed == 1


def cmd_accept(args) -> int:
    from . import schema

    principal, st, names = _context(args, owner=True)
    result = st.accept(principal, args.board, args.version, sha256=args.sha256, connections=names)
    cursor = result.cursor
    if result.arm_needed and _arm(st, result.version.board):
        cursor = "armed"
    _print(
        {
            "accepted": result.version.ref,
            "board": result.version.board,
            "channel": result.version.channel,
            "sha256": result.version.sha256,
            "previous": result.previous.ref if result.previous else None,
            "cursor": cursor,
            "diff": schema.format_diff(result.changes),
        }
    )
    return 0


def cmd_reject(args) -> int:
    principal, st, _names = _context(args, owner=True)
    _print(st.reject(principal, args.board, args.version).public())
    return 0


def cmd_exp_retire(args) -> int:
    principal, st, _names = _context(args, owner=True)
    rows = st.retire(principal, args.board, f"exp:{args.name}")
    _print([row.public() for row in rows])
    return 0


def cmd_restore_status(args) -> int:
    _principal, st, _names = _context(args, owner=False)
    state = st.restore_state()
    _print(
        {
            "restore_pending": state.flagged,
            "after": state.after,
            "ack_text": state.ack_text,
            "restored_at": state.restored_at,
            "acked_by": state.acked_by,
            "acked_at": state.acked_at,
        }
    )
    return 0


def cmd_restore_ack(args) -> int:
    principal, st, _names = _context(args, owner=True)
    reset = st.restore_ack(principal, args.text)
    arming = [{"board": b, "gen": g, "armed": _arm(st, b)} for b, g in reset]
    _print({"acknowledged": True, "arming": arming})
    return 0


def add_parsers(sub) -> None:
    """Register ``orch`` (and the top-level ``docs-check`` alias) on the main subparsers."""
    orch = sub.add_parser("orch", help="orchestrator configs and checks").add_subparsers(
        dest="action", required=True
    )

    def workspace(parser, *, by=False, board=True):
        parser.add_argument("--workspace", required=True)
        if board:
            parser.add_argument("--board", required=True, help="board slug")
        if by:
            parser.add_argument("--by", help="acting owner (default: the first admin)")

    s = orch.add_parser("submit", help="propose a config file")
    s.add_argument("file")
    s.add_argument("--workspace", required=True)
    s.add_argument("--board", help="board slug (default: the config's project)")
    s.add_argument("--channel", default="live", help="live or exp:<name>")
    s.add_argument("--source-ref", help="branch or commit the file came from (not verified)")
    s.add_argument("--sha256", help="refuse unless the file has this sha256")
    s.add_argument("--by")
    s.set_defaults(func=cmd_submit)

    s = orch.add_parser("list", help="list config versions, newest first")
    workspace(s)
    s.add_argument("--channel")
    s.add_argument("--status", choices=("proposed", "accepted", "rejected", "superseded"))
    s.set_defaults(func=cmd_list)

    s = orch.add_parser("render", help="print a version as Markdown")
    workspace(s)
    s.add_argument("version", nargs="?", type=_version)
    s.add_argument("--channel", default="live")
    s.set_defaults(func=cmd_render)

    s = orch.add_parser("diff", help="diff two versions")
    workspace(s)
    s.add_argument("old", type=_version)
    s.add_argument("new", type=_version)
    s.set_defaults(func=cmd_diff)

    s = orch.add_parser("accept", help="accept a proposal whose text has the given sha256")
    workspace(s, by=True)
    s.add_argument("version", type=_version)
    s.add_argument("--sha256", required=True)
    s.set_defaults(func=cmd_accept)

    s = orch.add_parser("reject", help="reject a proposal")
    workspace(s, by=True)
    s.add_argument("version", type=_version)
    s.set_defaults(func=cmd_reject)

    exp = orch.add_parser("exp", help="experiments").add_subparsers(
        dest="exp_action", required=True
    )
    s = exp.add_parser("retire", help="close the experiment channel exp:NAME")
    workspace(s, by=True)
    s.add_argument("name")
    s.set_defaults(func=cmd_exp_retire)

    s = orch.add_parser("restore-status", help="show the restore flag and the text to type")
    workspace(s, board=False)
    s.set_defaults(func=cmd_restore_status)

    s = orch.add_parser("restore-ack", help="acknowledge a restore with the exact text")
    workspace(s, by=True, board=False)
    s.add_argument("--text", required=True)
    s.set_defaults(func=cmd_restore_ack)

    s = orch.add_parser("plan-check", help="check a plan against the design-before-code grammar")
    plan_check.build_parser(s)
    s.set_defaults(func=plan_check.run)

    for parser in (
        orch.add_parser("docs-check", help="check MODULES.md and SYSTEM.md obligations"),
        sub.add_parser("docs-check", help="check MODULES.md and SYSTEM.md obligations"),
    ):
        docs_check.build_parser(parser)
        parser.set_defaults(func=docs_check.run)


def repo_only(args) -> bool:
    """Commands that read only the repository: no data directory, no data lock."""
    return args.command == "docs-check" or (
        args.command == "orch" and getattr(args, "action", None) in REPO_ONLY
    )
