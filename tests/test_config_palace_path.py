"""Tests for palace_path tilde expansion in MempalaceConfig."""

import json
import os
import ntpath
import tempfile
from unittest import mock
from mempalace.config import (
    MempalaceConfig,
    canonical_palace_path,
    _resolve_palace_leaf,
)


def _make_leaf(root: str, name: str = "palace") -> str:
    leaf = os.path.join(root, name) if name else root
    os.makedirs(leaf, exist_ok=True)
    with open(os.path.join(leaf, "chroma.sqlite3"), "wb") as fh:
        fh.write(b"SQLite format 3\x00")
    return leaf


def test_canonical_leaf_returns_itself_when_dir_holds_store(tmp_path):
    leaf = _make_leaf(str(tmp_path / "palace"))
    assert canonical_palace_path(leaf) == leaf


def test_canonical_leaf_repoints_parent_to_single_leaf(tmp_path):
    leaf = _make_leaf(str(tmp_path), name="palace")
    assert canonical_palace_path(str(tmp_path)) == leaf


def test_canonical_leaf_returns_parent_when_no_child_has_store(tmp_path):
    os.makedirs(str(tmp_path / "not-a-palace"))
    assert canonical_palace_path(str(tmp_path)) == os.path.abspath(str(tmp_path))


def test_canonical_leaf_is_ambiguous_with_multiple_leaves(tmp_path):
    _make_leaf(str(tmp_path), name="a")
    _make_leaf(str(tmp_path), name="b")
    # Two store-holding siblings: never guess, return the parent unchanged.
    assert canonical_palace_path(str(tmp_path)) == os.path.abspath(str(tmp_path))


def test_canonical_leaf_missing_dir_passes_through(tmp_path):
    missing = tmp_path / "does-not-exist"
    assert canonical_palace_path(str(missing)) == os.path.abspath(str(missing))


def test_palace_path_property_repoints_env_parent_to_leaf(tmp_path, monkeypatch):
    leaf = _make_leaf(str(tmp_path), name="palace")
    monkeypatch.setenv("MEMPALACE_PALACE_PATH", str(tmp_path))
    cfg = MempalaceConfig(config_dir=str(tmp_path))
    assert cfg.palace_path == leaf


def test_palace_path_property_keeps_leaf_env(tmp_path, monkeypatch):
    leaf = _make_leaf(str(tmp_path), name="palace")
    monkeypatch.setenv("MEMPALACE_PALACE_PATH", leaf)
    cfg = MempalaceConfig(config_dir=str(tmp_path))
    assert cfg.palace_path == leaf


def test_palace_path_constructor_override_repoints_parent(tmp_path):
    leaf = _make_leaf(str(tmp_path), name="palace")
    cfg = MempalaceConfig(config_dir=str(tmp_path), palace_path=str(tmp_path))
    assert cfg.palace_path == leaf


def test_palace_path_constructor_override_keeps_leaf(tmp_path):
    leaf = _make_leaf(str(tmp_path), name="palace")
    cfg = MempalaceConfig(config_dir=str(tmp_path), palace_path=leaf)
    assert cfg.palace_path == leaf


def test_palace_path_property_config_file_parent_repoints(tmp_path):
    leaf = _make_leaf(str(tmp_path), name="palace")
    cfg = MempalaceConfig(config_dir=str(tmp_path))
    cfg._file_config["palace_path"] = str(tmp_path)
    assert cfg.palace_path == leaf


def test_palace_path_expands_tilde_from_config_file():
    """palace_path must expand ~ even when read from config.json, not env."""
    cfg = MempalaceConfig()
    cfg._file_config["palace_path"] = "~/.mempalace/palace"
    result = cfg.palace_path
    assert not result.startswith("~"), (
        f"palace_path returned unexpanded tilde: {result!r}. "
        "This causes mempalace mine to create a literal '~' directory "
        "relative to CWD instead of writing to the home directory."
    )
    # Mirror the code's normalization (abspath + expanduser). expanduser alone
    # would diverge on Windows, where abspath also prepends the drive letter.
    assert result == os.path.abspath(os.path.expanduser("~/.mempalace/palace"))


def test_palace_path_expands_tilde_nested():
    """Nested tilde paths (e.g. ~/custom/palace) are also expanded."""
    cfg = MempalaceConfig()
    cfg._file_config["palace_path"] = "~/custom/mempalace"
    result = cfg.palace_path
    assert not result.startswith("~")
    # Mirror the code's normalization so the assertion holds cross-platform
    # (see the tilde test above for the Windows drive-letter reasoning).
    assert result == os.path.abspath(os.path.expanduser("~/custom/mempalace"))


def test_palace_path_absolute_unchanged():
    """Absolute paths pass through without modification."""
    cfg = MempalaceConfig()
    cfg._file_config["palace_path"] = "/tmp/test_palace"
    # The code returns the abspath-normalized form; resolve the expectation
    # the same way so it pins the leaf without being host-specific.
    assert cfg.palace_path == os.path.abspath(os.path.expanduser("/tmp/test_palace"))


def test_init_persists_constructor_override_not_default():
    """init() must persist the resolved palace_path, not the hardcoded default.

    `mempalace --palace <custom> init` passes palace_path via the constructor
    (mirrored from cli.py's env-var write for cmd_init). The persisted
    config.json must record that custom path so a later invocation with no
    --palace flag (e.g. `mempalace status`) still finds it.
    """
    config_dir = tempfile.mkdtemp()
    custom_palace = os.path.join(tempfile.mkdtemp(), "custom-palace")
    cfg = MempalaceConfig(config_dir=config_dir, palace_path=custom_palace)
    assert cfg.palace_path == custom_palace

    cfg.init()

    with open(os.path.join(config_dir, "config.json")) as f:
        saved = json.load(f)
    assert saved["palace_path"] == custom_palace

    # A later invocation with no override must read the persisted path back.
    later_cfg = MempalaceConfig(config_dir=config_dir)
    assert later_cfg.palace_path == custom_palace


def test_init_persists_env_var_palace_path():
    """init() must persist a MEMPALACE_PALACE_PATH override, not the default.

    cmd_init sets this env var before constructing MempalaceConfig() when
    --palace is passed (cli.py:308); init() must write what it resolved to,
    not the module-level default.
    """
    config_dir = tempfile.mkdtemp()
    custom_palace = os.path.join(tempfile.mkdtemp(), "env-palace")
    os.environ["MEMPALACE_PALACE_PATH"] = custom_palace
    try:
        cfg = MempalaceConfig(config_dir=config_dir)
        cfg.init()
    finally:
        del os.environ["MEMPALACE_PALACE_PATH"]

    with open(os.path.join(config_dir, "config.json")) as f:
        saved = json.load(f)
    assert saved["palace_path"] == custom_palace

    # A later invocation with no --palace and no env var must still resolve
    # to the persisted custom path, not silently fall back to the default.
    later_cfg = MempalaceConfig(config_dir=config_dir)
    assert later_cfg.palace_path == custom_palace


# --- Instance-level canonicalisation cache (#2418) --------------------------


def test_palace_path_file_branch_cached_exactly_once(tmp_path, monkeypatch):
    """Acceptance (a) — file-config branch: helper runs ONCE across N reads.

    The per-access ``isfile``/``listdir`` probe is the hot path behind 147
    call sites; this asserts memoization removes the repeated I/O while the
    #2404 invariant (reader opens the store-owning leaf) is preserved.
    """
    leaf = _make_leaf(str(tmp_path), name="palace")
    cfg = MempalaceConfig(config_dir=str(tmp_path))
    cfg._file_config["palace_path"] = str(tmp_path)  # file-config branch
    assert cfg._palace_path_override is None

    with mock.patch(
        "mempalace.config.canonical_palace_path", side_effect=canonical_palace_path
    ) as sp:
        results = [cfg.palace_path for _ in range(50)]
        assert sp.call_count == 1, f"expected 1 resolve, got {sp.call_count}"
    assert all(r == leaf for r in results)


def test_palace_path_override_branch_cached_once_and_stable(tmp_path):
    """Acceptance — constructor-override branch also memoizes.

    A stable-instance override (``--palace <x>`` on the CLI) is a constant
    for the process lifetime, so it is cached too: one resolve, then hits.
    """
    leaf = _make_leaf(str(tmp_path), name="palace")
    cfg = MempalaceConfig(config_dir=str(tmp_path), palace_path=str(tmp_path))

    with mock.patch(
        "mempalace.config.canonical_palace_path", side_effect=canonical_palace_path
    ) as sp:
        for _ in range(50):
            assert cfg.palace_path == leaf
        assert sp.call_count == 1


def test_palace_path_set_backend_invalidates_file_cache(tmp_path):
    """Acceptance (b) — a setter that rewrites config.json clears the cache.

    ``set_backend`` persists ``_file_config`` via ``_atomic_write_json``;
    after that the file may disagree with the cached value, so the next
    ``palace_path`` access must re-resolve (a second helper call).
    """
    leaf = _make_leaf(str(tmp_path), name="palace")
    cfg = MempalaceConfig(config_dir=str(tmp_path))
    cfg._file_config["palace_path"] = str(tmp_path)
    assert cfg.palace_path == leaf  # prime the cache (file branch)

    with mock.patch(
        "mempalace.config.canonical_palace_path", side_effect=canonical_palace_path
    ) as sp:
        cfg.set_backend("chroma")  # writes config.json -> clears cache
        cfg._file_config["palace_path"] = leaf  # a value a real edit might persist
        assert cfg.palace_path == leaf
        assert sp.call_count == 1  # the post-rewrite re-resolve


def test_palace_path_env_branch_re_resolves_across_env_mutation(tmp_path, monkeypatch):
    """Acceptance — env branch is NOT cached across live env mutation.

    The CLI sets ``MEMPALACE_PALACE_PATH`` per invocation, so a same-instance
    env change must still be honored: two reads with a mutation between them
    reflect both values (no stale cache hides the environment).
    """
    leaf_a = _make_leaf(str(tmp_path / "a"), name="palace-a")
    leaf_b = _make_leaf(str(tmp_path / "b"), name="palace-b")
    cfg = MempalaceConfig(config_dir=str(tmp_path))
    assert cfg._palace_path_override is None

    monkeypatch.setenv("MEMPALACE_PALACE_PATH", leaf_a)
    first = cfg.palace_path
    monkeypatch.setenv("MEMPALACE_PALACE_PATH", leaf_b)
    second = cfg.palace_path

    assert first == leaf_a
    assert second == leaf_b


def test_palace_path_init_roundtrip_leaf_stable(tmp_path):
    """Acceptance (c) — init() roundtrip keeps leaf unchanged, no double-read.

    ``init()`` persists ``self.palace_path`` (a no-op rewrite of the same
    value); it must not re-resolve a stale value, and a fresh reader reading
    the same config dir afterward must land on the identical leaf.
    """
    leaf = _make_leaf(str(tmp_path / "src"), name="palace")
    config_dir = str(tmp_path / "config-dir")
    os.makedirs(config_dir, exist_ok=True)

    cfg = MempalaceConfig(config_dir=config_dir)
    cfg._file_config["palace_path"] = str(tmp_path / "src")  # parent dir
    before = cfg.palace_path
    assert before == leaf

    cfg.init()  # writes config.json with the resolved value

    with open(os.path.join(config_dir, "config.json")) as f:
        saved = json.load(f)
    assert saved["palace_path"] == leaf

    after = cfg.palace_path  # must be unchanged, not re-guessed
    assert after == leaf

    later = MempalaceConfig(config_dir=config_dir)
    assert later.palace_path == leaf


def test_palace_path_cache_slot_seeded_none(tmp_path):
    """Acceptance — cache slot exists and starts empty (not pre-populated)."""
    cfg = MempalaceConfig(config_dir=str(tmp_path))
    assert hasattr(cfg, "_palace_path_cache")
    assert cfg._palace_path_cache is None


# ---------------------------------------------------------------------------
# Windows-shaped simulation (#2418 / t_f253e712)
#
# The seven CI failures were Windows-only assertions: the code normalises
# palace_path through os.path.abspath (which on Windows prepends the drive
# letter and emits backslashes) while the tests compared against bare POSIX
# literals or expanduser() without abspath. The fixes above align the
# assertions with the code's own primitives. Beyond that, the tests below
# drive the *real* leaf-resolution logic under Windows path semantics
# (ntpath + a recorded FS) so a regression is caught on any host, without
# needing a Windows runner. They pin that D:\-prefixed leaf selection is
# deterministic and correct under a Windows drive+backslash shape.
# ---------------------------------------------------------------------------


class _FakeWindowsOS:
    """Minimal stand-in for the :mod:`os` surface _resolve_palace_leaf uses.

    Path semantics come from :mod:`ntpath` (drive letters + backslash
    joins + abspath), while the filesystem predicates (isdir / isfile) and
    listdir are recorded as a small in-memory layout so a single
    Windows-shaped tree can be asserted on any host. The resolver reads
    ``os.listdir`` from the top-level object and ``os.path.<pred>`` from the
    ``path`` namespace, so both are provided here explicitly.
    """

    def __init__(self, files=(), dirs=(), listable=(), unreadable=()):
        self.files = set(files)
        self.dirs = set(dirs)
        self._listable = dict(listable)
        self._unreadable = set(unreadable)
        # _resolve_palace_leaf accesses these via ``_os.path.*`` and
        # ``_os.listdir`` — expose the same surface it expects.
        self.listdir = self._listdir
        self.isdir = lambda p: p in self.dirs
        self.isfile = lambda p: p in self.files
        self.path = _NtpathFake(self.dirs, self.files)

    def _listdir(self, base):
        if base in self._unreadable:
            raise OSError("simulated unreadable directory")
        return self._listable.get(base, [])


class _NtpathFake:
    """``os.path`` replacement: ntpath text ops + set-backed FS predicates."""

    def __init__(self, dirs, files):
        self._dirs = dirs
        self._files = files
        self.abspath = staticmethod(ntpath.abspath)
        self.expanduser = staticmethod(ntpath.expanduser)
        self.join = staticmethod(ntpath.join)
        self.isdir = lambda p: p in dirs
        self.isfile = lambda p: p in files


def test_canonical_leaf_windows_single_store_child():
    """D:\\palace-root with one child holding chroma.sqlite3 -> that child."""
    base = "D:\\palace-root"
    leaf = "D:\\palace-root\\palace"
    leaf_store = "D:\\palace-root\\palace\\chroma.sqlite3"
    fs = _FakeWindowsOS(
        files={leaf_store},
        dirs={leaf},
        listable={base: ["palace"]},
    )
    assert _resolve_palace_leaf(base, _os=fs, _pathmod=ntpath) == leaf


def test_canonical_leaf_windows_base_holds_store_unchanged():
    """A base that already holds the store must be returned unchanged."""
    base = "D:\\palace-root"
    base_store = "D:\\palace-root\\chroma.sqlite3"
    fs = _FakeWindowsOS(files={base_store}, dirs={base}, listable={base: ["x"]})
    assert _resolve_palace_leaf(base, _os=fs, _pathmod=ntpath) == base


def test_canonical_leaf_windows_ambiguous_returns_base():
    """Two store-holding siblings -> never guess, return the base unchanged."""
    base = "D:\\palace-root"
    a = "D:\\palace-root\\a"
    b = "D:\\palace-root\\b"
    fs = _FakeWindowsOS(
        files={
            "D:\\palace-root\\a\\chroma.sqlite3",
            "D:\\palace-root\\b\\chroma.sqlite3",
        },
        dirs={a, b},
        listable={base: ["a", "b"]},
    )
    assert _resolve_palace_leaf(base, _os=fs, _pathmod=ntpath) == base


def test_canonical_leaf_windows_missing_dir_passes_through():
    """Unreadable directory -> base returned (abspath-normalised) unchanged."""
    base = "D:\\palace-root"
    fs = _FakeWindowsOS(unreadable={base})
    assert _resolve_palace_leaf(base, _os=fs, _pathmod=ntpath) == "D:\\palace-root"
