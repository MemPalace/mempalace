"""Which directory a source file was mined from, recorded as its inode.

``sync --apply`` removes a drawer whose source file is not at its path only
when the palace can still see a source of its own, a regular file, in that
same directory (#2320). A neighbour proves the directory is reachable. It
does not prove it is the same directory the missing file was mined from, and
three mount shapes turn that gap into deleted drawers:

* a mount point that also holds a mined file of its own, since that file is
  in the layer underneath and outlives the unmount;
* a volume mounted *over* a directory the palace already knows a file in,
  since the volume's own files then corroborate the hidden one's absence;
* a bind mount of another directory over one the palace knows, which is what
  a container does with ``-v /host/elsewhere:/project/sub``.

What separates them is the inode of the directory itself. A path is only a
name: mount something at it and the name resolves to the root of what was
mounted, which is a different inode, and unmounting brings the original back.
A project directory answers with its own number, something mounted over it
answers with the root of what was mounted, the original answers again after
the unmount, and a bind mount of a sibling answers with the sibling's number.
``tests/test_source_identity.py`` drives those mounts and asserts that the
answer changes and comes back; that a mount point answers with the mounted
filesystem's own root is asserted separately, against the ``/proc`` Linux
mounts on every boot.

``st_dev`` is deliberately not used, and not because it is unstable. For a
filesystem on a block device it is that device's ``major:minor``, which
outlives a reboot; only filesystems on anonymous devices, tmpfs and procfs
among them, take a number from a pool in the order mounts happen. What rules
it out is the bind mount: the sibling and the directory it is mounted over
are on the same filesystem, so ``st_dev`` is identical on both sides and the
shape reads as no change at all. An inode belongs to the filesystem's own
structure instead, so it separates that shape and survives unmounting and
remounting elsewhere.

Nothing here writes to the source tree. The project's README promises that
mining never does, and its container recipes mount sources read-only.

Two things this cannot separate. One volume swapped for another whose root
holds the same number, which is what two filesystems of the same type mounted
at the same path give: every ext4 root is inode 2 and every tmpfs root is 1.
And a filesystem that reports no inode of its own, which answers ``0``:
``mcp_server`` names FAT and exFAT among those and gates its own reconnect
detection on exactly that number. Both answer ``None`` here, which the miner
reads as "record nothing" and sync reads as "decide this drawer the way
drawers were decided before this existed". Neither turns into a removal, and
neither gains the protection either.
"""

import os
from pathlib import Path
from typing import Optional, Union


def directory_identity(directory: Union[Path, str]) -> Optional[str]:
    """The inode of ``directory`` as a string, or ``None``.

    Taken with ``os.stat``, so a directory reached through a symlink answers
    with the inode of what the link points at. A file reached through a
    symlink of its own is a different matter: the directory here is the one
    the link sits in, not the one holding the file it names. Both are the
    directory the drawer's ``source_file`` names, which is the one ``sync``
    looks for witnesses in, so the two readings stay about the same place.

    A directory that is not there, or cannot be stat'ed at all, answers
    ``None``: that is not an identity, and the caller treats it as one it
    never established rather than as a mismatch.

    A zero inode answers ``None`` for the same reason. ``mcp_server`` records
    that FAT and exFAT may report one rather than a number of their own, and
    gates its own reconnect detection on exactly that; taking it at face value
    here would make every directory on such a filesystem answer for every
    other, quietly and with nothing in the report to say so.
    """
    # Answered as text because that is what a drawer carries: metadata goes
    # to a backend and comes back, and the string spelling of an inode
    # survives that on every one of them, where a 64-bit integer need not.
    try:
        identity = os.stat(directory).st_ino
    except (OSError, ValueError):
        return None
    return str(identity) if identity else None


def source_directory_identity(file_path: Union[Path, str]) -> Optional[str]:
    """The identity of the directory ``file_path`` sits in, or ``None``.

    Every writer needs this rather than ``directory_identity`` itself, and it
    is one function so that the step between them is taken once. What makes a
    recorded identity mean anything is that it was read from the directory of
    the very path the drawer goes on to name in ``source_file``: ``sync``
    looks the drawer up by that value, so an identity taken from any other
    path names a directory the drawer never mentions, and nothing that
    directory can ever answer would match it. Derived at each call site, that
    correspondence is something to re-establish every time, and it has been
    got wrong once already.
    """
    return directory_identity(os.path.dirname(str(file_path)))


def identity_metadata(file_path: Union[Path, str]) -> dict[str, str]:
    """``{"source_dir_ino": ...}`` for the directory ``file_path`` sits in.

    Empty when there is no identity to record, so a caller can merge it into a
    metadata dict without a branch. Storing ``None`` under the key instead
    would be a third state for ``sync`` to read, and the two that exist, a
    recorded inode and no key at all, already cover it.

    Callers that carry the identity down as a value of its own, rather than
    merging a dict here, take ``source_directory_identity`` instead.
    """
    identity = source_directory_identity(file_path)
    return {"source_dir_ino": identity} if identity else {}
