"""Source adapter registry + entry-point discovery (RFC 002 §3).

Third-party adapters ship as installable packages that declare a
``mempalace.sources`` entry point::

    # pyproject.toml of mempalace-source-cursor
    [project.entry-points."mempalace.sources"]
    cursor = "mempalace_source_cursor:CursorAdapter"

MemPalace discovers them at process start. In-tree tests and local
development can register manually via :func:`register`. Explicit
registration wins on name conflict (RFC 002 §3.2).

Unlike storage backends (RFC 001 §3.3), source adapters are never auto-
detected — the user selects the adapter explicitly via ``--source NAME``
or config (§3.3). The default when no adapter is named is ``filesystem``
(to preserve current ``mempalace mine <path>`` behavior).
"""

from __future__ import annotations

import logging
from importlib import metadata
from threading import Lock
from typing import Type

from .base import BaseSourceAdapter

logger = logging.getLogger(__name__)

_ENTRY_POINT_GROUP = "mempalace.sources"
_DEFAULT_ADAPTER = "filesystem"

_registry: dict[str, Type[BaseSourceAdapter]] = {}
_instances: dict[str, BaseSourceAdapter] = {}
_explicit: set[str] = set()
_discovered = False
_lock = Lock()


def register(name: str, adapter_cls: Type[BaseSourceAdapter]) -> None:
    """Register ``adapter_cls`` under ``name``.

    Explicit registration wins over entry-point discovery on conflict (§3.2).
    """
    with _lock:
        _registry[name] = adapter_cls
        _explicit.add(name)
        _instances.pop(name, None)


def unregister(name: str) -> None:
    """Remove an adapter registration (primarily for tests)."""
    with _lock:
        _registry.pop(name, None)
        _explicit.discard(name)
        _instances.pop(name, None)


def _discover_entry_points() -> None:
    global _discovered
    if _discovered:
        return
    with _lock:
        if _discovered:
            return
        try:
            eps = metadata.entry_points()
            group = (
                eps.select(group=_ENTRY_POINT_GROUP)
                if hasattr(eps, "select")
                else eps.get(_ENTRY_POINT_GROUP, [])
            )
        except Exception:
            logger.exception("entry-point discovery for %s failed", _ENTRY_POINT_GROUP)
            group = []
        for ep in group:
            if ep.name in _explicit:
                continue  # explicit registration wins
            try:
                cls = ep.load()
            except Exception:
                logger.exception("failed to load adapter entry point %r", ep.name)
                continue
            if not isinstance(cls, type) or not issubclass(cls, BaseSourceAdapter):
                logger.warning(
                    "entry point %r did not resolve to a BaseSourceAdapter subclass (got %r)",
                    ep.name,
                    cls,
                )
                continue
            _registry.setdefault(ep.name, cls)
        _discovered = True


def available_adapters() -> list[str]:
    """Return sorted list of all registered adapter names."""
    _discover_entry_points()
    return sorted(_registry.keys())


def get_adapter_class(name: str) -> Type[BaseSourceAdapter]:
    """Return the registered adapter class for ``name``."""
    _discover_entry_points()
    try:
        return _registry[name]
    except KeyError as e:
        raise KeyError(f"unknown source adapter {name!r}; available: {available_adapters()}") from e


def get_adapter(name: str) -> BaseSourceAdapter:
    """Return a long-lived instance of the named adapter.

    Instances are cached per-name; repeated calls return the same object.
    Call :func:`reset_adapters` in tests that need isolation.
    """
    _discover_entry_points()
    with _lock:
        inst = _instances.get(name)
        if inst is not None:
            return inst
        cls = _registry.get(name)
        if cls is None:
            raise KeyError(
                f"unknown source adapter {name!r}; available: {sorted(_registry.keys())}"
            )
        inst = cls()
        _instances[name] = inst
        return inst


def reset_adapters() -> None:
    """Close and drop all cached adapter instances (primarily for tests)."""
    with _lock:
        for inst in _instances.values():
            try:
                inst.close()
            except Exception:
                logger.exception("error closing adapter during reset")
        _instances.clear()


def resolve_adapter_for_source(
    *,
    explicit: str | None = None,
    config_value: str | None = None,
    default: str = _DEFAULT_ADAPTER,
) -> str:
    """Resolve the adapter name per RFC 002 §3.3 priority order.

    1. Explicit ``--source`` flag or kwarg
    2. Per-source config value
    3. Default (``filesystem``)

    Auto-detection is *intentionally* absent on the read side (§3.3); a
    directory containing ``.git`` + ``workspaceStorage/`` + an ``mbox`` file
    is not a signal of user intent.
    """
    for candidate in (explicit, config_value):
        if candidate:
            return candidate
    return default
