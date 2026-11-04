"""Permission-aware projection of project chat status."""

import time

from commonplace import redact
from commonplace.boards.service import BoardService
from commonplace.chat import front, settings
from commonplace.chat.types import LINKED_CARDS_MAX
from commonplace.extensions import ToolContext, ToolError
from commonplace.runner.jobs import open_store, status


def snapshot(gateway, principal, board: str) -> dict:
    service = BoardService(ToolContext(principal=principal, gateway=gateway))
    cards = service.task_list(board=board, all_statuses=True)
    orchestrator = front.orchestrator(gateway, principal, board)
    linked = sorted(
        (card for card in cards if card["message_id"] is not None),
        key=lambda card: card["number"],
        reverse=True,
    )[:LINKED_CARDS_MAX]
    starts = []
    if linked:
        try:
            store = open_store(gateway, principal.workspace_id)
            for card in linked:
                for row in status(store, card["number"], principal):
                    label = row["machine_label"]
                    starts.append(
                        {
                            **row,
                            "task": card["number"],
                            "machine_label": redact.redact(label)[0] if label is not None else None,
                        }
                    )
        except ValueError:
            starts = []
    try:
        groups = service.needs_me()
    except ToolError:
        groups = {}
    needs_you = {
        item["number"]
        for items in groups.values()
        if isinstance(items, list)
        for item in items
        if item["board"] == board
    }
    config = settings.front(gateway.db, principal.workspace_id)
    mode = "off" if not config.enabled else "basic" if config.provider == "none" else "model"
    model = f"{config.provider}/{config.model}" if mode == "model" else None
    runner = None
    if config.enabled and config.provider == "none" and getattr(gateway, "runner_installed", False):
        from commonplace.projects import models
        from commonplace.runner import chat_relay

        choice = models.resolve(gateway.db, principal.workspace_id, board, "main")
        runner = chat_relay.caller_state(
            gateway.db,
            principal.workspace_id,
            principal.user_id,
            time.time(),
            choice.provider if choice else None,
        )
        if runner["online"] and not chat_relay.main_active(gateway, principal, board, time.time()):
            mode = "runner"
            model = f"{choice.provider}/{choice.model}" if choice else None
    return {
        "board": board,
        "orchestrator": orchestrator.public() if orchestrator is not None else None,
        "starts": starts,
        "needs_you": len(needs_you),
        "front": {
            "mode": mode,
            "model": model,
            "runner": runner,
        },
    }
