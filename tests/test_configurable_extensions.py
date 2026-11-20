"""Configurable mineable file extensions (MemPalace/mempalace#2931).

``include_extensions`` / ``exclude_extensions`` work at three layers —
command-line flags, project ``mempalace.yaml``, machine-wide
``config.json`` — additively on top of the built-in
``READABLE_EXTENSIONS`` list, with the most specific layer winning per
suffix. These tests pin the normalization, the precedence rules, and the
wiring through ``scan_project`` / ``mine``.
"""

import json

import pytest
import yaml

from mempalace.config import (
    MempalaceConfig,
    normalize_extension_list,
    normalize_file_extension,
)
from mempalace.miner import (
    READABLE_EXTENSIONS,
    _filter_prescanned_by_extensions,
    effective_readable_extensions,
    mine,
    resolve_readable_extensions,
    scan_project,
)


def write_file(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (".mdx", ".mdx"),
        ("mdx", ".mdx"),  # leading dot optional
        ("  .MDX  ", ".mdx"),  # surrounding whitespace tolerated, lowercased
        (".R", ".r"),
    ],
)
def test_normalize_file_extension_valid(raw, expected):
    assert normalize_file_extension(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        ".",  # bare dot
        "mdx txt",  # inner whitespace — two extensions, not one
        "notes.md",  # a filename, not an extension
        "foo/bar",  # path separator
        5,
        None,
        [".mdx"],
    ],
)
def test_normalize_file_extension_invalid(raw):
    assert normalize_file_extension(raw) is None


def test_normalize_extension_list_dedupes_and_reports_invalid(capsys):
    normalized = normalize_extension_list(
        [".mdx", "mdx", "rst", 5, "bad value"], "test include_extensions"
    )
    assert normalized == [".mdx", ".rst"]
    err = capsys.readouterr().err
    assert "test include_extensions" in err
    assert "5" in err
    assert "bad value" in err


def test_normalize_extension_list_rejects_non_list(capsys):
    assert normalize_extension_list("mdx", "test include_extensions") == []
    assert "not a list" in capsys.readouterr().err
    assert normalize_extension_list(None, "test") == []


class TestResolveReadableExtensions:
    def test_no_layers_is_the_builtin_list(self):
        assert resolve_readable_extensions() == frozenset(READABLE_EXTENSIONS)

    def test_machine_include_adds_without_replacing(self):
        effective = resolve_readable_extensions(machine_include=[".mdx"])
        assert ".mdx" in effective
        assert ".py" in effective  # the base list survives untouched

    def test_machine_exclude_removes(self):
        effective = resolve_readable_extensions(machine_exclude=[".py"])
        assert ".py" not in effective
        assert ".md" in effective

    def test_project_beats_machine(self):
        effective = resolve_readable_extensions(
            project_include=[".mdx"],
            machine_exclude=[".mdx"],
        )
        assert ".mdx" in effective

        effective = resolve_readable_extensions(
            project_exclude=[".mdx"],
            machine_include=[".mdx"],
        )
        assert ".mdx" not in effective

    def test_cli_beats_project_and_machine(self):
        effective = resolve_readable_extensions(
            cli_include=[".mdx"],
            project_exclude=[".mdx"],
            machine_exclude=[".mdx"],
        )
        assert ".mdx" in effective

        effective = resolve_readable_extensions(
            cli_exclude=[".mdx"],
            project_include=[".mdx"],
            machine_include=[".mdx"],
        )
        assert ".mdx" not in effective

    def test_same_layer_exclude_wins_over_include(self):
        effective = resolve_readable_extensions(cli_include=[".mdx"], cli_exclude=[".mdx"])
        assert ".mdx" not in effective

    def test_unmentioned_suffix_falls_through_to_builtin(self):
        effective = resolve_readable_extensions(cli_include=[".mdx"], machine_exclude=[".py"])
        assert ".txt" in effective  # nobody mentioned it


class TestScanProjectWhitelist:
    @pytest.fixture
    def project(self, tmp_path):
        write_file(tmp_path / "app.py", "print('hello')\n")
        write_file(tmp_path / "doc.mdx", "# Verbatim docs\n")
        write_file(tmp_path / "notes.txt", "plain notes\n")
        return tmp_path

    def scanned(self, project, **kwargs):
        return sorted(p.name for p in scan_project(str(project), **kwargs))

    def test_builtin_default_skips_mdx(self, project):
        assert self.scanned(project) == ["app.py", "notes.txt"]

    def test_include_and_exclude_apply(self, project):
        effective = resolve_readable_extensions(cli_include=[".mdx"], cli_exclude=[".txt"])
        assert self.scanned(project, readable_extensions=effective) == [
            "app.py",
            "doc.mdx",
        ]

    def test_force_included_exact_path_bypasses_whitelist(self, project):
        effective = resolve_readable_extensions(cli_exclude=[".txt"])
        # notes.txt itself is force-included verbatim.
        assert self.scanned(
            project,
            readable_extensions=effective,
            include_ignored=["notes.txt"],
        ) == ["app.py", "notes.txt"]


def test_filter_prescanned_by_extensions(tmp_path):
    write_file(tmp_path / "app.py", "x = 1\n")
    write_file(tmp_path / "doc.mdx", "docs\n")
    files = [tmp_path / "app.py", tmp_path / "doc.mdx"]
    effective = resolve_readable_extensions()  # builtin only

    kept = _filter_prescanned_by_extensions(files, effective, tmp_path, [])
    assert kept == [tmp_path / "app.py"]

    # Exact force-includes keep bypassing the filter.
    kept = _filter_prescanned_by_extensions(files, effective, tmp_path, ["doc.mdx"])
    assert kept == files


class TestMachineConfigLayer:
    def test_include_and_exclude_keys(self, tmp_path):
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "config.json").write_text(
            json.dumps({"include_extensions": [".mdx", "MDX"], "exclude_extensions": ["py"]}),
            encoding="utf-8",
        )
        cfg = MempalaceConfig(config_dir=str(config_dir))
        assert cfg.include_extensions == [".mdx"]
        assert cfg.exclude_extensions == [".py"]

    def test_invalid_entries_reported_not_silently_dropped(self, tmp_path, capsys):
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "config.json").write_text(
            json.dumps({"include_extensions": ["notes.md", 7]}),
            encoding="utf-8",
        )
        cfg = MempalaceConfig(config_dir=str(config_dir))
        assert cfg.include_extensions == []
        err = capsys.readouterr().err
        assert "include_extensions" in err
        assert "notes.md" in err

    def test_non_list_value_is_reported(self, tmp_path, capsys):
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "config.json").write_text(
            json.dumps({"exclude_extensions": ".mdx"}),
            encoding="utf-8",
        )
        cfg = MempalaceConfig(config_dir=str(config_dir))
        assert cfg.exclude_extensions == []
        assert "not a list" in capsys.readouterr().err

    def test_unset_keys_are_empty(self, tmp_path):
        cfg = MempalaceConfig(config_dir=str(tmp_path))
        assert cfg.include_extensions == []
        assert cfg.exclude_extensions == []


def write_project_config(project_dir, config_dict):
    with open(project_dir / "mempalace.yaml", "w", encoding="utf-8") as f:
        yaml.dump(config_dict, f)


class TestEffectiveReadableExtensions:
    """All three layers resolved for one project directory."""

    def test_machine_and_project_layers_combine(self, tmp_path, monkeypatch):
        config_dir = tmp_path / "machine-config"
        config_dir.mkdir()
        (config_dir / "config.json").write_text(
            json.dumps({"include_extensions": [".mdx"], "exclude_extensions": ["py"]}),
            encoding="utf-8",
        )
        monkeypatch.setenv("MEMPALACE_CONFIG_DIR", str(config_dir))

        project = tmp_path / "project"
        project.mkdir()
        write_project_config(
            project,
            {
                "wing": "test_project",
                "include_extensions": ["rst"],  # leading dot optional in yaml
                "exclude_extensions": [".md"],
            },
        )

        effective = effective_readable_extensions(str(project))
        assert ".mdx" in effective  # machine include
        assert ".rst" in effective  # project include, dot-less value normalized
        assert ".py" not in effective  # machine exclude
        assert ".md" not in effective  # project exclude

    def test_project_exclude_beats_machine_include(self, tmp_path, monkeypatch):
        config_dir = tmp_path / "machine-config"
        config_dir.mkdir()
        (config_dir / "config.json").write_text(
            json.dumps({"include_extensions": [".mdx"]}),
            encoding="utf-8",
        )
        monkeypatch.setenv("MEMPALACE_CONFIG_DIR", str(config_dir))

        project = tmp_path / "project"
        project.mkdir()
        write_project_config(project, {"wing": "test_project", "exclude_extensions": [".mdx"]})

        assert ".mdx" not in effective_readable_extensions(str(project))

    def test_cli_beats_project_yaml(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MEMPALACE_CONFIG_DIR", str(tmp_path / "empty-config"))
        project = tmp_path / "project"
        project.mkdir()
        write_project_config(project, {"wing": "test_project", "include_extensions": [".mdx"]})

        effective = effective_readable_extensions(str(project), cli_exclude=[".mdx"])
        assert ".mdx" not in effective


class TestMineWiring:
    """mine() applies the layers end to end (dry run — no palace writes)."""

    @pytest.fixture(autouse=True)
    def isolated_config(self, tmp_path, monkeypatch):
        config_dir = tmp_path / "machine-config"
        config_dir.mkdir()
        (config_dir / "config.json").write_text(
            json.dumps({"include_extensions": [".machine-ext"]}),
            encoding="utf-8",
        )
        monkeypatch.setenv("MEMPALACE_CONFIG_DIR", str(config_dir))
        self.config_dir = config_dir

    def test_mine_honors_all_layers(self, tmp_path, capsys):
        project = tmp_path / "project"
        write_file(project / "app.py", "print('hello world')\n" * 10)
        write_file(project / "doc.mdx", "# Verbatim docs\n" * 10)
        write_file(project / "page.machine-ext", "machine-layer content\n" * 10)
        write_file(project / "old.rst", "restructured text\n" * 10)
        write_file(project / "notes.txt", "plain notes\n" * 10)
        write_project_config(project, {"wing": "test_project", "include_extensions": [".rst"]})

        mine(
            str(project),
            str(tmp_path / "palace"),
            dry_run=True,
            exclude_extensions=[".txt"],
        )
        out = capsys.readouterr().out
        # All four files were dry-run candidates: builtin .py, machine
        # .machine-ext, project .rst — but not .txt (CLI exclude) and not
        # the unconfigured anything-else.
        assert "app.py" in out
        assert "doc.mdx" not in out
        assert "page.machine-ext" in out
        assert "old.rst" in out
        assert "notes.txt" not in out
        # The header names the deltas from the built-in list.
        assert "-.txt" in out
        assert ".machine-ext" in out

    def test_mine_refilters_prescanned_files(self, tmp_path, capsys):
        project = tmp_path / "project"
        write_file(project / "app.py", "print('hello world')\n" * 10)
        write_file(project / "notes.txt", "plain notes\n" * 10)
        prescanned = scan_project(str(project))  # builtin whitelist: both files

        mine(
            str(project),
            str(tmp_path / "palace"),
            dry_run=True,
            exclude_extensions=[".txt"],
            files=prescanned,
        )
        out = capsys.readouterr().out
        assert "app.py" in out
        assert "notes.txt" not in out

    def test_mine_invalid_flag_values_are_reported(self, tmp_path, capsys):
        project = tmp_path / "project"
        write_file(project / "app.py", "print('hello world')\n" * 10)
        mine(
            str(project),
            str(tmp_path / "palace"),
            dry_run=True,
            include_extensions=["not an extension"],
        )
        err = capsys.readouterr().err
        assert "--include-extension" in err
        assert "not an extension" in err


class TestCliFlags:
    """The mine parser accepts the new flags like --include-ignored."""

    def test_flags_parse_and_repeat(self):
        from unittest.mock import patch

        from mempalace.cli import main as cli_main

        argv = [
            "mempalace",
            "mine",
            ".",
            "--include-extension",
            ".mdx",
            "--include-extension",
            ".rst,xlf",
            "--exclude-extension",
            ".json",
        ]
        with (
            patch("sys.argv", argv),
            patch("mempalace.cli.cmd_mine") as mock_cmd,
        ):
            cli_main()

        args = mock_cmd.call_args.args[0]
        assert args.include_extension == [".mdx", ".rst,xlf"]
        assert args.exclude_extension == [".json"]

    def test_flags_default_empty(self):
        from unittest.mock import patch

        from mempalace.cli import main as cli_main

        with (
            patch("sys.argv", ["mempalace", "mine", "."]),
            patch("mempalace.cli.cmd_mine") as mock_cmd,
        ):
            cli_main()

        args = mock_cmd.call_args.args[0]
        assert args.include_extension == []
        assert args.exclude_extension == []


def test_resolver_reads_the_single_builtin_list(monkeypatch):
    """The built-in list lives in exactly one place.

    Patching ``miner.READABLE_EXTENSIONS`` must change the resolver's base:
    if the resolver (or any config layer) kept its own copy of the default
    list, this test would see the stale original.
    """
    from mempalace import miner

    monkeypatch.setattr(miner, "READABLE_EXTENSIONS", frozenset({".txt"}))
    effective = miner.resolve_readable_extensions(machine_include=[".mdx"])
    assert set(effective) == {".txt", ".mdx"}
