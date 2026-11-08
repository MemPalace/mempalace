"""``mempalace init`` never writes through a link planted in the project.

The project is a checkout the user may not control. ``mempalace.yaml``,
``entities.json`` and ``.gitignore`` shipped as symbolic links would, with
``exists()``/``is_file()`` guards, be written through to wherever they point
(a dangling link even creates the target). The writers decide by ``lstat``
and refuse anything that is not a regular file or absent.
"""

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from mempalace.cli import _ensure_mempalace_files_gitignored, _save_project_entities
from mempalace.fsio import is_regular_or_absent
from mempalace.room_detector_local import save_config

ROOMS = [{"name": "docs", "description": "d", "keywords": ["doc"], "folders": ["docs"]}]


def _symlink(target: Path, link: Path) -> None:
    try:
        os.symlink(str(target), str(link))
    except (OSError, NotImplementedError) as exc:  # Windows without the privilege
        pytest.skip(f"cannot create symlinks here: {exc}")


@pytest.fixture
def victim(tmp_path):
    """A file outside the project standing in for ``~/.bashrc``."""
    path = tmp_path / "home" / "victim.txt"
    path.parent.mkdir()
    path.write_text("precious\n", encoding="utf-8")
    return path


@pytest.fixture
def project(tmp_path):
    path = tmp_path / "project"
    path.mkdir()
    (path / "docs").mkdir()
    return path


def test_is_regular_or_absent(tmp_path, victim):
    assert is_regular_or_absent(tmp_path / "absent") is True
    assert is_regular_or_absent(victim) is True
    assert is_regular_or_absent(tmp_path) is False
    link = tmp_path / "link"
    _symlink(victim, link)
    assert is_regular_or_absent(link) is False
    dangling = tmp_path / "dangling"
    _symlink(tmp_path / "nowhere", dangling)
    assert is_regular_or_absent(dangling) is False


class TestSaveConfig:
    def test_refuses_a_linked_config(self, project, victim):
        _symlink(victim, project / "mempalace.yaml")
        with pytest.raises(OSError, match="not a regular file"):
            save_config(str(project), "wing", ROOMS)
        assert victim.read_text(encoding="utf-8") == "precious\n"

    def test_refuses_a_dangling_link(self, project, tmp_path):
        target = tmp_path / "home" / "created_by_init"
        target.parent.mkdir(exist_ok=True)
        _symlink(target, project / "mempalace.yaml")
        with pytest.raises(OSError, match="not a regular file"):
            save_config(str(project), "wing", ROOMS)
        assert not target.exists()

    def test_still_writes_a_regular_config(self, project):
        save_config(str(project), "wing", ROOMS)
        text = (project / "mempalace.yaml").read_text(encoding="utf-8")
        assert "wing: wing" in text
        assert not (project / "mempalace.yaml").is_symlink()


class TestSaveProjectEntities:
    def test_refuses_a_linked_entities_file(self, project, victim, capsys):
        _symlink(victim, project / "entities.json")
        _save_project_entities(project / "entities.json", {"people": ["Ann"], "projects": []})
        assert victim.read_text(encoding="utf-8") == "precious\n"
        assert "not a regular file" in capsys.readouterr().err

    def test_refuses_a_dangling_link(self, project, tmp_path, capsys):
        target = tmp_path / "home" / "created_by_init"
        target.parent.mkdir(exist_ok=True)
        _symlink(target, project / "entities.json")
        _save_project_entities(project / "entities.json", {"people": ["Ann"], "projects": []})
        assert not target.exists()

    def test_still_writes_a_regular_file(self, project, capsys):
        path = project / "entities.json"
        _save_project_entities(path, {"people": ["Ann"], "projects": ["X"]})
        assert json.loads(path.read_text(encoding="utf-8")) == {
            "people": ["Ann"],
            "projects": ["X"],
        }
        assert "Entities saved" in capsys.readouterr().out


class TestGitignore:
    @staticmethod
    def _git_init(path: Path) -> None:
        subprocess.run(["git", "init", "-q", str(path)], check=True)

    def test_leaves_a_linked_gitignore_alone(self, project, victim):
        self._git_init(project)
        _symlink(victim, project / ".gitignore")
        assert _ensure_mempalace_files_gitignored(str(project)) is False
        assert victim.read_text(encoding="utf-8") == "precious\n"

    def test_leaves_a_dangling_link_alone(self, project, tmp_path):
        self._git_init(project)
        target = tmp_path / "home" / "created_by_init"
        target.parent.mkdir(exist_ok=True)
        _symlink(target, project / ".gitignore")
        assert _ensure_mempalace_files_gitignored(str(project)) is False
        assert not target.exists()

    def test_still_appends_to_a_regular_gitignore(self, project):
        self._git_init(project)
        (project / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
        assert _ensure_mempalace_files_gitignored(str(project)) is True
        text = (project / ".gitignore").read_text(encoding="utf-8")
        assert "node_modules/" in text and "entities.json" in text


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_init_writers_keep_a_locked_down_file_locked_down(tmp_path):
    """Refusing links must not cost the user the 0600 they put on these files."""
    config, entities = tmp_path / "mempalace.yaml", tmp_path / "entities.json"
    config.write_text("wing: review\nrooms: []\n", encoding="utf-8")
    entities.write_text("{}", encoding="utf-8")
    for path in (config, entities):
        path.chmod(0o600)
    old = os.umask(0o022)
    try:
        save_config(str(tmp_path), "review", ROOMS)
        _save_project_entities(entities, {"people": ["synthetic"]})
    finally:
        os.umask(old)
    for path in (config, entities):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, path.name
    assert json.loads(entities.read_text(encoding="utf-8")) == {"people": ["synthetic"]}
