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

    def _read_pyvenv_home(cfg_path: str) -> str:
        # pyvenv.cfg is a small key=value file (no section header); parse the
        # ``home`` line directly so we never need a full INI parser on the
        # import hot path.
        try:
            with open(cfg_path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith(("#", ";")):
                        continue
                    key, sep, value = line.partition("=")
                    if sep and key.strip().lower() == "home":
                        return value.strip()
        except OSError:
            return ""
        return ""

    def _is_detached_venv_site_packages(resolved: str) -> bool:
        # Detached-venv layout: the host runs a standalone
        # interpreter (``sys.executable``) and activates a *second* venv purely
        # via PYTHONPATH. That venv's site-packages is never in the running
        # interpreter's own sysconfig paths, so the set check above misses it
        # and would strip the directory where mempalace/chromadb actually live,
        # breaking ``import mempalace``'s later dependency imports. Preserve
        # any leaked entry that is a venv's site-packages and whose venv is a
        # different environment from the one we are running in.
        #
        # "Different environment" means the venv root (first ancestor with a
        # pyvenv.cfg) is not the running interpreter's own prefix, and the
        # venv's pyvenv.cfg names a base interpreter other than the running
        # one. Comparing against the running prefix is what keeps the foreign
        # same-prefix cases (a sibling lib/pythonX.Y, a nested venv) stripped,
        # while a genuine separate venv is preserved.
        if not resolved.endswith("site-packages"):
            return False
        running_prefixes = {_norm(p) for p in (sys.prefix, getattr(sys, "base_prefix", sys.prefix))}
        running_dir = _norm(os.path.dirname(sys.executable))
        cur = os.path.dirname(resolved)
        for _ in range(8):
            if os.path.isfile(os.path.join(cur, "pyvenv.cfg")):
                venv_root = _norm(cur)
                if venv_root in running_prefixes:
                    return False  # the running environment's own venv: let the set check decide
                home = _read_pyvenv_home(os.path.join(cur, "pyvenv.cfg"))
                return bool(home) and _norm(os.path.realpath(home)) != running_dir
            if cur == os.path.dirname(cur):
                break  # reached the filesystem root without finding a venv
            cur = os.path.dirname(cur)
        return False

    def _belongs_to_this_environment(path: str) -> bool:
        try:
            resolved = _norm(os.path.realpath(path))
        except OSError:
            return False
        return resolved in own_site_packages or _is_detached_venv_site_packages(resolved)

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
