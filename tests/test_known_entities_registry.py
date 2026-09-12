"""Tests for mempalace.miner.add_to_known_entities.

Covers the init → miner wire-up: init's confirmed entities merged into
``~/.mempalace/known_entities.json`` so the miner's drawer-tagging path
recognizes them at mine time.

Every test redirects the registry path to a tmp_path to avoid touching
the real ~/.mempalace/ on the developer's machine.
"""

import errno
import json
import os

import pytest

from mempalace import miner


@pytest.fixture
def temp_registry(tmp_path, monkeypatch):
    """Redirect the module-level registry path to a tmp file and reset cache."""
    registry = tmp_path / "known_entities.json"
    monkeypatch.setattr(miner, "_ENTITY_REGISTRY_PATH", str(registry))
    miner._ENTITY_REGISTRY_CACHE.update({"mtime": None, "names": frozenset(), "raw": {}})
    return registry


# ── fresh-file cases ────────────────────────────────────────────────────


def test_creates_registry_when_absent(temp_registry):
    assert not temp_registry.exists()
    miner.add_to_known_entities({"people": ["Alice", "Bob"], "projects": ["foo"]})
    assert temp_registry.exists()
    data = json.loads(temp_registry.read_text())
    assert sorted(data["people"]) == ["Alice", "Bob"]
    assert data["projects"] == ["foo"]


def test_returns_registry_path(temp_registry):
    result = miner.add_to_known_entities({"people": ["Alice"]})
    assert result == str(temp_registry)


def test_empty_input_still_creates_file(temp_registry):
    """A no-op merge still touches the file (idempotent), but no entries added."""
    miner.add_to_known_entities({})
    # File may or may not be written for a truly empty call — tolerate either.
    if temp_registry.exists():
        data = json.loads(temp_registry.read_text())
        assert data == {} or all(not v for v in data.values())


def test_skips_empty_name_strings(temp_registry):
    miner.add_to_known_entities({"people": ["Alice", "", None]})
    data = json.loads(temp_registry.read_text())
    assert data["people"] == ["Alice"]


# ── union / dedup cases ────────────────────────────────────────────────


def test_unions_with_existing_list_category(temp_registry):
    temp_registry.write_text(json.dumps({"people": ["Alice", "Bob"]}))
    miner.add_to_known_entities({"people": ["Bob", "Carol"]})
    data = json.loads(temp_registry.read_text())
    # Bob not duplicated, Carol appended, original order preserved
    assert data["people"] == ["Alice", "Bob", "Carol"]


def test_case_insensitive_dedup_preserves_first_seen_variant(temp_registry):
    temp_registry.write_text(json.dumps({"people": ["Alice"]}))
    miner.add_to_known_entities({"people": ["alice", "ALICE", "Bob"]})
    data = json.loads(temp_registry.read_text())
    # Alice stays as-is; lowercase/uppercase variants don't create new entries
    assert data["people"] == ["Alice", "Bob"]


def test_preserves_untouched_categories(temp_registry):
    """A category the caller didn't mention must be left alone."""
    temp_registry.write_text(json.dumps({"people": ["Alice"], "places": ["Paris", "Tokyo"]}))
    miner.add_to_known_entities({"people": ["Bob"]})
    data = json.loads(temp_registry.read_text())
    assert data["places"] == ["Paris", "Tokyo"]
    assert data["people"] == ["Alice", "Bob"]


def test_adds_new_categories(temp_registry):
    temp_registry.write_text(json.dumps({"people": ["Alice"]}))
    miner.add_to_known_entities({"projects": ["foo", "bar"]})
    data = json.loads(temp_registry.read_text())
    assert data["people"] == ["Alice"]
    assert data["projects"] == ["foo", "bar"]


def test_dedupes_within_input(temp_registry):
    miner.add_to_known_entities({"people": ["Alice", "alice", "Alice"]})
    data = json.loads(temp_registry.read_text())
    assert data["people"] == ["Alice"]


# ── dict-format existing registry ──────────────────────────────────────


def test_dict_format_existing_category_gets_new_keys(temp_registry):
    """Miner supports {name: code} dict categories (alternate registry shape).
    New names are added as keys without overwriting existing codes."""
    temp_registry.write_text(json.dumps({"people": {"Alice": "ALC", "Bob": "BOB"}}))
    miner.add_to_known_entities({"people": ["Alice", "Carol"]})
    data = json.loads(temp_registry.read_text())
    # Alice's code survives; Carol added with None; Bob untouched
    assert data["people"]["Alice"] == "ALC"
    assert data["people"]["Bob"] == "BOB"
    assert "Carol" in data["people"]
    assert data["people"]["Carol"] is None


def test_dict_format_dedupes_case_insensitively_and_stringifies_new_names(temp_registry):
    temp_registry.write_text(json.dumps({"people": {"Alice": "ALC"}}))
    miner.add_to_known_entities({"people": ["alice", 123]})
    data = json.loads(temp_registry.read_text())
    assert data["people"] == {"Alice": "ALC", "123": None}


# ── error tolerance ───────────────────────────────────────────────────


def test_malformed_existing_registry_starts_fresh(temp_registry):
    temp_registry.write_text("{ not valid json")
    miner.add_to_known_entities({"people": ["Alice"]})
    data = json.loads(temp_registry.read_text())
    assert data == {"people": ["Alice"]}


def test_non_dict_existing_registry_starts_fresh(temp_registry):
    temp_registry.write_text(json.dumps(["unexpected", "array"]))
    miner.add_to_known_entities({"people": ["Alice"]})
    data = json.loads(temp_registry.read_text())
    assert data == {"people": ["Alice"]}


def test_non_list_input_category_ignored(temp_registry):
    miner.add_to_known_entities({"people": ["Alice"], "weird": "not a list"})
    data = json.loads(temp_registry.read_text())
    assert "weird" not in data or data.get("weird") == "not a list"
    assert data["people"] == ["Alice"]


# ── cache invalidation ───────────────────────────────────────────────


def test_cache_invalidated_so_subsequent_load_sees_write(temp_registry):
    """cmd_init → cmd_mine runs in the same process; the load path must
    see what init just wrote without a process restart."""
    # Prime the cache with an empty state
    miner._load_known_entities()
    assert miner._load_known_entities() == frozenset()

    miner.add_to_known_entities({"people": ["Alice", "Bob"], "projects": ["foo"]})

    loaded = miner._load_known_entities()
    assert "Alice" in loaded
    assert "Bob" in loaded
    assert "foo" in loaded


def test_raw_view_reflects_write(temp_registry):
    miner.add_to_known_entities({"people": ["Alice"]})
    raw = miner._load_known_entities_raw()
    assert raw.get("people") == ["Alice"]


# ── Unicode round-trip ────────────────────────────────────────────────


def test_unicode_names_written_literally_not_escaped(temp_registry):
    """`ensure_ascii=False` so non-ASCII names stay readable on disk."""
    miner.add_to_known_entities({"people": ["Gergő Móricz", "Arturo Domínguez"]})
    raw_text = temp_registry.read_text(encoding="utf-8")
    assert "Gergő" in raw_text
    assert "Móricz" in raw_text
    # Round-trips through JSON
    data = json.loads(raw_text)
    assert "Gergő Móricz" in data["people"]


# ── end-to-end: does the write actually help _extract_entities_for_metadata? ──


def test_populated_registry_improves_miner_recall(temp_registry):
    """The whole point of the wire-up: names written via add_to_known_entities
    must be recognized by the miner's entity-extraction metadata pass."""
    miner.add_to_known_entities(
        {
            "people": ["Julia Grib", "Kevin Heifner"],
            "projects": ["hyperion-history", "mempalace"],
        }
    )

    sample = (
        "Met with Julia Grib yesterday about the mempalace release. "
        "Kevin Heifner pushed the hyperion-history fix."
    )
    result = miner._extract_entities_for_metadata(sample)
    tagged = set(result.split(";")) if result else set()

    # All four registered entities should land in the metadata string
    for expected in ("Julia Grib", "Kevin Heifner", "hyperion-history", "mempalace"):
        assert expected in tagged, f"expected '{expected}' in metadata {tagged!r}"


# ── topics_by_wing — cross-wing tunnel signal source (issue #1180) ──


def test_topics_persisted_under_topics_by_wing(temp_registry):
    miner.add_to_known_entities(
        {"people": ["Alice"], "topics": ["Angular", "OpenAPI"]},
        wing="wing_alpha",
    )
    data = json.loads(temp_registry.read_text())
    # Topics also stored as a flat list (existing-style aggregate).
    assert "Angular" in data["topics"]
    # And recorded by wing for tunnel computation.
    assert data["topics_by_wing"]["wing_alpha"] == ["Angular", "OpenAPI"]


def test_topics_by_wing_replaces_on_reinit(temp_registry):
    """Re-running init for the same wing should reflect the latest list,
    not accumulate stale topics indefinitely."""
    miner.add_to_known_entities({"topics": ["Angular", "OpenAPI"]}, wing="wing_alpha")
    miner.add_to_known_entities({"topics": ["OpenAPI", "Postgres"]}, wing="wing_alpha")
    data = json.loads(temp_registry.read_text())
    assert data["topics_by_wing"]["wing_alpha"] == ["OpenAPI", "Postgres"]


def test_topics_by_wing_multiple_wings_coexist(temp_registry):
    miner.add_to_known_entities({"topics": ["foo"]}, wing="wing_a")
    miner.add_to_known_entities({"topics": ["foo", "bar"]}, wing="wing_b")
    data = json.loads(temp_registry.read_text())
    assert data["topics_by_wing"] == {"wing_a": ["foo"], "wing_b": ["foo", "bar"]}


def test_topics_by_wing_skipped_without_wing(temp_registry):
    miner.add_to_known_entities({"topics": ["foo"]})
    data = json.loads(temp_registry.read_text())
    # No wing → no topics_by_wing entry, but topics list still saved.
    assert "topics_by_wing" not in data
    assert data["topics"] == ["foo"]


def test_topics_by_wing_dedupes_case_insensitive(temp_registry):
    miner.add_to_known_entities({"topics": ["OpenAPI", "openapi", "OPENAPI"]}, wing="wing_a")
    data = json.loads(temp_registry.read_text())
    # Only one entry, casing of the first observed name preserved.
    assert data["topics_by_wing"]["wing_a"] == ["OpenAPI"]


def test_get_topics_by_wing_reads_registry(temp_registry):
    miner.add_to_known_entities({"topics": ["foo"]}, wing="wing_a")
    miner.add_to_known_entities({"topics": ["foo", "bar"]}, wing="wing_b")
    result = miner.get_topics_by_wing()
    assert result == {"wing_a": ["foo"], "wing_b": ["foo", "bar"]}


def test_get_topics_by_wing_empty_when_missing(temp_registry):
    miner.add_to_known_entities({"people": ["Alice"]})
    assert miner.get_topics_by_wing() == {}


def test_topics_by_wing_does_not_pollute_known_names(temp_registry):
    """Wing names in topics_by_wing must NOT leak into the flat known-names
    set used by ``_extract_entities_for_metadata`` — only the topic strings
    themselves should be recognized."""
    miner.add_to_known_entities({"topics": ["Angular"]}, wing="wing_super_secret_project")
    known = miner._load_known_entities()
    assert "Angular" in known
    assert "wing_super_secret_project" not in known


# ── a registry whose contents were never established ───────────────────
#
# The merge reads the registry, falls back to an empty dict when that read
# does not conclude, and then rewrites the file whole. Everything the file
# held is gone, and the caller is told the registry was updated. The
# registry's own writer is what produces those unreadable files: a write
# interrupted anywhere leaves the truncated remains of the previous one.


def _quarantine_files(registry):
    return sorted(p for p in registry.parent.iterdir() if p.name != registry.name)


def test_unparseable_registry_is_preserved_beside_the_new_one(temp_registry):
    temp_registry.write_text('{"people": ["Alice", "Bo"], "projects": ["Ori')
    original = temp_registry.read_bytes()

    miner.add_to_known_entities({"people": ["Dana"]})

    kept = _quarantine_files(temp_registry)
    assert len(kept) == 1, f"expected the old registry to be kept, found {kept}"
    assert kept[0].read_bytes() == original
    assert json.loads(temp_registry.read_text()) == {"people": ["Dana"]}


def test_non_dict_registry_is_preserved_beside_the_new_one(temp_registry):
    temp_registry.write_text(json.dumps(["unexpected", "array"]))
    original = temp_registry.read_bytes()

    miner.add_to_known_entities({"people": ["Alice"]})

    kept = _quarantine_files(temp_registry)
    assert len(kept) == 1
    assert kept[0].read_bytes() == original
    assert json.loads(temp_registry.read_text()) == {"people": ["Alice"]}


def test_registry_with_undecodable_bytes_does_not_raise(temp_registry):
    """A byte that is not valid UTF-8 used to leave ``UnicodeDecodeError``
    out of this call, and ``mempalace init`` renders it as a traceback."""
    temp_registry.write_bytes(b'{"people": ["Alic\xffe Kern"]}')
    original = temp_registry.read_bytes()

    miner.add_to_known_entities({"people": ["Dana"]})

    kept = _quarantine_files(temp_registry)
    assert len(kept) == 1
    assert kept[0].read_bytes() == original
    assert json.loads(temp_registry.read_text()) == {"people": ["Dana"]}


def test_absent_registry_is_still_a_fresh_start(temp_registry):
    """Nothing to preserve when the file was never there."""
    assert not temp_registry.exists()
    miner.add_to_known_entities({"people": ["Alice"]})
    assert _quarantine_files(temp_registry) == []
    assert json.loads(temp_registry.read_text()) == {"people": ["Alice"]}


def test_readable_registry_is_not_quarantined(temp_registry):
    temp_registry.write_text(json.dumps({"people": ["Alice"]}))
    miner.add_to_known_entities({"people": ["Bo"]})
    assert _quarantine_files(temp_registry) == []
    assert sorted(json.loads(temp_registry.read_text())["people"]) == ["Alice", "Bo"]


def test_a_failed_write_leaves_the_previous_registry_intact(temp_registry, monkeypatch):
    """The rename is what publishes the new registry, so a write that dies
    before it leaves the old file byte-for-byte where it was."""
    temp_registry.write_text(json.dumps({"people": ["Alice"], "projects": ["Orion"]}))
    original = temp_registry.read_bytes()

    def boom(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(miner.os, "replace", boom)
    with pytest.raises(OSError):
        miner.add_to_known_entities({"people": ["Dana"]})

    assert temp_registry.read_bytes() == original
    assert _quarantine_files(temp_registry) == [], "a .tmp sidecar was left behind"


def test_successful_write_leaves_no_tmp_sidecar(temp_registry):
    miner.add_to_known_entities({"people": ["Alice"]})
    miner.add_to_known_entities({"people": ["Bo"]})
    assert _quarantine_files(temp_registry) == []


def test_a_leftover_temp_file_from_this_process_is_reused(temp_registry):
    """A signal between the write and the rename leaves the temporary file
    behind. It carries the pid, so the next run of that process writes over
    it instead of adding another one."""
    stale = temp_registry.with_name(f"{temp_registry.name}.tmp-{os.getpid()}")
    stale.write_text("half a registry from a run that was killed")

    miner.add_to_known_entities({"people": ["Alice"]})

    assert not stale.exists()
    assert json.loads(temp_registry.read_text()) == {"people": ["Alice"]}
    assert _quarantine_files(temp_registry) == []


needs_unprivileged_posix = pytest.mark.skipif(
    os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="permission bits mean nothing to root, and Windows has none",
)


@needs_unprivileged_posix
def test_present_but_unreadable_registry_is_not_written_over(temp_registry, capsys):
    """The file is there and this process cannot read it. What is in memory is
    this one call's entities, not a registry."""
    temp_registry.write_text(json.dumps({"people": ["Alice"], "projects": ["Orion"]}))
    original = temp_registry.read_bytes()
    os.chmod(temp_registry, 0o000)
    try:
        miner.add_to_known_entities({"people": ["Dana"]})
        err = capsys.readouterr().err
        os.chmod(temp_registry, 0o600)
        assert temp_registry.read_bytes() == original
        assert _quarantine_files(temp_registry) == []
    finally:
        os.chmod(temp_registry, 0o600)
    assert "could not be read" in err


def test_an_unreadable_registry_answers_none_so_the_caller_says_nothing(temp_registry):
    """``cmd_init`` prints "Registry updated" from this return value, so a call
    that wrote nothing must not hand it a path."""
    temp_registry.write_text(json.dumps({"people": ["Alice"]}))
    directory = temp_registry.parent / "known_entities.json.d"
    directory.mkdir()
    miner._ENTITY_REGISTRY_PATH = str(directory)

    assert miner.add_to_known_entities({"people": ["Dana"]}) is None


def test_a_written_registry_answers_its_path(temp_registry):
    assert miner.add_to_known_entities({"people": ["Alice"]}) == str(temp_registry)


def test_a_registry_reached_through_a_symlink_is_written_through_it(temp_registry, tmp_path):
    """A registry kept in a dotfiles checkout is reached by a link. Replacing
    the link would leave the real file holding what it held."""
    real = tmp_path / "dotfiles" / "known_entities.json"
    real.parent.mkdir()
    real.write_text(json.dumps({"people": ["Alice"], "projects": ["Orion"]}))
    try:
        temp_registry.symlink_to(real)
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

    miner.add_to_known_entities({"people": ["Dana"]})

    assert temp_registry.is_symlink()
    assert json.loads(real.read_text())["people"] == ["Alice", "Dana"]
    assert [p for p in _quarantine_files(temp_registry) if "unreadable" in p.name] == []


def test_a_bom_is_not_a_registry_that_failed_to_parse(temp_registry):
    """``json.loads`` on bytes accepts a BOM, which is what a Windows editor
    leaves behind, so decoding by hand has to accept one too."""
    temp_registry.write_bytes(
        b"\xef\xbb\xbf" + json.dumps({"people": ["Alice"], "projects": ["Orion"]}).encode()
    )

    miner.add_to_known_entities({"people": ["Dana"]})

    merged = json.loads(temp_registry.read_text(encoding="utf-8-sig"))
    assert merged["people"] == ["Alice", "Dana"]
    assert merged["projects"] == ["Orion"]
    assert _quarantine_files(temp_registry) == []


@needs_unprivileged_posix
def test_a_directory_that_refuses_a_temporary_file_still_gets_the_merge(temp_registry, capsys):
    """The atomic write needs the directory; the write it replaced needed only
    the file. Losing the merge outright is worse than losing crash-safety."""
    temp_registry.write_text(json.dumps({"people": ["Alice"], "projects": ["Orion"]}))
    os.chmod(temp_registry.parent, 0o555)
    try:
        miner.add_to_known_entities({"people": ["Dana"]})
        err = capsys.readouterr().err
    finally:
        os.chmod(temp_registry.parent, 0o755)

    merged = json.loads(temp_registry.read_text())
    assert merged["people"] == ["Alice", "Dana"]
    assert merged["projects"] == ["Orion"]
    assert "written in place" in err
    assert _quarantine_files(temp_registry) == []


@needs_unprivileged_posix
def test_a_registry_that_cannot_be_moved_aside_is_not_written_over(temp_registry, capsys):
    """The quarantine is what frees the name. A directory that will not take
    the rename leaves the unparseable bytes where they are."""
    temp_registry.write_text("{ not valid json")
    original = temp_registry.read_bytes()
    os.chmod(temp_registry.parent, 0o555)
    try:
        result = miner.add_to_known_entities({"people": ["Dana"]})
        err = capsys.readouterr().err
    finally:
        os.chmod(temp_registry.parent, 0o755)

    assert result is None
    assert temp_registry.read_bytes() == original
    assert "could not be moved aside" in err


def test_the_rename_is_made_durable(temp_registry, monkeypatch):
    """The rename's own durability needs the parent directory synced, which is
    what ``EntityRegistry.save`` does and explains. Nothing observable comes
    out of an fsync, so this asserts the call rather than its effect."""
    synced = []
    monkeypatch.setattr(miner, "_fsync_directory", lambda d: synced.append(str(d)))

    miner.add_to_known_entities({"people": ["Alice"]})

    assert synced == [str(temp_registry.parent)]


@needs_unprivileged_posix
def test_a_symlink_at_the_temporary_name_is_not_written_through(temp_registry):
    """The temporary name is predictable, so it is opened ``O_NOFOLLOW``: a
    link left there by someone else must not turn a merge into a write to
    whatever it points at."""
    victim = temp_registry.parent / "victim"
    victim.write_text("untouched")
    temp_registry.write_text(json.dumps({"people": ["Alice"]}))
    stale = temp_registry.with_name(f"{temp_registry.name}.tmp-{os.getpid()}")
    stale.symlink_to(victim)

    try:
        miner.add_to_known_entities({"people": ["Dana"]})
    except OSError:
        pass  # the write refuses rather than following the link; either way:

    assert victim.read_text() == "untouched"


@needs_unprivileged_posix
def test_an_orphan_at_the_temporary_name_does_not_cost_the_rename(temp_registry, capsys):
    """A run killed between the write and the rename leaves the pid-named file
    behind, and another user's run leaves one this user cannot open. Reading
    that as "the directory takes no temporary file" turns the atomic write off
    in a directory that was never the problem."""
    temp_registry.write_text(json.dumps({"people": ["Alice"], "projects": ["Orion"]}))
    orphan = temp_registry.with_name(f"{temp_registry.name}.tmp-{os.getpid()}")
    orphan.write_text("left behind")
    os.chmod(orphan, 0o400)

    try:
        result = miner.add_to_known_entities({"people": ["Dana"]})
    finally:
        os.chmod(orphan, 0o600)

    assert result is not None
    data = json.loads(temp_registry.read_text())
    assert sorted(data["people"]) == ["Alice", "Dana"]
    assert data["projects"] == ["Orion"]
    # The write kept the rename, so nothing said it gave it up.
    assert "written in place" not in capsys.readouterr().err
    # The orphan is not this write's to clean up, and it was not written into.
    assert orphan.read_text() == "left behind"
    leftovers = [
        p.name
        for p in temp_registry.parent.iterdir()
        if p.name not in (temp_registry.name, orphan.name)
    ]
    assert leftovers == [], leftovers


@needs_unprivileged_posix
def test_a_reusable_orphan_does_not_widen_the_registry(temp_registry):
    """The pid-named file can be one this process left behind itself, with
    whatever mode it had. ``O_CREAT`` on an existing name does not touch the
    mode, so without the explicit ``chmod`` the registry inherits it."""
    temp_registry.write_text(json.dumps({"people": ["Alice"]}))
    orphan = temp_registry.with_name(f"{temp_registry.name}.tmp-{os.getpid()}")
    orphan.write_text("left behind")
    os.chmod(orphan, 0o666)

    miner.add_to_known_entities({"people": ["Dana"]})

    import stat as stat_mod

    assert stat_mod.S_IMODE(os.stat(temp_registry).st_mode) == 0o600


def test_the_temporary_file_is_synced(temp_registry, monkeypatch):
    """The rename publishes whatever the temporary file holds. Syncing the
    directory makes the rename survive a crash; syncing the file is what makes
    the bytes it publishes survive one."""
    real_fsync = os.fsync
    synced_fds = []

    def record(fd):
        synced_fds.append(fd)
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", record)
    monkeypatch.setattr(miner, "_fsync_directory", lambda d: None)

    miner.add_to_known_entities({"people": ["Alice"]})

    assert synced_fds, "the temporary file was published without being synced"


@needs_unprivileged_posix
def test_a_hard_link_at_the_temporary_name_is_not_written_through(temp_registry, capsys):
    """``O_NOFOLLOW`` sees symlinks, not hard links: to ``open`` a second link
    is an ordinary writable file, which is exactly what this write reuses. The
    truncate would empty a file nobody named here, and the rename would make it
    an alias of the registry."""
    victim = temp_registry.parent / "important_notes.txt"
    victim.write_text("notes worth keeping")
    temp_registry.write_text(json.dumps({"people": ["Alice"]}))
    link = temp_registry.with_name(f"{temp_registry.name}.tmp-{os.getpid()}")
    os.link(victim, link)

    miner.add_to_known_entities({"people": ["Dana"]})

    assert victim.read_text() == "notes worth keeping"
    assert sorted(json.loads(temp_registry.read_text())["people"]) == ["Alice", "Dana"]


@needs_unprivileged_posix
def test_a_temporary_file_the_fallback_cannot_remove_is_named(temp_registry, capsys):
    """Removing it needs the directory, which is what refused the rename. It
    holds what the call was asked to save, so leaving the user to find it is
    worse than saying where it is."""
    temp_registry.write_text(json.dumps({"people": ["Alice"]}))
    orphan = temp_registry.with_name(f"{temp_registry.name}.tmp-{os.getpid()}")
    orphan.write_text("half a registry from a run that was killed")
    os.chmod(orphan, 0o600)
    os.chmod(temp_registry.parent, 0o555)
    try:
        miner.add_to_known_entities({"people": ["Dana"]})
        err = capsys.readouterr().err
    finally:
        os.chmod(temp_registry.parent, 0o700)

    assert orphan.exists(), "the fallback could not have removed it here"
    assert orphan.name in err
    assert "could not be removed" in err
    assert sorted(json.loads(temp_registry.read_text())["people"]) == ["Alice", "Dana"]


@needs_unprivileged_posix
def test_a_read_only_directory_with_a_usable_orphan_still_merges(temp_registry, capsys):
    """Opening a temporary file that already exists needs the file, not the
    directory, so a run that reuses an orphan at the pid name never asks the
    directory anything and reaches the rename, where a read-only directory
    refuses. ``develop`` wrote the registry in place there and kept the merge;
    raising instead would lose it."""
    temp_registry.write_text(json.dumps({"people": ["Alice"], "projects": ["Orion"]}))
    orphan = temp_registry.with_name(f"{temp_registry.name}.tmp-{os.getpid()}")
    orphan.write_text("half a registry from a run that was killed")
    os.chmod(orphan, 0o600)
    os.chmod(temp_registry.parent, 0o555)
    try:
        result = miner.add_to_known_entities({"people": ["Dana"]})
        err = capsys.readouterr().err
    finally:
        os.chmod(temp_registry.parent, 0o700)

    assert result is not None
    data = json.loads(temp_registry.read_text())
    assert sorted(data["people"]) == ["Alice", "Dana"]
    assert data["projects"] == ["Orion"]
    assert "written in place" in err


def test_a_failed_write_in_place_keeps_the_finished_copy(temp_registry, monkeypatch, capsys):
    """The temporary file holds this merge, complete and fsynced. Removing it
    before the write in place would trade a finished copy for a write that
    truncates first, and an interruption there would leave nothing at all."""
    temp_registry.write_text(json.dumps({"people": ["Alice"]}))
    original = temp_registry.read_bytes()
    real_replace = os.replace

    def refused(src, dst, *args, **kwargs):
        if str(dst).endswith(temp_registry.name):
            raise OSError(errno.EPERM, "Operation not permitted")
        return real_replace(src, dst, *args, **kwargs)

    def dies(registry_path, payload):
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(os, "replace", refused)
    monkeypatch.setattr(miner, "_write_registry_in_place", dies)

    with pytest.raises(OSError):
        miner.add_to_known_entities({"people": ["Dana"]})

    err = capsys.readouterr().err
    leftovers = [p for p in temp_registry.parent.iterdir() if p.name != temp_registry.name]
    assert len(leftovers) == 1, leftovers
    assert sorted(json.loads(leftovers[0].read_text())["people"]) == ["Alice", "Dana"]
    assert leftovers[0].name in err
    # The write in place truncates before it serializes, so what was there
    # is gone whether or not this one finished; the message says so.
    assert "truncated" in err
    assert temp_registry.read_bytes() == original


def test_a_signal_during_the_rename_leaves_no_temporary_file(temp_registry, monkeypatch):
    """Nothing has been published at that point, so the temporary file is not
    a copy of anything the user still needs: it is just a name left in the
    directory. Only the errnos the fallback is for keep it."""
    temp_registry.write_text(json.dumps({"people": ["Alice"]}))
    original = temp_registry.read_bytes()

    def interrupted(src, dst, *args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(os, "replace", interrupted)

    with pytest.raises(KeyboardInterrupt):
        miner.add_to_known_entities({"people": ["Dana"]})

    leftovers = [p.name for p in temp_registry.parent.iterdir() if p.name != temp_registry.name]
    assert leftovers == [], leftovers
    assert temp_registry.read_bytes() == original


def test_a_rename_the_directory_refuses_falls_back(temp_registry, monkeypatch, capsys):
    """The rename is the second place the directory can answer, and it answers
    with the same three errnos. Anything else stays a failure, or the write
    without the rename comes back for reasons it was never meant to cover."""
    temp_registry.write_text(json.dumps({"people": ["Alice"]}))
    real_replace = os.replace

    def refused(src, dst, *args, **kwargs):
        if str(dst).endswith(temp_registry.name):
            raise OSError(errno.EPERM, "Operation not permitted")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", refused)
    miner.add_to_known_entities({"people": ["Dana"]})

    data = json.loads(temp_registry.read_text())
    assert sorted(data["people"]) == ["Alice", "Dana"]
    assert "would not take the rename" in capsys.readouterr().err
    leftovers = [p.name for p in temp_registry.parent.iterdir() if p.name != temp_registry.name]
    assert leftovers == [], leftovers


def test_eperm_on_both_names_falls_back_too(temp_registry, monkeypatch, capsys):
    """``EPERM`` reaches the gate from a filesystem that refuses the operation
    rather than the caller, an NFS export among them. It belongs beside
    ``EACCES`` and ``EROFS``, and nothing else pins it."""
    temp_registry.write_text(json.dumps({"people": ["Alice"]}))
    real_open = os.open

    def not_permitted(path, *args, **kwargs):
        name = os.path.basename(str(path))
        if name.startswith(temp_registry.name + ".") or name.startswith(
            "." + temp_registry.name + "."
        ):
            raise OSError(errno.EPERM, "Operation not permitted")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", not_permitted)
    miner.add_to_known_entities({"people": ["Dana"]})

    data = json.loads(temp_registry.read_text())
    assert sorted(data["people"]) == ["Alice", "Dana"]
    assert "written in place" in capsys.readouterr().err


def test_erofs_on_both_names_falls_back_rather_than_raising(temp_registry, monkeypatch, capsys):
    """``EROFS`` names the case the fallback is for: the directory takes no new
    name at all, from the pid-named file or from one it picks itself. A real
    read-only mount refuses the write in place too, and the merge reports that
    instead, so the errno is injected on the two temporary names only, which is
    what pins the gate."""
    temp_registry.write_text(json.dumps({"people": ["Alice"]}))
    real_open = os.open

    def read_only(path, *args, **kwargs):
        name = os.path.basename(str(path))
        if name.startswith(temp_registry.name + ".") or name.startswith(
            "." + temp_registry.name + "."
        ):
            raise OSError(errno.EROFS, "Read-only file system")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", read_only)
    miner.add_to_known_entities({"people": ["Dana"]})

    data = json.loads(temp_registry.read_text())
    assert sorted(data["people"]) == ["Alice", "Dana"]
    assert "written in place" in capsys.readouterr().err


def test_the_write_in_place_is_synced(temp_registry, monkeypatch):
    """The fallback gives up the rename, not durability: the caller is told the
    write can be truncated by an interruption, not that it can vanish after
    returning."""
    temp_registry.write_text(json.dumps({"people": ["Alice"]}))
    real_open = os.open
    real_fsync = os.fsync
    synced_fds = []

    def read_only(path, *args, **kwargs):
        name = os.path.basename(str(path))
        if name.startswith(temp_registry.name + ".") or name.startswith(
            "." + temp_registry.name + "."
        ):
            raise OSError(errno.EROFS, "Read-only file system")
        return real_open(path, *args, **kwargs)

    def record(fd):
        synced_fds.append(fd)
        return real_fsync(fd)

    monkeypatch.setattr(os, "open", read_only)
    monkeypatch.setattr(os, "fsync", record)

    miner.add_to_known_entities({"people": ["Dana"]})

    assert synced_fds, "the write in place returned without being synced"


@needs_unprivileged_posix
def test_the_symlink_probe_raises_when_it_cannot_tell(tmp_path):
    """The probe classifies: link, not a link, or could not tell. Answering
    "not a link" to the third would send the write through ``os.replace`` and
    put a regular file where the link was. Nothing reaches it that way through
    the merge, since a registry this process cannot ``lstat`` fails its read
    first and the merge declines before writing, so this calls the probe."""
    locked = tmp_path / "locked"
    locked.mkdir()
    target = locked / "known_entities.json"
    os.chmod(locked, 0o000)
    try:
        with pytest.raises(OSError) as caught:
            miner._registry_write_target(target)
    finally:
        os.chmod(locked, 0o700)

    assert caught.value.errno in (errno.EACCES, errno.EPERM)


@needs_unprivileged_posix
def test_the_umask_does_not_leave_the_temporary_file_unwritable(temp_registry, monkeypatch):
    """``O_CREAT`` is masked by the umask, so a umask that clears the owner's
    write bit creates the temporary file at 0400. Setting the mode after the
    write leaves it that way for as long as the file exists, and one left
    behind by a killed run is then a name its own owner cannot open."""
    import stat as stat_mod

    seen = {}
    real_fsync = os.fsync

    def record(fd):
        candidate = temp_registry.with_name(f"{temp_registry.name}.tmp-{os.getpid()}")
        if candidate.exists():
            seen["mode"] = stat_mod.S_IMODE(os.stat(candidate).st_mode)
        return real_fsync(fd)

    previous = os.umask(0o200)
    try:
        temp_registry.write_text(json.dumps({"people": ["Alice"]}))
        monkeypatch.setattr(os, "fsync", record)
        miner.add_to_known_entities({"people": ["Dana"]})
    finally:
        os.umask(previous)

    assert seen.get("mode") == 0o600, seen
    assert sorted(json.loads(temp_registry.read_text())["people"]) == ["Alice", "Dana"]


def test_a_full_disk_does_not_fall_back_to_writing_in_place(temp_registry, monkeypatch):
    """The fallback exists for a directory that refuses a new name while the
    registry itself is writable. Every other failure has to stay a failure."""
    temp_registry.write_text(json.dumps({"people": ["Alice"], "projects": ["Orion"]}))
    original = temp_registry.read_bytes()
    real_open = os.open

    # A full disk refuses every name in the directory, the pid-named one and
    # the one the directory picks alike. Denying only the first would let the
    # write succeed under the second and prove nothing.
    def no_space(path, *args, **kwargs):
        name = os.path.basename(str(path))
        if name.startswith(temp_registry.name + ".") or name.startswith(
            "." + temp_registry.name + "."
        ):
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", no_space)
    with pytest.raises(OSError):
        miner.add_to_known_entities({"people": ["Dana"]})

    assert temp_registry.read_bytes() == original
