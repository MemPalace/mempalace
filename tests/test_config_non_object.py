"""A config.json that is valid JSON but not an object is ignored, loudly (#2234).

JSON allows ``null``, arrays, strings, numbers and booleans at the top level.
``MempalaceConfig`` reads every setting through ``.get()``, so such a file
must fall back to defaults like one that does not parse, and the user has to
be told: the CLI, MCP server, hooks and miners otherwise run on defaults
(another palace path, another embedder) with no hint why.
"""

import json

import pytest

import mempalace.config as config_mod
from mempalace.config import MempalaceConfig

NON_OBJECTS = [
    ("null", "null"),
    ("[]", "array"),
    ('["unexpected", "array"]', "array"),
    ('"oops"', "string"),
    ("42", "number"),
    ("3.5", "number"),
    ("true", "boolean"),
    ("false", "boolean"),
]


@pytest.fixture(autouse=True)
def _fresh_warning_state(monkeypatch):
    monkeypatch.setattr(config_mod, "_WARNED_UNUSABLE_CONFIGS", set(), raising=False)
    for name in ("MEMPALACE_PALACE_PATH", "MEMPAL_PALACE_PATH", "MEMPALACE_BACKEND"):
        monkeypatch.delenv(name, raising=False)


def _settings(cfg):
    """Every public property of ``cfg``, as value or exception type name."""
    out = {}
    for name in sorted(dir(type(cfg))):
        if name.startswith("_") or not isinstance(getattr(type(cfg), name), property):
            continue
        try:
            out[name] = getattr(cfg, name)
        except Exception as exc:  # noqa: BLE001 - recorded and compared below
            out[name] = f"raised {type(exc).__name__}"
    return out


@pytest.mark.parametrize("text, json_type", NON_OBJECTS)
def test_non_object_config_reads_as_empty(tmp_path, text, json_type):
    """Each setting resolves exactly as it would from ``{}``; none raises."""
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    (empty_dir / "config.json").write_text("{}\n", encoding="utf-8")
    bad_dir = tmp_path / "bad"
    bad_dir.mkdir()
    (bad_dir / "config.json").write_text(text + "\n", encoding="utf-8")

    expected = _settings(MempalaceConfig(config_dir=empty_dir))
    actual = _settings(MempalaceConfig(config_dir=bad_dir))

    # config_dir / paths derived from it legitimately differ between the two.
    differing = {
        k for k in expected if expected[k] != actual[k] and str(empty_dir) not in str(expected[k])
    }
    assert differing == set(), {k: (expected[k], actual[k]) for k in differing}
    assert not [k for k, v in actual.items() if str(v).startswith("raised Attribute")]


@pytest.mark.parametrize("text, json_type", NON_OBJECTS)
def test_non_object_config_warns_once_and_is_not_rewritten(tmp_path, capsys, text, json_type):
    config_file = tmp_path / "config.json"
    config_file.write_text(text + "\n", encoding="utf-8")
    original = config_file.read_bytes()

    MempalaceConfig(config_dir=tmp_path).palace_path
    err = capsys.readouterr().err
    assert str(config_file) in err
    assert f"is a JSON {json_type}, not an object" in err
    assert "using default settings" in err

    MempalaceConfig(config_dir=tmp_path).palace_path
    assert capsys.readouterr().err == "", "the warning repeated for the same file"

    assert config_file.read_bytes() == original, "loading must not rewrite the file"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["config.json"]


def test_unparseable_config_warns_on_load(tmp_path, capsys):
    config_file = tmp_path / "config.json"
    config_file.write_text('{"palace_path": "/mnt/data/palace", "hoo', encoding="utf-8")

    MempalaceConfig(config_dir=tmp_path)

    err = capsys.readouterr().err
    assert f"{config_file} does not parse; ignoring it" in err


def test_object_config_is_quiet(tmp_path, capsys):
    (tmp_path / "config.json").write_text(json.dumps({"palace_path": "/x"}), encoding="utf-8")
    assert MempalaceConfig(config_dir=tmp_path).palace_path == "/x"
    assert capsys.readouterr().err == ""


def test_setter_names_the_json_type_when_it_keeps_the_file(tmp_path, capsys):
    config_file = tmp_path / "config.json"
    config_file.write_text("[]\n", encoding="utf-8")

    cfg = MempalaceConfig(config_dir=tmp_path)
    capsys.readouterr()
    cfg.set_hook_setting("daemon", True)

    err = capsys.readouterr().err
    assert "is a JSON array, not an object; kept it as" in err
    assert json.loads(config_file.read_text(encoding="utf-8")) == {"hooks": {"daemon": True}}
    kept = [p for p in tmp_path.iterdir() if p.name != "config.json"]
    assert len(kept) == 1 and kept[0].read_text(encoding="utf-8") == "[]\n"
