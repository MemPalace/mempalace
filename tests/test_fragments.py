"""The split packages load their fragments through the bytecode cache."""

import importlib.machinery
import os
import subprocess
import sys
from pathlib import Path

from mempalace._fragments import fragment_code


def test_fragment_is_compiled_once_and_recompiled_when_its_source_changes(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "dont_write_bytecode", False)
    source = tmp_path / "cmd_demo.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    compiled = []
    compile_source = importlib.machinery.SourceFileLoader.source_to_code

    def counting(self, data, path, **kwargs):
        compiled.append(path)
        return compile_source(self, data, path, **kwargs)

    monkeypatch.setattr(importlib.machinery.SourceFileLoader, "source_to_code", counting)

    namespace = {}
    exec(fragment_code("demo", source), namespace)
    exec(fragment_code("demo", source), namespace)
    assert namespace["VALUE"] == 1
    assert len(compiled) == 1  # the second load read __pycache__

    source.write_text("VALUE = 22  # longer, so the cached size no longer matches\n", "utf-8")
    exec(fragment_code("demo", source), namespace)
    assert namespace["VALUE"] == 22
    assert len(compiled) == 2


def test_fragment_code_keeps_the_source_path_for_tracebacks(tmp_path):
    source = tmp_path / "cmd_demo.py"
    source.write_text("def boom():\n    raise ValueError('x')\n", encoding="utf-8")
    assert fragment_code("demo", source).co_filename == str(source)


def test_split_packages_start_without_compiling_their_fragments():
    """After one warm-up start, importing the CLI compiles no mempalace source."""
    root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(root) + os.pathsep + env.get("PYTHONPATH", "")
    env.pop("PYTHONDONTWRITEBYTECODE", None)
    script = """
import builtins, sys
compiled = []
real = builtins.compile
def spy(source, filename, *args, **kwargs):
    if "mempalace" in str(filename):
        compiled.append(str(filename))
    return real(source, filename, *args, **kwargs)
builtins.compile = spy
import mempalace.cli, mempalace.palace, mempalace.searcher, mempalace.mcp_server
assert mempalace.cli.main and mempalace.palace.get_collection
if sys.argv[1] == "check":
    assert not compiled, compiled
"""
    subprocess.check_call([sys.executable, "-c", script, "warm"], env=env)
    subprocess.check_call([sys.executable, "-c", script, "check"], env=env)


def test_the_http_opener_is_built_on_first_use():
    root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(root) + os.pathsep + env.get("PYTHONPATH", "")
    script = "import sys, mempalace._net; assert mempalace._net._opener is None"
    subprocess.check_call([sys.executable, "-c", script], env=env)
