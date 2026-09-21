"""test_encoding_hardening.py — UTF-8 round-trip for files MemPalace owns.

Regression coverage for finding #51 (dialect.py open() calls omit
encoding=) and #84 (config.py reads config.json via the OS locale codepage
while writing it as UTF-8).

The real defect is an ASYMMETRY: MemPalace writes JSON as UTF-8 (config.py's
save paths already pin encoding="utf-8"), but the read paths omit encoding=,
so on a non-UTF-8-locale process (German Windows = cp1252) the UTF-8 bytes are
decoded as cp1252 -> mojibake. We reproduce this deterministically on any
platform by:
  1. writing the file as real UTF-8 *bytes* on disk (what MemPalace does), and
  2. forcing encoding-less text opens to default to cp1252 (what a German
     Windows process does).
A read path that pins encoding="utf-8" survives; one that relies on the locale
default corrupts the umlaut. Each test asserts the umlaut round-trips.
"""

import builtins
import json

import pytest

from mempalace.config import MempalaceConfig
from mempalace.dialect import Dialect

UMLAUT_NAME = "Müller"


def _write_utf8_bytes(path, obj):
    """Write JSON as real UTF-8 bytes, bypassing any text-mode default."""
    path.write_bytes(json.dumps(obj, ensure_ascii=False).encode("utf-8"))


@pytest.fixture
def cp1252_default_open(monkeypatch):
    """Force encoding-less TEXT opens to use cp1252 (German Windows default).

    Binary opens and opens that pin encoding="utf-8" are left untouched, so
    only code that relies on the locale default is affected — exactly the
    defect under test.
    """
    real_open = builtins.open

    def fake_open(file, mode="r", buffering=-1, encoding=None, *args, **kwargs):
        if "b" not in mode and encoding is None:
            encoding = "cp1252"
        return real_open(file, mode, buffering, encoding, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", fake_open)
    return fake_open


class TestDialectConfigEncoding:
    def test_from_config_reads_utf8_bytes_under_cp1252(self, tmp_path, cp1252_default_open):
        """UTF-8 file on disk must decode correctly even under a cp1252 default."""
        cfg = tmp_path / "dialect.json"
        _write_utf8_bytes(cfg, {"entities": {UMLAUT_NAME: "MUE"}, "skip_names": []})

        loaded = Dialect.from_config(str(cfg))
        assert UMLAUT_NAME in loaded.entity_codes, (
            f"umlaut name lost/mangled: {list(loaded.entity_codes)!r}"
        )

    def test_from_config_reads_raw_utf8_skip_name(self, tmp_path, cp1252_default_open):
        """A raw-UTF-8 (non-escaped) umlaut in skip_names must decode correctly.

        Unlike save_config's ensure_ascii=True output (which escapes umlauts to
        codec-agnostic ASCII), this writes the umlaut as raw UTF-8 bytes — the
        case a hand-edited config or a non-mempalace writer produces. Without the
        read-path encoding fix it decodes as cp1252 and the umlaut is mangled, so
        this test genuinely fails pre-fix (it is not a passes-either-way check).
        """
        cfg = tmp_path / "dialect.json"
        _write_utf8_bytes(cfg, {"entities": {}, "skip_names": [UMLAUT_NAME]})

        loaded = Dialect.from_config(str(cfg))
        # skip_names are normalized to lowercase on load; assert the umlaut
        # survived the READ (u -> ü), independent of that casing.
        assert UMLAUT_NAME.lower() in loaded.skip_names, (
            f"skip_name umlaut mangled on read: {loaded.skip_names!r}"
        )


class TestMempalaceConfigEncoding:
    def test_config_json_umlaut_reads_back_under_cp1252(self, tmp_path, cp1252_default_open):
        """config.json written UTF-8 must read back UTF-8, not via cp1252."""
        cfg_file = tmp_path / "config.json"
        _write_utf8_bytes(cfg_file, {"people_map": {"Mueller": UMLAUT_NAME}})

        conf = MempalaceConfig(config_dir=str(tmp_path))
        assert conf._file_config.get("people_map", {}).get("Mueller") == UMLAUT_NAME, (
            f"config.json read as cp1252: {conf._file_config!r}"
        )


def _write_cp1252_bytes(path, obj):
    """Write legacy Windows cp1252 JSON bytes for migration-path tests."""
    path.write_bytes(json.dumps(obj, ensure_ascii=False).encode("cp1252"))


class TestLegacyCodepageMigration:
    def test_config_json_legacy_cp1252_is_ignored_instead_of_crashing(self, tmp_path):
        """Legacy non-UTF-8 config.json must follow the existing invalid-config fallback."""
        _write_cp1252_bytes(
            tmp_path / "config.json",
            {"people_map": {"Mueller": UMLAUT_NAME}},
        )

        conf = MempalaceConfig(config_dir=str(tmp_path))

        assert conf._file_config == {}

    def test_people_map_legacy_cp1252_falls_back_instead_of_crashing(self, tmp_path):
        """Legacy non-UTF-8 people_map.json must fall back instead of raising."""
        _write_cp1252_bytes(
            tmp_path / "people_map.json",
            {"Mueller": UMLAUT_NAME},
        )

        conf = MempalaceConfig(config_dir=str(tmp_path))

        assert conf.people_map == {}

    def test_dialect_legacy_cp1252_reports_utf8_migration_error(self, tmp_path):
        """Hand-edited legacy config should fail with an actionable UTF-8 message."""
        cfg = tmp_path / "dialect.json"
        _write_cp1252_bytes(
            cfg,
            {"entities": {UMLAUT_NAME: "MUE"}, "skip_names": []},
        )

        with pytest.raises(
            ValueError,
            match=r"not valid UTF-8.*re-save it as UTF-8",
        ):
            Dialect.from_config(str(cfg))
