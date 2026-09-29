"""
test_config_encoding.py — mempalace.yaml is UTF-8, not the platform default.

load_config() used to open the config with the platform default encoding.
On Windows (cp950) any project whose mempalace.yaml carries unescaped CJK —
e.g. ``description: Files from 20-專案/`` — raised UnicodeDecodeError and
killed the whole mine run. It went unnoticed for a day because the scheduled
mine sent stderr to /dev/null.

The fix has now been dropped twice by upstream merges, so it gets a test.
"""

from pathlib import Path

import pytest
import yaml

from mempalace.miner import load_config
from mempalace.room_detector_local import save_config


CJK_DESCRIPTION = "Files from 20-專案/"


def write_config(project_dir: Path, description: str = CJK_DESCRIPTION):
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "mempalace.yaml").write_text(
        yaml.dump(
            {
                "wing": "brain",
                "rooms": [{"name": "general", "description": description}],
            },
            default_flow_style=False,
            sort_keys=False,
            allow_unicode=True,  # the shape that actually broke: raw CJK bytes
        ),
        encoding="utf-8",
    )
    return project_dir


def test_load_config_reads_cjk_description(tmp_path):
    project_dir = write_config(tmp_path / "proj")

    config = load_config(str(project_dir))

    assert config["wing"] == "brain"
    assert config["rooms"][0]["description"] == CJK_DESCRIPTION


def test_load_config_does_not_depend_on_platform_encoding(tmp_path):
    """The file is UTF-8 on disk; load_config must not use the locale codec.

    GitHub Windows runners report preferred encoding cp1252, and Python
    UTF-8 mode makes bare ``open()`` succeed anyway. cp1252 also accepts
    these UTF-8 CJK bytes as mojibake, so a locale-default ``open()`` does
    not raise. Prove the on-disk bytes are UTF-8 (ascii and cp950 reject
    them) and that load_config still returns the original CJK string.
    """
    project_dir = write_config(tmp_path / "proj")
    raw = (project_dir / "mempalace.yaml").read_bytes()
    assert CJK_DESCRIPTION.encode("utf-8") in raw
    with pytest.raises(UnicodeDecodeError):
        raw.decode("ascii")
    with pytest.raises(UnicodeDecodeError):
        raw.decode("cp950")
    assert load_config(str(project_dir))["rooms"][0]["description"] == CJK_DESCRIPTION


def test_save_then_load_config_round_trips_cjk(tmp_path):
    # save_config writes the file that load_config later reads. Both ends have
    # to agree on UTF-8, or `mempalace init` on a CJK project produces a config
    # that the next `mempalace mine` cannot open.
    project_dir = tmp_path / "proj"
    project_dir.mkdir(parents=True, exist_ok=True)

    save_config(
        str(project_dir),
        "大腦風暴",
        [{"name": "general", "description": CJK_DESCRIPTION}],
    )

    config = load_config(str(project_dir))
    assert config["wing"] == "大腦風暴"
    assert config["rooms"][0]["description"] == CJK_DESCRIPTION
