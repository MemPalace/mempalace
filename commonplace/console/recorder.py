"""Record opted-in agent calls without affecting their responses."""

from time import monotonic

from commonplace import extensions, policy, redact
from commonplace.console import settings, store
from commonplace.rules import checks


def observe(mediator, principal, request, response, started) -> None:
    try:
        if checks.is_internal_call():
            return
        if principal.person:
            return
        if principal.runner == "machine":
            return
        if not settings.recording(mediator.db, principal.connection_id):
            return

        params = request.get("params") if isinstance(request, dict) else None
        name = params.get("name") if isinstance(params, dict) else None
        if not isinstance(name, str) or not (
            extensions.get(name) is not None or name in policy.TOOL_RULES
        ):
            name = "unknown"
        name, _ = redact.redact(name)
        name = name[:64]

        envelope = response if isinstance(response, dict) else {}
        error = envelope.get("error")
        result = envelope.get("result")
        if response is None:
            outcome = "error"
        elif isinstance(error, dict) and error.get("code") == -32601:
            outcome = "denied"
        elif "error" in envelope:
            outcome = "error"
        elif isinstance(result, dict) and result.get("isError"):
            outcome = "error"
        else:
            outcome = "ok"

        ms = max(0, min(10_000_000, int((monotonic() - started) * 1000)))
        store.open_activity(mediator.gateway, principal.workspace_id).append(
            principal.connection_id, name, outcome, ms
        )
    except Exception:
        # Observation must never interrupt a call or expose its data in logs.
        return
