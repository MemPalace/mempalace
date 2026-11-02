"""``commonplace`` command line: set up accounts and tokens, run the gateway."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
from contextlib import ExitStack
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import plans
from .backup import BackupError, data_lock
from .control_db import ControlDB, ControlError
from .extensions import ToolError
from .mailer import MailError
from .policy import PROFILES, TRUST_LEVELS, Policy, PolicyError

DATA_ENV = "COMMONPLACE_DATA"


def _data_dir(args) -> Path:
    return Path(args.data or os.environ.get(DATA_ENV) or Path.home() / ".commonplace").expanduser()


def _db(args) -> ControlDB:
    return ControlDB(str(_data_dir(args) / "control.sqlite3"))


def _csv(value):
    if value is None:
        return None
    return [v.strip() for v in value.split(",") if v.strip()]


def _actor(db: ControlDB, args) -> str:
    if getattr(args, "by", None):
        return args.by
    admins = [u for u in db.users() if u["is_admin"]]
    if not admins:
        raise ControlError("no admin yet: run `commonplace init` first")
    return admins[0]["id"]


def cmd_init(args) -> int:
    data = _data_dir(args)
    db = _db(args)
    if db.has_users():
        raise ControlError(f"{data} is already set up")
    uid = db.create_user(args.admin_email, is_admin=True)
    wid = db.create_workspace(args.workspace, uid, personal=True)
    print(f"data       {data}")
    print(f"admin      {args.admin_email} ({uid})")
    print(f"workspace  {args.workspace} ({wid})")
    return 0


def cmd_user_add(args) -> int:
    db = _db(args)
    uid = db.create_user(args.email, args.name or "")
    print(uid)
    if not args.no_workspace:
        print(db.create_workspace(args.email, uid, personal=True))
    return 0


def cmd_user_list(args) -> int:
    for u in _db(args).users():
        print(f"{u['id']}  {u['email']}{'  admin' if u['is_admin'] else ''}")
    return 0


def cmd_plan_set(args) -> int:
    db = _db(args)
    try:
        user = db.user(args.user)
        actor = db.user(_actor(db, args))
        if not actor["is_admin"]:
            raise ControlError("admin access required")
        plans.set_plan(db, user["id"], args.plan, actor=actor["id"])
        print(f"plan  {args.plan}")
        if not plans.enabled():
            print("Plans are off.")
        return 0
    finally:
        db.close()


def cmd_plan_show(args) -> int:
    from .workers import WorkerSupervisor

    db = _db(args)
    try:
        user = db.user(args.user)
        plan = plans.get_plan(db, user["id"])
        meter = plans.StorageMeter()
        supervisor = WorkerSupervisor(str(_data_dir(args)))
        used = sum(
            meter.used(ws["id"], supervisor.workspace_dir(ws["id"]))
            for ws in db.workspaces()
            if ws["personal_of"] == user["id"]
        )
        storage = (
            str(plan.storage_bytes) if plan and plan.storage_bytes is not None else "unlimited"
        )
        voice = (
            str(plan.transcription_seconds)
            if plan and plan.transcription_seconds is not None
            else "unlimited"
        )
        if plan and plan.name == "paid":
            storage = voice = "unlimited**"
        print(f"plan  {plan.name if plan else 'none'}")
        print(f"storage  {used} / {storage} bytes")
        print(f"transcription today  {plans.transcription_used(db, user['id'])} / {voice} seconds")
        print(f"sync interval  {plans.sync_interval(db, user['id'])} seconds")
        if plan and plan.name == "paid":
            print(plans.PAID_FOOTNOTE)
        if not plans.enabled():
            print("Plans are off.")
        return 0
    finally:
        db.close()


def cmd_workspace_add(args) -> int:
    print(_db(args).create_workspace(args.name, args.owner))
    return 0


def cmd_workspace_list(args) -> int:
    db = _db(args)
    for w in db.workspaces():
        members = ", ".join(f"{m['email']} ({m['role']})" for m in db.members(w["id"]))
        print(f"{w['id']}  {w['name']}  {members}")
    return 0


def cmd_member_add(args) -> int:
    db = _db(args)
    db.add_member(args.workspace, args.email, args.role, actor=_actor(db, args))
    return 0


def cmd_token_add(args) -> int:
    db = _db(args)
    from .bridge import CLIENT_PRESETS, DEFAULT_CLIENT_PRESET

    defaults = CLIENT_PRESETS.get(args.client, DEFAULT_CLIENT_PRESET)

    def value(name, fallback):
        explicit = getattr(args, name)
        return explicit if explicit is not None else defaults.get(name, fallback)

    policy = Policy.build(
        profile=value("profile", "slim"),
        tools=_csv(args.tools),
        scopes=_csv(args.scopes),
        read_wings=_csv(value("read_wings", "")),
        write_wings=_csv(value("write_wings", "")),
        write_wing=value("write_wing", ""),
        trust=value("trust", "untrusted"),
    )
    actor = _actor(db, args)
    user = args.user or actor
    _cid, token = db.create_token(
        workspace_ref=args.workspace,
        user_ref=user,
        name=args.name,
        client=args.client,
        policy=policy,
        actor=actor,
        expires_days=args.expires_days or None,
        parent=args.parent,
    )
    # The only time the token is shown. Stdout carries nothing else, so
    # `commonplace token add ... > file` captures exactly the token.
    print(token)
    print(json.dumps(policy.summary()), file=sys.stderr)
    return 0


def cmd_token_list(args) -> int:
    db = _db(args)
    for c in db.connections(args.workspace):
        state = "revoked" if c["revoked_at"] else f"expires {c['expires_at'] or 'never'}"
        policy = Policy.from_json(c["policy"]).summary()
        print(
            f"{c['name']}  {c['token_hint']}  {c['client']}  {c['workspace_id']}  {state}  "
            f"parent {c['parent_id'] or '-'}  started_by {c['started_by']}  "
            f"last used {c['last_used_at'] or 'never'}  {json.dumps(policy)}"
        )
    return 0


def cmd_token_revoke(args) -> int:
    db = _db(args)
    db.revoke(args.name, actor=_actor(db, args), workspace_ref=args.workspace)
    return 0


def cmd_audit(args) -> int:
    for e in _db(args).audit_entries(args.workspace, args.limit):
        print(
            f"{e['seq']}  {e['at']}  {e['action']}  {e['tool'] or '-'}  {e['decision']}  "
            f"conn={e['connection_id'] or '-'}  wing={e['wing'] or '-'}  ids={e['target_ids']}"
            + (f"  {e['reason']}" if e["reason"] else "")
        )
    return 0


def _owner(db, args, workspace_id):
    user = db.user(_actor(db, args))
    db.require_owner(workspace_id, user["id"])
    return user


def _settings(db, handoff_db):
    from .handoff.events import Events
    from .handoff.settings import SettingsService

    return SettingsService(handoff_db, Events(db, handoff_db))


def _setting_scope(args):
    if args.agent is not None:
        return "agent", args.agent
    if args.task is not None:
        return "task", args.task
    return "workspace", ""


def cmd_handoff_status(args) -> int:
    from .handoff.db import HandoffDB

    db = _db(args)
    workspace = db.workspace(args.workspace)["id"]
    setting = _settings(db, HandoffDB(db)).effective(workspace, args.agent or "", args.task or "")
    print(
        f"handoff: {'on' if setting.enabled else 'off'} ({setting.source}), "
        f"threshold {setting.threshold:.2f}, window {setting.context_window or 'auto'}, "
        f"launcher {setting.launcher.kind}"
    )
    return 0


def cmd_handoff_set(args) -> int:
    from .handoff.db import HandoffDB
    from .handoff.models import LauncherConfig, SettingRow

    db = _db(args)
    workspace = db.workspace(args.workspace)["id"]
    user = _owner(db, args, workspace)
    scope, ref = _setting_scope(args)
    launcher = None
    if args.launcher is None:
        if any(
            value is not None
            for value in (args.launcher_argv, args.launcher_url, args.launcher_secret_env)
        ):
            raise ControlError("--launcher is required with launcher options")
    else:
        secret = ""
        if args.launcher_secret_env is not None:
            secret = os.environ.get(args.launcher_secret_env, "")
            if not secret:
                raise ControlError(f"environment variable {args.launcher_secret_env} is not set")
        launcher = LauncherConfig(
            kind=args.launcher,
            argv=tuple(args.launcher_argv or ()),
            url=args.launcher_url or "",
            secret=secret,
        )
    row = SettingRow(
        workspace_id=workspace,
        scope=scope,
        ref=ref,
        enabled=args.action == "enable",
        threshold=args.threshold,
        context_window=args.window,
        launcher=launcher,
    )
    _settings(db, HandoffDB(db)).set(row, actor=user["id"], is_owner=True)
    print(f"handoff: {'on' if row.enabled else 'off'} ({scope})")
    return 0


def cmd_handoff_clear(args) -> int:
    from .handoff.db import HandoffDB

    db = _db(args)
    workspace = db.workspace(args.workspace)["id"]
    user = _owner(db, args, workspace)
    scope, ref = _setting_scope(args)
    _settings(db, HandoffDB(db)).clear(workspace, scope, ref, actor=user["id"], is_owner=True)
    print(f"handoff: cleared ({scope})")
    return 0


def _handoff_record(handoff_db, handoff_id):
    record = handoff_db.get(handoff_id)
    if record is None:
        raise ControlError("handoff not found")
    return record


def _print_handoff(record):
    successor = record.successor.agent_ref if record.successor is not None else "-"
    print(
        f"{record.id}  {record.state}  thread {record.stored_thread_id or '-'}  successor {successor}"
    )


def cmd_handoff_list(args) -> int:
    from .handoff.db import HandoffDB

    db = _db(args)
    workspace = db.workspace(args.workspace)["id"]
    for record in HandoffDB(db).list(workspace, task_ref=args.task or "", limit=args.limit):
        _print_handoff(record)
    return 0


def cmd_handoff_show(args) -> int:
    from .handoff.db import HandoffDB

    db = _db(args)
    with ExitStack() as stack:
        documents = _stores(args, stack, root=Path(db.path).parent)
        record = _handoff_record(HandoffDB(db, document_store_for=documents), args.handoff_id)
        _print_handoff(record)
        sys.stdout.write(record.doc_markdown)
    return 0


def _stores(args, stack, *, root=None):
    from .workspace_store import open_threads, workspace_path

    stores = {}

    def store_for(workspace_id):
        if workspace_id not in stores:
            directory = workspace_path(root or _data_dir(args), workspace_id)
            directory.mkdir(parents=True, exist_ok=True)
            store = stores[workspace_id] = open_threads(directory)
            stack.callback(store.close)
        return stores[workspace_id]

    return store_for


def cmd_handoff_relaunch(args) -> int:
    from .control_db import Principal
    from .handoff.db import HandoffDB
    from .handoff.events import Events
    from .handoff.service import HandoffService

    db = _db(args)
    handoff_db = HandoffDB(db)
    record = _handoff_record(handoff_db, args.handoff_id)
    user = _owner(db, args, record.workspace_id)
    principal = Principal(
        user_id=user["id"],
        user_email=user["email"],
        workspace_id=record.workspace_id,
        connection_id="",
        connection="cli",
        client="commonplace",
        auth="local",
        role="owner",
        policy=Policy.build(
            profile="full",
            scopes=["memory:read", "private:read", "threads:read", "handoff:write", "admin"],
            read_wings=["*"],
            trust="trusted",
        ),
    )
    events = Events(db, handoff_db)
    with ExitStack() as stack:
        service = HandoffService(
            control_db=db,
            handoff_db=handoff_db,
            settings=_settings(db, handoff_db),
            store_for=_stores(args, stack),
            events=events,
            prompt_dir=str(_data_dir(args) / "handoff-prompts"),
        )
        result = service.relaunch(principal, record.id)
    if (
        result.action != "handoff"
        or result.handoff is None
        or result.handoff.state in ("failed", "launch_failed")
    ):
        raise ControlError(
            result.reason
            or (result.handoff.error if result.handoff is not None else "")
            or "handoff relaunch failed"
        )
    if result.launch:
        sys.stdout.write(result.prompt)
    elif result.handoff is not None:
        _print_handoff(result.handoff)
    return 0


def cmd_handoff_start(args) -> int:
    from .handoff.manual import run_cli

    return run_cli(args)


def cmd_thread(args) -> int:
    db = _db(args)
    workspace = db.workspace(args.workspace)["id"]
    if args.action == "get" and args.end is not None and args.end < args.start:
        raise ControlError("end must be at least start")
    with ExitStack() as stack:
        store = _stores(args, stack)(workspace)
        thread = store.get(args.thread_id)
        if thread is None or thread.workspace_id != workspace:
            raise ControlError("thread not found")
        if args.action == "get":
            entries = store.messages(args.thread_id, args.start, args.end)
        else:
            entries = store.search([args.thread_id], args.query)
        for entry in entries:
            sys.stdout.write(entry.text)
    return 0


def _positive_int(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def _nonnegative_int(value):
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be nonnegative")
    return number


def _threshold(value):
    from .handoff.settings import THRESHOLD_MAX, THRESHOLD_MIN

    number = float(value)
    if not THRESHOLD_MIN <= number <= THRESHOLD_MAX:
        raise argparse.ArgumentTypeError(f"must be between {THRESHOLD_MIN} and {THRESHOLD_MAX}")
    return number


def _launcher_argv(value):
    try:
        argv = json.loads(value)
    except json.JSONDecodeError:
        raise argparse.ArgumentTypeError("must be a JSON array of strings") from None
    if not isinstance(argv, list) or not all(isinstance(item, str) for item in argv):
        raise argparse.ArgumentTypeError("must be a JSON array of strings")
    return argv


def _webhook_url(value):
    try:
        url = urlsplit(value)
        valid = (
            url.scheme in ("http", "https")
            and url.hostname
            and url.port != 0
            and url.username is None
            and url.password is None
            and not url.fragment
            and not any(char.isspace() for char in value)
        )
    except ValueError:
        valid = False
    if not valid:
        raise argparse.ArgumentTypeError("must be an HTTP URL without credentials")
    return value


def _handoff_parsers(sub):
    handoff = sub.add_parser("handoff").add_subparsers(dest="action", required=True)
    s = handoff.add_parser("start", help="hand a chosen T3 thread to another provider/model")
    s.add_argument("--thread", required=True)
    s.add_argument("--to", required=True)
    s.add_argument("--effort", default="")
    s.add_argument("--note", default="")
    s.add_argument("--workspace")
    s.add_argument("--agent", default="")
    s.add_argument("--task", default="")
    s.add_argument("--by")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(func=cmd_handoff_start)
    s = handoff.add_parser("status")
    s.add_argument("--workspace", required=True)
    s.add_argument("--agent")
    s.add_argument("--task")
    s.set_defaults(func=cmd_handoff_status)
    for action in ("enable", "disable", "clear"):
        s = handoff.add_parser(action)
        s.add_argument("--workspace", required=True)
        scope = s.add_mutually_exclusive_group(required=action == "clear")
        scope.add_argument("--agent")
        scope.add_argument("--task")
        s.add_argument("--by")
        if action == "clear":
            s.set_defaults(func=cmd_handoff_clear)
            continue
        s.add_argument("--threshold", type=_threshold)
        s.add_argument("--window", type=_positive_int)
        s.add_argument("--launcher", choices=("client", "command", "webhook"))
        s.add_argument("--launcher-argv", type=_launcher_argv)
        s.add_argument("--launcher-url", type=_webhook_url)
        s.add_argument("--launcher-secret-env")
        s.set_defaults(func=cmd_handoff_set)
    s = handoff.add_parser("list")
    s.add_argument("--workspace", required=True)
    s.add_argument("--task")
    s.add_argument("--limit", type=_positive_int, default=50)
    s.set_defaults(func=cmd_handoff_list)
    s = handoff.add_parser("show")
    s.add_argument("handoff_id")
    s.set_defaults(func=cmd_handoff_show)
    s = handoff.add_parser("relaunch")
    s.add_argument("handoff_id")
    s.add_argument("--by")
    s.set_defaults(func=cmd_handoff_relaunch)

    thread = sub.add_parser("thread").add_subparsers(dest="action", required=True)
    for action in ("get", "recall"):
        s = thread.add_parser(action)
        s.add_argument("thread_id")
        s.add_argument("--workspace", required=True)
        if action == "get":
            s.add_argument("--start", type=_nonnegative_int, default=0)
            s.add_argument("--end", type=_nonnegative_int)
        else:
            s.add_argument("query")
        s.set_defaults(func=cmd_thread)


def cmd_serve(args) -> int:
    from .gateway import Gateway
    from .workers import WorkerSupervisor

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    db = _db(args)
    if not db.has_users():
        raise ControlError("not set up: run `commonplace init` first")
    supervisor = WorkerSupervisor(str(_data_dir(args)), idle_stop_s=args.idle_minutes * 60)
    Gateway(db, supervisor).serve(args.host, args.port)
    return 0


def add_webhook_commands(sub):
    from .webhooks import cli_command

    commands = sub.add_parser("webhook").add_subparsers(dest="action", required=True)
    for name in ("add", "list", "remove"):
        parser = commands.add_parser(name)
        parser.add_argument("--workspace", required=True)
        if name == "add":
            parser.add_argument("url")
            parser.add_argument("--events", type=_csv)
            parser.add_argument("--board", default="")
            parser.add_argument("--waiting-on", default="")
        elif name == "remove":
            parser.add_argument("id")
        parser.set_defaults(func=cli_command)


def cmd_login_link(args) -> int:
    from .bridge import create_login_url

    if not args.send:
        print(create_login_url(_db(args), args.email, args.workspace, args.base_url, args.minutes))
        return 0
    from datetime import datetime, timedelta, timezone

    from . import sessions
    from .mailer import NOT_CONFIGURED, MailNotConfigured, from_env

    # Check mail before minting, so an unconfigured server never creates a link.
    mailer = from_env()
    if not mailer.configured:
        raise MailNotConfigured(NOT_CONFIGURED)
    db = _db(args)
    to = db.user(args.email)["email"]
    minutes = sessions._link_minutes(args.minutes)
    url = create_login_url(db, to, args.workspace, args.base_url, minutes)
    until = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).strftime("%H:%M UTC")
    text = (
        f"Sign in to Commonplace:\n\n{url}\n\n"
        f"This link works once and expires in {minutes} minutes (at {until}).\n"
        "If you did not ask for it, ignore this email.\n"
    )
    try:
        mailer.send(to, "Your Commonplace sign-in link", text)
    except BaseException:
        # The link was never delivered: make sure it can never be used.
        sessions.discard_login_link(db, parse_qs(urlsplit(url).query)["t"][0])
        raise
    print(f"login link sent to {to}")
    return 0


def cmd_connect(args) -> int:
    from .bridge import run_bridge

    return run_bridge(args.url, args.token_env)


def _client(args):
    from .client import Client

    return Client(url=args.url, token_env=args.token_env)


def _client_paths(args):
    home = (args.home or Path.home()).expanduser()
    project = None if args.no_project else (args.project or Path.cwd()).expanduser()
    return home, project


def _print_install_report(report):
    refused = False
    for item in report.get("files", []):
        name = Path(item["path"]).name
        print(f"{name}: {item['status']}" + (f"; {item['reason']}" if item.get("reason") else ""))
        refused |= item["status"] == "refused"
    for warning in report.get("warnings", []):
        print(f"Warning: {warning}", file=sys.stderr)
    for note in report.get("notes", []):
        print(note)
    return refused or bool(report.get("warnings"))


def cmd_onboard(args) -> int:
    from .client_install import install_client_config
    from .onboarding import onboard

    home, project = _client_paths(args)
    # Preflight never calls the client, so a dry run needs no credential.
    client = None if args.dry_run else _client(args)
    migration = onboard(client, home=home, project=project, dry_run=args.dry_run)
    warnings = _print_install_report(migration)
    config = install_client_config(
        home=home,
        project=project,
        url=args.url,
        token_env=args.token_env,
        dry_run=args.dry_run,
    )
    warnings |= _print_install_report(config)
    if args.dry_run:
        print("Dry run: no files or memories changed.")
    else:
        print("Onboarding finished with warnings." if warnings else "Onboarding complete.")
    print("Imported rules await owner approval.")
    return 1 if warnings else 0


def cmd_remember(args) -> int:
    print(_client(args).remember(args.text))
    return 0


def cmd_rule(args) -> int:
    print(_client(args).publish_rule(args.text, agent=args.agent, kind=args.kind))
    print("Rule proposals require owner approval before taking effect.")
    return 0


def cmd_check(args) -> int:
    from .onboarding import check_local_only

    home, project = _client_paths(args)
    findings = check_local_only(home=home, project=project)
    for finding in findings:
        print(
            f"Warning: {Path(finding['path']).name}: local-only instructions or memory; "
            "save with memory_save (commonplace remember) or propose with "
            "rules_publish (commonplace rule).",
            file=sys.stderr,
        )
    if not findings:
        print("No local-only instructions or memory found.")
    return 1 if findings else 0


def _client_parsers(sub):
    for command, handler in (
        ("onboard", cmd_onboard),
        ("remember", cmd_remember),
        ("rule", cmd_rule),
    ):
        parser = sub.add_parser(command)
        parser.add_argument(
            "--url", help="Commonplace URL (default $COMMONPLACE_URL or https://app.cmmn.dev)"
        )
        parser.add_argument("--token-env", default="COMMONPLACE_TOKEN")
        if command == "onboard":
            parser.add_argument("--dry-run", action="store_true")
        else:
            parser.add_argument("text")
        if command == "rule":
            parser.add_argument("--agent", choices=("self", "*"), default="self")
            parser.add_argument(
                "--kind",
                choices=("instruction", "permission", "limit", "style"),
                default="instruction",
            )
        parser.set_defaults(func=handler)
    check = sub.add_parser("check", help="warn about instructions or memory saved only locally")
    check.set_defaults(func=cmd_check)
    for parser in (sub.choices["onboard"], check):
        parser.add_argument("--home", type=Path)
        project = parser.add_mutually_exclusive_group()
        project.add_argument("--project", type=Path)
        project.add_argument("--no-project", action="store_true")


def cmd_seed(args) -> int:
    from .seed import seed_workspace

    db = _db(args)
    result = seed_workspace(db, _data_dir(args), args.workspace, args.file, _actor(db, args))
    print(json.dumps(result))
    return 0


def cmd_workspace_store(args) -> int:
    from .store_switch import switch_workspace

    def authorize(ref):
        db = _db(args)
        try:
            row = db.workspace(ref)
            if row is None:
                raise ControlError("no such workspace")
            _owner(db, args, row["id"])
            return row["id"]
        finally:
            db.close()

    print(
        json.dumps(
            switch_workspace(_data_dir(args), args.workspace, args.store, authorize=authorize),
            sort_keys=True,
        )
    )
    return 0


def cmd_doctor(args) -> int:
    from .store_switch import doctor_workspace

    db = _db(args)
    status = 0
    try:
        rows = [db.workspace(args.workspace)] if args.workspace else db.workspaces()
        for workspace in rows:
            report = doctor_workspace(_data_dir(args), workspace["id"])
            print(json.dumps(report, sort_keys=True))
            if (
                report.get("previous_pointer_difference")
                or report.get("recovery_conflict")
                or report.get("emptied_retained_root")
                or report.get("interrupted_deletions")
                or report.get("retired_findings")
            ):
                status = 3
    finally:
        db.close()
    return status


def cmd_workspace_prune(args) -> int:
    from .store_switch import prune_generations

    def authorize(ref):
        db = _db(args)
        try:
            row = db.workspace(ref)
            if row is None:
                raise ControlError("no such workspace")
            _owner(db, args, row["id"])
            return row["id"]
        finally:
            db.close()

    report = prune_generations(
        _data_dir(args), args.workspace, authorize=authorize, report_only=args.report_only
    )
    if report is not None:
        print(json.dumps(report, sort_keys=True))
        if report.get("findings") or report.get("repair"):
            return 3
    return 0


def cmd_backup(args) -> int:
    from .backup import backup

    backup(_data_dir(args), args.dest_dir)
    print(Path(args.dest_dir).expanduser().resolve() / "manifest.json")
    return 0


def cmd_restore(args) -> int:
    from .backup import restore

    print(json.dumps(restore(args.backup_dir, args.dest_dir), sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="commonplace")
    p.add_argument("--data", help=f"data directory (default ${DATA_ENV} or ~/.commonplace)")
    sub = p.add_subparsers(dest="command", required=True)
    add_webhook_commands(sub)
    _client_parsers(sub)

    s = sub.add_parser("init", help="create the control database and the first admin")
    s.add_argument("--admin-email", required=True)
    s.add_argument("--workspace", default="personal")
    s.set_defaults(func=cmd_init)

    users = sub.add_parser("user").add_subparsers(dest="action", required=True)
    s = users.add_parser("add")
    s.add_argument("email")
    s.add_argument("--name")
    s.add_argument("--no-workspace", action="store_true")
    s.set_defaults(func=cmd_user_add)
    users.add_parser("list").set_defaults(func=cmd_user_list)

    plan = sub.add_parser("plan").add_subparsers(dest="action", required=True)
    s = plan.add_parser("set")
    s.add_argument("user")
    s.add_argument("plan", choices=["free", "paid", "none"])
    s.add_argument("--by")
    s.set_defaults(func=cmd_plan_set)
    s = plan.add_parser("show")
    s.add_argument("user")
    s.set_defaults(func=cmd_plan_show)

    ws = sub.add_parser("workspace").add_subparsers(dest="action", required=True)
    s = ws.add_parser("add")
    s.add_argument("name")
    s.add_argument("--owner", required=True)
    s.set_defaults(func=cmd_workspace_add)
    ws.add_parser("list").set_defaults(func=cmd_workspace_list)
    s = ws.add_parser("store")
    s.add_argument("workspace")
    s.add_argument("store", choices=("v1", "compact"))
    s.add_argument("--by")
    s.set_defaults(func=cmd_workspace_store)
    s = ws.add_parser("prune-stores")
    s.add_argument("workspace")
    s.add_argument("--by")
    s.add_argument("--report-only", action="store_true")
    s.set_defaults(func=cmd_workspace_prune)
    s = sub.add_parser("doctor")
    s.add_argument("--workspace")
    s.set_defaults(func=cmd_doctor)

    member = sub.add_parser("member").add_subparsers(dest="action", required=True)
    s = member.add_parser("add")
    s.add_argument("workspace")
    s.add_argument("email")
    s.add_argument("--role", choices=["owner", "member"], default="member")
    s.add_argument("--by")
    s.set_defaults(func=cmd_member_add)

    tok = sub.add_parser("token").add_subparsers(dest="action", required=True)
    s = tok.add_parser("add", help="create a connection; prints its token once")
    s.add_argument("name")
    s.add_argument("--workspace", required=True)
    s.add_argument("--user", help="token holder (default: --by)")
    s.add_argument("--client", default="other")
    s.add_argument("--parent", help="parent connection name or id")
    s.add_argument("--profile", choices=sorted(PROFILES))
    s.add_argument("--tools", help="explicit tool list instead of --profile")
    s.add_argument("--scopes")
    s.add_argument("--read-wings")
    s.add_argument("--write-wings")
    s.add_argument("--write-wing")
    s.add_argument("--trust", choices=TRUST_LEVELS)
    s.add_argument("--expires-days", type=int, default=365, help="0 for no expiry")
    s.add_argument("--by")
    s.set_defaults(func=cmd_token_add)
    s = tok.add_parser("list")
    s.add_argument("--workspace")
    s.set_defaults(func=cmd_token_list)
    s = tok.add_parser("revoke")
    s.add_argument("name")
    s.add_argument("--workspace")
    s.add_argument("--by")
    s.set_defaults(func=cmd_token_revoke)

    s = sub.add_parser("audit")
    s.add_argument("--workspace")
    s.add_argument("--limit", type=int, default=50)
    s.set_defaults(func=cmd_audit)

    _handoff_parsers(sub)

    s = sub.add_parser("serve")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--idle-minutes", type=float, default=15.0)
    s.set_defaults(func=cmd_serve)

    s = sub.add_parser("login-link")
    s.add_argument("email")
    s.add_argument("--workspace")
    s.add_argument("--base-url")
    s.add_argument("--minutes", type=int, default=15, help="valid for (max 1440); works once")
    s.add_argument("--send", action="store_true", help="email the link instead of printing it")
    s.set_defaults(func=cmd_login_link)

    s = sub.add_parser("connect")
    s.add_argument("--url", required=True)
    s.add_argument("--token-env", default="COMMONPLACE_TOKEN")
    s.set_defaults(func=cmd_connect)

    s = sub.add_parser("seed")
    s.add_argument("file")
    s.add_argument("--workspace", required=True)
    s.add_argument("--by")
    s.set_defaults(func=cmd_seed)

    s = sub.add_parser("backup")
    s.add_argument("dest_dir")
    s.set_defaults(func=cmd_backup)
    s = sub.add_parser("restore", help="verify and restore a backup into a new directory")
    s.add_argument("backup_dir")
    s.add_argument("dest_dir")
    s.set_defaults(func=cmd_restore)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.command in {"onboard", "remember", "rule", "check"}:
        from .client import ClientError

        # Client operations neither open server databases nor take their lock.
        try:
            return args.func(args)
        except (ClientError, OSError, ValueError):
            print(
                f"commonplace: {args.command} failed; check the connection, token environment "
                "variable and local file permissions.",
                file=sys.stderr,
            )
            return 2
    errors = (ControlError, PolicyError, BackupError, ToolError, MailError, OSError, sqlite3.Error)
    if args.command in ("handoff", "thread"):
        from .handoff.db import HandoffDBError
        from .handoff.manual import ManualHandoffError
        from .handoff.recall import ThreadNotFound
        from .handoff.settings import SettingsError
        from .handoff.t3 import T3Error

        errors += (HandoffDBError, SettingsError, ThreadNotFound, ManualHandoffError, T3Error)
    try:
        if args.command in {"connect", "backup", "restore"} or (
            args.command == "workspace" and args.action in {"store", "prune-stores"}
        ):
            return args.func(args)
        with data_lock(_data_dir(args)):
            return args.func(args)
    except errors as exc:
        print(f"commonplace: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        if args.command not in {"login-link", "seed", "backup", "workspace", "doctor"}:
            raise
        print(f"commonplace: {exc}", file=sys.stderr)
        return 2
