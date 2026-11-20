"""Legacy metadata keys must not become filesystem paths or lose recovery state."""

import json
import os
import stat
from pathlib import Path

import pytest

from mempalace.config import MempalaceConfig, sanitize_name
from mempalace.rooms import (
    RoomSet,
    RoomSpec,
    clear_pending_apply,
    load_pending_apply,
    load_room_set,
    pending_apply_path,
    room_set_path,
    save_pending_apply,
    save_room_set,
)
from mempalace.wing_split import (
    assert_split_pending_matches_plan,
    load_split_plan,
    save_split_plan,
    split_pending_path,
    split_plan_path,
    write_split_pending,
)

LEGACY_WING = "-Users-fixture-Documents-project"
BUILDERS = [split_plan_path, split_pending_path, room_set_path, pending_apply_path]


def _plan(wing):
    return {
        "wing": wing,
        "projects": {"p--portal": {"target": "portal", "drawers": 1}},
    }


@pytest.mark.parametrize("build", BUILDERS)
def test_legacy_keys_get_distinct_contained_artifacts(tmp_path, build):
    cfg = MempalaceConfig(palace_path=str(tmp_path))
    names = [LEGACY_WING, LEGACY_WING.lower(), "_foo", "-foo", "foo", "foo-"]
    paths = [Path(build(cfg, wing)) for wing in names]
    assert len({str(path).casefold() for path in paths}) == len(names)
    assert all(path.parent in (tmp_path / "wings", tmp_path / "rooms") for path in paths)
    assert [str(path) for path in paths] == [build(cfg, wing) for wing in names]
    with pytest.raises(ValueError):
        sanitize_name(LEGACY_WING, "wing")  # New-name validation must stay strict.


@pytest.mark.parametrize("build", BUILDERS)
@pytest.mark.parametrize("wing", ["../outside", r"..\outside", "C:outside", "a/b", "a" * 500])
def test_legacy_filename_encoding_cannot_escape_or_exceed_component_limit(tmp_path, build, wing):
    path = Path(build(MempalaceConfig(palace_path=str(tmp_path)), wing))
    assert path.parent in (tmp_path / "wings", tmp_path / "rooms")
    assert len(path.name.encode("utf-8")) < 255


@pytest.mark.parametrize("build", BUILDERS)
@pytest.mark.parametrize("wing", ["", " ", "\x00bad", None])
def test_empty_or_nul_keys_still_fail_before_filesystem_access(tmp_path, build, wing):
    with pytest.raises(ValueError):
        build(MempalaceConfig(palace_path=str(tmp_path)), wing)
    assert list(tmp_path.iterdir()) == []


def test_ordinary_artifact_names_are_unchanged(tmp_path):
    cfg = MempalaceConfig(palace_path=str(tmp_path))
    assert split_plan_path(cfg, "existing_wing") == str(tmp_path / "wings/split-existing_wing.json")
    assert split_pending_path(cfg, "existing_wing") == str(
        tmp_path / "wings/split-existing_wing.pending"
    )
    assert room_set_path(cfg, "existing_wing") == str(tmp_path / "rooms/existing_wing.json")
    assert pending_apply_path(cfg, "existing_wing") == str(
        tmp_path / "rooms/existing_wing.apply-pending.json"
    )


def test_legacy_split_plan_and_fingerprint_keep_exact_wing(tmp_path):
    cfg = MempalaceConfig(palace_path=str(tmp_path))
    plan = _plan(LEGACY_WING)
    saved = Path(save_split_plan(cfg, plan))
    marker = Path(write_split_pending(cfg, LEGACY_WING, plan))
    assert json.loads(saved.read_text())["wing"] == LEGACY_WING
    loaded = load_split_plan(cfg, LEGACY_WING)
    assert loaded["wing"] == LEGACY_WING
    assert_split_pending_matches_plan(cfg, LEGACY_WING, loaded)
    before = marker.read_bytes()
    loaded["projects"]["p--portal"]["target"] = "other"
    with pytest.raises(ValueError, match="changed since"):
        assert_split_pending_matches_plan(cfg, LEGACY_WING, loaded)
    assert marker.read_bytes() == before


def test_legacy_room_set_and_apply_journal_round_trip(tmp_path):
    cfg = MempalaceConfig(palace_path=str(tmp_path))
    approved = RoomSet(LEGACY_WING, [RoomSpec("releases", "Release work")])
    save_room_set(cfg, approved)
    assert load_room_set(cfg, LEGACY_WING).wing == LEGACY_WING
    marker = Path(save_pending_apply(cfg, LEGACY_WING, {("s", "general"): "releases"}, 0))
    assert json.loads(marker.read_text())["wing"] == LEGACY_WING
    assert load_pending_apply(cfg, LEGACY_WING)[0] == {("s", "general"): "releases"}
    clear_pending_apply(cfg, LEGACY_WING)
    assert not marker.exists()
    assert load_room_set(cfg, LEGACY_WING).wing == LEGACY_WING


@pytest.mark.parametrize(
    "build,folder,prefix,suffix,partner",
    [
        (split_plan_path, "wings", "split-", ".json", ".pending"),
        (split_pending_path, "wings", "split-", ".pending", ".json"),
        (room_set_path, "rooms", "", ".json", ".apply-pending.json"),
        (pending_apply_path, "rooms", "", ".apply-pending.json", ".json"),
    ],
)
def test_safe_historical_partner_keeps_the_whole_family_on_raw_paths(
    tmp_path, build, folder, prefix, suffix, partner
):
    cfg = MempalaceConfig(palace_path=str(tmp_path))
    directory = tmp_path / folder
    directory.mkdir()
    old_partner = directory / f"{prefix}{LEGACY_WING}{partner}"
    old_partner.write_text(json.dumps({"wing": LEGACY_WING}), encoding="utf-8")
    if folder == "wings" and partner == ".pending":
        (directory / f"{prefix}{LEGACY_WING}.json").write_text(
            json.dumps(_plan(LEGACY_WING)), encoding="utf-8"
        )
    before = old_partner.read_bytes()
    assert build(cfg, LEGACY_WING) == str(directory / f"{prefix}{LEGACY_WING}{suffix}")
    assert old_partner.read_bytes() == before


@pytest.mark.parametrize(
    "build,folder,prefix,suffix",
    [
        (split_plan_path, "wings", "split-", ".pending"),
        (split_pending_path, "wings", "split-", ".json"),
        (room_set_path, "rooms", "", ".apply-pending.json"),
        (pending_apply_path, "rooms", "", ".json"),
    ],
)
def test_conflicting_encoded_and_raw_artifacts_preserve_both(
    tmp_path, build, folder, prefix, suffix
):
    cfg = MempalaceConfig(palace_path=str(tmp_path))
    encoded = Path(build(cfg, LEGACY_WING))
    encoded.parent.mkdir()
    encoded.write_text("new recovery state", encoding="utf-8")
    raw = tmp_path / folder / f"{prefix}{LEGACY_WING}{suffix}"
    raw.write_text("old recovery state", encoding="utf-8")
    with pytest.raises(ValueError, match="both.*artifacts"):
        build(cfg, LEGACY_WING)
    assert encoded.read_text() == "new recovery state"
    assert raw.read_text() == "old recovery state"


@pytest.mark.parametrize("build", BUILDERS)
def test_legacy_artifacts_refuse_symlinked_state_directory(tmp_path, build):
    outside = tmp_path / "outside"
    outside.mkdir()
    palace = tmp_path / "palace"
    palace.mkdir()
    try:
        for folder in ("wings", "rooms"):
            (palace / folder).symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"Symlink creation unavailable: {exc}")
    with pytest.raises(ValueError, match="symlink"):
        build(MempalaceConfig(palace_path=str(palace)), LEGACY_WING)
    assert list(outside.iterdir()) == []


def test_failed_legacy_plan_publish_keeps_the_previous_bytes(tmp_path, monkeypatch):
    cfg = MempalaceConfig(palace_path=str(tmp_path))
    original = _plan(LEGACY_WING)
    path = Path(save_split_plan(cfg, original))
    before = path.read_bytes()

    def no_replace(*args):
        raise OSError("injected publish failure")

    monkeypatch.setattr(os, "replace", no_replace)
    edited = _plan(LEGACY_WING)
    edited["projects"]["p--portal"]["target"] = "other"
    with pytest.raises(OSError, match="publish failure"):
        save_split_plan(cfg, edited)
    assert path.read_bytes() == before
    assert not list(path.parent.glob("*.tmp"))


@pytest.mark.parametrize("kind", ["split", "rooms"])
def test_encoded_name_cannot_overwrite_state_for_a_different_raw_wing(tmp_path, kind):
    cfg = MempalaceConfig(palace_path=str(tmp_path))
    build = split_plan_path if kind == "split" else room_set_path
    path = Path(build(cfg, LEGACY_WING))
    path.parent.mkdir()
    # A legacy raw wing may itself look like the new reserved digest name.
    # Filename equality is not proof of ownership; use the saved exact key.
    path.write_text(json.dumps({"wing": "different legacy wing"}), encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(ValueError, match="wing"):
        if kind == "split":
            save_split_plan(cfg, _plan(LEGACY_WING))
        else:
            save_room_set(cfg, RoomSet(LEGACY_WING, [RoomSpec("releases", "Release work")]))
    assert path.read_bytes() == before


@pytest.mark.parametrize("build", BUILDERS)
def test_legacy_artifact_symlink_is_never_followed(tmp_path, build):
    cfg = MempalaceConfig(palace_path=str(tmp_path))
    path = Path(build(cfg, LEGACY_WING))
    path.parent.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text("preserve exact bytes", encoding="utf-8")
    try:
        path.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"Symlink creation unavailable: {exc}")
    with pytest.raises(ValueError, match="symlink"):
        build(cfg, LEGACY_WING)
    assert outside.read_text() == "preserve exact bytes"


@pytest.mark.parametrize("operation", ["save", "load", "clear"])
def test_foreign_pending_only_room_marker_cannot_be_claimed(tmp_path, operation):
    cfg = MempalaceConfig(palace_path=str(tmp_path))
    marker = Path(pending_apply_path(cfg, LEGACY_WING))
    marker.parent.mkdir()
    marker.write_text(
        json.dumps({"wing": "another legacy key", "closets": [], "ambiguous": 0}),
        encoding="utf-8",
    )
    before = marker.read_bytes()
    with pytest.raises(ValueError, match="wing"):
        if operation == "save":
            save_room_set(cfg, RoomSet(LEGACY_WING, [RoomSpec("releases", "Release work")]))
        elif operation == "load":
            load_pending_apply(cfg, LEGACY_WING)
        else:
            clear_pending_apply(cfg, LEGACY_WING)
    assert marker.read_bytes() == before
    assert len(list(marker.parent.iterdir())) == 1


def test_pending_only_split_marker_without_owned_plan_is_not_abandoned(tmp_path):
    cfg = MempalaceConfig(palace_path=str(tmp_path))
    marker = Path(split_pending_path(cfg, LEGACY_WING))
    marker.parent.mkdir()
    marker.write_text('{"plan_sha256": "unknown owner"}', encoding="utf-8")
    before = marker.read_bytes()
    with pytest.raises(ValueError, match="owned.*plan"):
        save_split_plan(cfg, _plan(LEGACY_WING))
    assert marker.read_bytes() == before
    assert len(list(marker.parent.iterdir())) == 1


def test_safe_raw_name_resembling_encoding_prefix_still_loads(tmp_path):
    cfg = MempalaceConfig(palace_path=str(tmp_path))
    wing = "_legacy_notes"
    directory = tmp_path / "wings"
    directory.mkdir()
    path = directory / "split-_legacy_notes.json"
    path.write_text(json.dumps(_plan(wing)), encoding="utf-8")
    assert load_split_plan(cfg, wing)["wing"] == wing
    assert split_plan_path(cfg, wing) == str(path)


@pytest.mark.parametrize(
    "wing",
    ["-Users-fixture-R&D", "_notes (v1)+[draft]@x", "-日本語🚀", "-foo.", "-foo ", "-notes..old"],
)
@pytest.mark.parametrize("family", ["split", "rooms"])
def test_portable_punctuation_keeps_historical_plan_and_marker(tmp_path, wing, family):
    cfg = MempalaceConfig(palace_path=str(tmp_path))
    if family == "split":
        folder, prefix, suffix = "wings", "split-", ".pending"
        payload = _plan(wing)
    else:
        folder, prefix, suffix = "rooms", "", ".apply-pending.json"
        payload = RoomSet(wing, [RoomSpec("releases", "Release work")]).to_dict()
    directory = tmp_path / folder
    directory.mkdir()
    plan = directory / f"{prefix}{wing}.json"
    marker = directory / f"{prefix}{wing}{suffix}"
    plan.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    marker.write_text(json.dumps({"wing": wing, "closets": [], "ambiguous": 0}), encoding="utf-8")
    before = (plan.read_bytes(), marker.read_bytes())
    if family == "split":
        assert split_plan_path(cfg, wing) == str(plan)
        assert split_pending_path(cfg, wing) == str(marker)
        assert load_split_plan(cfg, wing)["wing"] == wing
    else:
        assert room_set_path(cfg, wing) == str(plan)
        assert pending_apply_path(cfg, wing) == str(marker)
        assert load_room_set(cfg, wing).wing == wing
        assert load_pending_apply(cfg, wing)[0] == {}
    assert (plan.read_bytes(), marker.read_bytes()) == before


@pytest.mark.parametrize("family", ["split", "rooms"])
def test_long_portable_raw_pair_is_not_hidden(tmp_path, family):
    # The final artifact component, rather than an arbitrary smaller wing
    # budget, determines whether an old file can exist on a portable filesystem.
    palace = Path("\\\\?\\" + str(tmp_path)) if os.name == "nt" else tmp_path
    wing = "-" + "a" * 230
    test_portable_punctuation_keeps_historical_plan_and_marker(palace, wing, family)


@pytest.mark.skipif(os.name != "nt", reason="Windows component length uses UTF-16 units")
@pytest.mark.parametrize("family", ["split", "rooms"])
def test_existing_windows_unicode_raw_pair_is_not_hidden(tmp_path, family):
    palace = Path("\\\\?\\" + str(tmp_path))
    wing = "-" + "日" * 120
    assert len(wing.encode("utf-8")) > 255
    assert len(wing.encode("utf-16-le")) // 2 < 255
    test_portable_punctuation_keeps_historical_plan_and_marker(palace, wing, family)


def test_long_room_plan_is_not_hidden_by_an_oversized_missing_marker(tmp_path):
    palace = Path("\\\\?\\" + str(tmp_path)) if os.name == "nt" else tmp_path
    cfg = MempalaceConfig(palace_path=str(palace))
    wing = "-" + "a" * 239
    directory = palace / "rooms"
    directory.mkdir()
    plan = directory / f"{wing}.json"
    plan.write_text(
        json.dumps(RoomSet(wing, [RoomSpec("releases", "Release work")]).to_dict()),
        encoding="utf-8",
    )
    before = plan.read_bytes()
    assert room_set_path(cfg, wing) == str(plan)
    assert load_room_set(cfg, wing).wing == wing
    with pytest.raises(ValueError, match="portable.*filename"):
        pending_apply_path(cfg, wing)
    assert plan.read_bytes() == before


def test_device_key_is_safe_when_split_prefix_changes_actual_basename(tmp_path):
    test_portable_punctuation_keeps_historical_plan_and_marker(tmp_path, "CONOUT$", "split")


@pytest.mark.parametrize("build", [room_set_path, pending_apply_path])
def test_room_device_basename_is_not_probed(tmp_path, monkeypatch, build):
    test_nonportable_raw_keys_are_not_probed_as_artifact_paths(
        tmp_path, monkeypatch, build, "CONOUT$"
    )


@pytest.mark.parametrize("build", BUILDERS)
@pytest.mark.parametrize(
    "wing",
    [
        "../outside",
        r"..\outside",
        "-foo:stream",
        "-foo<bar",
        "-foo\nbar",
    ],
)
def test_nonportable_raw_keys_are_not_probed_as_artifact_paths(tmp_path, monkeypatch, build, wing):
    cfg = MempalaceConfig(palace_path=str(tmp_path))
    actual_lstat = os.lstat

    def contained_only(path, *args, **kwargs):
        assert wing not in str(path), "nonportable raw key reached the filesystem"
        return actual_lstat(path, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", contained_only)
    selected = Path(build(cfg, wing))
    assert selected.parent in (tmp_path / "wings", tmp_path / "rooms")


@pytest.mark.skipif(os.name != "nt", reason="Windows directory junction")
@pytest.mark.parametrize("build", BUILDERS)
def test_legacy_artifacts_refuse_junction_state_directory(tmp_path, build):
    # The same call CPython's own tests use to make a junction. A PowerShell
    # ``New-Item -ItemType Junction`` did the job before at about 2 s a spawn.
    import _winapi

    outside = tmp_path / "outside"
    outside.mkdir()
    palace = tmp_path / "palace"
    palace.mkdir()
    for folder in ("wings", "rooms"):
        _winapi.CreateJunction(str(outside), str(palace / folder))
        attributes = os.lstat(palace / folder).st_file_attributes
        assert attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT, folder
    with pytest.raises(ValueError, match="symlink|reparse|junction"):
        build(MempalaceConfig(palace_path=str(palace)), LEGACY_WING)
    assert list(outside.iterdir()) == []
