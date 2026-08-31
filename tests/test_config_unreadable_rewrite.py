"""A config.json this process could not read is not one to write over.

``MempalaceConfig`` reads the file once in ``__init__`` and falls back to an
empty dict whenever that read does not conclude. Every setter then writes that
dict back over the whole file, so one unreadable read turns into the permanent
loss of every setting the file held. The setters' own writes are what produce
those unreadable files: they truncate the file first and serialize into it
afterwards, so anything that interrupts one leaves the remains behind.
"""

import errno
import json
import os
import stat

import pytest

from mempalace.config import DEFAULT_PALACE_PATH, MempalaceConfig

REAL = {
    "palace_path": "/mnt/data/palace",
    "collection_name": "memories",
    "embedding_model": "embeddinggemma",
    "entity_languages": ["en", "ru"],
    "hooks": {"daemon": True, "silent_save": False},
    "people_map": {"mv": "Mikhail Valentsev"},
}


def _config_file(tmp_path):
    return tmp_path / "config.json"


def _kept_files(tmp_path):
    return sorted(p for p in tmp_path.iterdir() if p.name != "config.json")


def test_unparseable_config_is_kept_when_a_setter_writes(tmp_path, capsys):
    _config_file(tmp_path).write_text('{"palace_path": "/mnt/data/palace", "hoo')
    original = _config_file(tmp_path).read_bytes()

    cfg = MempalaceConfig(config_dir=str(tmp_path))
    cfg.set_backend("sqlite_exact")

    kept = _kept_files(tmp_path)
    assert len(kept) == 1, f"expected the old config to be kept, found {kept}"
    assert kept[0].read_bytes() == original
    assert json.loads(_config_file(tmp_path).read_text()) == {"backend": "sqlite_exact"}
    assert "does not parse" in capsys.readouterr().err


def test_non_dict_config_is_kept(tmp_path):
    _config_file(tmp_path).write_text(json.dumps(["unexpected", "array"]))
    original = _config_file(tmp_path).read_bytes()

    cfg = MempalaceConfig(config_dir=str(tmp_path))
    cfg.set_hook_setting("daemon", True)

    kept = _kept_files(tmp_path)
    assert len(kept) == 1
    assert kept[0].read_bytes() == original


def test_config_with_undecodable_bytes_is_kept(tmp_path):
    _config_file(tmp_path).write_bytes(b'{"palace_path": "/mnt/\xffdata"}')
    original = _config_file(tmp_path).read_bytes()

    cfg = MempalaceConfig(config_dir=str(tmp_path))
    cfg.set_embedding_model("minilm")

    kept = _kept_files(tmp_path)
    assert len(kept) == 1
    assert kept[0].read_bytes() == original


def test_absent_config_is_a_fresh_start(tmp_path):
    cfg = MempalaceConfig(config_dir=str(tmp_path))
    cfg.set_backend("chroma")

    assert _kept_files(tmp_path) == []
    assert json.loads(_config_file(tmp_path).read_text()) == {"backend": "chroma"}


def test_readable_config_keeps_every_other_setting(tmp_path):
    _config_file(tmp_path).write_text(json.dumps(REAL))

    cfg = MempalaceConfig(config_dir=str(tmp_path))
    cfg.set_backend("qdrant")

    assert _kept_files(tmp_path) == []
    on_disk = json.loads(_config_file(tmp_path).read_text())
    assert on_disk["backend"] == "qdrant"
    assert on_disk["palace_path"] == REAL["palace_path"]
    assert on_disk["people_map"] == REAL["people_map"]
    assert on_disk["hooks"] == REAL["hooks"]

    reloaded = MempalaceConfig(config_dir=str(tmp_path))
    assert reloaded.palace_path == REAL["palace_path"]
    assert reloaded.palace_path != os.path.expanduser(DEFAULT_PALACE_PATH)


def test_a_failed_write_leaves_the_previous_config_intact(tmp_path, monkeypatch):
    _config_file(tmp_path).write_text(json.dumps(REAL))
    original = _config_file(tmp_path).read_bytes()
    cfg = MempalaceConfig(config_dir=str(tmp_path))

    def boom(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "replace", boom)
    cfg.set_backend("qdrant")

    assert _config_file(tmp_path).read_bytes() == original
    assert _kept_files(tmp_path) == [], "a temporary file was left behind"


needs_unprivileged_posix = pytest.mark.skipif(
    os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="permission bits mean nothing to root, and Windows has none",
)


@needs_unprivileged_posix
def test_a_failed_write_is_reported_not_swallowed(tmp_path, capsys):
    """A directory that takes neither a temporary file nor a write leaves the
    setting unsaved. ``develop`` swallowed that in ``except OSError: pass``;
    the config is still intact either way, and now the reason is printed."""
    _config_file(tmp_path).write_text(json.dumps(REAL))
    cfg = MempalaceConfig(config_dir=str(tmp_path))
    # The directory refuses a temporary file and the config itself refuses the
    # write in place, so neither path can save the setting.
    os.chmod(_config_file(tmp_path), 0o400)
    os.chmod(tmp_path, 0o500)
    try:
        cfg.set_backend("qdrant")
        err = capsys.readouterr().err
    finally:
        os.chmod(tmp_path, 0o700)
        os.chmod(_config_file(tmp_path), 0o600)

    assert "could not write" in err.lower()
    assert os.strerror(errno.EACCES).lower() in err.lower()
    # Nothing was written in place either, so nothing may say it was: the two
    # messages together would tell the user the setting both landed and did not.
    assert "written in place" not in err
    assert json.loads(_config_file(tmp_path).read_text())["palace_path"] == REAL["palace_path"]
    assert "backend" not in json.loads(_config_file(tmp_path).read_text())


@needs_unprivileged_posix
def test_present_but_unreadable_config_is_not_written_over(tmp_path, capsys):
    """The file is there and this process cannot read it. Its settings are
    whatever they are; the ones in memory are this session's defaults."""
    path = _config_file(tmp_path)
    path.write_text(json.dumps(REAL))
    original = path.read_bytes()
    os.chmod(path, 0o000)
    try:
        cfg = MempalaceConfig(config_dir=str(tmp_path))
        cfg.set_backend("qdrant")
        err = capsys.readouterr().err
        os.chmod(path, 0o600)
        assert path.read_bytes() == original
        assert _kept_files(tmp_path) == []
    finally:
        os.chmod(path, 0o600)
    assert "could not be read" in err
    # The state covers a permission bit, a directory at that name, a symlink
    # loop and a share that stopped answering. Naming which one is the whole
    # reason the error is carried out of ``__init__``.
    assert os.strerror(errno.EACCES).lower() in err.lower()


def test_people_map_write_leaves_no_temporary_file(tmp_path):
    cfg = MempalaceConfig(config_dir=str(tmp_path))
    cfg.save_people_map({"mv": "Mikhail Valentsev"})
    leftovers = sorted(p.name for p in tmp_path.iterdir() if p.name != "people_map.json")
    assert leftovers == []
    assert json.loads((tmp_path / "people_map.json").read_text()) == {"mv": "Mikhail Valentsev"}


@needs_unprivileged_posix
def test_people_map_reports_why_the_directory_could_not_be_made(tmp_path):
    """``save_people_map`` raised before the directory helper existed and still
    does. Swallowing the helper's error turns "no permission on the parent"
    into "no such file" about a temporary name the caller never chose."""
    parent = tmp_path / "locked"
    parent.mkdir()
    os.chmod(parent, 0o500)
    cfg = MempalaceConfig(config_dir=str(parent / ".mempalace"))
    try:
        with pytest.raises(OSError) as caught:
            cfg.save_people_map({"mv": "Mikhail Valentsev"})
    finally:
        os.chmod(parent, 0o700)

    assert caught.value.errno in (errno.EACCES, errno.EPERM)
    assert str(parent) in str(caught.value.filename)
    assert ".tmp-" not in str(caught.value.filename or "")


def test_a_config_reached_through_a_symlink_is_written_through_it(tmp_path):
    """A config kept in a dotfiles checkout is reached by a link. Replacing
    the link would leave the real file holding what it held, and every later
    setting would go somewhere the user is not looking."""
    real = tmp_path / "dotfiles" / "config.json"
    real.parent.mkdir()
    real.write_text(json.dumps(REAL))
    try:
        _config_file(tmp_path).symlink_to(real)
    except OSError as exc:
        # Windows reports the missing privilege as winerror 1314 with an errno
        # that says nothing. Widening the gate to "any OSError on nt" would
        # turn a real failure into a skip, which is how a test disappears from
        # a green run.
        unprivileged = exc.errno in (errno.EPERM, errno.EACCES) or (
            getattr(exc, "winerror", None) == 1314
        )
        if not unprivileged:
            raise
        pytest.skip(f"symlink creation not permitted for this user: {exc}")

    MempalaceConfig(config_dir=str(tmp_path)).set_backend("qdrant")

    assert _config_file(tmp_path).is_symlink()
    on_disk = json.loads(real.read_text())
    assert on_disk["backend"] == "qdrant"
    assert on_disk["palace_path"] == REAL["palace_path"]
    assert [p for p in _kept_files(tmp_path) if "unreadable" in p.name] == []


def test_a_bom_is_not_a_config_that_failed_to_parse(tmp_path):
    """``json.loads`` on bytes accepts a BOM, which is what a Windows editor
    leaves behind, so decoding by hand has to accept one too."""
    _config_file(tmp_path).write_bytes(b"\xef\xbb\xbf" + json.dumps(REAL).encode())

    MempalaceConfig(config_dir=str(tmp_path)).set_backend("qdrant")

    on_disk = json.loads(_config_file(tmp_path).read_text(encoding="utf-8-sig"))
    assert on_disk["backend"] == "qdrant"
    assert on_disk["palace_path"] == REAL["palace_path"]
    assert _kept_files(tmp_path) == []


@needs_unprivileged_posix
def test_a_directory_that_refuses_a_temporary_file_still_saves_the_setting(tmp_path, capsys):
    """Writing to an existing file needs the file; writing through a temporary
    one needs the directory. Losing the setting outright is worse than losing
    crash-safety, so the setting is written and the message says which it is."""
    _config_file(tmp_path).write_text(json.dumps(REAL))
    os.chmod(tmp_path, 0o500)
    try:
        MempalaceConfig(config_dir=str(tmp_path)).set_backend("qdrant")
        err = capsys.readouterr().err
    finally:
        os.chmod(tmp_path, 0o700)

    on_disk = json.loads(_config_file(tmp_path).read_text())
    assert on_disk["backend"] == "qdrant"
    assert on_disk["palace_path"] == REAL["palace_path"]
    assert "written in place" in err


def test_the_rename_is_made_durable(tmp_path, monkeypatch):
    """The rename's own durability needs the parent directory synced, which is
    what ``EntityRegistry.save`` does and explains. Nothing observable comes
    out of an fsync, so this asserts the call rather than its effect."""
    import mempalace.config as config_module

    synced = []
    monkeypatch.setattr(config_module, "_fsync_directory", lambda d: synced.append(str(d)))

    MempalaceConfig(config_dir=str(tmp_path)).set_backend("qdrant")

    assert synced == [str(tmp_path)]


def test_a_setter_creates_the_config_directory_for_the_owner_only(tmp_path):
    """``set_hook_setting`` reaches a machine where ``mempalace init`` never
    ran. The config it writes holds the user's ``people_map``."""
    directory = tmp_path / "fresh"
    MempalaceConfig(config_dir=str(directory)).set_hook_setting("daemon", True)

    assert directory.is_dir()
    if os.name != "nt":
        assert stat.S_IMODE(os.stat(directory).st_mode) == 0o700


@needs_unprivileged_posix
def test_a_config_that_cannot_be_moved_aside_is_not_written_over(tmp_path, capsys):
    """The quarantine is what frees the name. A directory that will not take
    the rename leaves the unparseable bytes where they are."""
    _config_file(tmp_path).write_text("{ not valid json")
    original = _config_file(tmp_path).read_bytes()
    cfg = MempalaceConfig(config_dir=str(tmp_path))
    os.chmod(tmp_path, 0o500)
    try:
        cfg.set_backend("qdrant")
        err = capsys.readouterr().err
    finally:
        os.chmod(tmp_path, 0o700)

    assert _config_file(tmp_path).read_bytes() == original
    assert "could not be moved aside" in err


@needs_unprivileged_posix
def test_a_directory_that_cannot_be_made_is_raised_or_reported_as_before(tmp_path, capsys):
    """``develop`` created the directory inside three setters, outside any
    ``try``, so those raised; ``set_hook_setting`` did not create it at all and
    returned, which is what ``tool_hook_settings`` relies on, since it does not
    wrap the call. Moving the creation into one place would have flattened that
    difference in whichever direction the one place chose."""
    parent = tmp_path / "locked"
    parent.mkdir()
    os.chmod(parent, 0o500)
    config_dir = str(parent / ".mempalace")
    try:
        for setter in (
            lambda c: c.set_backend("qdrant"),
            lambda c: c.set_embedding_model("minilm"),
            lambda c: c.set_entity_languages(["en"]),
            lambda c: c.save_people_map({"mv": "Mikhail Valentsev"}),
        ):
            with pytest.raises(OSError) as caught:
                setter(MempalaceConfig(config_dir=config_dir))
            assert caught.value.errno in (errno.EACCES, errno.EPERM)
            assert str(parent) in str(caught.value.filename)

        MempalaceConfig(config_dir=config_dir).set_hook_setting("daemon", True)
        err = capsys.readouterr().err
    finally:
        os.chmod(parent, 0o700)

    assert "could not create" in err.lower()
    # The message names why, not just that: the state covers a permission bit,
    # a read-only filesystem and a file sitting at that name.
    assert os.strerror(errno.EACCES).lower() in err.lower()


def test_an_existing_config_directory_keeps_the_permissions_it_has(tmp_path):
    """A directory the user already made is theirs. Only one this call creates
    is restricted."""
    existing = tmp_path / "existing"
    existing.mkdir()
    os.chmod(existing, 0o755)

    MempalaceConfig(config_dir=str(existing)).set_hook_setting("daemon", True)

    if os.name != "nt":
        assert stat.S_IMODE(os.stat(existing).st_mode) == 0o755


@needs_unprivileged_posix
def test_a_symlink_at_the_temporary_name_is_not_written_through(tmp_path):
    """The temporary name is predictable, so it is opened ``O_NOFOLLOW``: a
    link left there by someone else must not turn a config write into a write
    to whatever it points at."""
    victim = tmp_path / "victim"
    victim.write_text("untouched")
    _config_file(tmp_path).write_text(json.dumps(REAL))
    (tmp_path / f"config.json.tmp-{os.getpid()}").symlink_to(victim)

    MempalaceConfig(config_dir=str(tmp_path)).set_backend("qdrant")

    assert victim.read_text() == "untouched"
    assert json.loads(_config_file(tmp_path).read_text())["palace_path"] == REAL["palace_path"]


@needs_unprivileged_posix
def test_an_orphan_at_the_temporary_name_does_not_cost_the_rename(tmp_path, capsys):
    """A run killed between the write and the rename leaves the pid-named file
    behind, and another user's run leaves one this user cannot open. Reading
    that as "the directory takes no temporary file" turns the atomic write off
    in a directory that was never the problem."""
    _config_file(tmp_path).write_text(json.dumps(REAL))
    orphan = tmp_path / f"config.json.tmp-{os.getpid()}"
    orphan.write_text("left behind")
    os.chmod(orphan, 0o400)

    try:
        MempalaceConfig(config_dir=str(tmp_path)).set_backend("qdrant")
    finally:
        os.chmod(orphan, 0o600)

    on_disk = json.loads(_config_file(tmp_path).read_text())
    assert on_disk["backend"] == "qdrant"
    assert on_disk["palace_path"] == REAL["palace_path"]
    # The write kept the rename, so nothing said it gave it up.
    assert "written in place" not in capsys.readouterr().err
    # The orphan is not this write's to clean up, and it was not written into.
    assert orphan.read_text() == "left behind"
    leftovers = [p.name for p in tmp_path.iterdir() if p.name not in ("config.json", orphan.name)]
    assert leftovers == [], leftovers


@needs_unprivileged_posix
def test_a_reusable_orphan_does_not_widen_the_config(tmp_path):
    """The pid-named file can be one this process left behind itself, with
    whatever mode it had. ``O_CREAT`` on an existing name does not touch the
    mode, so without the explicit ``chmod`` the config inherits it."""
    _config_file(tmp_path).write_text(json.dumps(REAL))
    orphan = tmp_path / f"config.json.tmp-{os.getpid()}"
    orphan.write_text("left behind")
    os.chmod(orphan, 0o666)

    MempalaceConfig(config_dir=str(tmp_path)).set_backend("qdrant")

    assert stat.S_IMODE(os.stat(_config_file(tmp_path)).st_mode) == 0o600


def test_the_temporary_file_is_synced(tmp_path, monkeypatch):
    """The rename publishes whatever the temporary file holds. Syncing the
    directory makes the rename survive a crash; syncing the file is what makes
    the bytes it publishes survive one."""
    from mempalace import config as config_mod

    _config_file(tmp_path).write_text(json.dumps(REAL))
    real_fsync = os.fsync
    synced_fds = []

    def record(fd):
        synced_fds.append(fd)
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", record)
    monkeypatch.setattr(config_mod, "_fsync_directory", lambda d: None)

    MempalaceConfig(config_dir=str(tmp_path)).set_backend("qdrant")

    assert synced_fds, "the temporary file was published without being synced"


@needs_unprivileged_posix
def test_a_hard_link_at_the_temporary_name_is_not_written_through(tmp_path, capsys):
    """``O_NOFOLLOW`` sees symlinks, not hard links: to ``open`` a second link
    is an ordinary writable file, which is exactly what this write reuses. The
    truncate would empty a file nobody named here, and the rename would make it
    an alias of the config."""
    victim = tmp_path / "important_notes.txt"
    victim.write_text("notes worth keeping")
    _config_file(tmp_path).write_text(json.dumps(REAL))
    link = tmp_path / f"config.json.tmp-{os.getpid()}"
    os.link(victim, link)

    MempalaceConfig(config_dir=str(tmp_path)).set_backend("qdrant")

    assert victim.read_text() == "notes worth keeping"
    on_disk = json.loads(_config_file(tmp_path).read_text())
    assert on_disk["backend"] == "qdrant"
    assert on_disk["palace_path"] == REAL["palace_path"]


@needs_unprivileged_posix
def test_a_temporary_file_the_fallback_cannot_remove_is_named(tmp_path, capsys):
    """Removing it needs the directory, which is what refused the rename. It
    holds what the call was asked to save, so leaving the user to find it is
    worse than saying where it is."""
    _config_file(tmp_path).write_text(json.dumps(REAL))
    orphan = tmp_path / f"config.json.tmp-{os.getpid()}"
    orphan.write_text("half a config from a run that was killed")
    os.chmod(orphan, 0o600)
    os.chmod(tmp_path, 0o555)
    try:
        MempalaceConfig(config_dir=str(tmp_path)).set_backend("qdrant")
        err = capsys.readouterr().err
    finally:
        os.chmod(tmp_path, 0o700)

    assert orphan.exists(), "the fallback could not have removed it here"
    assert orphan.name in err
    assert "could not be removed" in err
    assert json.loads(_config_file(tmp_path).read_text())["backend"] == "qdrant"


@needs_unprivileged_posix
def test_a_read_only_directory_with_a_usable_orphan_still_saves(tmp_path, capsys):
    """Opening a temporary file that already exists needs the file, not the
    directory, so a run that reuses an orphan at the pid name never asks the
    directory anything and reaches the rename, where a read-only directory
    refuses. ``develop`` wrote the config in place there and saved the setting;
    raising instead would lose it."""
    _config_file(tmp_path).write_text(json.dumps(REAL))
    orphan = tmp_path / f"config.json.tmp-{os.getpid()}"
    orphan.write_text("half a config from a run that was killed")
    os.chmod(orphan, 0o600)
    os.chmod(tmp_path, 0o555)
    try:
        MempalaceConfig(config_dir=str(tmp_path)).set_backend("qdrant")
        err = capsys.readouterr().err
    finally:
        os.chmod(tmp_path, 0o700)

    on_disk = json.loads(_config_file(tmp_path).read_text())
    assert on_disk["backend"] == "qdrant"
    assert on_disk["palace_path"] == REAL["palace_path"]
    assert "written in place" in err


def test_a_failed_write_in_place_keeps_the_finished_copy(tmp_path, monkeypatch, capsys):
    """The temporary file holds this write, complete and fsynced. Removing it
    before the write in place would trade a finished copy for a write that
    truncates first, and an interruption there would leave nothing at all."""
    _config_file(tmp_path).write_text(json.dumps(REAL))
    cfg = MempalaceConfig(config_dir=str(tmp_path))
    real_replace = os.replace

    def refused(src, dst, *args, **kwargs):
        if str(dst).endswith("config.json"):
            raise OSError(errno.EPERM, "Operation not permitted")
        return real_replace(src, dst, *args, **kwargs)

    def dies(path, payload):
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(os, "replace", refused)
    from mempalace import config as config_mod

    monkeypatch.setattr(config_mod, "_write_json_in_place", dies)

    # The setter reports rather than raising, which is what `cli.py` relies on.
    cfg.set_backend("qdrant")

    err = capsys.readouterr().err
    assert "could not write" in err.lower()
    leftovers = [p for p in tmp_path.iterdir() if p.name != "config.json"]
    assert len(leftovers) == 1, leftovers
    assert json.loads(leftovers[0].read_text())["backend"] == "qdrant"
    assert leftovers[0].name in err
    # The write in place truncates before it serializes, so what was there
    # is gone whether or not this one finished; the message says so.
    assert "truncated" in err
    assert json.loads(_config_file(tmp_path).read_text())["palace_path"] == REAL["palace_path"]


def test_a_signal_during_the_rename_leaves_no_temporary_file(tmp_path, monkeypatch):
    """Nothing has been published at that point, so the temporary file is not
    a copy of anything the user still needs: it is just a name left in the
    directory. Only the errnos the fallback is for keep it."""
    _config_file(tmp_path).write_text(json.dumps(REAL))
    cfg = MempalaceConfig(config_dir=str(tmp_path))

    def interrupted(src, dst, *args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(os, "replace", interrupted)

    with pytest.raises(KeyboardInterrupt):
        cfg.set_backend("qdrant")

    leftovers = [p.name for p in tmp_path.iterdir() if p.name != "config.json"]
    assert leftovers == [], leftovers
    assert json.loads(_config_file(tmp_path).read_text())["palace_path"] == REAL["palace_path"]


def test_a_rename_the_directory_refuses_falls_back(tmp_path, monkeypatch, capsys):
    """The rename is the second place the directory can answer, and it answers
    with the same three errnos. Anything else stays a failure, or the write
    without the rename comes back for reasons it was never meant to cover."""
    _config_file(tmp_path).write_text(json.dumps(REAL))
    cfg = MempalaceConfig(config_dir=str(tmp_path))
    real_replace = os.replace

    def refused(src, dst, *args, **kwargs):
        if str(dst).endswith("config.json"):
            raise OSError(errno.EPERM, "Operation not permitted")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", refused)
    cfg.set_backend("qdrant")

    on_disk = json.loads(_config_file(tmp_path).read_text())
    assert on_disk["backend"] == "qdrant"
    assert "would not take the rename" in capsys.readouterr().err
    leftovers = [p.name for p in tmp_path.iterdir() if p.name != "config.json"]
    assert leftovers == [], leftovers


def test_eperm_on_both_names_falls_back_too(tmp_path, monkeypatch, capsys):
    """``EPERM`` reaches the gate from a filesystem that refuses the operation
    rather than the caller, an NFS export among them. It belongs beside
    ``EACCES`` and ``EROFS``, and nothing else pins it."""
    _config_file(tmp_path).write_text(json.dumps(REAL))
    cfg = MempalaceConfig(config_dir=str(tmp_path))
    real_open = os.open

    def not_permitted(path, *args, **kwargs):
        name = os.path.basename(str(path))
        if name.startswith("config.json.") or name.startswith(".config.json."):
            raise OSError(errno.EPERM, "Operation not permitted")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", not_permitted)
    cfg.set_backend("qdrant")

    assert json.loads(_config_file(tmp_path).read_text())["backend"] == "qdrant"
    assert "written in place" in capsys.readouterr().err


@needs_unprivileged_posix
def test_erofs_on_both_names_falls_back_rather_than_raising(tmp_path, monkeypatch, capsys):
    """``EROFS`` names the case the fallback is for: the directory takes no new
    name at all, from the pid-named file or from one it picks itself. A real
    read-only mount refuses the write in place too, and the setter reports that
    instead, so the errno is injected on the two temporary names only, which is
    what pins the gate."""
    _config_file(tmp_path).write_text(json.dumps(REAL))
    cfg = MempalaceConfig(config_dir=str(tmp_path))
    real_open = os.open

    def read_only(path, *args, **kwargs):
        name = os.path.basename(str(path))
        if name.startswith("config.json.") or name.startswith(".config.json."):
            raise OSError(errno.EROFS, "Read-only file system")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", read_only)
    cfg.set_backend("qdrant")

    on_disk = json.loads(_config_file(tmp_path).read_text())
    assert on_disk["backend"] == "qdrant"
    assert "written in place" in capsys.readouterr().err


@needs_unprivileged_posix
def test_the_write_in_place_is_synced(tmp_path, monkeypatch):
    """The fallback gives up the rename, not durability: the caller is told the
    write can be truncated by an interruption, not that it can vanish after
    returning."""
    _config_file(tmp_path).write_text(json.dumps(REAL))
    cfg = MempalaceConfig(config_dir=str(tmp_path))
    real_open = os.open
    real_fsync = os.fsync
    synced_fds = []

    def read_only(path, *args, **kwargs):
        name = os.path.basename(str(path))
        if name.startswith("config.json.") or name.startswith(".config.json."):
            raise OSError(errno.EROFS, "Read-only file system")
        return real_open(path, *args, **kwargs)

    def record(fd):
        synced_fds.append(fd)
        return real_fsync(fd)

    monkeypatch.setattr(os, "open", read_only)
    monkeypatch.setattr(os, "fsync", record)

    cfg.set_backend("qdrant")

    assert synced_fds, "the write in place returned without being synced"


@needs_unprivileged_posix
def test_the_symlink_probe_raises_when_it_cannot_tell(tmp_path):
    """The probe classifies: link, not a link, or could not tell. Answering
    "not a link" to the third would send the write through ``os.replace`` and
    put a regular file where the link was. Nothing reaches it that way through
    a setter, since a config this process cannot ``lstat`` fails its read first
    and the setter declines before writing, so this calls the probe itself."""
    from mempalace.config import _write_target

    locked = tmp_path / "locked"
    locked.mkdir()
    target = locked / "config.json"
    os.chmod(locked, 0o000)
    try:
        with pytest.raises(OSError) as caught:
            _write_target(target)
    finally:
        os.chmod(locked, 0o700)

    assert caught.value.errno in (errno.EACCES, errno.EPERM)


@needs_unprivileged_posix
def test_the_umask_does_not_leave_the_temporary_file_unwritable(tmp_path, monkeypatch):
    """``O_CREAT`` is masked by the umask, so a umask that clears the owner's
    write bit creates the temporary file at 0400. Setting the mode after the
    write leaves it that way for as long as the file exists, and one left
    behind by a killed run is then a name its own owner cannot open."""
    seen = {}
    real_fsync = os.fsync

    def record(fd):
        candidate = tmp_path / f"config.json.tmp-{os.getpid()}"
        if candidate.exists():
            seen["mode"] = stat.S_IMODE(os.stat(candidate).st_mode)
        return real_fsync(fd)

    previous = os.umask(0o200)
    try:
        _config_file(tmp_path).write_text(json.dumps(REAL))
        monkeypatch.setattr(os, "fsync", record)
        MempalaceConfig(config_dir=str(tmp_path)).set_backend("qdrant")
    finally:
        os.umask(previous)

    assert seen.get("mode") == 0o600, seen
    assert json.loads(_config_file(tmp_path).read_text())["backend"] == "qdrant"


def test_a_full_disk_does_not_fall_back_to_writing_in_place(tmp_path, monkeypatch, capsys):
    """The fallback exists for a directory that refuses a new name while the
    file itself is writable. Every other failure has to stay a failure, or the
    unatomic write comes back for reasons it was never meant to cover."""
    _config_file(tmp_path).write_text(json.dumps(REAL))
    original = _config_file(tmp_path).read_bytes()
    cfg = MempalaceConfig(config_dir=str(tmp_path))

    real_open = os.open

    # A full disk refuses every name in the directory, the pid-named one and
    # the one the directory picks alike. Denying only the first would let the
    # write succeed under the second and prove nothing.
    def no_space(path, *args, **kwargs):
        name = os.path.basename(str(path))
        in_this_dir = os.path.dirname(str(path)) == str(tmp_path)
        if in_this_dir and (name.startswith("config.json.") or name.startswith(".config.json.")):
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", no_space)
    cfg.set_backend("qdrant")

    assert _config_file(tmp_path).read_bytes() == original
    assert "could not write" in capsys.readouterr().err.lower()
