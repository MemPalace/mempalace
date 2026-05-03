"""
llm_client.py — Minimal provider abstraction for LLM-assisted entity refinement.

Four providers cover the useful space:

- ``ollama`` (default): local models via http://localhost:11434. Works fully
  offline. Honors MemPalace's "zero-API required" principle.
- ``openai-compat``: any OpenAI-compatible ``/v1/chat/completions`` endpoint.
  Covers OpenRouter, LM Studio, llama.cpp server, vLLM, Groq, Fireworks,
  Together, and most self-hosted setups.
- ``anthropic``: the official Messages API. Opt-in for users who want Haiku
  quality without setting up a local model.
- ``claude-code``: the local ``claude`` CLI binary. Routes through the user's
  existing Claude Pro/Max subscription via ``claude auth login`` -- no API
  key required. Subprocess-based, zero new pip deps. Subject to Anthropic
  policy on subscription use from third-party tools.

All providers expose the same ``classify(system, user, json_mode)`` method and
the same ``check_available()`` probe. No external SDK dependencies -- stdlib
``urllib`` plus ``subprocess`` (for ``claude-code``) only.

JSON mode matters here: we always ask for structured output. Providers
differ on how to request it (Ollama: ``format: json``; OpenAI-compat:
``response_format``; Anthropic: prompt-level instruction; claude-code:
prompt-level instruction) and this module normalizes that away from the
caller.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


# ── External-service heuristic (issue #24 — privacy warning support) ─────
# Used by ``LLMProvider.is_external_service`` to decide whether the
# provider's configured endpoint will send user content off the local
# machine/network. Single source of truth so all three providers share
# identical "local vs external" semantics.

_LOCALHOST_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def _endpoint_is_local(url: Optional[str]) -> bool:
    """Return True if ``url``'s hostname is on the user's machine or
    private network.

    Local includes:
      - localhost, 127.0.0.1, ::1
      - hostnames ending in .local (mDNS/Bonjour)
      - IPv4 RFC1918: 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16
      - IPv4 CGNAT (Tailscale and similar VPN/tunnel networks):
        100.64.0.0/10 — first octet 100, second octet 64-127 inclusive
      - IPv6 unique-local addresses (fc00::/7) — fc.../fd... prefixes

    None / empty / unparseable URLs are treated as local (defensive default —
    no endpoint means no external request can happen yet).

    Anything else (including public IPs and FQDNs) is external.
    """
    if not url:
        return True
    try:
        host = (urlparse(url).hostname or "").lower()
    except (ValueError, AttributeError):
        return False
    if not host:
        return True
    if host in _LOCALHOST_HOSTS:
        return True
    if host.endswith(".local"):
        return True
    if host.startswith("10."):
        return True
    if host.startswith("192.168."):
        return True
    if host.startswith("172."):
        # 172.16.0.0 - 172.31.255.255
        parts = host.split(".")
        if len(parts) >= 2:
            try:
                if 16 <= int(parts[1]) <= 31:
                    return True
            except ValueError:
                pass
    if host.startswith("100."):
        # 100.64.0.0/10 — Tailscale CGNAT range. First octet 100, second
        # octet 64-127 inclusive. Users running a local LLM (LM Studio,
        # Ollama, etc.) accessible via Tailscale on a 100.x.x.x address
        # should not trigger the external-API privacy warning.
        # 100.x.x.x outside this range is regular allocated public space
        # and remains external.
        parts = host.split(".")
        if len(parts) >= 2:
            try:
                if 64 <= int(parts[1]) <= 127:
                    return True
            except ValueError:
                pass
    # IPv6 unique-local addresses fc00::/7 — match leading hex chars
    if host.startswith("fc") or host.startswith("fd"):
        return True
    return False


class LLMError(RuntimeError):
    """Raised for any provider failure — transport, parse, auth, missing model."""


@dataclass
class LLMResponse:
    text: str
    model: str
    provider: str
    raw: dict


# ==================== BASE ====================


class LLMProvider:
    name: str = "base"

    def __init__(
        self,
        model: str,
        endpoint: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout: int = 120,
        api_key_source: Optional[str] = None,
    ):
        self.model = model
        self.endpoint = endpoint
        self.api_key = api_key
        self.timeout = timeout
        # Provenance of api_key (issue #26): "flag" when the constructor
        # received an explicit api_key arg, "env" when it fell back to an
        # environment variable, None when no key is in play. cmd_init
        # uses this to gate the consent prompt — stray env-resolved keys
        # require explicit user confirmation.
        self.api_key_source = api_key_source

    def classify(
        self,
        system: str,
        user: str,
        json_mode: bool = True,
        think: Optional[bool] = None,
    ) -> LLMResponse:
        """Classify a (system, user) pair into a structured response.

        ``think`` controls reasoning emission for thinking-capable models
        (currently honored by ``OllamaProvider`` for Qwen 3 / DeepSeek-R1
        style toggles). Other providers ignore it. Pass ``False`` to
        disable reasoning when the caller wants a fast classification
        without ``<think>`` overhead.
        """
        raise NotImplementedError

    def check_available(self) -> tuple[bool, str]:
        """Return ``(ok, message)``. Fast probe that the provider is reachable."""
        raise NotImplementedError

    @property
    def is_external_service(self) -> bool:
        """Return True if this provider's endpoint will send user content
        off the local machine/network.

        Used by ``mempalace init`` to decide whether to print a privacy
        warning before first use (issue #24). URL-based heuristic only —
        the endpoint determines, regardless of which provider class.
        Subclasses that resolve their endpoint dynamically should override
        if needed; the default works for the three in-tree providers
        (Ollama / OpenAI-compat / Anthropic).
        """
        return not _endpoint_is_local(self.endpoint)


def _http_post_json(url: str, body: dict, headers: dict, timeout: int) -> dict:
    """POST JSON and return the parsed response. Raises LLMError on any failure."""
    req = Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
    )
    try:
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            pass
        raise LLMError(f"HTTP {e.code} from {url}: {detail or e.reason}") from e
    except (URLError, OSError) as e:
        raise LLMError(f"Cannot reach {url}: {e}") from e
    except json.JSONDecodeError as e:
        raise LLMError(f"Malformed response from {url}: {e}") from e


# ==================== OLLAMA ====================


class OllamaProvider(LLMProvider):
    name = "ollama"
    DEFAULT_ENDPOINT = "http://localhost:11434"

    def __init__(
        self,
        model: str,
        endpoint: Optional[str] = None,
        timeout: int = 180,
        num_ctx: Optional[int] = None,
        **_: object,
    ):
        super().__init__(
            model=model,
            endpoint=endpoint or self.DEFAULT_ENDPOINT,
            timeout=timeout,
        )
        self.num_ctx = num_ctx

    def check_available(self) -> tuple[bool, str]:
        try:
            with urlopen(f"{self.endpoint}/api/tags", timeout=5) as resp:
                data = json.loads(resp.read())
        except (URLError, HTTPError, OSError, json.JSONDecodeError) as e:
            return False, f"Cannot reach Ollama at {self.endpoint}: {e}"
        names = {m.get("name", "") for m in data.get("models", []) or []}
        # Ollama tags may or may not include ':latest' — accept either form
        wanted = {self.model, f"{self.model}:latest"}
        if not names & wanted:
            return (
                False,
                f"Model '{self.model}' not loaded in Ollama. Run: ollama pull {self.model}",
            )
        return True, "ok"

    def classify(
        self,
        system: str,
        user: str,
        json_mode: bool = True,
        think: Optional[bool] = None,
    ) -> LLMResponse:
        options: dict = {"temperature": 0.1}
        if self.num_ctx is not None:
            options["num_ctx"] = self.num_ctx
        body: dict = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "options": options,
        }
        if json_mode:
            body["format"] = "json"
        if think is not None:
            # Ollama 0.7+ supports `think` for thinking-capable models (Qwen 3
            # family, DeepSeek-R1). Pure-instruct models ignore it. We forward
            # only when the caller explicitly opts in/out so the wire format
            # stays minimal for the common case.
            body["think"] = think
        data = _http_post_json(f"{self.endpoint}/api/chat", body, headers={}, timeout=self.timeout)
        text = (data.get("message") or {}).get("content", "")
        if not text:
            raise LLMError(f"Empty response from Ollama (model={self.model})")
        return LLMResponse(text=text, model=self.model, provider=self.name, raw=data)


# ==================== OPENAI-COMPAT ====================


class OpenAICompatProvider(LLMProvider):
    """Any OpenAI-compatible ``/v1/chat/completions`` endpoint.

    Supply ``--llm-endpoint http://host:port`` (with or without ``/v1``).
    API key via ``--llm-api-key`` or the ``OPENAI_API_KEY`` env var.
    """

    name = "openai-compat"

    def __init__(
        self,
        model: str,
        endpoint: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout: int = 120,
        **_: object,
    ):
        if api_key:
            resolved_key = api_key
            source: Optional[str] = "flag"
        else:
            env_key = os.environ.get("OPENAI_API_KEY")
            resolved_key = env_key or None
            source = "env" if env_key else None
        super().__init__(
            model=model,
            endpoint=endpoint,
            api_key=resolved_key,
            timeout=timeout,
            api_key_source=source,
        )

    def _resolve_url(self) -> str:
        if not self.endpoint:
            raise LLMError("openai-compat provider requires --llm-endpoint")
        url = self.endpoint.rstrip("/")
        if url.endswith("/chat/completions"):
            return url
        if not url.endswith("/v1"):
            url = f"{url}/v1"
        return f"{url}/chat/completions"

    def check_available(self) -> tuple[bool, str]:
        if not self.endpoint:
            return False, "no --llm-endpoint configured"
        base = self.endpoint.rstrip("/")
        base = base.removesuffix("/chat/completions").removesuffix("/v1")
        try:
            req = Request(f"{base}/v1/models")
            if self.api_key:
                req.add_header("Authorization", f"Bearer {self.api_key}")
            with urlopen(req, timeout=5):
                pass
        except (URLError, HTTPError, OSError) as e:
            return False, f"Cannot reach {self.endpoint}: {e}"
        return True, "ok"

    def classify(
        self,
        system: str,
        user: str,
        json_mode: bool = True,
        think: Optional[bool] = None,  # noqa: ARG002 — accepted for interface compat; OpenAI-compat has no thinking toggle
    ) -> LLMResponse:
        body: dict = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.1,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        headers = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        data = _http_post_json(self._resolve_url(), body, headers=headers, timeout=self.timeout)
        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError(f"Unexpected response shape: {e}") from e
        if not text:
            raise LLMError(f"Empty response from {self.name} (model={self.model})")
        return LLMResponse(text=text, model=self.model, provider=self.name, raw=data)


# ==================== ANTHROPIC ====================


class AnthropicProvider(LLMProvider):
    name = "anthropic"
    DEFAULT_ENDPOINT = "https://api.anthropic.com"
    API_VERSION = "2023-06-01"

    def __init__(
        self,
        model: str,
        api_key: Optional[str] = None,
        endpoint: Optional[str] = None,
        timeout: int = 120,
        **_: object,
    ):
        if api_key:
            resolved_key = api_key
            source: Optional[str] = "flag"
        else:
            env_key = os.environ.get("ANTHROPIC_API_KEY")
            resolved_key = env_key or None
            source = "env" if env_key else None
        super().__init__(
            model=model,
            endpoint=endpoint or self.DEFAULT_ENDPOINT,
            api_key=resolved_key,
            timeout=timeout,
            api_key_source=source,
        )

    def check_available(self) -> tuple[bool, str]:
        if not self.api_key:
            return False, "ANTHROPIC_API_KEY not set (use --llm-api-key or env)"
        # Don't probe — a live request would cost money. First real call will
        # surface auth errors if the key is invalid.
        return True, "ok"

    def classify(
        self,
        system: str,
        user: str,
        json_mode: bool = True,
        think: Optional[bool] = None,  # noqa: ARG002 — accepted for interface compat; Anthropic extended thinking is configured separately
    ) -> LLMResponse:
        if not self.api_key:
            raise LLMError("Anthropic provider requires ANTHROPIC_API_KEY env or --llm-api-key")
        sys_prompt = system
        if json_mode:
            sys_prompt += "\n\nRespond with valid JSON only, no prose."
        body = {
            "model": self.model,
            "max_tokens": 2048,
            "temperature": 0.1,
            "system": sys_prompt,
            "messages": [{"role": "user", "content": user}],
        }
        headers = {
            "X-API-Key": self.api_key,
            "anthropic-version": self.API_VERSION,
        }
        data = _http_post_json(
            f"{self.endpoint}/v1/messages", body, headers=headers, timeout=self.timeout
        )
        try:
            text = "".join(
                b.get("text", "") for b in data.get("content", []) or [] if b.get("type") == "text"
            )
        except (AttributeError, TypeError) as e:
            raise LLMError(f"Unexpected response shape: {e}") from e
        if not text:
            raise LLMError(f"Empty response from Anthropic (model={self.model})")
        return LLMResponse(text=text, model=self.model, provider=self.name, raw=data)


# ==================== CLAUDE CODE (CLI subprocess) ====================


class ClaudeCodeProvider(LLMProvider):
    """Routes through the local ``claude`` CLI binary using subscription auth.

    Auth happens once via ``claude auth login`` (stored in the user's keychain
    by Claude Code itself); we shell out to ``claude -p`` for each call. No
    API key, no new pip dependencies -- the CLI itself is the bundled
    transport.

    Going direct via ``subprocess`` rather than the ``claude-agent-sdk``
    Python wrapper is deliberate: the SDK is async-only, requires
    Python >= 3.10 (we still support 3.9), and itself spawns the same binary.
    Skipping the wrapper avoids a dependency and an asyncio bridge.

    Subscription use from third-party harnesses is governed by Anthropic's
    policy, which has changed in 2026. The ``claude -p`` CLI invocation
    pattern is currently sanctioned for first-party tools but may be
    restricted later; ``check_available()`` will surface auth errors at that
    point so callers can fall back.
    """

    name = "claude-code"
    DEFAULT_MODEL = "claude-haiku-4-5"

    def __init__(
        self,
        model: str,
        timeout: int = 120,
        **_: object,  # endpoint/api_key ignored -- auth comes from `claude auth login`
    ):
        super().__init__(model=model, timeout=timeout)

    @property
    def is_external_service(self) -> bool:
        # The CLI binary runs locally but routes every classify call to
        # Anthropic's hosted models, so user content does leave the machine.
        # Override the URL-based default in the base class.
        return True

    # Only the credential-bearing ANTHROPIC_* vars get stripped. Configuration
    # vars like ANTHROPIC_BASE_URL (corporate proxy / custom endpoint) must
    # pass through so users behind an internal gateway keep working. A broad
    # prefix-strip would break those deployments without warning.
    _SUBPROCESS_ENV_STRIP = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")

    def _subprocess_env(self) -> dict:
        # Strip credential env vars before spawning `claude -p`. If the user
        # has ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN exported in their
        # shell, the CLI may fall back to API-key auth and bill the API
        # account instead of the subscription this provider is built
        # around. Removing them forces OAuth / keychain auth, which is the
        # documented path. Other ANTHROPIC_* (BASE_URL, etc.) are passed
        # through.
        return {k: v for k, v in os.environ.items() if k.upper() not in self._SUBPROCESS_ENV_STRIP}

    def check_available(self) -> tuple[bool, str]:
        binary = shutil.which("claude")
        if not binary:
            return (
                False,
                "`claude` CLI not found in PATH. "
                "Install Claude Code: https://claude.com/product/claude-code",
            )
        try:
            r = subprocess.run(
                [binary, "auth", "status", "--text"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                env=self._subprocess_env(),
            )
        except (subprocess.SubprocessError, OSError) as e:
            return False, f"`claude auth status` failed: {e}"
        if r.returncode != 0:
            return (
                False,
                "Not authenticated. Run `claude auth login` to use your Claude subscription.",
            )
        return True, "ok"

    def classify(self, system: str, user: str, json_mode: bool = True) -> LLMResponse:
        sys_prompt = system
        if json_mode:
            sys_prompt += "\n\nRespond with valid JSON only, no prose."
        binary = shutil.which("claude")
        if not binary:
            raise LLMError("`claude` CLI not found in PATH")
        # `--bare` would skip hooks, plugins, CLAUDE.md auto-discovery, but it
        # also forces claude to use ANTHROPIC_API_KEY only and ignore OAuth /
        # keychain. That defeats this provider's whole point (subscription
        # auth), so we omit it. To keep the surrounding context minimal we
        # invoke from a temp cwd so claude does not pick up a project-level
        # CLAUDE.md it does not need.
        #
        # System and user content go through stdin (not argv) so they are
        # not visible to other local users via `ps` / /proc/*/cmdline, and
        # to keep prompt-injection surface narrow we frame them with XML-
        # like tags rather than literal "SYSTEM:" / "USER:" markers a
        # malicious drawer could spoof.
        cmd = [
            binary,
            "-p",
            "--no-session-persistence",  # don't pollute Claude Code session history
            "--output-format",
            "json",
            "--model",
            self.model,
        ]
        combined_input = f"<system>\n{sys_prompt}\n</system>\n<user>\n{user}\n</user>"
        try:
            r = subprocess.run(
                cmd,
                input=combined_input,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout,
                cwd=tempfile.gettempdir(),
                env=self._subprocess_env(),
            )
        except subprocess.TimeoutExpired as e:
            raise LLMError(f"`claude -p` timed out after {self.timeout}s") from e
        except OSError as e:
            raise LLMError(f"`claude -p` failed to spawn: {e}") from e
        if r.returncode != 0:
            stderr = (r.stderr or "").strip()[:500]
            raise LLMError(f"`claude -p` exited {r.returncode}: {stderr or 'no stderr'}")
        try:
            envelope = json.loads(r.stdout)
        except json.JSONDecodeError as e:
            stdout_excerpt = (r.stdout or "").strip()[:200]
            raise LLMError(
                f"`claude -p` returned non-JSON envelope: {e}; stdout={stdout_excerpt!r}"
            ) from e
        # `--output-format json` returns:
        # {"type":"result","result":"<text>","total_cost_usd":...,...}
        text = envelope.get("result", "")
        if not text:
            raise LLMError(f"`claude -p` returned empty result: {envelope}")
        return LLMResponse(text=text, model=self.model, provider=self.name, raw=envelope)


# ==================== FACTORY ====================


PROVIDERS: dict[str, type[LLMProvider]] = {
    "ollama": OllamaProvider,
    "openai-compat": OpenAICompatProvider,
    "anthropic": AnthropicProvider,
    "claude-code": ClaudeCodeProvider,
}


def get_provider(
    name: str,
    model: str,
    endpoint: Optional[str] = None,
    api_key: Optional[str] = None,
    timeout: int = 120,
    **provider_kwargs: object,
) -> LLMProvider:
    """Build a provider by name. Raises LLMError on unknown provider.

    Extra kwargs (e.g. num_ctx for Ollama) are forwarded to the provider's
    constructor; providers that don't recognize them ignore via **_.
    """
    cls = PROVIDERS.get(name)
    if cls is None:
        raise LLMError(f"Unknown provider '{name}'. Choices: {sorted(PROVIDERS.keys())}")
    return cls(model=model, endpoint=endpoint, api_key=api_key, timeout=timeout, **provider_kwargs)
