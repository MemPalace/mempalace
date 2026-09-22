"""
llm_client.py — Minimal provider abstraction for LLM-assisted entity refinement.

Three providers cover the useful space:

- ``ollama`` (default): local models via http://localhost:11434. Works fully
  offline. Honors MemPalace's "zero-API required" principle.
- ``openai-compat``: any OpenAI-compatible ``/v1/chat/completions`` endpoint.
  Covers OpenRouter, LM Studio, llama.cpp server, vLLM, Groq, Fireworks,
  Together, and most self-hosted setups.
- ``anthropic``: the official Messages API. Opt-in for users who want Haiku
  quality without setting up a local model.

All providers expose the same ``classify(system, user, json_mode)`` method and
the same ``check_available()`` probe. No external SDK dependencies — stdlib
``urllib`` only.

JSON mode matters here: we always ask for structured output. Providers
differ on how to request it (Ollama: ``format: json``; OpenAI-compat:
``response_format``; Anthropic: prompt-level instruction) and this module
normalizes that away from the caller.
"""

from __future__ import annotations

import json
import os
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
    # A single-label hostname (``gpu-box``, ``gpu-box``) has no domain,
    # so it can only resolve through the LAN: mDNS, the router's DNS, a hosts
    # file, or a search domain the user configured. It is the user's own
    # network by construction, the same as ``.local``.
    if "." not in host and not host.startswith("["):
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

    def _models_url(self) -> str:
        base = self.endpoint.rstrip("/")
        base = base.removesuffix("/chat/completions").removesuffix("/v1")
        return f"{base}/v1/models"

    def served_models(self) -> list[str]:
        """Model ids the endpoint lists; ``[]`` when it cannot be read."""
        req = Request(self._models_url())
        if self.api_key and (self.api_key_source != "env" or not self.is_external_service):
            req.add_header("Authorization", f"Bearer {self.api_key}")
        with urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
        items = data.get("data") if isinstance(data, dict) else data
        return [str(m.get("id")) for m in (items or []) if isinstance(m, dict) and m.get("id")]

    def check_available(self) -> tuple[bool, str]:
        """Reachability probe; ``model="auto"`` is resolved here to the served model.

        A local server (vLLM, NInfer, LM Studio) serves one model at a time
        and its id changes whenever the operator swaps it; ``auto`` follows
        the swap instead of failing with ``model_not_found``.
        """
        if not self.endpoint:
            return False, "no --llm-endpoint configured"
        try:
            models = self.served_models()
        except (URLError, HTTPError, OSError, ValueError) as e:
            return False, f"Cannot reach {self.endpoint}: {e}"
        if self.model == "auto":
            if not models:
                return False, f"{self.endpoint} lists no models to pick from"
            self.model = models[0]
        elif models and self.model not in models:
            return False, (
                f"model {self.model!r} is not served at {self.endpoint}; served: "
                f"{', '.join(models)} (use --llm-model auto to follow the server)"
            )
        return True, "ok"

    def classify(
        self,
        system: str,
        user: str,
        json_mode: bool = True,
        think: Optional[bool] = None,
    ) -> LLMResponse:
        """``think=False`` sends ``reasoning_effort: "none"``.

        Reasoning models served through an OpenAI-compatible endpoint (Qwen 3.x
        on vLLM, NInfer, llama.cpp) think before answering, and their default
        effort can spend the whole completion budget on the reasoning channel
        and return an empty ``content`` — a 60-excerpt room proposal did
        exactly that at 8192 reasoning tokens. ``reasoning_effort`` is the
        OpenAI field for this and the servers above honor it; a server that
        rejects unknown fields with HTTP 400 gets one retry without it.
        """
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
        if think is False:
            body["reasoning_effort"] = "none"
        headers = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        url = self._resolve_url()
        try:
            data = _http_post_json(url, body, headers=headers, timeout=self.timeout)
        except LLMError as e:
            if "reasoning_effort" not in body or "HTTP 400" not in str(e):
                raise
            body.pop("reasoning_effort")
            data = _http_post_json(url, body, headers=headers, timeout=self.timeout)
        try:
            choice = data["choices"][0]
            text = choice["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError(f"Unexpected response shape: {e}") from e
        if not text:
            reason = ""
            if isinstance(choice, dict) and choice.get("finish_reason") == "length":
                reason = (
                    " — the completion budget ran out (finish_reason=length); a reasoning "
                    "model likely spent it thinking. Pass think=False or lower the "
                    "model's reasoning effort."
                )
            raise LLMError(f"Empty response from {self.name} (model={self.model}){reason}")
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


# ==================== FACTORY ====================


PROVIDERS: dict[str, type[LLMProvider]] = {
    "ollama": OllamaProvider,
    "openai-compat": OpenAICompatProvider,
    "anthropic": AnthropicProvider,
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
