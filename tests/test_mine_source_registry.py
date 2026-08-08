"""RFC 002 source-adapter CLI seam: ``mine --source NAME`` routes through the
registry and files adapter-authored ``DrawerRecord``s via ``PalaceContext``.

This covers the seam wired into ``cmd_mine`` — the registry/ingest-loop path is
opt-in and leaves the legacy ``--mode`` dispatch untouched.
"""

from __future__ import annotations

import subprocess
import sys
from argparse import Namespace
from typing import Iterator

import pytest

from mempalace.sources.base import (
    AdapterSchema,
    BaseSourceAdapter,
    DrawerRecord,
    FieldSpec,
    IngestResult,
    SourceItemMetadata,
    SourceRef,
)
from mempalace.sources.registry import register, reset_adapters, unregister


class _FakeAdapter(BaseSourceAdapter):
    name = "fake-test"
    adapter_version = "0.1.0"
    capabilities = frozenset({"byte_preserving"})
    supported_modes = frozenset({"whole_record"})
    declared_transformations = frozenset()
    default_privacy_class = "internal"

    def ingest(self, *, source: SourceRef, palace) -> Iterator[IngestResult]:
        yield SourceItemMetadata(source_file="fake://item/1", version="v1")
        yield DrawerRecord(
            content="the operator said the verb leads",
            source_file="fake://item/1",
            metadata={"wing": "wing_test", "room": "general", "lar_demo": "1"},
        )

    def describe_schema(self) -> AdapterSchema:
        return AdapterSchema(
            version="1.0",
            fields={"lar_demo": FieldSpec(type="string", required=False, description="demo field")},
        )


@pytest.fixture
def _fake_adapter():
    register("fake-test", _FakeAdapter)
    yield
    reset_adapters()
    unregister("fake-test")


def test_mine_source_files_pre_annotated_record(tmp_dir, palace_path, _fake_adapter):
    from mempalace.cli import _mine_via_source_adapter
    from mempalace.palace import get_collection

    args = Namespace(source="fake-test", dir=tmp_dir, wing=None, dry_run=False)
    _mine_via_source_adapter(args, palace_path)

    col = get_collection(palace_path, create=True)
    got = col.get(where={"source_file": "fake://item/1"})
    assert got["documents"] == ["the operator said the verb leads"]
    meta = got["metadatas"][0]
    assert meta["lar_demo"] == "1"  # adapter-authored metadata lands verbatim
    assert meta["adapter_name"] == "fake-test"  # stamped by PalaceContext, not by the adapter
    assert meta["adapter_version"] == "0.1.0"


def test_mine_source_dry_run_files_nothing(tmp_dir, palace_path, _fake_adapter):
    from mempalace.cli import _mine_via_source_adapter
    from mempalace.palace import get_collection

    args = Namespace(source="fake-test", dir=tmp_dir, wing=None, dry_run=True)
    _mine_via_source_adapter(args, palace_path)

    col = get_collection(palace_path, create=True)
    assert col.get(where={"source_file": "fake://item/1"})["ids"] == []


def test_mine_source_unknown_adapter_exits(tmp_dir, palace_path):
    from mempalace.cli import _mine_via_source_adapter

    args = Namespace(source="does-not-exist", dir=tmp_dir, wing=None, dry_run=False)
    with pytest.raises(SystemExit):
        _mine_via_source_adapter(args, palace_path)


def _run_mine(*argv: str):
    """Invoke the real CLI so the assertions read the parser users actually meet."""
    return subprocess.run(
        [sys.executable, "-m", "mempalace.cli", "mine", *argv],
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_mine_source_and_explicit_mode_are_mutually_exclusive():
    """`--source` and an explicitly supplied `--mode` cannot both apply.

    All three spellings must fail alike. argparse counts an option as "seen" for exclusivity
    only when its value differs from its default, so a `--mode` default of "projects" would
    admit `--mode projects` here while rejecting `--mode convos`; the parser carries no
    default, so the three behave the same.
    """
    for mode in ("projects", "convos", "extract"):
        out = _run_mine("/tmp/does-not-matter", "--source", "fake-test", "--mode", mode)
        assert out.returncode == 2, f"--mode {mode} alongside --source should fail the parser"
        assert "not allowed with" in out.stderr


def test_mine_help_documents_source_as_the_extension_path():
    out = _run_mine("--help")
    assert out.returncode == 0
    assert "--source" in out.stdout and "RFC 002" in out.stdout
