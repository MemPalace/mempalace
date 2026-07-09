"""
copilot_provider.py — GitHub Copilot CLI as a MemPalace LLM provider.

Drives the user's installed & authenticated GitHub Copilot CLI through the
official ``github-copilot-sdk`` (JSON-RPC over stdio) so that
``mempalace init <dir> --llm-provider copilot`` performs Pass-0 corpus-origin
detection and Pass-1 entity refinement with Copilot's models.

Design & safety
---------------
- **External BYOK, never silent.** The Copilot CLI relays prompts to GitHub's
  cloud, so this provider is ALWAYS treated as an external service
  (``is_external_service`` returns ``True`` unconditionally). ``cmd_init``
  prints the external-API warning and gates on explicit consent before any
  content leaves the machine. Selecting ``--llm-provider copilot`` is the
  deliberate opt-in.
- **Tool-denied, stateless classification (SEC-001).** Every classification
  runs in an ephemeral session with an empty tool allowlist (``available_tools=[]``,
  ``tools=[]``) plus a deny-all permission handler, in a neutral temporary
  working directory — so a classification turn is text-only and can never read
  the user's project or edit files.
- **Reentrancy-safe async→sync bridge.** MemPalace callers are synchronous; the
  SDK is async-native and binds its client/session objects to the loop they were
  created on. ``_CopilotBridge`` owns one persistent event loop on a daemon
  thread and runs every SDK coroutine there, so the boundary is safe even if the
  caller already holds a running loop.
- **Optional dependency, graceful degradation.** ``github-copilot-sdk`` ships
  only via the ``mempalace[copilot]`` extra (Python 3.11+). This module imports
  cleanly without the SDK; the actionable install error surfaces at
  ``check_available()`` / ``classify()`` time, so a missing SDK, an
  unauthenticated CLI, or a Python < 3.11 interpreter degrades ``init`` to
  heuristics-only rather than crashing.

Runtime prerequisites
----------------------
- Installed & authenticated GitHub Copilot CLI (run ``copilot`` once to sign in).
- The SDK downloads a pinned runtime on first use. Override or disable that with:
  - ``COPILOT_CLI_PATH``      — reuse an already-installed Copilot CLI binary.
  - ``COPILOT_SKIP_CLI_DOWNLOAD=1`` — never auto-download (must supply the binary).
  - ``python -m copilot download-runtime`` — pre-fetch the runtime.

This module is the single place that touches the SDK; keeping all SDK symbols
behind :func:`_ensure_sdk` makes API drift a one-file fix and gives tests one
seam to fake.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import inspect
import sys
import tempfile
import threading
import weakref
from dataclasses import dataclass
from typing import Any, Optional

from .llm_client import LLMError, LLMProvider, LLMResponse

__all__ = ["CopilotProvider"]


# Appended to the system message in JSON mode. Copilot exposes no native
# ``response_format``; like AnthropicProvider we request JSON at the prompt
# level and let the existing robust extractors (llm_refine._parse_response,
# corpus_origin._extract_json) consume it.
_JSON_DIRECTIVE = (
    "\n\nRespond with valid JSON only. No prose, no explanations, no code "
    "fences, and do not call any tools."
)

# Reasoning-effort tokens the SDK/models may accept. The union across all
# advertised Copilot models (data-driven: ``ModelInfo.supported_reasoning_efforts``
# observed live). This is only a coarse sanity filter for the caller's *preference*;
# the authoritative decision is per-model in ``_effort_for_session`` — many models
# (e.g. ``auto``, ``claude-sonnet-4.5``, ``claude-haiku-4.5``) reject the parameter
# entirely (``supported_reasoning_efforts=None``) and MUST have it omitted.
_VALID_REASONING = frozenset({"none", "minimal", "low", "medium", "high", "xhigh", "max"})

_DENY_FEEDBACK = "MemPalace runs Copilot entity-classification tool-free."

# Upper bound (seconds) on how long ``_CopilotBridge.start()`` waits for its
# daemon loop thread to come up before declaring the bridge unusable. Bounds a
# pathological thread-start failure so the provider never hangs indefinitely.
_BRIDGE_START_TIMEOUT = 10.0


@dataclass
class _Sdk:
    """Handles to the exact ``github-copilot-sdk`` symbols the provider uses.

    Centralizing them keeps every SDK touch-point in one place and gives tests a
    single object to fake (patch :func:`_ensure_sdk` to return a fake ``_Sdk``).
    """

    CopilotClient: Any
    RuntimeConnection: Any
    PermissionDecisionReject: Any
    AssistantMessageData: Any
    SessionIdleData: Any
    SessionErrorData: Any


_SDK_CACHE: Optional[_Sdk] = None


def _ensure_sdk() -> _Sdk:
    """Lazily import and cache the SDK symbols. Raises :class:`LLMError`.

    Guards the Python floor first (SDK requires 3.11+) so a 3.9/3.10 interpreter
    gets a precise message instead of an ``ImportError``. Never imported at
    module top-level, preserving the stdlib purity of ``llm_client.py``.
    """
    global _SDK_CACHE
    if _SDK_CACHE is not None:
        return _SDK_CACHE
    if sys.version_info < (3, 11):
        raise LLMError(
            "Copilot provider needs Python 3.11+ (github-copilot-sdk floor); this "
            f"interpreter is {sys.version_info.major}.{sys.version_info.minor}. "
            "Use --llm-provider ollama|openai-compat|anthropic on this interpreter."
        )
    try:
        from copilot import CopilotClient, RuntimeConnection
        from copilot.generated.rpc import PermissionDecisionReject
        from copilot.session_events import (
            AssistantMessageData,
            SessionErrorData,
            SessionIdleData,
        )
    except ImportError as e:
        raise LLMError(
            'copilot provider requires: pip install "mempalace[copilot]" (Python '
            "3.11+). Also ensure the GitHub Copilot CLI is installed and "
            "authenticated (run `copilot` once to sign in)."
        ) from e
    except Exception as e:
        # A present-but-broken install (partial/incompatible SDK) can raise a
        # non-ImportError at import time (RuntimeError, AttributeError, …).
        # Normalize it to LLMError so every caller — classify() (whose consumer
        # refine_entities only catches LLMError) and check_available() — degrades
        # gracefully instead of crashing init. BaseException (KeyboardInterrupt/
        # SystemExit) is intentionally NOT caught.
        raise LLMError(
            f"Copilot SDK failed to load ({type(e).__name__}: {e}). Reinstall with "
            'pip install "mempalace[copilot]".'
        ) from e
    _SDK_CACHE = _Sdk(
        CopilotClient=CopilotClient,
        RuntimeConnection=RuntimeConnection,
        PermissionDecisionReject=PermissionDecisionReject,
        AssistantMessageData=AssistantMessageData,
        SessionIdleData=SessionIdleData,
        SessionErrorData=SessionErrorData,
    )
    return _SDK_CACHE


class _CopilotBridge:
    """Runs SDK coroutines on a dedicated background event loop.

    Owns ONE persistent loop on a daemon thread; every coroutine (``start``,
    ``create_session``, ``send_and_wait``, ``disconnect``, ``stop``) is submitted
    to that same loop via :func:`asyncio.run_coroutine_threadsafe`. This makes
    the async→sync boundary reentrancy-safe (works even if the calling thread
    already runs a loop) and keeps all SDK state on one consistent loop.
    """

    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def start(self) -> None:
        """Start the background loop thread (idempotent)."""
        with self._lock:
            if self._loop is not None:
                return
            if self._closed:
                raise LLMError("Copilot bridge already closed")
            loop = asyncio.new_event_loop()
            ready = threading.Event()

            def _run() -> None:
                asyncio.set_event_loop(loop)
                ready.set()
                loop.run_forever()

            thread = threading.Thread(target=_run, name="mempalace-copilot-loop", daemon=True)
            thread.start()
            if not ready.wait(timeout=_BRIDGE_START_TIMEOUT) or not thread.is_alive():
                try:
                    loop.close()
                except Exception:
                    pass
                raise LLMError(
                    f"Copilot bridge event loop failed to start within {_BRIDGE_START_TIMEOUT:.0f}s"
                )
            self._loop = loop
            self._thread = thread

    def submit(self, coro: Any, timeout: float) -> Any:
        """Run ``coro`` on the loop thread and block up to ``timeout`` seconds.

        The guard-and-schedule runs under ``self._lock`` — the SAME lock ``close``
        uses to flip ``_closed`` and null ``_loop`` — so a concurrent ``close`` can
        never interleave between the readiness check and
        ``run_coroutine_threadsafe``. Without it, ``close`` could stop the loop
        *after* the check but *before* scheduling, leaving the coroutine queued on a
        dead loop: ``run_coroutine_threadsafe`` would not raise (loop stopped, not
        yet closed), the callback would never run, and ``future.result`` would block
        the FULL ``timeout`` while the coroutine leaked un-awaited. Holding the lock
        makes the two orderings the only possibilities — submit wins (schedules onto
        the live loop; FIFO guarantees its task exists before ``_drain_and_stop``
        runs, so cancellation resolves the future fast) or close wins (submit then
        sees ``_closed``/``_loop is None`` and raises immediately).

        The BLOCKING ``future.result`` wait stays OUTSIDE the lock so ``close`` is
        never serialized behind an in-flight call. ``run_coroutine_threadsafe`` only
        enqueues (never blocks on the loop) and the loop thread never takes
        ``self._lock``, so holding it across the schedule cannot deadlock.
        """
        with self._lock:
            loop = self._loop
            if loop is None or self._closed:
                if asyncio.iscoroutine(coro):
                    coro.close()  # avoid "coroutine was never awaited"
                raise LLMError("Copilot bridge not started")
            try:
                future = asyncio.run_coroutine_threadsafe(coro, loop)
            except RuntimeError as e:
                # Loop already stopped/closed — normalize to LLMError.
                if asyncio.iscoroutine(coro):
                    coro.close()
                raise LLMError("Copilot bridge is shutting down") from e
        try:
            return future.result(timeout)
        except concurrent.futures.CancelledError as e:
            # close() cancelled in-flight tasks — surface as LLMError so an
            # in-flight caller returns promptly instead of blocking to `timeout`.
            raise LLMError("Copilot call cancelled (bridge closed)") from e
        except concurrent.futures.TimeoutError as e:
            future.cancel()
            raise LLMError(f"Copilot call exceeded {timeout:.0f}s") from e

    def close(self) -> None:
        """Stop the loop and join the thread (idempotent, exception-safe).

        Pending SDK tasks are cancelled AND briefly awaited on the loop thread so
        any in-flight :meth:`submit` future resolves promptly (``CancelledError``)
        instead of blocking until its call timeout; then the loop is stopped. A
        2s drain bound keeps teardown snappy even if a task refuses to cancel, and
        the daemon-thread join(5s) is the final backstop.
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
            loop, thread = self._loop, self._thread
            self._loop = None
            self._thread = None
        if loop is not None and thread is not None and thread.is_alive():

            async def _drain_and_stop() -> None:
                pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.wait(pending, timeout=2.0)
                loop.stop()

            try:
                asyncio.run_coroutine_threadsafe(_drain_and_stop(), loop)
            except RuntimeError:
                # loop already stopped/closed — best-effort direct stop
                try:
                    loop.call_soon_threadsafe(loop.stop)
                except RuntimeError:
                    pass
        elif loop is not None:
            try:
                loop.call_soon_threadsafe(loop.stop)
            except RuntimeError:
                pass
        if thread is not None:
            thread.join(timeout=5)
        # Close the loop only once its thread has actually exited — closing a
        # still-running loop raises. `_drain_and_stop`'s 2s-bounded wait then
        # `loop.stop()` guarantees `run_forever` returns for any cancellation-
        # honoring task (all our SDK coroutines await I/O), so the thread exits and
        # this runs. A pathological task that ignored cancellation AND blocked the
        # loop thread synchronously would keep it alive; we then abandon the daemon
        # rather than force-close (the atexit finalizer is the last-resort backstop)
        # — never our SDK, and any such in-flight caller is still bounded by its own
        # call timeout.
        if loop is not None and (thread is None or not thread.is_alive()):
            try:
                loop.close()
            except Exception:
                pass


def _make_deny_all(sdk: _Sdk):
    """Permission handler that refuses every tool/permission request (SEC-001).

    Combined with an empty tool allowlist, a classification turn is text-only and
    can never touch the filesystem. Accepts any argument shape the SDK passes so
    it is resilient to handler-signature drift.
    """

    def _deny_all(*_args: Any, **_kwargs: Any):
        return sdk.PermissionDecisionReject(feedback=_DENY_FEEDBACK)

    return _deny_all


def _content_from_event(event: Any) -> str:
    """Extract assistant text from a ``send_and_wait`` result event."""
    data = getattr(event, "data", None) if event is not None else None
    content = getattr(data, "content", None) if data is not None else None
    return content if isinstance(content, str) else ""


def _accepts_kwarg(func: Any, name: str) -> bool:
    """True if ``func`` can be called with keyword ``name`` (or accepts ``**kwargs``).

    Used to choose the turn-execution path *before* sending a prompt, so a
    signature-incompatible ``send_and_wait`` routes to the manual capture path
    without ever dispatching a prompt twice. If the signature cannot be
    introspected (e.g. a C-accelerated callable), assume compatible and prefer
    the primary, live-validated path.
    """
    try:
        params = inspect.signature(func).parameters.values()
    except (TypeError, ValueError):
        return True
    for p in params:
        if p.kind is inspect.Parameter.VAR_KEYWORD:
            return True
        if p.name == name and p.kind in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        ):
            return True
    return False


def _diagnose(exc: Exception) -> str:
    """Map a readiness/transport failure to an actionable one-line message."""
    msg = str(exc)
    low = msg.lower()
    if isinstance(exc, (TimeoutError, concurrent.futures.TimeoutError)) or "exceeded" in low:
        return (
            "Copilot CLI did not respond in time — ensure the runtime is installed "
            "(`python -m copilot download-runtime`) or set COPILOT_CLI_PATH."
        )
    if any(k in low for k in ("auth", "sign in", "signin", "unauthor", "401", "403", "login")):
        return "Copilot CLI not authenticated — run `copilot` and sign in, then retry."
    if any(
        k in low
        for k in (
            "runtime",
            "download",
            "enoent",
            "not found",
            "no such file",
            "cannot find",
            "spawn",
        )
    ):
        return (
            "Copilot CLI runtime unavailable — set COPILOT_CLI_PATH to an installed "
            "binary or run `python -m copilot download-runtime` (unset "
            "COPILOT_SKIP_CLI_DOWNLOAD)."
        )
    return f"Copilot unavailable: {msg}"


def _finalize(bridge: _CopilotBridge, workdir_box: list) -> None:
    """weakref/atexit teardown: stop the loop thread and drop the temp workdir."""
    try:
        bridge.close()
    except Exception:
        pass
    workdir = workdir_box[0] if workdir_box else None
    if workdir:
        import shutil

        shutil.rmtree(workdir, ignore_errors=True)


class CopilotProvider(LLMProvider):
    """LLM provider backed by the installed GitHub Copilot CLI.

    Conforms to the :class:`mempalace.llm_client.LLMProvider` contract so every
    downstream consumer (``llm_refine.refine_entities``,
    ``corpus_origin.detect_origin_llm``, ``project_scanner.discover_entities``)
    works unchanged.
    """

    name = "copilot"

    def __init__(
        self,
        model: str,
        endpoint: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout: int = 180,
        reasoning_effort: str = "low",
        working_directory: Optional[str] = None,
        **_: object,
    ) -> None:
        super().__init__(
            model=model,
            endpoint=endpoint,
            api_key=api_key,
            timeout=timeout,
            # Copilot authenticates via the signed-in Copilot CLI, never an API
            # key. An --llm-api-key passed here is inert and MUST NOT be construed
            # as external-egress consent, so api_key_source stays None and the
            # consent gate always applies (matches the cmd_init comment that
            # Copilot is a keyless external provider). Setting "flag" here would
            # silently bypass the egress prompt — a consent-gate hole (SEC-004).
            api_key_source=None,
        )
        self.reasoning_effort = reasoning_effort if reasoning_effort in _VALID_REASONING else "low"
        self._explicit_workdir = working_directory
        self._workdir: Optional[str] = None
        self._workdir_box: list = [None]
        self._bridge = _CopilotBridge()
        self._client: Any = None
        self._models_by_id: Optional[dict] = None
        self._started = False
        self._closed = False
        self._state_lock = threading.RLock()
        # Best-effort teardown even if the caller forgets close(). weakref keeps the
        # provider normally GC-able and registers its OWN atexit hook, so cleanup
        # runs on GC or at interpreter exit — exactly once (finalize is idempotent).
        self._finalizer = weakref.finalize(self, _finalize, self._bridge, self._workdir_box)

    # ── interface ──────────────────────────────────────────────────────

    @property
    def is_external_service(self) -> bool:
        # The Copilot CLI relays prompts to GitHub's cloud models even when the
        # SDK connects to a local `copilot --server` over localhost, so this is
        # ALWAYS an external send regardless of endpoint (SEC-003).
        return True

    def classify(
        self,
        system: str,
        user: str,
        json_mode: bool = True,
        think: Optional[bool] = None,  # noqa: ARG002 — interface compat; Copilot uses reasoning_effort
    ) -> LLMResponse:
        self._ensure_started()
        sdk = _ensure_sdk()
        try:
            text = self._bridge.submit(
                self._classify_once(sdk, system, user, json_mode),
                timeout=self.timeout + 30,
            )
        except LLMError:
            raise
        except Exception as e:
            raise LLMError(f"Copilot classify failed (model={self.model}): {e}") from e
        text = (text or "").strip()
        if not text:
            raise LLMError(f"Empty response from Copilot (model={self.model})")
        return LLMResponse(text=text, model=self.model, provider=self.name, raw={})

    def check_available(self) -> tuple[bool, str]:
        if sys.version_info < (3, 11):
            return (
                False,
                f"Copilot provider needs Python 3.11+ (this is "
                f"{sys.version_info.major}.{sys.version_info.minor}); use "
                "ollama/openai-compat/anthropic here.",
            )
        try:
            _ensure_sdk()
        except LLMError as e:
            return (False, str(e))
        try:
            self._ensure_started()
            by_id = self._resolve_models()
        except LLMError as e:
            return (False, _diagnose(e))
        except Exception as e:  # pragma: no cover — defensive; bridge normalizes to LLMError
            return (False, _diagnose(e))
        ids = set(by_id)
        if ids and self.model not in ids:
            preview = ", ".join(sorted(ids)[:6])
            return (
                False,
                f"model '{self.model}' is not available to your Copilot account. "
                f"Available: {preview}. Pass --llm-model <id>.",
            )
        return (True, "ok")

    # ── lifecycle ──────────────────────────────────────────────────────

    def _client_kwargs(self, sdk: _Sdk) -> dict:
        kwargs: dict = {"working_directory": self._workdir, "log_level": "error"}
        if self.api_key:
            kwargs["github_token"] = self.api_key
        if self.endpoint:
            # Optional external `copilot --server` mode via --llm-endpoint host:port.
            kwargs["connection"] = sdk.RuntimeConnection.for_uri(self.endpoint)
        return kwargs

    async def _start_client(self, sdk: _Sdk) -> None:
        if self._client is None:
            self._client = sdk.CopilotClient(**self._client_kwargs(sdk))
        try:
            await self._client.start()
        except Exception:
            self._client = None  # allow a clean re-construct on the next attempt
            raise

    def _ensure_started(self) -> None:
        if self._started:
            return
        with self._state_lock:
            if self._started:
                return
            if self._closed:
                raise LLMError("Copilot provider is closed")
            sdk = _ensure_sdk()
            try:
                if self._workdir is None:
                    if self._explicit_workdir:
                        self._workdir = self._explicit_workdir
                    else:
                        self._workdir = tempfile.mkdtemp(prefix="mempalace-copilot-")
                        # only auto-created dirs are cleaned up
                        self._workdir_box[0] = self._workdir
                self._bridge.start()
                self._bridge.submit(self._start_client(sdk), timeout=self.timeout + 30)
            except LLMError:
                raise
            except Exception as e:
                # Normalize raw startup failures (mkdtemp OSError, SDK spawn/auth
                # errors from _start_client) to LLMError so every caller — including
                # classify(), whose consumer refine_entities only catches LLMError —
                # degrades gracefully instead of crashing (contract at class docstring).
                raise LLMError(f"Copilot startup failed: {e}") from e
            self._started = True
            # Best-effort model-capability metadata (drives per-model reasoning_effort).
            # Never fatal: if unavailable, _effort_for_session omits the parameter, which
            # every model accepts (each falls back to its own default effort).
            try:
                self._resolve_models()
            except Exception:
                self._models_by_id = None

    def _resolve_models(self, timeout: float = 45) -> dict:
        """Fetch & cache model metadata (id → ModelInfo). Requires a started client."""
        if self._models_by_id is not None:
            return self._models_by_id
        models = self._bridge.submit(self._client.list_models(), timeout=timeout)
        by_id: dict = {}
        for model in models or []:
            mid = getattr(model, "id", None)
            if isinstance(mid, str):
                by_id[mid] = model
        self._models_by_id = by_id
        return by_id

    def _effort_for_session(self) -> Optional[str]:
        """Reasoning effort to pass for this model, or ``None`` to omit the parameter.

        Data-driven from ``ModelInfo.supported_reasoning_efforts``: models that do
        not support reasoning effort (``auto``, ``claude-sonnet-4.5``,
        ``claude-haiku-4.5``, …) return ``None`` so ``create_session`` omits it —
        passing it to those models fails with JSON-RPC ``-32603``. When the model
        does support effort, honor the caller's preference if valid, else the
        model's own default, else the first supported value. Unknown metadata →
        ``None`` (omit), which every model accepts.
        """
        by_id = self._models_by_id
        info = by_id.get(self.model) if by_id else None
        raw = getattr(info, "supported_reasoning_efforts", None) if info else None
        if raw is None:
            return None
        # Normalize whatever shape the SDK reports (list/tuple/set/frozenset/str)
        # into a deterministic list[str]; anything non-iterable → omit the param.
        if isinstance(raw, str):
            supported = [raw]
        elif isinstance(raw, (set, frozenset)):
            supported = sorted(raw)  # deterministic ordering for the fallback pick
        else:
            try:
                supported = list(raw)
            except TypeError:
                return None
        supported = [s for s in supported if isinstance(s, str)]
        if not supported:
            return None
        if self.reasoning_effort in supported:
            return self.reasoning_effort
        default = getattr(info, "default_reasoning_effort", None)
        if isinstance(default, str) and default in supported:
            return default
        return supported[0]

    def close(self) -> None:
        """Tear down the client, loop thread, and temp workdir (idempotent)."""
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            client, started, bridge_open = (
                self._client,
                self._started,
                not self._bridge.closed,
            )
            self._started = False
        # Stop the SDK client outside the lock so a concurrent _ensure_started
        # blocks on the lock, observes _closed, and fails cleanly rather than hangs.
        try:
            if client is not None and started and bridge_open:
                try:
                    self._bridge.submit(client.stop(), timeout=15)
                except Exception:
                    pass
        finally:
            self._finalizer()  # closes bridge + removes workdir (idempotent)

    # ── turn execution ─────────────────────────────────────────────────

    async def _classify_once(self, sdk: _Sdk, system: str, user: str, json_mode: bool) -> str:
        """One ephemeral, tool-denied session per call (no cross-batch leakage)."""
        sys_content = system + _JSON_DIRECTIVE if json_mode else system
        create_kwargs: dict = {
            "on_permission_request": _make_deny_all(sdk),
            "model": self.model,
            "system_message": {"mode": "replace", "content": sys_content},
            "available_tools": [],
            "tools": [],
            "streaming": False,
        }
        effort = self._effort_for_session()
        if effort is not None:
            # Omitted for models that reject it (auto, claude-sonnet-4.5, …).
            create_kwargs["reasoning_effort"] = effort
        session = await self._client.create_session(**create_kwargs)
        try:
            return await self._run_turn(sdk, session, user, self.timeout)
        finally:
            try:
                await session.disconnect()
            except Exception:
                pass

    async def _run_turn(self, sdk: _Sdk, session: Any, prompt: str, timeout: float) -> str:
        """Send one prompt and return the assistant's final text.

        Primary path is the SDK's own ``send_and_wait`` (a robust event-driven
        capture terminated by ``session.idle``). Whether it is usable is decided
        *before* sending — by capability + signature — so a prompt is dispatched
        exactly once: a present, ``timeout``-compatible ``send_and_wait`` owns the
        turn (its errors surface as ``LLMError`` and never trigger a re-send), and
        only an absent or signature-incompatible ``send_and_wait`` routes to the
        manual single-send capture (REQ-004a).
        """
        send_and_wait = getattr(session, "send_and_wait", None)
        if callable(send_and_wait) and _accepts_kwarg(send_and_wait, "timeout"):
            try:
                event = await send_and_wait(prompt, timeout=float(timeout))
            except (TimeoutError, asyncio.TimeoutError) as e:
                raise LLMError(
                    f"Copilot timed out after {timeout:.0f}s (model={self.model})"
                ) from e
            except LLMError:
                raise
            except Exception as e:
                raise LLMError(f"Copilot session error: {e}") from e
            return _content_from_event(event)
        return await self._run_turn_manual(sdk, session, prompt, timeout)

    async def _run_turn_manual(self, sdk: _Sdk, session: Any, prompt: str, timeout: float) -> str:
        done = asyncio.Event()
        box: dict = {"content": "", "error": None}

        def handler(event: Any) -> None:
            data = getattr(event, "data", None)
            if isinstance(data, sdk.AssistantMessageData):
                content = getattr(data, "content", None)
                if isinstance(content, str):
                    box["content"] = content
            elif isinstance(data, sdk.SessionErrorData):
                box["error"] = getattr(data, "message", None) or "session error"
                done.set()
            elif isinstance(data, sdk.SessionIdleData):
                done.set()

        unsubscribe = session.on(handler)
        try:
            await session.send(prompt)
            await asyncio.wait_for(done.wait(), timeout=float(timeout))
        except (TimeoutError, asyncio.TimeoutError) as e:
            raise LLMError(f"Copilot timed out after {timeout:.0f}s (model={self.model})") from e
        finally:
            try:
                unsubscribe()
            except Exception:
                pass
        if box["error"]:
            raise LLMError(f"Copilot session error: {box['error']}")
        return box["content"]
