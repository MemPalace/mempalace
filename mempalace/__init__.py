"""MemPalace — Give your AI a memory. No API key required."""

import logging
import os
import sys


def _strip_leaked_pythonpath_from_sys_path() -> None:
    # Venvs inherit PYTHONPATH; on multi-Python systems it can cause
    # transitive imports to load compiled extensions (pydantic_core,
    # chromadb_rust_bindings) from the wrong ABI. Remove sys.path entries
    # the interpreter populated from PYTHONPATH so this process imports
    # only the venv's own packages. Comparison normalizes case + separators
    # so Windows paths and trailing-separator quirks do not slip through
    # string equality. The empty-string CWD marker on sys.path is preserved
    # regardless, so PYTHONPATH=. does not collapse the implicit current
    # directory.
    #
    # os.environ is intentionally NOT modified here. CLI entry points
    # (mempalace.cli:main, mempalace.mcp_server:main) drop PYTHONPATH from
    # the env themselves so any subprocess they spawn starts clean. Host
    # applications that embed mempalace as a library (e.g. import
    # mempalace.searcher) keep their PYTHONPATH intact for their own
    # unrelated subprocesses.
    leaked = os.environ.get("PYTHONPATH", None)
    if not leaked:
        return

    import sysconfig

    def _norm(path: str) -> str:
        return os.path.normcase(os.path.normpath(path))

    # Preserve this interpreter's package directories, not every
    # descendant of sys.prefix: shared installations and nested venvs can
    # contain packages built for another Python ABI under the same prefix.
    paths = sysconfig.get_paths()
    own_site_packages = {_norm(os.path.realpath(paths[name])) for name in ("purelib", "platlib")}

    def _belongs_to_this_environment(path: str) -> bool:
        try:
            resolved = _norm(os.path.realpath(path))
        except OSError:
            return False
        return resolved in own_site_packages

    leaked_entries = {_norm(p) for p in leaked.split(os.pathsep) if p}
    sys.path[:] = [
        p
        for p in sys.path
        if not p or _norm(p) not in leaked_entries or _belongs_to_this_environment(p)
    ]


_strip_leaked_pythonpath_from_sys_path()

from .version import __version__  # noqa: E402

# chromadb telemetry: posthog capture() was broken in 0.6.x causing noisy stderr
# warnings ("capture() takes 1 positional argument but 3 were given"). In 1.x the
# posthog client is a no-op stub, so this is now harmless — kept as a guard in
# case future chromadb versions re-introduce real telemetry calls.
logging.getLogger("chromadb.telemetry.product.posthog").setLevel(logging.CRITICAL)

# Silencing the logger only hides telemetry; this turns it off. The backend passes
# ``Settings(anonymized_telemetry=False)`` to every client it opens, and this
# covers chromadb clients opened by anything else in the process (a notebook, a
# plugin) that would otherwise inherit ChromaDB's opt-out-by-default. setdefault,
# so an operator who deliberately exports the variable still wins. (GHSA-8h77)
os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")

# Don't set ``ORT_DISABLE_COREML`` here for macOS arm64 crashes: ONNX Runtime
# ignores that variable, and those crashes come from the chromadb 0.x hnswlib
# binding, not from ONNX.

__all__ = ["__version__"]
