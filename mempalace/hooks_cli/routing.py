# Loaded into mempalace.hooks_cli via exec (see __init__.py). Not a standalone module.
if __name__ != "mempalace.hooks_cli":
    raise ImportError(f"{__name__} is an implementation fragment; import mempalace.hooks_cli")


def _hooks_daemon_enabled() -> bool:
    """Legacy compatibility helper for the pre-policy hook setting.

    New hook write paths use ``resolve_write_routing("hooks")``. This
    helper remains for callers/tests that still inspect ``hooks.daemon``.
    """
    try:
        return MempalaceConfig().hook_use_daemon is True
    except Exception:
        return False


def _daemon_mine_dedupe_key(source: str, mode: str) -> str:
    try:
        source_key = str(Path(source).expanduser().resolve())
    except OSError:
        source_key = str(Path(source).expanduser())
    return f"hook:mine:{mode}:{source_key}"


def _daemon_available() -> bool:
    """True iff a daemon is already running for the configured palace.

    This is a fast localhost health check, not a spawn: the hook time budget
    forbids cold-starting a long-lived daemon. ``prefer`` may fall back to the
    direct path when this returns false; ``require`` must block the write.
    """
    from ..daemon import HOOK_PROBE_TIMEOUT, get_client_if_running

    try:
        return (
            get_client_if_running(MempalaceConfig().palace_path, health_timeout=HOOK_PROBE_TIMEOUT)
            is not None
        )
    except Exception:
        return False


@dataclass(frozen=True)
class HookWriteRouting:
    """One hook invocation's resolved routing state."""

    decision: Optional[WriteRoutingDecision]
    source: str
    error: Optional[str] = None

    @property
    def use_daemon(self) -> bool:
        return self.decision is not None and self.decision.use_daemon

    @property
    def blocked(self) -> bool:
        return self.error is not None or (self.decision is not None and self.decision.blocked)

    @property
    def notice(self) -> str:
        if self.error is not None:
            return (
                "MemPalace hook writes were skipped because write-routing "
                f"configuration is invalid: {self.error}. No direct ChromaDB "
                "fallback was attempted."
            )
        if self.blocked:
            return (
                "MemPalace hook writes were skipped because routing is set to "
                "'require' but the local daemon is unavailable. Start it with "
                "`mempalace daemon start`; no direct ChromaDB fallback was attempted."
            )
        return ""


_HOOK_WRITE_ROUTING_CONTEXT = ContextVar(
    "mempalace_hook_write_routing",
    default=None,
)


def _resolve_configured_hook_policy() -> ResolvedWriteRoutingPolicy:
    """Resolve the new policy, with legacy-object compatibility."""

    config = MempalaceConfig()
    resolver = getattr(config, "resolve_write_routing", None)
    if callable(resolver):
        resolved = resolver("hooks")
        if isinstance(resolved, ResolvedWriteRoutingPolicy):
            return resolved

    # Compatibility for older/custom config objects and existing tests that
    # expose only the pre-policy ``hook_use_daemon`` property.
    policy = (
        WriteRoutingPolicy.PREFER
        if getattr(config, "hook_use_daemon", False) is True
        else WriteRoutingPolicy.DIRECT
    )
    return ResolvedWriteRoutingPolicy(
        policy=policy,
        source="legacy hook_use_daemon",
    )


def _compute_hook_write_routing() -> HookWriteRouting:
    """Resolve hook policy and probe daemon liveness at most once."""

    try:
        resolved = _resolve_configured_hook_policy()
    except WriteRoutingError as exc:
        routing = HookWriteRouting(
            decision=None,
            source="configuration-error",
            error=str(exc),
        )
        _log(routing.notice)
        return routing
    except Exception as exc:
        # Preserve the historical save-on-config-read-failure behavior. An
        # explicitly invalid routing value raises WriteRoutingError above and
        # fails closed; an unrelated config I/O/runtime failure falls back to
        # direct so a final checkpoint is not silently lost.
        _log(f"WARNING: could not resolve hook write routing: {exc}; defaulting to direct")
        resolved = ResolvedWriteRoutingPolicy(
            policy=WriteRoutingPolicy.DIRECT,
            source="config-unavailable fallback",
        )

    daemon_available = False
    if resolved.policy is not WriteRoutingPolicy.DIRECT:
        daemon_available = _daemon_available()

    decision = choose_write_route(
        resolved.policy,
        daemon_available=daemon_available,
        daemon_can_start=False,
    )
    routing = HookWriteRouting(
        decision=decision,
        source=resolved.source,
    )

    if decision.policy is not WriteRoutingPolicy.DIRECT:
        _log(
            "Hook write routing: "
            f"policy={decision.policy.value} source={resolved.source} "
            f"target={decision.target.value} reason={decision.reason}"
        )

    return routing


def _current_hook_write_routing() -> HookWriteRouting:
    routing = _HOOK_WRITE_ROUTING_CONTEXT.get()
    if routing is not None:
        return routing
    return _compute_hook_write_routing()


@contextmanager
def _hook_write_routing_context():
    """Share one policy resolution and one daemon probe across a hook fire."""

    routing = _compute_hook_write_routing()
    token = _HOOK_WRITE_ROUTING_CONTEXT.set(routing)
    try:
        yield routing
    finally:
        _HOOK_WRITE_ROUTING_CONTEXT.reset(token)


def _log_hook_write_blocked(routing: HookWriteRouting, operation: str) -> None:
    _log(f"{routing.notice} Operation skipped: {operation}.")


def _blocked_hook_output(routing: HookWriteRouting) -> dict:
    return {"systemMessage": routing.notice}


def _submit_daemon_job(
    kind: str,
    payload: dict,
    *,
    dedupe_key: str = None,
    priority: int = 0,
    wait: bool = False,
    timeout: float = 60.0,
):
    """Submit to an already-running daemon. Never auto-starts (see _daemon_available).

    Raises DaemonError on a real failure (job rejected, timeout, daemon died
    mid-submit). Callers must NOT fall back to the direct path on such errors —
    the daemon may already have accepted the job, and re-running it would
    duplicate verbatim content. Only an absent daemon (handled by the caller's
    _daemon_available() precheck) should fall back.
    """
    from ..daemon import submit_job

    palace_path = MempalaceConfig().palace_path
    return submit_job(
        kind,
        payload,
        palace_path=palace_path,
        dedupe_key=dedupe_key,
        priority=priority,
        wait=wait,
        auto_start=False,
        timeout=timeout,
        # A job refused the palace lock is deferred, not failed (#2014), so it
        # is never terminal while the holder lives. The waiting callers below
        # wait on purpose, but a parked job cannot reach the state they wait
        # for: they would burn the whole timeout and then report a failure that
        # did not happen. Take the parked job back instead; the daemon still
        # runs it once the lock frees.
        stop_on_lock_deferral=True,
    )


def _job_deferred_by_lock(job: dict) -> bool:
    """True when the daemon parked this job behind the palace write lock.

    Imported lazily like ``submit_job`` above: only callers that actually reach
    the daemon pay for the module, and by this point it is already loaded.
    """
    from ..daemon import job_deferred_by_lock

    return job_deferred_by_lock(job)


def _lock_deferral_reason(job: dict) -> str:
    """Operator-facing reason a job is parked, for the hook log."""
    reason = (job.get("error") or {}).get("message") or "the palace write lock is held"
    return f"{reason} (job {job.get('id')} stays queued and runs when the holder exits)"
