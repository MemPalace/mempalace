"""Unit tests for mempalace.copilot_provider.

Every test drives the provider through a fake ``github-copilot-sdk`` injected at
the single seam (:func:`mempalace.copilot_provider._ensure_sdk`), so the suite
runs without the real SDK, without the Copilot CLI, and without network. The
fakes mirror the exact SDK surface the provider touches (async
``CopilotClient`` with ``start``/``stop``/``list_models``/``create_session``, a
session exposing ``send_and_wait``/``on``/``send``/``disconnect``, the
``PermissionDecisionReject`` decision, and the three session-event data
classes), so the behaviour asserted here matches the live contract validated
end-to-end in the integration/live suites.

The provider runs SDK coroutines on a real background event loop, so the fakes
use real ``async def`` methods; each provider is closed via the ``providers``
fixture to reclaim the loop thread and temp workdir.
"""

from __future__ import annotations

import collections
import sys
import types

import pytest

import mempalace.copilot_provider as cp
from mempalace.llm_client import LLMError, LLMResponse

# ── Fakes mirroring the SDK surface the provider uses ───────────────────────

# sys.version_info stand-in: a namedtuple compares lexicographically against a
# short tuple like (3, 11) AND exposes .major/.minor like the real object.
_VerInfo = collections.namedtuple("_VerInfo", "major minor micro releaselevel serial")


class FakeModelInfo:
    def __init__(self, id, supported=None, default=None):
        self.id = id
        self.supported_reasoning_efforts = supported
        self.default_reasoning_effort = default


class FakeAssistantMessageData:
    def __init__(self, content):
        self.content = content


class FakeSessionIdleData:
    pass


class FakeSessionErrorData:
    def __init__(self, message):
        self.message = message


class FakeEvent:
    def __init__(self, data):
        self.data = data


class FakePermissionDecisionReject:
    def __init__(self, feedback=None):
        self.feedback = feedback


class FakeRuntimeConnection:
    @staticmethod
    def for_uri(uri):
        return ("uri", uri)


class _ManualMixin:
    """Shared ``on``/``send``/``disconnect`` for the manual event-capture path."""

    def _init_common(self, sdk, content, error):
        self._sdk = sdk
        self._content = content
        self._error = error
        self._handler = None
        self.sent = []
        self.disconnected = False

    def on(self, handler):
        self._handler = handler

        def _unsub():
            self._handler = None

        return _unsub

    async def send(self, prompt):
        self.sent.append(prompt)
        handler = self._handler
        if handler is None:
            return
        if self._error is not None:
            handler(FakeEvent(self._sdk.SessionErrorData(self._error)))
            return
        handler(FakeEvent(self._sdk.AssistantMessageData(self._content)))
        handler(FakeEvent(self._sdk.SessionIdleData()))

    async def disconnect(self):
        self.disconnected = True


class FakeSession(_ManualMixin):
    """Session that exposes ``send_and_wait`` (the provider's primary path)."""

    def __init__(self, sdk, *, mode="ok", content='{"ok": true}', error=None, saw_exc=None):
        self._init_common(sdk, content, error)
        self._mode = mode
        self._saw_exc = saw_exc

    async def send_and_wait(self, prompt, timeout=60.0):
        self.sent.append(prompt)
        if self._saw_exc is not None:
            raise self._saw_exc
        if self._mode == "timeout":
            raise TimeoutError("send_and_wait timed out")
        if self._mode == "error":
            raise Exception("Session error: boom")
        if self._mode == "empty":
            return FakeEvent(FakeAssistantMessageData(""))
        return FakeEvent(FakeAssistantMessageData(self._content))


class FakeManualSession(_ManualMixin):
    """Session WITHOUT ``send_and_wait`` — forces the manual fallback path."""

    def __init__(self, sdk, *, content='{"ok": true}', error=None):
        self._init_common(sdk, content, error)


class FakeSessionNoTimeoutKwarg(_ManualMixin):
    """``send_and_wait`` present but its signature can't take ``timeout``.

    The provider inspects the signature *before* sending, so it must route to
    the manual single-send path and never invoke this incompatible method.
    """

    def __init__(self, sdk, *, content='{"ok": true}', error=None):
        self._init_common(sdk, content, error)

    async def send_and_wait(self, prompt):  # no ``timeout`` kwarg, no ``**kwargs``
        raise AssertionError("signature-incompatible send_and_wait must not be called")


def build_sdk(
    *,
    models=None,
    session_factory=None,
    start_error=None,
    stop_error=None,
    list_models_error=None,
):
    """Return ``(sdk, ClientClass)`` wired to the requested behaviour.

    ``ClientClass.instances`` records every constructed client so a test can
    inspect constructor kwargs and recorded ``create_session`` calls.
    """
    resolved_models = models if models is not None else [FakeModelInfo("auto")]
    sdk_box = {}

    def default_factory(create_kwargs, sdk):
        return FakeSession(sdk)

    make_session = session_factory or default_factory

    class FakeClient:
        instances: list = []

        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.started = False
            self.stopped = False
            self.create_calls: list = []
            self.sessions: list = []
            FakeClient.instances.append(self)

        async def start(self):
            if start_error is not None:
                raise start_error
            self.started = True

        async def stop(self):
            if stop_error is not None:
                raise stop_error
            self.stopped = True

        async def list_models(self):
            if list_models_error is not None:
                raise list_models_error
            return resolved_models

        async def create_session(self, **kwargs):
            self.create_calls.append(kwargs)
            session = make_session(kwargs, sdk_box["sdk"])
            self.sessions.append(session)
            return session

    FakeClient.instances = []
    sdk = cp._Sdk(
        CopilotClient=FakeClient,
        RuntimeConnection=FakeRuntimeConnection,
        PermissionDecisionReject=FakePermissionDecisionReject,
        AssistantMessageData=FakeAssistantMessageData,
        SessionIdleData=FakeSessionIdleData,
        SessionErrorData=FakeSessionErrorData,
    )
    sdk_box["sdk"] = sdk
    return sdk, FakeClient


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def providers():
    """Yield a factory that tracks providers and closes them after the test."""
    created = []

    def _make(**kwargs):
        kwargs.setdefault("model", "auto")
        kwargs.setdefault("timeout", 30)
        provider = cp.CopilotProvider(**kwargs)
        created.append(provider)
        return provider

    yield _make
    for provider in created:
        try:
            provider.close()
        except Exception:
            pass


@pytest.fixture
def patched_sdk(monkeypatch):
    """Install a fake ``_Sdk`` at the ``_ensure_sdk`` seam; return the client class."""

    def _install(**build_kwargs):
        sdk, client_cls = build_sdk(**build_kwargs)
        monkeypatch.setattr(cp, "_ensure_sdk", lambda: sdk)
        return sdk, client_cls

    return _install


# ── classify: happy path & prompt construction ──────────────────────────────


def test_classify_returns_llmresponse(providers, patched_sdk):
    patched_sdk()
    provider = providers(model="auto")
    resp = provider.classify("system", "user", json_mode=True)
    assert isinstance(resp, LLMResponse)
    assert resp.text == '{"ok": true}'
    assert resp.provider == "copilot"
    assert resp.model == "auto"


def test_classify_injects_json_directive_in_json_mode(providers, patched_sdk):
    _, client_cls = patched_sdk()
    provider = providers(model="auto")
    provider.classify("SYS", "user", json_mode=True)
    content = client_cls.instances[0].create_calls[0]["system_message"]["content"]
    assert content.startswith("SYS")
    assert cp._JSON_DIRECTIVE in content
    assert client_cls.instances[0].create_calls[0]["system_message"]["mode"] == "replace"


def test_classify_no_directive_when_json_mode_false(providers, patched_sdk):
    _, client_cls = patched_sdk()
    provider = providers(model="auto")
    provider.classify("SYS", "user", json_mode=False)
    content = client_cls.instances[0].create_calls[0]["system_message"]["content"]
    assert content == "SYS"
    assert cp._JSON_DIRECTIVE not in content


def test_classify_session_is_tool_denied(providers, patched_sdk):
    """available_tools=[] + tools=[] + a deny-all permission handler (SEC-001)."""
    _, client_cls = patched_sdk()
    provider = providers(model="auto")
    provider.classify("system", "user")
    call = client_cls.instances[0].create_calls[0]
    assert call["available_tools"] == []
    assert call["tools"] == []
    assert call["streaming"] is False
    decision = call["on_permission_request"]("req", "invocation")
    assert isinstance(decision, FakePermissionDecisionReject)
    assert decision.feedback == cp._DENY_FEEDBACK


def test_classify_each_call_is_an_ephemeral_session(providers, patched_sdk):
    """Each classify opens a fresh session and disconnects it (no leakage)."""
    _, client_cls = patched_sdk()
    provider = providers(model="auto")
    provider.classify("system", "first")
    provider.classify("system", "second")
    client = client_cls.instances[0]
    assert len(client.create_calls) == 2
    assert len(client.sessions) == 2
    assert all(s.disconnected for s in client.sessions)


# ── capability-aware reasoning_effort (data-driven from live model matrix) ───


def test_reasoning_effort_omitted_for_auto(providers, patched_sdk):
    _, client_cls = patched_sdk(models=[FakeModelInfo("auto", supported=None)])
    provider = providers(model="auto", reasoning_effort="low")
    provider.classify("system", "user")
    assert "reasoning_effort" not in client_cls.instances[0].create_calls[0]


def test_reasoning_effort_passed_when_supported(providers, patched_sdk):
    _, client_cls = patched_sdk(
        models=[
            FakeModelInfo("gpt-5.5", supported=["none", "low", "medium", "high"], default="medium")
        ]
    )
    provider = providers(model="gpt-5.5", reasoning_effort="low")
    provider.classify("system", "user")
    assert client_cls.instances[0].create_calls[0]["reasoning_effort"] == "low"


def test_reasoning_effort_falls_back_to_model_default(providers, patched_sdk):
    """Caller preference not supported → use the model's own default effort."""
    _, client_cls = patched_sdk(
        models=[FakeModelInfo("m", supported=["medium", "high"], default="medium")]
    )
    provider = providers(model="m", reasoning_effort="low")
    provider.classify("system", "user")
    assert client_cls.instances[0].create_calls[0]["reasoning_effort"] == "medium"


def test_reasoning_effort_falls_back_to_first_supported(providers, patched_sdk):
    """Preference unsupported and no usable default → first supported value."""
    _, client_cls = patched_sdk(
        models=[FakeModelInfo("m", supported=["high", "xhigh"], default=None)]
    )
    provider = providers(model="m", reasoning_effort="low")
    provider.classify("system", "user")
    assert client_cls.instances[0].create_calls[0]["reasoning_effort"] == "high"


def test_effort_for_session_omits_when_metadata_unknown(providers, patched_sdk):
    patched_sdk()
    provider = providers(model="mystery-model")
    provider._ensure_started()
    # Model absent from list_models metadata → omit (universally safe).
    assert provider._effort_for_session() is None


@pytest.mark.parametrize(
    "supported, expected",
    [
        (["high", "xhigh"], "high"),  # list → first
        (("high", "xhigh"), "high"),  # tuple → first
        ({"xhigh", "high"}, "high"),  # set → sorted → deterministic 'high'
        (frozenset({"xhigh", "high"}), "high"),  # frozenset → sorted → 'high'
        ("high", "high"),  # bare str → [str] → 'high'
        ([], None),  # empty → omit
        ([123, None], None),  # no str entries → omit
    ],
)
def test_effort_for_session_normalizes_container_shapes(providers, supported, expected):
    """``supported_reasoning_efforts`` may be any container shape; the fallback
    pick must be robust (no crash) and deterministic (sets sorted)."""
    provider = providers(model="m", reasoning_effort="low")  # 'low' unsupported below
    provider._models_by_id = {"m": FakeModelInfo("m", supported=supported, default=None)}
    assert provider._effort_for_session() == expected


def test_effort_for_session_omits_on_non_iterable_metadata(providers):
    provider = providers(model="m")
    provider._models_by_id = {"m": FakeModelInfo("m", supported=42, default=None)}
    assert provider._effort_for_session() is None


def test_effort_for_session_honors_supported_preference_in_set(providers):
    """A valid caller preference is honored even when metadata is an unordered set."""
    provider = providers(model="m", reasoning_effort="high")
    provider._models_by_id = {"m": FakeModelInfo("m", supported={"low", "high"}, default=None)}
    assert provider._effort_for_session() == "high"


def test_bad_reasoning_effort_normalizes_to_low(providers):
    provider = providers(model="auto", reasoning_effort="bogus")
    assert provider.reasoning_effort == "low"


# ── classify: failure mapping ───────────────────────────────────────────────


def test_classify_empty_response_raises(providers, patched_sdk):
    patched_sdk(session_factory=lambda kw, sdk: FakeSession(sdk, mode="empty"))
    provider = providers(model="auto")
    with pytest.raises(LLMError, match="Empty response"):
        provider.classify("system", "user")


def test_classify_session_error_raises(providers, patched_sdk):
    patched_sdk(session_factory=lambda kw, sdk: FakeSession(sdk, mode="error"))
    provider = providers(model="auto")
    with pytest.raises(LLMError, match="Copilot session error"):
        provider.classify("system", "user")


def test_classify_timeout_raises(providers, patched_sdk):
    patched_sdk(session_factory=lambda kw, sdk: FakeSession(sdk, mode="timeout"))
    provider = providers(model="auto")
    with pytest.raises(LLMError, match="timed out"):
        provider.classify("system", "user")


# ── manual fallback path (REQ-004a: capability drift resilience) ─────────────


def test_manual_fallback_when_send_and_wait_absent(providers, patched_sdk):
    patched_sdk(session_factory=lambda kw, sdk: FakeManualSession(sdk, content='{"via": "manual"}'))
    provider = providers(model="auto")
    resp = provider.classify("system", "user")
    assert resp.text == '{"via": "manual"}'


def test_manual_fallback_when_send_and_wait_signature_incompatible(providers, patched_sdk):
    """A ``send_and_wait`` that can't accept ``timeout`` degrades to manual capture.

    The path is chosen by signature inspection *before* sending, so the
    incompatible method is never called (it would raise) and the prompt is
    dispatched exactly once via the manual API.
    """
    sessions: list = []

    def factory(kw, sdk):
        s = FakeSessionNoTimeoutKwarg(sdk, content='{"via": "manual2"}')
        sessions.append(s)
        return s

    patched_sdk(session_factory=factory)
    provider = providers(model="auto")
    resp = provider.classify("system", "user")
    assert resp.text == '{"via": "manual2"}'
    # Exactly one dispatch, via the manual ``send`` — no double send.
    assert sessions[0].sent == ["user"]


@pytest.mark.parametrize("exc", [RuntimeError("mid-turn boom"), TypeError("mid-turn boom")])
def test_send_and_wait_error_surfaces_without_double_send(providers, patched_sdk, exc):
    """A runtime error from a compatible ``send_and_wait`` raises LLMError and
    must NOT silently re-send the prompt through the manual path (no double egress)."""
    sessions: list = []

    def factory(kw, sdk):
        s = FakeSession(sdk, saw_exc=exc)
        sessions.append(s)
        return s

    patched_sdk(session_factory=factory)
    provider = providers(model="auto")
    with pytest.raises(LLMError, match="boom"):
        provider.classify("system", "user")
    # send_and_wait recorded exactly one send; the manual path never ran again.
    assert sessions[0].sent == ["user"]


def test_manual_fallback_surfaces_session_error(providers, patched_sdk):
    patched_sdk(session_factory=lambda kw, sdk: FakeManualSession(sdk, error="kaboom"))
    provider = providers(model="auto")
    with pytest.raises(LLMError, match="kaboom"):
        provider.classify("system", "user")


def test_classify_wraps_raw_startup_failure_as_llmerror(providers, patched_sdk):
    """A raw (non-LLMError) startup failure must surface as LLMError so the
    documented consumer (refine_entities, which only catches LLMError) degrades
    to heuristics instead of crashing."""
    patched_sdk(start_error=RuntimeError("spawn ENOENT copilot"))
    provider = providers(model="auto")
    with pytest.raises(LLMError):
        provider.classify("system", "user")


def test_classify_wraps_mkdtemp_oserror_as_llmerror(providers, patched_sdk, monkeypatch):
    """An OSError while allocating the temp workdir must also normalize to LLMError."""
    patched_sdk()
    provider = providers(model="auto")

    def _boom(*a, **k):
        raise OSError("read-only file system")

    monkeypatch.setattr(cp.tempfile, "mkdtemp", _boom)
    with pytest.raises(LLMError, match="startup failed"):
        provider.classify("system", "user")


# ── check_available ─────────────────────────────────────────────────────────


def test_check_available_ok(providers, patched_sdk):
    patched_sdk(models=[FakeModelInfo("auto"), FakeModelInfo("gpt-5.5")])
    provider = providers(model="auto")
    ok, msg = provider.check_available()
    assert ok is True
    assert msg == "ok"


def test_check_available_model_not_in_account(providers, patched_sdk):
    patched_sdk(models=[FakeModelInfo("auto"), FakeModelInfo("claude-sonnet-4.5")])
    provider = providers(model="gpt-5")
    ok, msg = provider.check_available()
    assert ok is False
    assert "not available" in msg
    assert "--llm-model" in msg


def test_check_available_maps_list_models_failure(providers, patched_sdk):
    patched_sdk(list_models_error=RuntimeError("not signed in — please login"))
    provider = providers(model="auto")
    ok, msg = provider.check_available()
    assert ok is False
    assert "authenticated" in msg.lower()


def test_check_available_never_raises_on_start_failure(providers, patched_sdk):
    patched_sdk(start_error=RuntimeError("spawn ENOENT copilot"))
    provider = providers(model="auto")
    ok, msg = provider.check_available()
    assert ok is False
    assert "runtime" in msg.lower()


# ── missing SDK / interpreter floor (graceful degradation) ───────────────────


def test_check_available_missing_sdk_returns_message(providers, monkeypatch):
    def _raise():
        raise LLMError('copilot provider requires: pip install "mempalace[copilot]"')

    monkeypatch.setattr(cp, "_ensure_sdk", _raise)
    provider = providers(model="auto")
    ok, msg = provider.check_available()
    assert ok is False
    assert "mempalace[copilot]" in msg


def test_classify_missing_sdk_raises_llmerror(providers, monkeypatch):
    def _raise():
        raise LLMError("SDK missing")

    monkeypatch.setattr(cp, "_ensure_sdk", _raise)
    provider = providers(model="auto")
    with pytest.raises(LLMError, match="SDK missing"):
        provider.classify("system", "user")


def test_check_available_rejects_old_python(providers, monkeypatch):
    monkeypatch.setattr(cp.sys, "version_info", _VerInfo(3, 10, 5, "final", 0))
    provider = providers(model="auto")
    ok, msg = provider.check_available()
    assert ok is False
    assert "3.11" in msg
    assert "3.10" in msg


def test_ensure_sdk_rejects_old_python(monkeypatch):
    monkeypatch.setattr(cp, "_SDK_CACHE", None)
    monkeypatch.setattr(cp.sys, "version_info", _VerInfo(3, 10, 0, "final", 0))
    with pytest.raises(LLMError, match="3.11"):
        cp._ensure_sdk()


def test_ensure_sdk_wraps_broken_install_as_llmerror(monkeypatch):
    """A present-but-broken SDK that raises a NON-ImportError at import time (a
    partial or version-incompatible install) must be normalized to LLMError rather
    than escape raw. ``_ensure_sdk`` is called by ``classify`` outside its
    LLMError-wrapping try, and ``refine_entities`` only catches LLMError, so a raw
    RuntimeError here would crash init. Guards the ``except Exception`` branch."""
    broken = types.ModuleType("copilot")

    def _raise(name):  # PEP 562 module __getattr__: `from copilot import X`
        raise RuntimeError(f"incompatible SDK build (attr {name})")

    broken.__getattr__ = _raise
    monkeypatch.setitem(sys.modules, "copilot", broken)
    monkeypatch.setattr(cp, "_SDK_CACHE", None)
    with pytest.raises(LLMError, match="failed to load") as excinfo:
        cp._ensure_sdk()
    # The original cause is preserved for diagnosis (chained, not swallowed).
    assert isinstance(excinfo.value.__cause__, RuntimeError)


# ── external-service flag & endpoint wiring ─────────────────────────────────


def test_is_external_service_always_true(providers):
    # True regardless of endpoint locality — Copilot always relays to the cloud.
    assert providers(model="auto").is_external_service is True
    assert providers(model="auto", endpoint="http://localhost:9999").is_external_service is True


def test_endpoint_wires_runtime_connection(providers, patched_sdk):
    _, client_cls = patched_sdk()
    provider = providers(model="auto", endpoint="localhost:4000")
    provider._ensure_started()
    assert client_cls.instances[0].kwargs["connection"] == ("uri", "localhost:4000")


def test_api_key_forwarded_as_github_token(providers, patched_sdk):
    _, client_cls = patched_sdk()
    provider = providers(model="auto", api_key="ghtok")
    provider._ensure_started()
    assert client_cls.instances[0].kwargs["github_token"] == "ghtok"


def test_api_key_never_marks_consent_source(providers):
    """SEC-004: Copilot authenticates via the signed-in CLI, never a key. An
    ``--llm-api-key`` MUST NOT set ``api_key_source='flag'`` — if it did, cmd_init's
    consent gate (``explicit_key = api_key_source == 'flag'``) would treat the key as
    egress authorization and SKIP the external-LLM prompt, silently sending folder
    content to GitHub. ``api_key_source`` stays None so the prompt always fires."""
    assert providers(model="auto").api_key_source is None
    # Even a passed key is inert for consent purposes (still forwarded to the SDK
    # as a github_token, but never treated as an egress opt-in).
    assert providers(model="auto", api_key="ghtok").api_key_source is None


# ── lifecycle ────────────────────────────────────────────────────────────────


def test_close_is_idempotent(providers, patched_sdk):
    patched_sdk()
    provider = providers(model="auto")
    provider.classify("system", "user")
    provider.close()
    provider.close()  # second close must be a no-op, not raise
    assert provider._closed is True


def test_classify_after_close_raises(providers, patched_sdk):
    patched_sdk()
    provider = providers(model="auto")
    provider.close()
    with pytest.raises(LLMError, match="closed"):
        provider.classify("system", "user")


def test_close_stops_client(providers, patched_sdk):
    _, client_cls = patched_sdk()
    provider = providers(model="auto")
    provider.classify("system", "user")
    provider.close()
    assert client_cls.instances[0].stopped is True


# ── pure-helper units ────────────────────────────────────────────────────────


def test_diagnose_auth():
    msg = cp._diagnose(RuntimeError("401 Unauthorized: please sign in"))
    assert "authenticated" in msg.lower()


def test_diagnose_runtime():
    msg = cp._diagnose(RuntimeError("spawn copilot ENOENT"))
    assert "runtime" in msg.lower()


def test_diagnose_timeout():
    import concurrent.futures

    msg = cp._diagnose(concurrent.futures.TimeoutError())
    assert "did not respond" in msg.lower()


def test_content_from_event_variants():
    assert cp._content_from_event(FakeEvent(FakeAssistantMessageData("hi"))) == "hi"
    assert cp._content_from_event(FakeEvent(FakeAssistantMessageData(None))) == ""
    assert cp._content_from_event(None) == ""


# ── turn-path selection (_accepts_kwarg) ─────────────────────────────────────


def test_accepts_kwarg_detects_named_and_varkw():
    def has_timeout(prompt, timeout=1.0):
        return None

    def has_varkw(prompt, **kw):
        return None

    def no_timeout(prompt):
        return None

    assert cp._accepts_kwarg(has_timeout, "timeout") is True
    assert cp._accepts_kwarg(has_varkw, "timeout") is True
    assert cp._accepts_kwarg(no_timeout, "timeout") is False


def test_accepts_kwarg_assumes_compatible_when_uninspectable(monkeypatch):
    # Some C-accelerated callables have no introspectable signature. When
    # inspect.signature raises, assume compatible so the primary, live-validated
    # path is preferred rather than silently skipped.
    def _boom(_func):
        raise ValueError("no signature found")

    monkeypatch.setattr(cp.inspect, "signature", _boom)
    assert cp._accepts_kwarg(lambda prompt: None, "timeout") is True


# ── SEC-001 tool-denial contract (deny-all permission handler) ───────────────


def test_deny_all_handler_rejects_every_request():
    sdk = cp._Sdk(
        CopilotClient=object,
        RuntimeConnection=object,
        PermissionDecisionReject=FakePermissionDecisionReject,
        AssistantMessageData=object,
        SessionIdleData=object,
        SessionErrorData=object,
    )
    deny = cp._make_deny_all(sdk)
    # The SDK invokes it as handler(permission_request, {"session_id": ...}).
    decision = deny({"tool": "read_file"}, {"session_id": "s1"})
    assert isinstance(decision, FakePermissionDecisionReject)
    assert decision.feedback == cp._DENY_FEEDBACK
    # Resilient to any calling convention the SDK might use.
    assert isinstance(deny(), FakePermissionDecisionReject)
    assert isinstance(deny(request=1, invocation=2), FakePermissionDecisionReject)


# ── bridge durability ────────────────────────────────────────────────────────


def test_bridge_start_times_out_when_loop_never_ready(monkeypatch):
    """If the loop thread never signals readiness, start() must fail fast (never hang)."""
    monkeypatch.setattr(cp, "_BRIDGE_START_TIMEOUT", 0.05)

    class _DeadThread:
        def __init__(self, *a, **k):
            pass

        def start(self):
            pass  # target never runs → readiness event never set

        def is_alive(self):
            return False

        def join(self, timeout=None):
            pass

    monkeypatch.setattr(cp.threading, "Thread", _DeadThread)
    bridge = cp._CopilotBridge()
    with pytest.raises(LLMError, match="failed to start"):
        bridge.start()
    assert bridge._loop is None
    bridge.close()  # safe on a never-started bridge


def test_bridge_close_unblocks_inflight_submit_promptly():
    """Concurrency durability: ``close()`` must cancel an in-flight ``submit`` and let
    its caller return an LLMError PROMPTLY (via ``CancelledError``), not block until
    the full call timeout. Proves ``_drain_and_stop`` awaits task cancellation before
    stopping the loop. The CLI is strictly sequential, but the bridge claims
    concurrency-safety, so this guards that contract."""
    import asyncio
    import threading
    import time

    bridge = cp._CopilotBridge()
    bridge.start()
    try:
        outcome: dict = {}
        scheduled = threading.Event()

        async def _long_sleep():
            await asyncio.sleep(10)
            return "done"

        def _worker():
            scheduled.set()
            try:
                bridge.submit(_long_sleep(), timeout=10)
                outcome["result"] = "returned"
            except LLMError as e:
                outcome["result"] = "llmerror"
                outcome["msg"] = str(e)
            except BaseException as e:  # pragma: no cover — surfaced via assertion
                outcome["result"] = type(e).__name__

        worker = threading.Thread(target=_worker)
        worker.start()
        scheduled.wait(timeout=2)
        time.sleep(0.25)  # let submit register the coroutine on the loop
        start = time.monotonic()
        bridge.close()
        worker.join(timeout=6)
        elapsed = time.monotonic() - start
        assert not worker.is_alive(), "in-flight submit never unblocked after close()"
        assert outcome.get("result") == "llmerror", outcome
        assert elapsed < 5.0, f"close()+unblock took {elapsed:.1f}s (expected prompt cancel)"
    finally:
        bridge.close()  # idempotent — safe even after the in-test close


def test_bridge_submit_after_close_raises_llmerror():
    """A ``submit`` after ``close`` must fail fast with LLMError (guarded ``_closed``),
    never schedule work on a dead loop or leak an un-awaited coroutine."""
    bridge = cp._CopilotBridge()
    bridge.start()
    bridge.close()

    async def _noop():
        return None

    with pytest.raises(LLMError, match="not started"):
        bridge.submit(_noop(), timeout=1)


def test_bridge_concurrent_submit_close_never_hangs():
    """Fuzz the submit-vs-close interleaving: across many cycles a ``submit`` racing
    a ``close`` must ALWAYS resolve to an LLMError promptly, never block until its call
    timeout. Guards the lock that makes guard-and-schedule atomic w.r.t. ``close`` —
    without it, ``close`` could stop the loop AFTER submit's readiness check but BEFORE
    ``run_coroutine_threadsafe``, queuing the coroutine on a stopped-but-not-closed loop
    (``run_coroutine_threadsafe`` does not raise there), so ``future.result`` blocks the
    FULL timeout and the coroutine leaks un-awaited. Complements
    ``test_bridge_close_unblocks_inflight_submit_promptly`` (which pins the
    submit-registers-first ordering); this one fuzzes the narrow middle window."""
    import asyncio
    import threading

    submit_timeout = 3.0

    def _cycle() -> tuple[bool, object]:
        bridge = cp._CopilotBridge()
        bridge.start()
        outcome: dict = {}

        async def _sleep():
            await asyncio.sleep(submit_timeout)

        def _worker():
            try:
                bridge.submit(_sleep(), timeout=submit_timeout)
                outcome["r"] = "returned"
            except LLMError:
                outcome["r"] = "llmerror"
            except BaseException as e:  # pragma: no cover — surfaced via assertion
                outcome["r"] = type(e).__name__

        worker = threading.Thread(target=_worker)
        worker.start()
        bridge.close()  # races the submit with no artificial delay
        worker.join(timeout=submit_timeout - 0.5)
        alive = worker.is_alive()
        bridge.close()  # idempotent safety
        worker.join(timeout=2)
        return alive, outcome.get("r")

    for _ in range(25):
        alive, result = _cycle()
        assert not alive, "submit hung under concurrent close — the guard/schedule race regressed"
        assert result == "llmerror", f"expected prompt LLMError, got {result!r}"


def test_bridge_submit_times_out_and_cancels():
    """A call exceeding its ``timeout`` is cancelled and surfaced as LLMError, never
    left to block indefinitely — the per-call durability bound."""
    import asyncio

    bridge = cp._CopilotBridge()
    bridge.start()
    try:

        async def _slow():
            await asyncio.sleep(5)

        with pytest.raises(LLMError, match="exceeded"):
            bridge.submit(_slow(), timeout=0.2)
    finally:
        bridge.close()


# ── concurrency (double-checked start) ───────────────────────────────────────


def test_concurrent_ensure_started_starts_client_once(providers, patched_sdk):
    import threading as _t

    _, client_cls = patched_sdk()
    provider = providers(model="auto")
    barrier = _t.Barrier(8)
    errors: list = []

    def worker():
        try:
            barrier.wait()
            provider._ensure_started()
        except Exception as e:  # pragma: no cover — surfaced via assertion below
            errors.append(e)

    threads = [_t.Thread(target=worker) for _ in range(8)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=10)

    assert errors == []
    # The state lock must serialize startup: exactly one client, started once.
    assert len(client_cls.instances) == 1
    assert client_cls.instances[0].started is True


def test_check_available_ok_when_list_models_empty(providers, patched_sdk):
    """Empty model metadata is treated as OK (not a false negative): 'auto' always
    works and classify degrades gracefully if a model is later unavailable."""
    patched_sdk(models=[])
    provider = providers(model="auto")
    ok, msg = provider.check_available()
    assert ok is True
    assert msg == "ok"
