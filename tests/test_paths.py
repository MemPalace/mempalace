"""State and config paths resolve the same way they did before paths.py.

The reference functions below are the expressions the call sites used before
they moved to ``mempalace.paths``; the matrix pins that every helper returns
exactly what those expressions returned.
"""

import ast
import hashlib
import os
from pathlib import Path

import pytest

from mempalace import paths

MEMPALACE = Path(__file__).resolve().parents[1] / "mempalace"


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("MEMPALACE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    return home


def _old_config_dir():
    env_dir = os.environ.get("MEMPALACE_CONFIG_DIR")
    if env_dir and env_dir.strip():
        return Path(env_dir).expanduser()
    legacy = Path.home() / ".mempalace"
    if legacy.is_dir() and (
        any((legacy / m).is_file() for m in ("config.json", "people_map.json"))
        or (legacy / "palace" / "chroma.sqlite3").is_file()
    ):
        return legacy
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg and xdg.strip():
        xdg_path = Path(xdg).expanduser()
        if xdg_path.is_absolute():
            return xdg_path / "mempalace"
    return Path.home() / ".config" / "mempalace"


def _old_daemon_key(palace_path):
    canonical = os.path.abspath(os.path.realpath(os.path.expanduser(palace_path)))
    return hashlib.sha256(os.path.normcase(canonical).encode("utf-8")).hexdigest()[:24]


def _old_lock_key(palace_path):
    resolved = os.path.realpath(os.path.expanduser(palace_path))
    return hashlib.sha256(os.path.normcase(resolved).encode()).hexdigest()[:16]


@pytest.mark.parametrize(
    "setup",
    [
        "none",
        "xdg",
        "xdg_relative",
        "xdg_tilde",
        "override",
        "override_tilde",
        "legacy_config",
        "legacy_palace",
        "legacy_empty",
    ],
)
def test_config_dir_matches_the_old_resolution(home, monkeypatch, tmp_path, setup):
    legacy = home / ".mempalace"
    if setup == "xdg":
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    elif setup == "xdg_relative":
        monkeypatch.setenv("XDG_CONFIG_HOME", "relative/xdg")
    elif setup == "xdg_tilde":
        monkeypatch.setenv("XDG_CONFIG_HOME", "~/xdg")
    elif setup == "override":
        monkeypatch.setenv("MEMPALACE_CONFIG_DIR", str(tmp_path / "override"))
    elif setup == "override_tilde":
        monkeypatch.setenv("MEMPALACE_CONFIG_DIR", "~/alt")
    elif setup == "legacy_config":
        legacy.mkdir()
        (legacy / "config.json").write_text("{}", encoding="utf-8")
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    elif setup == "legacy_palace":
        (legacy / "palace").mkdir(parents=True)
        (legacy / "palace" / "chroma.sqlite3").write_bytes(b"")
    elif setup == "legacy_empty":
        (legacy / "palace").mkdir(parents=True)
    assert paths.config_dir() == _old_config_dir()


def test_state_paths_match_the_old_expressions(home):
    assert paths.state_home() == Path.home() / ".mempalace"
    assert paths.state_path("hook_state") == Path.home() / ".mempalace" / "hook_state"
    assert paths.state_path("server", "k") == Path.home() / ".mempalace" / "server" / "k"
    assert (
        paths.state_path("watch", "a.json", home=str(home))
        == Path(str(home)) / ".mempalace" / "watch" / "a.json"
    )
    assert paths.lock_dir() == os.path.join(os.path.expanduser("~"), ".mempalace", "locks")
    assert str(paths.state_path("known_entities.json")) == os.path.join(
        os.path.expanduser("~"), ".mempalace", "known_entities.json"
    )
    assert paths.default_palace_path() == os.path.expanduser("~/.mempalace/palace")
    assert paths.state_str("knowledge_graph.sqlite3") == os.path.expanduser(
        "~/.mempalace/knowledge_graph.sqlite3"
    )
    assert paths.state_str("identity.txt") == os.path.expanduser("~/.mempalace/identity.txt")
    assert paths.display_state_path("palace") == "~/.mempalace/palace"


@pytest.mark.parametrize(
    "raw", ["~/palace", "relative/palace", "MixedCase/Palace", "/tmp/../tmp/palace"]
)
def test_palace_keys_match_the_old_derivations(home, raw):
    assert paths.palace_key(raw) == _old_daemon_key(raw)
    assert paths.palace_key(raw, length=16) == _old_lock_key(raw)


def _path_literal_violations(source):
    hits = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            value = node.value
            if value == paths.DIRNAME or value.startswith(
                ("~/.mempalace", "~\\.mempalace", ".mempalace/")
            ):
                hits.append((node.lineno, value))
    return hits


def test_no_hand_built_mempalace_paths_outside_paths_module():
    offenders = []
    for path in sorted(MEMPALACE.rglob("*.py")):
        if path.name == "paths.py" and path.parent == MEMPALACE:
            continue
        for lineno, value in _path_literal_violations(path.read_text(encoding="utf-8")):
            offenders.append(f"{path.relative_to(MEMPALACE.parent)}:{lineno}: {value!r}")
    assert not offenders, "build MemPalace paths through mempalace.paths:\n" + "\n".join(offenders)
