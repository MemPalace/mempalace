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

    def classify(self, system: str, user: str, json_mode: bool = True) -> LLMResponse:
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
        **_: object,
    ):
        super().__init__(
            model=model,
            endpoint=endpoint or self.DEFAULT_ENDPOINT,
            timeout=timeout,
        )

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

    def classify(self, system: str, user: str, json_mode: bool = True) -> LLMResponse:
        body: dict = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "options": {"temperature": 0.1},
        }
        if json_mode:
            body["format"] = "json"
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

    def classify(self, system: str, user: str, json_mode: bool = True) -> LLMResponse:
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

    def classify(self, system: str, user: str, json_mode: bool = True) -> LLMResponse:
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
) -> LLMProvider:
    """Build a provider by name. Raises LLMError on unknown provider."""
    cls = PROVIDERS.get(name)
    if cls is None:
        raise LLMError(f"Unknown provider '{name}'. Choices: {sorted(PROVIDERS.keys())}")
    return cls(model=model, endpoint=endpoint, api_key=api_key, timeout=timeout)
