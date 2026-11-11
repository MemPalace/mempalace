"""Per-person chat bots: storage, the API, the turn engine and the tools a bot may call."""

import importlib

from commonplace.bots import (  # noqa: F401  (tools registers the built-in tools)
    routes,
    tools,
    updates,
)

# Tool modules added by later slices; each registers its tools and hydrators on import.
TOOL_MODULES = ("tools_project", "tools_machines")


def _load_tools():
    for name in TOOL_MODULES:
        module = f"{__name__}.{name}"
        try:
            importlib.import_module(module)
        except ModuleNotFoundError as exc:
            if exc.name != module:
                raise


def install(gateway):
    if getattr(gateway, "bots_installed", False):
        return
    _load_tools()
    routes.install(gateway)
    updates.install(gateway)
    gateway.bots_installed = True
