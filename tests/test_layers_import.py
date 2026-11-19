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


def _run(script: str, *args: str) -> str:
    root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(root) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.check_output([sys.executable, "-c", script, *args], env=env, text=True)


def test_wake_up_reads_a_chroma_palace_without_loading_chromadb(tmp_path):
    """Wake-up shows recent drawers; the read is plain SQLite.

    Importing chromadb and building its client costs about a second, several
    times the 500 ms a session-start hook allows, and wake-up uses neither.
    """
    from mempalace.backends import ChromaBackend, PalaceRef

    palace = tmp_path / "palace"
    backend = ChromaBackend()
    ref = PalaceRef(id=str(palace), local_path=str(palace))
    backend.get_collection(palace=ref, collection_name="mempalace_drawers", create=True).add(
        ids=["drawer_one", "drawer_two"],
        documents=[
            "We chose Postgres over MySQL for the audit log.",
            "Rotate the API keys monthly.",
        ],
        metadatas=[
            {"wing": "acme_app", "room": "decisions", "filed_at": "2026-01-02T00:00:00+00:00"},
            {"wing": "other", "room": "ops", "filed_at": "2026-01-03T00:00:00+00:00"},
        ],
        embeddings=[[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]],
    )
    backend.close_palace(ref)
    wake = """
import sys
from mempalace.layers import MemoryStack
text = MemoryStack(palace_path=sys.argv[1], identity_path=sys.argv[1] + "/none.txt").wake_up(
    wing=sys.argv[2] or None
)
heavy = sorted(
    name for name in ("chromadb", "onnxruntime", "mempalace.searcher")
    if name in sys.modules
)
assert not heavy, heavy
print(text)
"""
    everything = _run(wake, str(palace), "")
    assert "We chose Postgres over MySQL for the audit log." in everything
    assert "Rotate the API keys monthly." in everything
    one_wing = _run(wake, str(palace), "acme_app")
    assert "We chose Postgres over MySQL for the audit log." in one_wing
    assert "Rotate the API keys monthly." not in one_wing


def test_importing_the_chroma_backend_does_not_import_chromadb():
    _run(
        """
import sys
import mempalace.backends
import mempalace.palace
assert "chromadb" not in sys.modules
from mempalace.backends import chroma
assert chroma.chromadb.__name__ == "chromadb"
"""
    )
