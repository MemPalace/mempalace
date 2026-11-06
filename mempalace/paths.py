"""Where MemPalace keeps its own files.

Two roots, kept apart on purpose:

* The **config directory** follows the XDG Base Directory Specification with
  back-compat for existing installs:

    1. ``$MEMPALACE_CONFIG_DIR`` if set (explicit override, used by tests and CI)
    2. ``~/.mempalace`` if it already holds a real install
    3. ``$XDG_CONFIG_HOME/mempalace`` if ``XDG_CONFIG_HOME`` is set and absolute
    4. ``~/.config/mempalace``

* The **state home** is always ``~/.mempalace``: hook state, locks, hub and
  daemon state, watch cursors, the entity registries and the legacy defaults
  live there whatever the config directory resolves to.

``DIRNAME`` is also the name of the metadata directory MemPalace writes inside
a project or palace (``<palace>/.mempalace/origin.json``).

Keep this module import-light: hooks import it on their startup budget.
"""

import os
from pathlib import Path
from typing import Optional

DIRNAME = ".mempalace"

# Files that mark ~/.mempalace as a real legacy install rather than an
# empty or leftover directory. Any one is enough.
_LEGACY_MARKERS = ("config.json", "people_map.json")


def state_home(home: Optional[str] = None) -> Path:
    """``~/.mempalace`` (or ``<home>/.mempalace`` when ``home`` is given)."""
    base = Path(home) if home is not None else Path.home()
    return base / DIRNAME


def state_path(*parts: str, home: Optional[str] = None) -> Path:
    """A path under the state home."""
    return state_home(home).joinpath(*parts)


def state_str(*parts: str) -> str:
    """``~/.mempalace/<parts>`` expanded exactly as ``os.path.expanduser`` does.

    Some long-standing defaults were built that way (forward slashes kept on
    Windows); this keeps their strings identical.
    """
    return os.path.expanduser("/".join(("~", DIRNAME) + parts))


def display_state_path(*parts: str) -> str:
    """``~/.mempalace/<parts>`` unexpanded, for defaults shown to users."""
    return "/".join(("~", DIRNAME) + parts)


def default_palace_path() -> str:
    """The legacy default palace location, ``~/.mempalace/palace``."""
    return state_str("palace")


KNOWLEDGE_GRAPH_FILENAME = "knowledge_graph.sqlite3"


def legacy_knowledge_graph_path() -> str:
    """``~/.mempalace/knowledge_graph.sqlite3``, the legacy default palace's graph."""
    return state_str(KNOWLEDGE_GRAPH_FILENAME)


def historical_knowledge_graph_path(
    palace_path: str, legacy: Optional[str] = None, explicit: bool = False
) -> Optional[str]:
    """The graph earlier releases used for ``palace_path``, when not its own file.

    A caller's own older layout (``legacy``) first. Otherwise
    ``~/.mempalace/knowledge_graph.sqlite3``, which served the legacy default
    palace and every palace selected through ``MEMPALACE_PALACE_PATH`` or
    ``config.json``; a palace named with ``--palace`` (``explicit``) always
    kept its graph inside itself.
    """
    if legacy is not None:
        return legacy
    if not explicit or same_path(palace_path, default_palace_path()):
        return legacy_knowledge_graph_path()
    return None


def knowledge_graph_has_rows(path: str) -> bool:
    """True when the graph at ``path`` holds an entity or a triple.

    A file that exists but cannot be read counts as holding rows, so it is
    never passed over for another graph.
    """
    if not os.path.isfile(path):
        return False
    import sqlite3

    try:
        conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    except sqlite3.Error:
        return True
    try:
        for table in ("triples", "entities"):
            try:
                if conn.execute(f"SELECT EXISTS (SELECT 1 FROM {table})").fetchone()[0]:
                    return True
            except sqlite3.OperationalError as exc:
                if "no such table" not in str(exc):
                    return True
        return False
    except sqlite3.Error:
        return True
    finally:
        conn.close()


def knowledge_graph_path(
    palace_path: str, legacy: Optional[str] = None, explicit: bool = False
) -> str:
    """The knowledge graph that belongs to ``palace_path``, for reads and writes.

    The palace-local ``knowledge_graph.sqlite3`` when it holds facts. Otherwise
    the graph this palace used before (see
    :func:`historical_knowledge_graph_path`) when that one holds facts, so its
    facts never split across two files. Otherwise the palace-local path.
    Graphs are never merged, and no file is created here.
    """
    local = os.path.join(palace_path, KNOWLEDGE_GRAPH_FILENAME)
    other = historical_knowledge_graph_path(palace_path, legacy=legacy, explicit=explicit)
    if (
        other is not None
        and os.path.isfile(other)
        and not same_path(other, local)
        and not knowledge_graph_has_rows(local)
        and knowledge_graph_has_rows(other)
    ):
        return other
    return local


def same_path(a: str, b: str) -> bool:
    """True when ``a`` and ``b`` name the same location."""
    return os.path.normcase(canonical_palace_path(a)) == os.path.normcase(canonical_palace_path(b))


def lock_dir() -> str:
    """Directory holding the per-file and per-palace mine locks."""
    return str(state_path("locks"))


def _has_legacy_install(legacy: Path) -> bool:
    """True if ``legacy`` is a real install, not an empty directory.

    A bare ``palace`` directory created by another tool must not hijack the
    XDG path, so the palace only counts when it holds a ChromaDB store.
    """
    if any((legacy / marker).is_file() for marker in _LEGACY_MARKERS):
        return True
    return (legacy / "palace" / "chroma.sqlite3").is_file()


def config_dir() -> Path:
    """The config directory; see the module docstring for the order."""
    env_dir = os.environ.get("MEMPALACE_CONFIG_DIR")
    if env_dir and env_dir.strip():
        return Path(env_dir).expanduser()

    legacy = state_home()
    if legacy.is_dir() and _has_legacy_install(legacy):
        return legacy

    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg and xdg.strip():
        xdg_path = Path(xdg).expanduser()
        # Per the XDG spec, relative paths are invalid and ignored.
        if xdg_path.is_absolute():
            return xdg_path / "mempalace"

    return Path.home() / ".config" / "mempalace"


def canonical_palace_path(palace_path: str) -> str:
    """Absolute, symlink-resolved form of ``palace_path``."""
    return os.path.abspath(os.path.realpath(os.path.expanduser(palace_path)))


def palace_key(palace_path: str, length: int = 24) -> str:
    """Stable per-palace key used to name hub, daemon and lock state."""
    import hashlib

    normalized = os.path.normcase(canonical_palace_path(palace_path))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:length]
