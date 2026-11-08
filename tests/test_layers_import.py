"""Import-time budget for the wake-up stack."""

import os
import subprocess
import sys
from pathlib import Path


def test_importing_layers_does_not_import_chromadb():
    root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(root) + os.pathsep + env.get("PYTHONPATH", "")
    script = """
import sys
import mempalace.layers
banned = ("chromadb", "mempalace.searcher", "mempalace.backends", "mempalace.palace")
loaded = [
    name
    for name in banned
    if name in sys.modules or any(mod.startswith(name + ".") for mod in sys.modules)
]
assert not loaded, loaded
"""
    subprocess.check_call([sys.executable, "-c", script], env=env)
