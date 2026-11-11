"""chromadb's "Could not reconstruct embedding function embeddinggemma2" warning.

chromadb persists EmbeddingGemma 2's EF config and warns on every later open
that its registry cannot rebuild it. MemPalace filters exactly that warning
(see mempalace.backends.chroma); other reconstruct warnings still show.
"""

import subprocess
import sys
import textwrap
import warnings

import pytest

# The chromadb-only run must not import mempalace (importing the package
# loads the chroma backend, which installs the filter), so it uses a stand-in
# with EmbeddingGemma 2's Chroma name and config shape.
_STAND_IN = textwrap.dedent(
    """
    class EG2:
        def __init__(self, *a, **k): pass
        def __call__(self, input): return [[0.1] * 768 for _ in input]
        @staticmethod
        def name(): return "embeddinggemma2"
        def get_config(self): return {"dimension": 768}
        @staticmethod
        def build_from_config(config): return EG2()
        def is_legacy(self): return False
        def default_space(self): return "cosine"
        def supported_spaces(self): return ["cosine"]
    """
)
_CREATE = _STAND_IN + textwrap.dedent(
    """
    import sys, chromadb
    client = chromadb.PersistentClient(sys.argv[1])
    col = client.create_collection("mempalace_drawers", embedding_function=EG2())
    col.add(ids=["a"], documents=["hello"], embeddings=[[0.1] * 768])
    """
)
_REOPEN = _STAND_IN + textwrap.dedent(
    """
    import sys, chromadb
    if sys.argv[2] == "mempalace":
        import mempalace.backends.chroma  # noqa: F401  (installs the filter)
    client = chromadb.PersistentClient(sys.argv[1])
    col = client.get_collection("mempalace_drawers", embedding_function=EG2())
    # A write reloads the persisted schema, which is where chromadb warns.
    col.add(ids=["b"], documents=["again"], embeddings=[[0.2] * 768])
    assert col.count() == 2
    """
)


def _run(code, *args):
    return subprocess.run(
        [sys.executable, "-W", "default", "-c", code, *args],
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )


def test_reopening_an_embeddinggemma2_collection_does_not_warn(tmp_path):
    palace = str(tmp_path / "palace")
    _run(_CREATE, palace)
    # Without MemPalace's chroma backend loaded, chromadb warns (the premise).
    assert "Could not reconstruct embedding function embeddinggemma2" in (
        _run(_REOPEN, palace, "chromadb-only").stderr
    )
    palace2 = str(tmp_path / "palace2")
    _run(_CREATE, palace2)
    assert "Could not reconstruct" not in _run(_REOPEN, palace2, "mempalace").stderr


def _shown(message):
    from mempalace.backends.chroma import _ignore_embeddinggemma2_reconstruct_warning

    with warnings.catch_warnings(record=True) as caught:
        # pytest restores the warning filters after each test, so install the
        # filter again here (a CLI or MCP process installs it once at import).
        _ignore_embeddinggemma2_reconstruct_warning()
        warnings.warn_explicit(
            message, UserWarning, "types.py", 2915, module="chromadb.api.types", registry={}
        )
    return any(
        warning.category is UserWarning and str(warning.message) == message for warning in caught
    )


@pytest.mark.parametrize("unrelated_warning", [False, True])
def test_the_filter_is_narrow(monkeypatch, unrelated_warning):
    if unrelated_warning:
        warn_explicit = warnings.warn_explicit

        def warn_with_cleanup(*args, **kwargs):
            warn_explicit(
                "Unclosed database during collection.",
                ResourceWarning,
                "cleanup.py",
                1,
                module="sqlite3",
                registry={},
            )
            return warn_explicit(*args, **kwargs)

        # Python 3.13 can emit SQLite cleanup warnings during allocations in
        # the probe. They must not decide whether this UserWarning was shown.
        warnings.simplefilter("always", ResourceWarning)
        monkeypatch.setattr(warnings, "warn_explicit", warn_with_cleanup)
    assert not _shown(
        "Could not reconstruct embedding function embeddinggemma2: 'embeddinggemma2'. "
        "Setting to None."
    )
    assert _shown("Could not reconstruct embedding function openai: 'openai'. Setting to None.")
    assert _shown(
        "Could not reconstruct embedding function embeddinggemma2x: 'x'. Setting to None."
    )
