"""Manual T3 handoffs: save, claim once, confirm start, register, then settle."""

from __future__ import annotations

import hashlib
import json
import os
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

from ..admission import GATE
from .authority import require_launch_authority
from .hook import _atomic_text, _private_dir
from .launchers import _write_prompt
from .models import HandoffRequest, SuccessorRef, ThreadMeta
from .t3 import T3, T3Error, Target, UnsupportedLaunch


class ManualHandoffError(ValueError):
    pass


@contextmanager
def _start_lock(directory, workspace_id, thread_id):
    """Serialize local manual callers without holding a database transaction."""
    key = hashlib.sha256(f"{workspace_id}\0{thread_id}".encode()).hexdigest()
    with GATE.creating():
        _private_dir(directory)
        descriptor = os.open(
            directory / f"{key}.lock", os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600
        )
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def client_config():
    path = Path.home() / ".commonplace" / "handoff-client.json"
    try:
        config = json.loads(path.read_text()) if path.exists() else {}
        if not isinstance(config, dict) or not isinstance(config.get("workspaces", {}), dict):
            raise ValueError
        return config
    except (OSError, ValueError):
        raise ManualHandoffError("invalid ~/.commonplace/handoff-client.json") from None


def workspace_for(cwd):
    """Most specific configured checkout wins; no credentials in this map."""
    directory = Path(cwd).resolve()
    matches = []
    for root, workspace in client_config().get("workspaces", {}).items():
        path = Path(root).expanduser().resolve()
        if directory == path or path in directory.parents:
            matches.append((len(path.parts), workspace))
    return max(matches, default=(0, ""))[1]


def _workspace_hint(db):
    choices = ", ".join(f"{row['name']!r} ({row['id']})" for row in db.workspaces())
    if not choices:
        return "no local workspaces; run `commonplace init` or `commonplace workspace add`"
    return (
        f"choose --workspace <ID> from: {choices}; "
        "run `commonplace workspace list` to list names and IDs"
    )


class ManualHandoff:
    def __init__(self, service, *, t3=None, prompt_dir=None):
        self.service = service
        self.t3 = t3 or T3()
        self.prompt_dir = Path(prompt_dir or service.prompt_dir)

    def start(
        self,
        principal,
        *,
        thread_id,
        to,
        effort="",
        note="",
        agent="",
        task="",
        dry_run=False,
        source=None,
        require_binding=False,
    ):
        require_launch_authority(principal)
        target = Target.parse(to, effort)
        if os.environ.get("COMMONPLACE_HANDOFF", "").lower() in ("off", "0", "false"):
            raise ManualHandoffError("handoff is off for this run (COMMONPLACE_HANDOFF)")
        agent = agent or os.environ.get("COMMONPLACE_AGENT", "") or principal.connection
        task = task or os.environ.get("COMMONPLACE_TASK", "")
        setting = self.service.settings.effective(principal.workspace_id, agent, task)
        if not setting.enabled:
            raise ManualHandoffError(
                f"handoff is off ({setting.source}); enable it for this workspace, agent or task first"
            )
        existing = self.service.db.by_key(principal.workspace_id, f"thread:{thread_id}")
        if existing is not None:
            if not self.service._can_reuse(principal, existing):
                raise ManualHandoffError("in progress")
            if existing.successor is not None and not dry_run:
                # A successful handoff remains retryable after the old native
                # session or checkout has gone away.
                return self._result(existing, thread_id)
        source = source or self.t3.resolve(thread_id)
        if require_binding:
            binding = workspace_for(source.cwd) or workspace_for(source.workspace_root)
            workspace = self.service.control_db.workspace(principal.workspace_id)
            if binding not in (principal.workspace_id, workspace["name"]):
                raise ManualHandoffError(
                    "source checkout is not bound to this Commonplace workspace"
                )
        if dry_run:
            return {
                "dry_run": True,
                "source_thread_id": source.thread_id,
                "cwd": source.cwd,
                "source_provider": source.provider,
                "transcript_format": source.transcript_format,
                "transcript_paths": [str(path) for path in source.transcript_paths],
                "target": asdict(target),
                "agent": agent,
                "task_ref": task,
                "settings_source": setting.source,
            }
        self.t3.validate_launch(source)
        request = HandoffRequest(
            meta=ThreadMeta(
                thread_id=source.thread_id,
                agent=agent,
                client=source.transcript_format,
                model=source.model,
                task_ref=task,
                branch=source.branch,
                pr_url=source.pr_url,
                cwd=source.cwd,
            ),
            transcript=source.transcript(),
            transcript_format=source.transcript_format,
            note=note,
            idempotency_key=f"thread:{source.thread_id}",
        )
        with _start_lock(self.prompt_dir, principal.workspace_id, source.thread_id):
            return self._run(principal, source, target, request)

    def _run(self, principal, source, target, request):
        result = self.service.run(principal, request, manual=True)
        handoff = result.handoff
        if result.action != "handoff" or handoff is None:
            raise ManualHandoffError(
                result.reason or "thread storage failed; source is still running"
            )
        journal = self.prompt_dir / f"{handoff.id}.t3.json"
        if handoff.successor is None and result.launch:
            try:
                _write_prompt(self.prompt_dir, handoff.id, result.prompt)
                successor = self.t3.start(source, result.prompt, target)
            except (OSError, ValueError) as error:
                self.service.report_launch_failure(principal, handoff.id, str(error))
                if isinstance(error, UnsupportedLaunch):
                    raise ManualHandoffError(str(error)) from None
                raise ManualHandoffError(
                    "successor launch failed; source is still running; inspect T3 before relaunch"
                ) from None
            # A confirmed launch must never be classified as a failed launch,
            # even if journalling/registration fails: its claim remains held.
            try:
                with GATE.creating():
                    _atomic_text(journal, json.dumps(asdict(successor)))
                handoff = self.service.register_successor(principal, handoff.id, successor)
            except Exception:
                raise ManualHandoffError(
                    "successor started but registration failed; retry this handoff, do not relaunch"
                ) from None
        elif handoff.successor is None and handoff.state == "launched" and journal.exists():
            successor = SuccessorRef(**json.loads(journal.read_text()))
            handoff = self.service.register_successor(principal, handoff.id, successor)
        if handoff.state == "launch_failed":
            raise ManualHandoffError(
                "successor launch failed; source is still running; inspect T3 before relaunch"
            )
        return self._result(handoff, source.thread_id)

    def _result(self, handoff, source_thread_id):
        self.service.events.deliver_pending(workspace_id=handoff.workspace_id)
        settled = False
        if handoff.successor is not None:
            try:
                self.t3.settle(source_thread_id)
                settled = True
            except T3Error:
                pass  # Already started and registered: retries only retry settle.
        return {
            "handoff_id": handoff.id,
            "stored_thread_id": handoff.stored_thread_id,
            "state": handoff.state,
            "successor": asdict(handoff.successor) if handoff.successor else None,
            "source_settled": settled,
        }


def run_cli(args):
    from contextlib import ExitStack

    from commonplace import cli
    from commonplace.control_db import ControlError, Principal
    from commonplace.policy import Policy

    from .db import HandoffDB
    from .events import Events
    from .service import HandoffService

    target = Target.parse(args.to, args.effort)
    t3 = T3()
    source = None
    workspace_ref = args.workspace or os.environ.get("COMMONPLACE_WORKSPACE")
    if not workspace_ref:
        source = t3.resolve(args.thread)
        workspace_ref = workspace_for(source.cwd) or workspace_for(source.workspace_root)
    db = cli._db(args)
    if not workspace_ref:
        raise ManualHandoffError(
            f"{_workspace_hint(db)}; or map the source checkout in "
            "~/.commonplace/handoff-client.json (see docs/handoff.md)"
        )
    try:
        workspace = db.workspace(workspace_ref)["id"]
    except ControlError as error:
        raise ManualHandoffError(f"{error}; {_workspace_hint(db)}") from None
    user = cli._owner(db, args, workspace)
    principal = Principal(
        user_id=user["id"],
        user_email=user["email"],
        workspace_id=workspace,
        connection_id="",
        connection="cli",
        client="commonplace",
        auth="local",
        role="owner",
        policy=Policy.build(
            profile="full",
            scopes=["threads:read", "handoff:write"],
            read_wings=["*"],
            trust="trusted",
        ),
    )
    handoff_db = HandoffDB(db)
    with ExitStack() as stack:
        service = HandoffService(
            control_db=db,
            handoff_db=handoff_db,
            settings=cli._settings(db, handoff_db),
            store_for=cli._stores(args, stack),
            events=Events(db, handoff_db),
            prompt_dir=str(cli._data_dir(args) / "handoff-prompts"),
        )
        result = ManualHandoff(service, t3=t3).start(
            principal,
            thread_id=args.thread,
            to=f"{target.provider}/{target.model}",
            effort=target.effort,
            note=args.note,
            agent=args.agent,
            task=args.task,
            dry_run=args.dry_run,
            source=source,
        )
    print(json.dumps(result))
    return 0 if args.dry_run or result["source_settled"] else 2
