"""Maintenance filenames for exact, possibly legacy, stored wing identities."""

import hashlib
import json
import os
import re
import stat
import unicodedata

from .config import MempalaceConfig, sanitize_name
from .fsio import open_source_file

_FAMILIES = {
    "split": ("wings", "split-", (".json", ".pending")),
    "rooms": ("rooms", "", (".json", ".apply-pending.json")),
}


def _portable_leaf(component):
    stem = component.partition(".")[0].upper()
    return (
        not any(c in '<>:"/\\|?*' or unicodedata.category(c) in {"Cc", "Cs"} for c in component)
        and component not in {".", ".."}
        and not component.endswith((".", " "))
        and re.fullmatch(r"CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|(?:COM|LPT)[1-9¹²³]", stem) is None
        and (
            len(component.encode("utf-16-le")) // 2
            if os.name == "nt"
            else len(component.encode("utf-8"))
        )
        <= 255
    )


def _redirected(info):
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _existing_artifacts(directory, prefix, component, suffixes):
    found = False
    for extension in suffixes:
        leaf = f"{prefix}{component}{extension}"
        if not _portable_leaf(leaf):
            continue
        candidate = os.path.join(directory, leaf)
        try:
            info = os.lstat(candidate)
        except FileNotFoundError:
            continue
        if _redirected(info) or not stat.S_ISREG(info.st_mode):
            raise ValueError(
                f"maintenance artifact is not a regular file (possibly symlink): {candidate}"
            )
        found = True
    return found


def wing_artifact_path(config: MempalaceConfig, wing: str, family: str, suffix: str) -> str:
    """Keep valid filenames; encode legacy keys without changing their identity.

    A safe historical raw filename wins for the *whole* plan/marker family.
    Never hide an interrupted operation by creating its partner under a new
    name. If old and encoded state both exist, leave both for owner review.
    """
    folder, prefix, suffixes = _FAMILIES[family]
    if suffix not in suffixes:
        raise ValueError("unknown maintenance artifact suffix")
    directory = os.path.join(config.palace_path, folder)
    if not isinstance(wing, str) or not wing.strip() or "\x00" in wing:
        raise ValueError("wing must be a non-empty string without null bytes")
    try:
        name = sanitize_name(wing, "wing")
    except ValueError:
        name = "_legacy_" + hashlib.sha256(wing.encode("utf-8", errors="surrogatepass")).hexdigest()
    else:
        return os.path.join(directory, f"{prefix}{name}{suffix}")

    # Existing valid names keep their old behavior. Newly supported legacy
    # paths must not follow a state-directory or artifact symlink on reads.
    try:
        directory_info = os.lstat(directory)
    except FileNotFoundError:
        pass
    else:
        if _redirected(directory_info):
            raise ValueError(
                f"maintenance state directory is a symlink or reparse point: {directory}"
            )

    encoded_exists = _existing_artifacts(directory, prefix, name, suffixes)
    # Assess the composed leaf, not the stored key: a trailing dot in the
    # key is internal to the filename once its artifact suffix is appended.
    raw_exists = _existing_artifacts(directory, prefix, wing, suffixes)
    if encoded_exists and raw_exists:
        raise ValueError(f"both raw and encoded maintenance artifacts exist for wing {wing!r}")
    if raw_exists:
        name = wing
    if not _portable_leaf(f"{prefix}{name}{suffix}"):
        raise ValueError("existing maintenance state requires a non-portable artifact filename")
    # A historical raw name can resemble a reserved digest, and a
    # case-insensitive filesystem can alias two raw spellings. Never let
    # filename equality authorize overwriting another wing's approved plan.
    plan_path = os.path.join(directory, f"{prefix}{name}.json")
    owned_plan = False
    try:
        fd, _ = open_source_file(plan_path)
    except FileNotFoundError:
        pass
    else:
        with os.fdopen(fd, encoding="utf-8") as handle:
            plan = json.load(handle)
        if not isinstance(plan, dict) or plan.get("wing") != wing:
            raise ValueError(f"maintenance artifact at {plan_path} is not for wing {wing!r}")
        owned_plan = True
    marker_path = os.path.join(directory, f"{prefix}{name}{suffixes[-1]}")
    if not _portable_leaf(f"{prefix}{name}{suffixes[-1]}"):
        return os.path.join(directory, f"{prefix}{name}{suffix}")
    try:
        fd, _ = open_source_file(marker_path)
    except FileNotFoundError:
        pass
    else:
        with os.fdopen(fd, encoding="utf-8") as handle:
            if family == "rooms":
                marker = json.load(handle)
                if not isinstance(marker, dict) or marker.get("wing") != wing:
                    raise ValueError(
                        f"pending room apply at {marker_path} is not for wing {wing!r}"
                    )
            elif not owned_plan:
                # Old split markers identify only a plan fingerprint, not a
                # wing. An orphan must not authorize a fresh plan or deletion.
                raise ValueError(f"split marker at {marker_path} has no owned accompanying plan")
    return os.path.join(directory, f"{prefix}{name}{suffix}")
