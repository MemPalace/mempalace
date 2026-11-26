"""Load the source fragments a split package executes into its own namespace.

``mempalace.cli``, ``mempalace.palace``, ``mempalace.searcher`` and
``mempalace.mcp_server`` keep their historical single-module namespace by
executing their files into the package's globals. Compiling those files from
source on every start cost each ``mempalace`` process tens of milliseconds;
the import system's bytecode cache makes it a read.
"""

from importlib.machinery import SourceFileLoader
from pathlib import Path


def fragment_code(package: str, path: Path):
    """The code object for the fragment at ``path``, from ``__pycache__`` when fresh.

    Goes through :class:`~importlib.machinery.SourceFileLoader`, so the cache
    is checked against the source's mtime and size as for any module, written
    when the directory allows it, and skipped under ``-B`` or
    ``PYTHONDONTWRITEBYTECODE``.
    """
    name = f"{package}.{path.stem}"
    return SourceFileLoader(name, str(path)).get_code(name)
